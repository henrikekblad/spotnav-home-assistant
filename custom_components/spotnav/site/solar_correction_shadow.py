"""The solar forecast correction in Home Assistant, in shadow: its sources, its hourly refresh and its log.

The policy is pure (`site/solar_correction.py`), and so is the log (`site/forecast_log.py`, the site's one forecast
log, kept by the house forecast, kinds `sun_kwh` and `sun_kwh_corrected`); this is the thin shell around them. One per site, owned by its `SiteCapacityController`, and only busy on a site that names a solar
forecast (`CONF_SOLAR_FORECAST_ENTRIES`, the Hybrid strategy's). Nothing here is a setting, and nothing here decides
what a charger is told: the Hybrid strategy keeps reading the raw forecast (the battery plan, in shadow, reads
the corrected one: `battery/plan_runtime.py`). The corrected forecast is logged beside
the raw one, both are reviewed each night, and all of it is shown in the site's diagnostics (and so in the debug
bundle).

Sources, all found without a setting:

* **The forecast**: what the Hybrid strategy reads (`planning/hybrid_forecast.py`), in kWh per UTC hour.
* **Solcast's own estimates**: when a forecast entry is the Solcast integration, its p10, p50 and p90 per hour from
  its forecast sensors' attributes (`site/solcast_detail.py`), logged as `sun_kwh_p10/p50/p90` and scored beside the
  others. Nothing plans on them.
* **Production**: the Energy dashboard's solar sources (`stat_energy_from`), read from the recorder's statistics:
  hourly for the outcome, 5-minute for the inverter's ceiling (clipping). An hour is counted only when every solar
  source has it.
* **Export**: the Energy dashboard's grid export statistics, 5-minute, for an export limit or a zero-export site.
* **Cloud cover**: the weather entity's hourly forecast (`weather.forecast_home`, else the only weather entity).
* **Negative prices**: the site's price area (`pricing/price_source.site_price_source`), today and tomorrow.
* **The array's size**: the forecast entries' `modules_power` (W, as Forecast.Solar keeps it), only when every
  entry states it.

It refreshes when the UTC hour changes (checked every `TICK_S`), and again after `RETRY_S` when a source it found
could not be read. The nightly update runs on the first refresh of a local day from `NIGHTLY_HOUR` on, when the
previous day's statistics are in.

Its own store keeps the correction's state. A log kept there by the release before (`log`) is moved into the site's
forecast log once, at the start, and is no longer written there.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import logging
import math
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..const import CONF_SOLAR_FORECAST_ENTRIES, DOMAIN
from ..debug_day import DayStore
from ..planning.hybrid_forecast import async_read_forecast_wh
from .solar_correction import (
    MIN_CEILING_KW,
    WINDOW_DAYS,
    SolarCorrection,
    clipped_hours,
    curtailment,
    day_evidence,
    kind_of,
    plateau_ceiling,
    samples_from_log,
)
from .energy_statistics import async_energy_ids, async_statistics, summed
from .forecast_log import (
    KIND_SUN,
    KIND_SUN_CORRECTED,
    KIND_SUN_P10,
    KIND_SUN_P50,
    KIND_SUN_P90,
    ForecastLog,
)
from .solar_correction import STATUS_SHADOW as CORRECTION_LEARNT
from .house_forecast import SURPLUS_OK
from .solcast_detail import read_detail
from .surplus_forecast import (
    MAX_AGE,
    NEAR_HOURS,
    STATE_HOURS,
    SurplusHour,
    expected_surplus,
    remaining_today_kwh,
    window_kwh,
)

if TYPE_CHECKING:
    from .site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

#: How often the shell looks whether the hour changed (s).
TICK_S: Final = 300.0
#: A refresh that could not read a source it found tries again after this long (s).
RETRY_S: Final = 900.0
#: The nightly update waits for this local hour (the last day's statistics are compiled by then).
NIGHTLY_HOUR: Final = 1
#: Statistics are read this many days back: the window and the day before it.
READ_DAYS: Final = WINDOW_DAYS + 1
#: The diagnostics show the next this many hours of the raw and corrected forecast.
SHOWN_HOURS: Final = 24
STORE_VERSION: Final = 1
SAVE_DELAY_S: Final = 30.0
DEFAULT_WEATHER: Final = "weather.forecast_home"

#: Solcast's quantiles and the log kind each is kept under.
DETAIL_KINDS: Final = {"p10": KIND_SUN_P10, "p50": KIND_SUN_P50, "p90": KIND_SUN_P90}
STATUS_NO_FORECAST: Final = "no_forecast_source"
#: Why the expected surplus is unknown, beside the house forecast's reasons (`house_forecast.SURPLUS_*`): no sun
#: forecast read in the last `MAX_AGE`, or no hour both forecasts know.
SURPLUS_NO_SUN: Final = "no_sun_forecast"
SURPLUS_NO_HOURS: Final = "no_common_hours"
STATUS_NO_PRODUCTION: Final = "no_solar_statistics"


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.solar_correction.{entry_id}"


# ------------------------------------------------------------------------------------------------ pure helpers


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def quarter_power(slots_kwh: Mapping[datetime, float]) -> dict[datetime, float]:
    """Five-minute energy rows (kWh by UTC slot start) as the mean power (kW) of each quarter hour they fall in; a
    quarter with only some of its rows (the one running) is the mean of those."""
    sums: dict[datetime, list[float]] = {}
    for start, kwh in slots_kwh.items():
        quarter = start.replace(minute=start.minute - start.minute % 15, second=0, microsecond=0)
        sums.setdefault(quarter, []).append(kwh)
    return {quarter: sum(rows) * 60.0 / (5.0 * len(rows)) for quarter, rows in sorted(sums.items())}


def as_power(slots_kwh: Mapping[datetime, float], minutes: float = 5.0) -> dict[datetime, float]:
    """Energy per slot (kWh) as the slot's mean power (kW)."""
    return {start: kwh * 60.0 / minutes for start, kwh in slots_kwh.items()}


def pick_weather_entity(entity_ids: Iterable[str]) -> str | None:
    """`weather.forecast_home` when it exists, else the only weather entity, else `None`."""
    weather = sorted({entity_id for entity_id in entity_ids if entity_id.startswith("weather.")})
    if DEFAULT_WEATHER in weather:
        return DEFAULT_WEATHER
    return weather[0] if len(weather) == 1 else None


def hourly_cloud(response: Any, entity_id: str) -> dict[datetime, float]:
    """`weather.get_forecasts`' hourly answer as `{UTC hour start: cloud cover %}`; entries without it are skipped."""
    body = response.get(entity_id) if isinstance(response, Mapping) else None
    entries = body.get("forecast") if isinstance(body, Mapping) else None
    clouds: dict[datetime, float] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        moment = dt_util.parse_datetime(str(entry.get("datetime") or ""))
        cover = entry.get("cloud_coverage")
        if moment is None or moment.tzinfo is None or not _number(cover):
            continue
        hour = moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        clouds.setdefault(hour, min(100.0, max(0.0, float(cover))))
    return clouds


def negative_hours(documents: Iterable[Any]) -> dict[datetime, bool]:
    """Per UTC hour the price documents cover: whether any of its intervals had a negative market price."""
    hours: dict[datetime, bool] = {}
    for document in documents:
        for piece in document.pieces():
            for interval in piece.intervals:
                hour = interval.utc_start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                hours[hour] = hours.get(hour, False) or interval.eur_per_kwh < 0
    return hours


def array_kwp(entry_settings: Sequence[Mapping[str, Any] | None]) -> float | None:
    """The array's size (kWp) when every forecast entry states its `modules_power` (W), else `None`."""
    if not entry_settings:
        return None
    total = 0.0
    for settings in entry_settings:
        value = None if settings is None else settings.get("modules_power")
        if not _number(value) or value <= 0:
            return None
        total += float(value) / 1000.0
    return total


# ------------------------------------------------------------------------------------------------ Home Assistant


class SiteSolarCorrection:
    """One site's solar forecast correction in shadow (see the module). Never raises into the site."""

    def __init__(self, hass: HomeAssistant, site: SiteCapacityController) -> None:
        self.hass = hass
        self.site = site
        self.state = SolarCorrection()
        self._store = DayStore(hass, store_key(site.entry_id), self._stored, save_delay_s=SAVE_DELAY_S)
        self._timer_cancel: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._for_hour: datetime | None = None
        self._retry_at: datetime | None = None
        self._loaded = False
        self._closed = False
        self.status: str = STATUS_NO_FORECAST
        self.refreshes = 0
        self.last_refresh: datetime | None = None
        self.sources: dict[str, Any] = {}
        self.observed_max_kwh: float | None = None
        self.array_kwp: float | None = None
        self.ceiling_kw: float | None = None
        self.clipped: set[datetime] = set()
        self.evidence: list[dict[str, Any]] = []
        self.next_hours: list[dict[str, Any]] = []
        #: The sun's measured mean power (kW) per quarter hour of the local day so far (UTC quarter starts), from the
        #: recorder's 5-minute statistics, read in its executor at most once per five minutes (display only).
        self.quarters: dict[datetime, float] = {}
        self._quarters_for: datetime | None = None
        self._quarters_task: asyncio.Task[None] | None = None
        #: The sun the expected surplus counts on (`surplus`): the last refresh's forecast from the running hour on
        #: (kWh per UTC hour), corrected once the correction has learnt (the battery plan's rule), else as read.
        self.sun_kwh: dict[datetime, float] = {}
        self.sun_basis: str | None = None
        self.detail_hours: int = 0

    @property
    def log(self) -> ForecastLog:
        """The site's forecast log (the house forecast's), where the sun's kinds are kept."""
        return self.site.house_forecast.log

    def _stored(self) -> dict[str, Any]:
        return {"version": STORE_VERSION, "correction": self.state.as_dict()}

    async def async_start(self) -> None:
        raw = await self._store.async_load()
        try:
            if isinstance(raw, dict):
                self.state = SolarCorrection.from_dict(raw.get("correction"))
        except Exception:  # noqa: BLE001 - a store that cannot be read starts again
            _LOGGER.debug("SpotNav solar correction: the kept state could not be read", exc_info=True)
        try:
            # The log the release before kept here: moved into the site's forecast log once, then dropped from
            # this store at its next write.
            if isinstance(raw, dict) and raw.get("log") is not None:
                if self.log.take_kinds(raw.get("log"), (KIND_SUN, KIND_SUN_CORRECTED)):
                    self.site.house_forecast.log_changed()
                self._store.changed()
        except Exception:  # noqa: BLE001 - a log that cannot be moved starts again
            _LOGGER.debug("SpotNav solar correction: the kept log could not be moved", exc_info=True)
        self._loaded = True
        self._timer_cancel = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=TICK_S))
        self.tick(dt_util.utcnow())

    async def async_shutdown(self) -> None:
        self._closed = True
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None
        for task in (self._task, self._quarters_task):
            if task is not None and not task.done():
                task.cancel()
        await self._store.async_close()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await DayStore.async_remove(hass, store_key(entry_id))

    @callback
    def _async_tick(self, now: datetime) -> None:
        self.tick(now)

    @callback
    def tick(self, now: datetime) -> None:
        """Start a refresh when the UTC hour changed, or a retry is due."""
        if not self._loaded or self._closed:
            return
        self._tick_quarters(now)
        if self._task is not None and not self._task.done():
            return
        hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        retry = self._retry_at is not None and now >= self._retry_at
        if hour == self._for_hour and not retry:
            return
        self._for_hour = hour
        self._task = self.hass.async_create_background_task(
            self.async_refresh(now), "spotnav_solar_correction", eager_start=False
        )

    def _tick_quarters(self, now: datetime) -> None:
        """Start the read of today's quarters when the five minutes changed."""
        step = now.astimezone(timezone.utc).replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
        if step == self._quarters_for or (self._quarters_task is not None and not self._quarters_task.done()):
            return
        if not self._forecast_entries():
            return
        self._quarters_for = step
        self._quarters_task = self.hass.async_create_background_task(
            self.async_refresh_quarters(step), "spotnav_solar_quarters", eager_start=False
        )

    async def async_refresh_quarters(self, step: datetime) -> None:
        """Today's measured quarters up to `step`; empty where the 5-minute statistics are missing (the card then
        keeps the hourly figures)."""
        try:
            solar_ids, _export = await self._async_energy_ids()
            if not solar_ids:
                self.quarters = {}
                return
            zone = time_zones.require_zone(self.site.demand.tracker.tz)
            midnight = step.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
            rows = await self._async_statistics(solar_ids, midnight.astimezone(timezone.utc), step, "5minute")
            self.quarters = quarter_power(summed(rows, solar_ids))
        except Exception:  # noqa: BLE001 - no short-term statistics: the hourly figures stand
            _LOGGER.debug("SpotNav solar correction: the 5-minute statistics could not be read", exc_info=True)
            self.quarters = {}

    # ---- sources

    def _forecast_entries(self) -> list[str]:
        return list(self.site.config.get(CONF_SOLAR_FORECAST_ENTRIES) or [])

    async def _async_energy_ids(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return await async_energy_ids(self.hass)

    async def _async_statistics(
        self, ids: Sequence[str], start: datetime, end: datetime, period: str
    ) -> dict[str, list[dict[str, Any]]]:
        return await async_statistics(self.hass, ids, start, end, period)

    async def _async_clouds(self) -> tuple[str | None, dict[datetime, float], bool]:
        """The weather entity, its hourly cloud cover, and whether it was read as far as it exists."""
        entity_id = pick_weather_entity(self.hass.states.async_entity_ids("weather"))
        if entity_id is None:
            return None, {}, True
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except Exception:  # noqa: BLE001 - no hourly forecast (or not yet): every hour counts as clear
            _LOGGER.debug("SpotNav solar correction: no hourly forecast from %s", entity_id, exc_info=True)
            return entity_id, {}, False
        return entity_id, hourly_cloud(response, entity_id), True

    def _prices(self) -> tuple[str | None, dict[datetime, bool]]:
        from ..pricing.price_source import site_price_source  # noqa: PLC0415 - the pricing side imports the site
        from ..runtime import domain_data  # noqa: PLC0415

        source = site_price_source(self.hass, self.site.entry_id)
        manager = domain_data(self.hass).price_refresh
        if source is None or manager is None:
            return None, {}
        snapshot = manager.area_snapshot(source.area_id)
        if snapshot is None:
            return source.area_id, {}
        documents = [doc for doc in (snapshot.today_document, snapshot.tomorrow_document) if doc is not None]
        return source.area_id, negative_hours(documents)

    def _array_kwp(self, entries: Sequence[str]) -> float | None:
        settings: list[Mapping[str, Any] | None] = []
        for entry_id in entries:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            settings.append(None if entry is None else {**entry.data, **entry.options})
        return array_kwp(settings)

    # ---- the refresh

    async def async_refresh(self, now: datetime) -> None:
        """Read the sources, log the raw and corrected forecast and the outcomes, run the nightly update once a
        day, review the days that ended. Never raises."""
        try:
            await self._async_refresh(now.astimezone(timezone.utc))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a shadow forecast must never disturb the site
            _LOGGER.debug("SpotNav solar correction: the refresh failed", exc_info=True)
            self._retry_at = now + timedelta(seconds=RETRY_S)

    async def _async_refresh(self, now: datetime) -> None:
        entries = self._forecast_entries()
        if not entries:
            self.status = STATUS_NO_FORECAST
            self.sources = {}
            self._retry_at = None
            return
        tz = self.site.demand.tracker.tz
        self.log.tz = tz
        hour = now.replace(minute=0, second=0, microsecond=0)
        complete = True

        forecast = await async_read_forecast_wh(self.hass, entries, now=now)
        raw = {start: wh / 1000.0 for start, wh in (forecast.wh_hours or {}).items()}
        complete &= forecast.wh_hours is not None
        solar_ids, export_ids = await self._async_energy_ids()
        weather, clouds, read = await self._async_clouds()
        complete &= read
        area, negative = self._prices()
        self.array_kwp = self._array_kwp(entries)
        self.sources = {
            "forecast_entries": entries,
            "forecast_read": list(forecast.sources_read),
            "solar_statistics": list(solar_ids),
            "export_statistics": list(export_ids),
            "weather_entity": weather,
            "cloud_hours": len(clouds),
            "price_area": area,
        }

        first = hour - timedelta(days=READ_DAYS)
        hourly: dict[datetime, float] = {}
        solar_slots: dict[datetime, float] = {}
        export_hourly: dict[datetime, float] = {}
        export_slots: dict[datetime, float] | None = None
        if solar_ids:
            hourly = summed(await self._async_statistics(solar_ids, first, hour, "hour"), solar_ids)
            solar_slots = as_power(
                summed(await self._async_statistics(solar_ids, first, hour, "5minute"), solar_ids)
            )
        if export_ids:
            export_hourly = summed(await self._async_statistics(export_ids, first, hour, "hour"), export_ids)
            export_slots = as_power(
                summed(await self._async_statistics(export_ids, first, hour, "5minute"), export_ids)
            )
        if self._closed:
            return

        # The outcomes of the completed hours, for both kinds (the review scores each against the same outcome).
        for start, kwh in hourly.items():
            self.log.record_actual(KIND_SUN, start, kwh)
            self.log.record_actual(KIND_SUN_CORRECTED, start, kwh)
            for kind in DETAIL_KINDS.values():
                self.log.record_actual(kind, start, kwh)
        logged_actuals = [h.actual for h in self.log.hours(KIND_SUN).values() if h.actual is not None]
        observed = [*hourly.values(), *logged_actuals]
        self.observed_max_kwh = max(observed) if observed else None
        self.ceiling_kw = plateau_ceiling(solar_slots, min_kw=MIN_CEILING_KW)
        self.clipped = clipped_hours(solar_slots, self.ceiling_kw)

        # What is known of each hour: its cloud cover as first seen with its forecast, its price's sign.
        future = {start: kwh for start, kwh in raw.items() if start > now}
        for start in future:
            self.state.note_hour(start, cloud_pct=clouds.get(start), negative_price=negative.get(start))
        for start, is_negative in negative.items():
            if start in self.state.meta:
                self.state.meta[start].negative_price = is_negative

        # The guard rails' view of the window, and the nightly update once a local day, before today's forecast
        # is corrected.
        zone = time_zones.require_zone(tz)
        local = now.astimezone(zone)
        evidence = day_evidence(samples_from_log(self.log.hours(KIND_SUN), self.state.meta, self.clipped), tz)
        self.evidence = [item.as_dict() for _day, item in sorted(evidence.items())[-WINDOW_DAYS:]]
        if self.state.updated_on != local.date() and local.hour >= NIGHTLY_HOUR and solar_ids:
            window = [
                start for start in hourly if 1 <= (local.date() - start.astimezone(zone).date()).days <= WINDOW_DAYS
            ]
            curtailed = curtailment(
                evidence,
                local.date(),
                export_slots=export_slots,
                solar_kwh=sum(hourly[start] for start in window) if window else None,
                export_kwh=sum(export_hourly.get(start, 0.0) for start in window) if window and export_ids else None,
            )
            self.state.nightly(evidence, local.date(), curtailed=curtailed)
            _LOGGER.debug(
                "SpotNav solar correction (shadow): factors %s, curtailed %s", self.state.factors, curtailed
            )

        corrected = self.state.correct(
            future, observed_max_kwh=self.observed_max_kwh, array_kwp=self.array_kwp, cloud_pct=clouds
        )
        self.log.record_forecast(KIND_SUN, future, now)
        self.log.record_forecast(KIND_SUN_CORRECTED, corrected, now)
        # The own solar forecast in shadow keeps the provider's forecast for its sun-position factor and issues the
        # provider's candidates from it (`site/solar_shadow.py`).
        shadow = getattr(self.site, "solar_shadow", None)
        if shadow is not None:
            shadow.note_provider(now, future, corrected)
        running = {start: kwh for start, kwh in raw.items() if start >= hour}
        learnt = self.state.status == CORRECTION_LEARNT
        corrected_running = self.state.correct(
            running, observed_max_kwh=self.observed_max_kwh, array_kwp=self.array_kwp, cloud_pct=clouds
        )
        self.sun_kwh = corrected_running if learnt else running
        self.sun_basis = "corrected" if learnt else "raw"
        detail = read_detail(self.hass, entries)
        self.detail_hours = 0 if detail is None else len(detail["p50"])
        for quantile, kind in DETAIL_KINDS.items():
            if detail is not None:
                self.log.record_forecast(kind, {s: k for s, k in detail[quantile].items() if s > now}, now)
        self.log.review(now)
        self.site.house_forecast.log_changed()
        self.state.prune(now)
        # For display only: the running hour is shown too (its forecast is still the best figure for the hour that
        # is not finished), so the charger card's sun has no gap between the measured hours and the forecast.
        self.next_hours = [
            {
                "start": start.isoformat(),
                "raw_kwh": round(running[start], 3),
                "corrected_kwh": round(corrected_running[start], 3),
                "kind": kind_of(clouds.get(start)),
            }
            for start in sorted(running)[:SHOWN_HOURS]
            if start in corrected_running
        ]
        self.status = self.state.status if solar_ids else STATUS_NO_PRODUCTION
        self._retry_at = None if complete else now + timedelta(seconds=RETRY_S)
        self.refreshes += 1
        self.last_refresh = now
        self._store.changed()

    # ---- the expected surplus

    def surplus(self, now: datetime) -> list[SurplusHour] | None:
        """The hours of expected solar surplus (`site/surplus_forecast.py`): the sun of the last refresh less the
        house's whole load (its median, `SiteHouseForecast.surplus_house_by_hour`), never the import less the chargers,
        which has the sun taken off already. `None` without a sun forecast read in the last `MAX_AGE` or without the
        whole load; why is in `surplus_status`."""
        return self._surplus(now)[0]

    def surplus_status(self, now: datetime) -> str:
        """Why the expected surplus is unknown (`SURPLUS_*`), `ok` when it is not."""
        return self._surplus(now)[1]

    def _surplus(self, now: datetime) -> tuple[list[SurplusHour] | None, str]:
        if not self.sun_kwh or self.last_refresh is None or now - self.last_refresh > MAX_AGE:
            return None, SURPLUS_NO_SUN
        house, status = self.site.house_forecast.surplus_house_by_hour(now)
        if status != SURPLUS_OK:
            return None, status
        shown = expected_surplus(self.sun_kwh, house, now)
        return (shown, SURPLUS_OK) if shown else (None, SURPLUS_NO_HOURS)

    def surplus_snapshot(self, now: datetime) -> dict[str, Any] | None:
        """The state and attributes of the expected surplus sensor, also shown in the diagnostics; `None` when unknown."""
        shown = self.surplus(now)
        if shown is None:
            return None
        state = window_kwh(shown, now, timedelta(hours=STATE_HOURS))
        if state is None:
            return None
        today = remaining_today_kwh(shown, now, self.site.demand.tracker.tz)
        return {
            "kwh": round(state, 1),
            "attributes": {
                f"next_{NEAR_HOURS}h_kwh": round(window_kwh(shown, now, timedelta(hours=NEAR_HOURS)) or 0.0, 1),
                "remaining_today_kwh": None if today is None else round(today, 1),
                "window_hours": STATE_HOURS,
                "sun": self.sun_basis,
                "hours": [
                    {
                        "start": hour.start.isoformat(),
                        "sun_kwh": round(hour.sun_kwh, 2),
                        "house_kwh": round(hour.house_kwh, 2),
                        "surplus_kwh": round(hour.surplus_kwh, 2),
                    }
                    for hour in shown
                ],
            },
        }

    # ---- what is shown

    def snapshot(self) -> dict[str, Any]:
        """The status, the sources, the guard rails' findings, the factors, the next hours and the log."""
        state = self.state.as_dict()
        state.pop("meta", None)
        return {
            # The Hybrid strategy reads the raw forecast; this is logged and reviewed beside it, and the battery plan
            # (itself shadow) counts on it once learnt.
            "status": self.status,
            "curtailed": self.state.curtailed,
            "sources": self.sources,
            "array_kwp": None if self.array_kwp is None else round(self.array_kwp, 3),
            "observed_max_kwh": None if self.observed_max_kwh is None else round(self.observed_max_kwh, 3),
            "inverter_ceiling_kw": None if self.ceiling_kw is None else round(self.ceiling_kw, 3),
            "clipped_hours": sorted(start.isoformat() for start in self.clipped),
            "correction": state,
            "hours_known": len(self.state.meta),
            "evidence": self.evidence,
            "next_hours": self.next_hours,
            "refreshes": self.refreshes,
            "last_refresh": None if self.last_refresh is None else self.last_refresh.isoformat(),
            "retry_at": None if self._retry_at is None else self._retry_at.isoformat(),
            "solcast_hours": self.detail_hours,
            "expected_surplus": self.surplus_snapshot(dt_util.utcnow()),
            "expected_surplus_status": self.surplus_status(dt_util.utcnow()),
            "sun_basis": self.sun_basis,
            "log": self.log.diagnostics((KIND_SUN, KIND_SUN_CORRECTED, *DETAIL_KINDS.values())),
        }
