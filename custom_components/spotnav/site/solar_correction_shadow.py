"""The solar forecast correction in Home Assistant, in shadow: its sources, its hourly refresh and its log.

The policy is pure (`site/solar_correction.py`), and so is the log (`site/forecast_log.py`, the site's one forecast
log, kept by the house forecast, kinds `sun_kwh` and `sun_kwh_corrected`); this is the thin shell around them. One per site, owned by its `SiteCapacityController`, and only busy on a site that names a solar
forecast (`CONF_SOLAR_FORECAST_ENTRIES`, the Hybrid strategy's). Nothing here is a setting, and nothing here decides
what a charger is told: the Hybrid strategy keeps reading the raw forecast. The corrected forecast is logged beside
the raw one, both are reviewed each night, and all of it is shown in the site's diagnostics (and so in the debug
bundle).

Sources, all found without a setting:

* **The forecast**: what the Hybrid strategy reads (`planning/hybrid_forecast.py`), in kWh per UTC hour.
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
from .forecast_log import KIND_SUN, KIND_SUN_CORRECTED, ForecastLog

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

STATUS_NO_FORECAST: Final = "no_forecast_source"
STATUS_NO_PRODUCTION: Final = "no_solar_statistics"


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.solar_correction.{entry_id}"


# ------------------------------------------------------------------------------------------------ pure helpers


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


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
        """Start a refresh when the UTC hour changed, or a retry is due."""
        if not self._loaded or self._closed or (self._task is not None and not self._task.done()):
            return
        hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        retry = self._retry_at is not None and now >= self._retry_at
        if hour == self._for_hour and not retry:
            return
        self._for_hour = hour
        self._task = self.hass.async_create_background_task(
            self.async_refresh(now), "spotnav_solar_correction", eager_start=False
        )

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
        self.log.review(now)
        self.site.house_forecast.log_changed()
        self.state.prune(now)
        self.next_hours = [
            {
                "start": start.isoformat(),
                "raw_kwh": round(future[start], 3),
                "corrected_kwh": round(corrected[start], 3),
                "kind": kind_of(clouds.get(start)),
            }
            for start in sorted(future)[:SHOWN_HOURS]
            if start in corrected
        ]
        self.status = self.state.status if solar_ids else STATUS_NO_PRODUCTION
        self._retry_at = None if complete else now + timedelta(seconds=RETRY_S)
        self.refreshes += 1
        self.last_refresh = now
        self._store.changed()

    # ---- what is shown

    def snapshot(self) -> dict[str, Any]:
        """The status, the sources, the guard rails' findings, the factors, the next hours and the log."""
        state = self.state.as_dict()
        state.pop("meta", None)
        return {
            # Shadow only: the Hybrid strategy reads the raw forecast; this is logged and reviewed beside it.
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
            "log": self.log.diagnostics((KIND_SUN, KIND_SUN_CORRECTED)),
        }
