"""The own solar forecast and the sun-position correction in Home Assistant, in shadow (SpotNav 3.0).

Nothing here plans, and nothing is a setting: the candidates are logged beside the forecast the plans use, reviewed
each night and shown in the site's diagnostics (`solar_shadow`). Promotion is the owner's decision, after a season of
reviews. The math is pure (`site/sun_geometry.py`, `site/solar_planes.py`, `site/solar_own.py`), and so is what is
kept (`site/solar_shadow_store.py`); this is the thin shell around them, one per site, owned by its
`SiteCapacityController`.

* **Where it runs**: a solar-capable site (`site/solar_capability.py`) whose Energy dashboard names a solar
  production statistic (the outcome every candidate is learned from and scored against), with or without a solar
  forecast integration, and with Home Assistant's home position set (`open_meteo.rounded_position`). Otherwise it
  idles, says why in the diagnostics (`status`) and fetches nothing.
* **Measured production**: the Energy dashboard's solar statistics, hourly, from the recorder (in its executor):
  `KEEP_DAYS` days once at the start (the warm start), then the last `PV_READ_HOURS` on each refresh.
* **Irradiance** (`site/open_meteo.py`): the forecast is fetched at the first refresh in each of the local hours
  `FETCH_HOURS` (so 13:00's day-ahead issue and 06:00's morning issue are made from a fresh one), and after a start;
  four calls a day. Once, while the learned factors have no weather for the measured history, the archived runs of
  up to a year are fetched, `ARCHIVE_CHUNK_DAYS` per refresh, at most one request per refresh. A failure backs off
  (15 min doubling to 6 h); the issues then use the last fetch while it covers the hours.
* **Fallback**: an issued hour without fetched irradiance gets only the cloud-only candidate, from the weather
  entity's hourly cloud cover (`weather.get_forecasts`), marked in the issue (`inputs.weather_entity`).
* **Issues**: the day-ahead issue at the first refresh from 13:00 local (the next local day), the morning issue at
  the first refresh from 06:00 local before noon (the rest of today); a missed one is skipped, never made late. The
  own candidates are issued here; the provider's three (`provider`, `provider_corrected`, `provider_sunpos`) by the
  solar correction's hourly refresh (`note_provider`), which reads the provider's forecast and corrects it anyway.
* **Nightly** (the first refresh of a local day from `NIGHTLY_HOUR`, and at the start): the planes refitted once a
  month (weekly while the history is under `YOUNG_DAYS` days), every factor relearned, both in the executor; then
  the review of the days that ended, each logged at debug level, one line per day and issue.
* **Store** (`spotnav.solar_shadow.<entry>`): written at most every `SAVE_DELAY_S` and at an unload.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from functools import partial
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..const import CONF_GRID_POWER_SOURCE, CONF_MEASUREMENT_MODE, DOMAIN
from ..debug_day import DayStore
from ..pricing.fx_rates import USER_AGENT
from .energy_statistics import async_energy_ids, async_statistics, summed
from .open_meteo import (
    ARCHIVE_CHUNK_DAYS,
    ARCHIVE_URL,
    ERROR_BODY,
    FORECAST_URL,
    Backoff,
    FetchResult,
    Weather,
    archive_params,
    async_fetch_json,
    forecast_params,
    parse_archive,
    parse_hourly,
    rounded_position,
)
from .solar_capability import solar_capability
from .solar_correction_shadow import hourly_cloud, pick_weather_entity
from .solar_own import (
    CANDIDATE_PROVIDER,
    CANDIDATE_PROVIDER_CORRECTED,
    CANDIDATE_PROVIDER_SUNPOS,
    CANDIDATES,
    OWN_CANDIDATES,
    Factors,
    daylight_hours,
    learn,
    own_forecast,
    provider_sunpos,
)
from .solar_planes import TWO_PLANE_MIN_DAYS, envelope, fit_planes
from .solar_shadow_store import (
    ISSUE_DAY_AHEAD,
    ISSUE_MORNING,
    KEEP_DAYS,
    IssueLog,
    SolarHistory,
    planes_as_rows,
    planes_from_rows,
)
from .sun_geometry import Plane, hour_key, key_start

if TYPE_CHECKING:
    from .site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

TICK_S: Final = 300.0
#: The local hours whose first refresh fetches the forecast.
FETCH_HOURS: Final = (0, 6, 13, 18)
NIGHTLY_HOUR: Final = 1
MORNING_HOUR: Final = 6
#: The morning issue is made only before this local hour (a later one would not be a morning issue).
MORNING_LAST_HOUR: Final = 11
DAY_AHEAD_HOUR: Final = 13
PV_READ_HOURS: Final = 48
#: The archive is fetched back this far (the sun-position factor's window).
ARCHIVE_DAYS: Final = 365
#: The planes need this much history; under `YOUNG_DAYS` they are refitted weekly, then monthly.
FIT_MIN_DAYS: Final = 7
YOUNG_DAYS: Final = 60
#: The last fetch is used for an issue while it is younger than this.
WEATHER_MAX_AGE: Final = timedelta(hours=30)
SAVE_DELAY_S: Final = 3 * 3600.0
STORE_VERSION: Final = 1
#: The diagnostics show the next this many hours of every candidate.
SHOWN_HOURS: Final = 24

STATUS_STARTING: Final = "starting"
STATUS_NOT_SOLAR_CAPABLE: Final = "not_solar_capable"
STATUS_NO_POSITION: Final = "no_position"
STATUS_NO_PRODUCTION: Final = "no_solar_statistics"
STATUS_SHADOW: Final = "shadow"

GROUP_OWN: Final = "own"
GROUP_PROVIDER: Final = "provider"


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.solar_shadow.{entry_id}"


def fetch_due(fetched_at: datetime | None, now: datetime, tz: str) -> bool:
    """Whether a forecast fetch is due: none yet, or none since the last start of a `FETCH_HOURS` hour."""
    if fetched_at is None:
        return True
    zone = time_zones.require_zone(tz)
    local = now.astimezone(zone)
    for back in range(0, 25):
        moment = (local - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        if moment.hour in FETCH_HOURS:
            return fetched_at < moment.astimezone(timezone.utc)
    return True


def due_issues(now: datetime, tz: str) -> list[tuple[str, date, list[datetime]]]:
    """The issues whose time it is (see the module): `(issue, target local day, hours starting after now)`."""
    zone = time_zones.require_zone(tz)
    local = now.astimezone(zone)
    out: list[tuple[str, date, list[datetime]]] = []
    if MORNING_HOUR <= local.hour <= MORNING_LAST_HOUR:
        out.append((ISSUE_MORNING, local.date(), _day_hours(local.date(), zone, after=now)))
    if local.hour >= DAY_AHEAD_HOUR:
        tomorrow = local.date() + timedelta(days=1)
        out.append((ISSUE_DAY_AHEAD, tomorrow, _day_hours(tomorrow, zone, after=now)))
    return out


def _day_hours(day: date, zone: Any, after: datetime) -> list[datetime]:
    first = hour_key(datetime.combine(day, datetime.min.time(), zone))
    end = hour_key(datetime.combine(day + timedelta(days=1), datetime.min.time(), zone))
    return [start for key in range(first, end) if (start := key_start(key)) > after]


def fit_from_history(pv: Mapping[int, int], position: tuple[float, float], now: datetime) -> tuple[Plane, ...] | None:
    """The planes fitted on up to a year of the kept production (CPU: the executor)."""
    if not pv:
        return None
    end = hour_key(now)
    days = min(365, (end - min(pv)) // 24)
    if days < FIT_MIN_DAYS:
        return None
    first = end - days * 24
    kw = {key: value / 100 for key, value in pv.items() if first <= key < end}
    rows = envelope(kw, first, end, *position)
    return fit_planes(rows, *position, two=days >= TWO_PLANE_MIN_DAYS)


def _rounded(by_hour: Mapping[datetime, float]) -> dict[datetime, float]:
    return {start: round(kwh, 3) for start, kwh in by_hour.items()}


class SiteSolarShadow:
    """One site's own solar forecast and sun-position correction in shadow (see the module). Never raises into the
    site."""

    def __init__(self, hass: HomeAssistant, site: SiteCapacityController) -> None:
        self.hass = hass
        self.site = site
        self.history = SolarHistory()
        self.log = IssueLog(tz="UTC")
        self.planes: tuple[Plane, ...] | None = None
        self.planes_fitted_on: date | None = None
        self.factors: Factors | None = None
        self.learned_on: date | None = None
        self.warm = False
        self.archive_from: date | None = None
        self.archive_upto: date | None = None
        self.archive_done = False
        self.weather: dict[datetime, Weather] = {}
        self.fetched_at: datetime | None = None
        self.backoff = Backoff()
        self.last_error: str | None = None
        self.last_status: int | None = None
        self.calls: dict[str, int] = {}
        self.status = STATUS_STARTING
        self.next_hours: dict[str, dict[datetime, float]] = {}
        self._position: tuple[float, float] | None = None
        self._position_logged = False
        self._tz: str | None = None
        self._store = DayStore(hass, store_key(site.entry_id), self._stored, save_delay_s=SAVE_DELAY_S)
        self._timer_cancel: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._for_hour: datetime | None = None
        self._loaded = False
        self._closed = False

    # ---- storage

    def _stored(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "history": self.history.as_dict(),
            "log": self.log.as_dict(),
            "planes": planes_as_rows(self.planes),
            "planes_fitted_on": None if self.planes_fitted_on is None else self.planes_fitted_on.isoformat(),
            "warm": self.warm,
            "archive_from": None if self.archive_from is None else self.archive_from.isoformat(),
            "archive_upto": None if self.archive_upto is None else self.archive_upto.isoformat(),
            "archive_done": self.archive_done,
        }

    def _restore(self, raw: Any) -> None:
        if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
            return
        self.history.restore(raw.get("history"))
        self.log.restore(raw.get("log"))
        self.planes = planes_from_rows(raw.get("planes"))
        self.planes_fitted_on = _day(raw.get("planes_fitted_on")) if self.planes is not None else None
        self.warm = raw.get("warm") is True
        self.archive_from = _day(raw.get("archive_from"))
        self.archive_upto = _day(raw.get("archive_upto"))
        self.archive_done = raw.get("archive_done") is True

    async def async_start(self) -> None:
        raw = await self._store.async_load()
        try:
            self._restore(raw)
        except Exception:  # noqa: BLE001 - a store that cannot be read starts again
            _LOGGER.debug("SpotNav solar shadow: the kept state could not be read", exc_info=True)
            self.history, self.log, self.planes = SolarHistory(), IssueLog(tz="UTC"), None
        self._loaded = True
        self._timer_cancel = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=TICK_S))
        self.tick(dt_util.utcnow())

    async def async_shutdown(self) -> None:
        self._closed = True
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        await self._store.async_close()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await DayStore.async_remove(hass, store_key(entry_id))

    @callback
    def _async_tick(self, now: datetime) -> None:
        self.tick(now)

    @callback
    def tick(self, now: datetime) -> None:
        """Start a refresh when the UTC hour changed."""
        if not self._loaded or self._closed or (self._task is not None and not self._task.done()):
            return
        hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        if hour == self._for_hour:
            return
        self._for_hour = hour
        self._task = self.hass.async_create_background_task(
            self.async_refresh(now), "spotnav_solar_shadow", eager_start=False
        )

    # ---- what it runs on

    def _solar_capable(self) -> bool:
        config = self.site.config
        return solar_capability(config.get(CONF_MEASUREMENT_MODE), config.get(CONF_GRID_POWER_SOURCE)).capable

    def _home_position(self) -> tuple[float, float] | None:
        return rounded_position(self.hass.config.latitude, self.hass.config.longitude)

    async def _async_energy_ids(self) -> tuple[str, ...]:
        return (await async_energy_ids(self.hass))[0]

    async def _async_statistics(
        self, ids: tuple[str, ...], start: datetime, end: datetime
    ) -> dict[str, list[dict[str, Any]]]:
        return await async_statistics(self.hass, ids, start, end, "hour")

    async def _async_get(self, url: str, params: Mapping[str, str]) -> FetchResult:
        return await async_fetch_json(async_get_clientsession(self.hass), url, params, USER_AGENT)

    async def _async_clouds(self) -> dict[datetime, float]:
        """The weather entity's hourly cloud cover (the fallback's input); empty when there is none."""
        entity_id = pick_weather_entity(self.hass.states.async_entity_ids("weather"))
        if entity_id is None:
            return {}
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except Exception:  # noqa: BLE001 - no hourly forecast: no fallback
            _LOGGER.debug("SpotNav solar shadow: no hourly forecast from %s", entity_id, exc_info=True)
            return {}
        return hourly_cloud(response, entity_id)

    # ---- the refresh

    async def async_refresh(self, now: datetime) -> None:
        """Read, fetch, learn, issue and review as due (see the module). Never raises."""
        try:
            await self._async_refresh(now.astimezone(timezone.utc))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a shadow forecast must never disturb the site
            _LOGGER.debug("SpotNav solar shadow: the refresh failed", exc_info=True)

    def _idle(self, status: str) -> None:
        self.status = status
        self._position = None

    async def _async_refresh(self, now: datetime) -> None:
        if not self._solar_capable():
            self._idle(STATUS_NOT_SOLAR_CAPABLE)
            return
        position = self._home_position()
        if position is None:
            if not self._position_logged:
                _LOGGER.debug("SpotNav solar shadow: Home Assistant's home position is unset or implausible; idle")
                self._position_logged = True
            self._idle(STATUS_NO_POSITION)
            return
        ids = await self._async_energy_ids()
        if not ids:
            self._idle(STATUS_NO_PRODUCTION)
            return
        tz = self.site.demand.tracker.tz
        self.log.tz = tz
        self._tz = tz
        zone = time_zones.require_zone(tz)
        local = now.astimezone(zone)
        changed = await self._async_read_pv(ids, now)
        if self._closed:
            return
        if fetch_due(self.fetched_at, now, tz) and self.backoff.ready(now.timestamp()):
            changed |= await self._async_fetch(position, now, local.date())
        elif self.backoff.ready(now.timestamp()):
            changed |= await self._async_archive_step(position, now)
        if self._closed:
            return
        self._position = position
        self.status = STATUS_SHADOW
        if self.factors is None or (self.learned_on != local.date() and local.hour >= NIGHTLY_HOUR):
            await self._async_nightly(position, tz, now, local.date())
            changed = True
        changed |= await self._async_issue_own(position, now, tz)
        if changed:
            self._store.changed()

    async def _async_read_pv(self, ids: tuple[str, ...], now: datetime) -> bool:
        hour = now.replace(minute=0, second=0, microsecond=0)
        first = hour - (timedelta(days=KEEP_DAYS) if not self.warm else timedelta(hours=PV_READ_HOURS))
        hourly = summed(await self._async_statistics(ids, first, hour), ids)
        changed = self.history.merge_pv(hourly)
        if not self.warm and hourly:
            # Warm once the year was read with something in it; until then (no production yet, or the recorder not
            # ready) the whole span is asked for again at the next refresh.
            self.warm = True
            changed = True
        return changed

    def _count_call(self, now: datetime) -> None:
        day = now.astimezone(time_zones.require_zone(self._tz or "UTC")).date().isoformat()
        self.calls = {day: self.calls.get(day, 0) + 1}

    def _failed(self, now: datetime, result: FetchResult) -> None:
        wait = self.backoff.failed(now.timestamp(), result)
        self.last_error, self.last_status = result.error, result.status
        _LOGGER.debug(
            "SpotNav solar shadow: Open-Meteo answered %s (%s); next try in %.0f s", result.error, result.status, wait
        )

    async def _async_fetch(self, position: tuple[float, float], now: datetime, today: date) -> bool:
        self._count_call(now)
        result = await self._async_get(FORECAST_URL, forecast_params(position))
        weather = {} if result.body is None else parse_hourly(result.body)
        if not weather:
            self._failed(now, result if result.error else FetchResult(error=ERROR_BODY, status=result.status))
            return False
        self.backoff.succeeded()
        self.last_error, self.last_status = None, result.status
        self.weather, self.fetched_at = weather, now
        if self.archive_upto is None:
            self.archive_upto = now.date()
        return self.history.note_weather(weather, now)

    def _archive_target_from(self, now: datetime) -> date | None:
        if not self.history.pv:
            return None
        oldest = key_start(min(self.history.pv)).date()
        return max(now.date() - timedelta(days=ARCHIVE_DAYS), oldest)

    async def _async_archive_step(self, position: tuple[float, float], now: datetime) -> bool:
        """One request of the archive while it is not done (see the module); whether anything changed."""
        if self.archive_done or self.archive_upto is None:
            return False
        target_from = self._archive_target_from(now)
        last = self.archive_upto if self.archive_from is None else self.archive_from - timedelta(days=1)
        if target_from is None:
            return False  # no production kept yet: nothing to learn the archive against
        if last < target_from:
            self.archive_done = True
            self.learned_on = None  # relearn with the whole archive at the next refresh
            return True
        first = max(target_from, last - timedelta(days=ARCHIVE_CHUNK_DAYS - 1))
        self._count_call(now)
        result = await self._async_get(ARCHIVE_URL, archive_params(position, first, last))
        runs = {} if result.body is None else parse_archive(result.body)
        if not any(runs.values()):
            self._failed(now, result if result.error else FetchResult(error=ERROR_BODY, status=result.status))
            return False
        self.backoff.succeeded()
        self.last_error, self.last_status = None, result.status
        for run, by_hour in runs.items():
            self.history.note_archive(run, by_hour)
        self.archive_from = first
        _LOGGER.debug("SpotNav solar shadow: archived weather %s..%s kept", first, last)
        return True

    def _refit_due(self, today: date) -> bool:
        if self.planes is None or self.planes_fitted_on is None:
            return True
        if not self.history.pv:
            return False
        days = (today - key_start(min(self.history.pv)).date()).days
        if days < YOUNG_DAYS:
            return (today - self.planes_fitted_on).days >= 7
        return (today.year, today.month) != (self.planes_fitted_on.year, self.planes_fitted_on.month)

    async def _async_nightly(self, position: tuple[float, float], tz: str, now: datetime, today: date) -> None:
        """Refit the planes when due, relearn every factor, review the days that ended (see the module)."""
        if self._refit_due(today):
            planes = await self.hass.async_add_executor_job(fit_from_history, dict(self.history.pv), position, now)
            if planes is not None:
                self.planes, self.planes_fitted_on = planes, today
                _LOGGER.debug("SpotNav solar shadow: planes fitted %s", planes_as_rows(planes))
        self.factors = await self.hass.async_add_executor_job(
            learn, self.history.copy(), self.planes, position, tz, today - timedelta(days=1)
        )
        self.learned_on = today
        reviewed = self.log.review(self.history, now)
        by_issue: dict[tuple[str, str], list[str]] = {}
        for (candidate, issue, day), score in reviewed:
            by_issue.setdefault((day, issue), []).append(
                f"{candidate} {score.mae:.3f}/{score.error_kwh:+.1f}"
            )
        for (day, issue), parts in sorted(by_issue.items()):
            _LOGGER.debug(
                "SpotNav solar shadow review %s %s (hourly MAE kWh / day error kWh): %s", day, issue, ", ".join(parts)
            )
        self.history.prune(now)
        self.log.prune(now)

    # ---- the issues

    def _usable_weather(self, now: datetime) -> dict[datetime, Weather]:
        if self.fetched_at is None or now - self.fetched_at > WEATHER_MAX_AGE:
            return {}
        return self.weather

    async def _async_issue_own(self, position: tuple[float, float], now: datetime, tz: str) -> bool:
        """The own candidates' issues whose time it is, and the next hours shown in the diagnostics."""
        if self.factors is None:
            return False
        weather = self._usable_weather(now)
        changed = False
        for issue, day, hours in due_issues(now, tz):
            if self.log.issued(issue, day, GROUP_OWN) or not hours:
                continue
            fallback = None
            if any(start not in weather for start in hours):
                fallback = await self._async_clouds()
            forecast, used = await self.hass.async_add_executor_job(
                partial(own_forecast, self.factors, self.planes, position, now, hours, weather, fallback)
            )
            meta = {
                "made_at": now.isoformat(),
                "weather_fetched_at": None if self.fetched_at is None or not weather else self.fetched_at.isoformat(),
                "inputs": used,
            }
            self.log.record(issue, day, GROUP_OWN, forecast, meta)
            changed = True
        if weather:
            hours = [start for start in sorted(weather) if start > now][:SHOWN_HOURS]
            shown, _used = await self.hass.async_add_executor_job(
                partial(own_forecast, self.factors, self.planes, position, now, hours, weather, None)
            )
            for name in OWN_CANDIDATES:
                self.next_hours.pop(name, None)
            self.next_hours.update({name: _rounded(by_hour) for name, by_hour in shown.items()})
        return changed

    @callback
    def note_provider(
        self, now: datetime, raw: Mapping[datetime, float], corrected: Mapping[datetime, float]
    ) -> None:
        """The provider's forecast as the solar correction read it (`raw`, hours ahead, kWh) and corrected it: kept
        for the provider's sun-position factor, and issued as the provider's candidates when an issue is due. Never
        raises."""
        try:
            self._note_provider(now.astimezone(timezone.utc), raw, corrected)
        except Exception:  # noqa: BLE001 - the correction must go on whatever happens here
            _LOGGER.debug("SpotNav solar shadow: the provider's forecast could not be noted", exc_info=True)

    def _note_provider(
        self, now: datetime, raw: Mapping[datetime, float], corrected: Mapping[datetime, float]
    ) -> None:
        position, tz = self._position, self._tz
        if position is None or tz is None or not raw:
            return
        changed = self.history.note_provider(raw, now)
        clouds = {start: w.cloud for start, w in self._usable_weather(now).items()}
        for issue, day, hours in due_issues(now, tz):
            if self.log.issued(issue, day, GROUP_PROVIDER):
                continue
            wanted = {start for start in daylight_hours(hours, position) if start in raw}
            if not wanted:
                continue
            day_raw = {start: raw[start] for start in wanted}
            candidates = {
                CANDIDATE_PROVIDER: day_raw,
                CANDIDATE_PROVIDER_CORRECTED: {start: corrected[start] for start in wanted if start in corrected},
            }
            if self.factors is not None:
                candidates[CANDIDATE_PROVIDER_SUNPOS] = provider_sunpos(self.factors, position, now, day_raw, clouds)
            self.log.record(issue, day, GROUP_PROVIDER, candidates, {"made_at": now.isoformat()})
            changed = True
        if self.factors is not None:
            ahead = {start: kwh for start, kwh in sorted(raw.items()) if start > now}
            shown = dict(list(ahead.items())[:SHOWN_HOURS])
            self.next_hours[CANDIDATE_PROVIDER] = _rounded(shown)
            self.next_hours[CANDIDATE_PROVIDER_SUNPOS] = _rounded(
                provider_sunpos(self.factors, position, now, shown, clouds)
            )
        if changed:
            self._store.changed()

    # ---- what is shown

    def snapshot(self) -> dict[str, Any]:
        """The status, the fetch, what is kept, the planes, the factors, the candidates' next hours and the review."""
        now = dt_util.utcnow()
        hours = sorted({start for by_hour in self.next_hours.values() for start in by_hour})
        return {
            # Shadow only: nothing here feeds a plan.
            "status": self.status,
            "candidates": list(CANDIDATES),
            "fetch": {
                "source": "open-meteo.com (CC BY 4.0)",
                "fetch_hours_local": list(FETCH_HOURS),
                "last_fetch": None if self.fetched_at is None else self.fetched_at.isoformat(),
                "hours_ahead": len([start for start in self.weather if start > now]),
                "last_error": self.last_error,
                "last_status": self.last_status,
                "calls": dict(self.calls),
                "backoff": self.backoff.as_dict(now.timestamp()),
            },
            "archive": {
                "from": None if self.archive_from is None else self.archive_from.isoformat(),
                "upto": None if self.archive_upto is None else self.archive_upto.isoformat(),
                "done": self.archive_done,
            },
            "kept": self.history.diagnostics(),
            "planes": planes_as_rows(self.planes),
            "planes_fitted_on": None if self.planes_fitted_on is None else self.planes_fitted_on.isoformat(),
            "factors": None if self.factors is None else self.factors.as_dict(),
            "next_hours": [
                {
                    "start": start.isoformat(),
                    **{name: by_hour[start] for name, by_hour in self.next_hours.items() if start in by_hour},
                }
                for start in hours[:SHOWN_HOURS]
            ],
            "issues": [
                {"issue": issue, "date": day, **meta}
                for (issue, day), meta in sorted(self.log.issues.items(), key=lambda item: item[0][1])[-4:]
            ],
            "review_30d": self.log.summary(now),
            "review_recent": self.log.recent(CANDIDATES),
        }


def _day(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None

