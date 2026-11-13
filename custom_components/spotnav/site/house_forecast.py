"""The site's house load forecast in Home Assistant: its sources, its hourly refresh and its log.

The model is pure (`site/load_forecast.py`), and so is the log with its nightly review (`site/forecast_log.py`); this
is the thin shell around them. Nothing here is a setting, and nothing here decides what a charger is told (a plan
uses the forecast in a later release); it is shown on the site's state sensor (`forecast`), in the diagnostics and
in the debug bundle.

* **Weather** (no setting): `weather.forecast_home` (the core Met.no default), else the only weather entity, else
  none. Its `temperature` is read on every reading of the site into the running hour (`HourMean.outdoor_c`); its
  hourly forecast is fetched once an hour (`weather.get_forecasts`). Fahrenheit is turned into Celsius.
* **Holidays** (no setting): a calendar of Home Assistant's core Holiday integration, the one for Home Assistant's
  country when there are several, else the only one; its dates are read once a day (`calendar.get_events`). Without
  one: weekday/weekend only, nothing guessed.
* **Refresh**: when the running hour changes (an hour completed, or after a start), in the background; the model runs
  in the executor. A refresh that could not read a source it found is tried again after `RETRY_S`.
* **Log**: each hour's first forecast and its outcome (the hour's measured `house_kw`), reviewed per local day the
  night after; kept in the site's own store (`spotnav.load_forecast.<entry>`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, time as dt_time, timedelta, timezone
from functools import partial
import logging
from typing import Any, Final

from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..const import DOMAIN
from ..debug_day import DayStore
from ..planning.demand_charge import hour_start
from .demand_tracker import outdoor_reading, SiteDemand
from .forecast_log import ForecastLog, KEEP_HOURS_DAYS, KIND_HOUSE
from .load_forecast import forecast_house_load, LoadForecast, WINDOW_DAYS

_LOGGER = logging.getLogger(__name__)

#: The core Met.no weather entity every new Home Assistant has.
DEFAULT_WEATHER: Final = "weather.forecast_home"
#: The core Holiday integration's platform name in the entity registry.
HOLIDAY_PLATFORM: Final = "holiday"
#: A refresh that could not read a source it found tries again after this long.
RETRY_S: Final = 600.0
#: The sensor's `forecast` attribute shows this many hours.
ATTRIBUTE_HOURS: Final = 24
#: The log's store waits this long before it is written (it changes once an hour).
SAVE_DELAY_S: Final = 30.0

WEATHER_NONE: Final = "no_weather_entity"
WEATHER_OK: Final = "ok"
WEATHER_NO_FORECAST: Final = "no_hourly_forecast"
HOLIDAYS_NONE: Final = "weekday_weekend_only"


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.load_forecast.{entry_id}"


# ------------------------------------------------------------------------------------------------ pure helpers


def pick_weather_entity(entity_ids: Iterable[str]) -> str | None:
    """`weather.forecast_home` when it exists, else the only weather entity, else `None`."""
    weather = sorted({entity_id for entity_id in entity_ids if entity_id.startswith("weather.")})
    if DEFAULT_WEATHER in weather:
        return DEFAULT_WEATHER
    return weather[0] if len(weather) == 1 else None


def to_celsius(value: Any, unit: Any) -> float | None:
    """A temperature in `unit` as °C, `None` when it is not a number or the unit is not one of the two."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if unit in (None, UnitOfTemperature.CELSIUS):
        return outdoor_reading(number)
    if unit == UnitOfTemperature.FAHRENHEIT:
        return outdoor_reading((number - 32.0) * 5.0 / 9.0)
    return None


def hourly_temperatures(response: Any, entity_id: str, unit: Any, tz: str) -> dict[datetime, float]:
    """`weather.get_forecasts`' hourly answer as `{hour_start (UTC): °C}`; unreadable entries are skipped."""
    body = response.get(entity_id) if isinstance(response, Mapping) else None
    entries = body.get("forecast") if isinstance(body, Mapping) else None
    temperatures: dict[datetime, float] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        moment = dt_util.parse_datetime(str(entry.get("datetime") or ""))
        celsius = to_celsius(entry.get("temperature"), unit)
        if moment is None or moment.tzinfo is None or celsius is None:
            continue
        temperatures.setdefault(hour_start(moment.astimezone(timezone.utc), tz), celsius)
    return temperatures


def holiday_dates(response: Any, entity_id: str) -> set[date]:
    """`calendar.get_events`' answer as the dates its all-day events cover (a holiday calendar's start and end are
    the same day); an event at a time counts on its start's date."""
    body = response.get(entity_id) if isinstance(response, Mapping) else None
    events = body.get("events") if isinstance(body, Mapping) else None
    dates: set[date] = set()
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, Mapping):
            continue
        first, last = _event_day(event.get("start")), _event_day(event.get("end"))
        if first is None:
            continue
        dates.add(first)
        if last is not None and isinstance(event.get("end"), str) and len(str(event.get("end"))) == 10:
            # An all-day event's end is exclusive by the calendar's rules; a holiday calendar sets it to the start.
            day = first + timedelta(days=1)
            while day < last and len(dates) < 1000:
                dates.add(day)
                day += timedelta(days=1)
    return dates


def _event_day(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw) if len(raw) == 10 else datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def pick_holiday_calendar(candidates: Mapping[str, str | None], country: str | None) -> str | None:
    """Of the Holiday integration's calendars (`{entity_id: its country}`), the one for `country` when there is one,
    else the only one, else `None`."""
    matching = sorted(entity_id for entity_id, own in candidates.items() if country and own == country)
    if matching:
        return matching[0]
    return next(iter(candidates)) if len(candidates) == 1 else None


# ------------------------------------------------------------------------------------------------ Home Assistant


class SiteHouseForecast:
    """One site's house load forecast (see the module). Never raises into the site."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str | None,
        demand: SiteDemand,
        *,
        flat_kw: Callable[[], float | None],
        notify: Callable[[], None],
    ) -> None:
        self.hass = hass
        self._demand = demand
        self._flat_kw = flat_kw
        self._notify = notify
        self.log = ForecastLog(tz=demand.tracker.tz)
        self.forecast: LoadForecast | None = None
        self.weather_entity_id: str | None = None
        self.weather_status: str = WEATHER_NONE
        self.holiday_entity_id: str | None = None
        self.holidays: frozenset[date] | None = None
        self._holidays_day: date | None = None
        self._temperatures: dict[datetime, float] = {}
        self._for_hour: datetime | None = None
        self._retry_at: datetime | None = None
        self._running = False
        self._closed = False
        self._attribute: list[dict[str, Any]] = []
        self.refreshes = 0
        self._loaded = entry_id is None
        self._store: DayStore | None = None
        if entry_id is not None:
            self._store = DayStore(hass, store_key(entry_id), self.log.as_dict, save_delay_s=SAVE_DELAY_S)

    async def async_load(self) -> None:
        if self._store is not None:
            raw = await self._store.async_load()
            try:
                self.log.restore(raw)
            except Exception:  # noqa: BLE001 - a log that cannot be read starts again
                _LOGGER.debug("SpotNav house forecast: the kept log could not be read", exc_info=True)
        self._loaded = True

    async def async_close(self) -> None:
        self._closed = True
        if self._store is not None:
            await self._store.async_close()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The site is gone for good: forget its log."""
        await DayStore.async_remove(hass, store_key(entry_id))

    # ---- sources

    def _weather_entity(self) -> str | None:
        return pick_weather_entity(self.hass.states.async_entity_ids("weather"))

    def outdoor_c(self) -> float | None:
        """The weather entity's temperature now (°C), `None` without one."""
        entity_id = self._weather_entity()
        state = None if entity_id is None else self.hass.states.get(entity_id)
        if state is None:
            return None
        return to_celsius(state.attributes.get("temperature"), state.attributes.get("temperature_unit"))

    def _holiday_entity(self) -> str | None:
        registry = er.async_get(self.hass)
        candidates: dict[str, str | None] = {}
        for entry in registry.entities.values():
            if entry.domain != "calendar" or entry.platform != HOLIDAY_PLATFORM or entry.disabled_by is not None:
                continue
            config = None
            if entry.config_entry_id is not None:
                config = self.hass.config_entries.async_get_entry(entry.config_entry_id)
            country = None if config is None else config.data.get("country")
            candidates[entry.entity_id] = country if isinstance(country, str) else None
        return pick_holiday_calendar(candidates, self.hass.config.country)

    async def _async_temperatures(self, tz: str) -> bool:
        """Fetch the weather entity's hourly forecast; whether the sources were read as far as they exist."""
        entity_id = self.weather_entity_id = self._weather_entity()
        if entity_id is None:
            self.weather_status = WEATHER_NONE
            self._temperatures = {}
            return True
        state = self.hass.states.get(entity_id)
        unit = None if state is None else state.attributes.get("temperature_unit")
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except Exception:  # noqa: BLE001 - no hourly forecast (or not yet): the profile runs without the term
            _LOGGER.debug("SpotNav house forecast: no hourly forecast from %s", entity_id, exc_info=True)
            self.weather_status = WEATHER_NO_FORECAST
            return False
        temperatures = hourly_temperatures(response, entity_id, unit, tz)
        self.weather_status = WEATHER_OK if temperatures else WEATHER_NO_FORECAST
        if temperatures:
            self._temperatures = temperatures
        return bool(temperatures)

    async def _async_holidays(self, now: datetime, tz: str) -> bool:
        """Read the holiday calendar's dates (once a day); whether the sources were read as far as they exist."""
        zone = time_zones.require_zone(tz)
        today = now.astimezone(zone).date()
        entity_id = self._holiday_entity()
        if entity_id == self.holiday_entity_id and self._holidays_day == today:
            return True
        self.holiday_entity_id = entity_id
        if entity_id is None:
            self.holidays, self._holidays_day = None, today
            return True
        start = datetime.combine(today - timedelta(days=WINDOW_DAYS + 1), dt_time.min, zone)
        end = datetime.combine(today + timedelta(days=4), dt_time.min, zone)
        try:
            response = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {"entity_id": entity_id, "start_date_time": start.isoformat(), "end_date_time": end.isoformat()},
                blocking=True,
                return_response=True,
            )
        except Exception:  # noqa: BLE001 - weekday/weekend until the calendar answers
            _LOGGER.debug("SpotNav house forecast: %s could not be read", entity_id, exc_info=True)
            return False
        self.holidays, self._holidays_day = frozenset(holiday_dates(response, entity_id)), today
        return True

    # ---- the hourly refresh

    @callback
    def tick(self) -> None:
        """Called on each reading of the site: start a refresh when the running hour changed (or a retry is due)."""
        if not self._loaded or self._running or self._closed:
            return
        now = self._demand.now()
        current = hour_start(now, self._demand.tracker.tz)
        retry = self._retry_at is not None and now >= self._retry_at
        if current == self._for_hour and not retry:
            return
        self._running = True
        self._for_hour = current
        self.hass.async_create_background_task(self.async_refresh(), "spotnav_house_forecast")

    async def async_refresh(self) -> None:
        """Read the sources, run the model in the executor, log the forecast and the hours' outcomes, review the
        days that ended. Never raises."""
        self._running = True
        try:
            now = self._demand.now()
            tracker = self._demand.tracker
            tz = tracker.tz
            complete = await self._async_temperatures(tz)
            complete &= await self._async_holidays(now, tz)
            self._retry_at = None if complete else now + timedelta(seconds=RETRY_S)
            hours = (*tracker.previous, *tracker.hours)
            temperatures = dict(self._temperatures)
            running_outdoor = self.outdoor_c()
            current = hour_start(now, tz)
            if running_outdoor is not None:
                temperatures.setdefault(current, running_outdoor)
            forecast = await self.hass.async_add_executor_job(
                partial(
                    forecast_house_load,
                    hours,
                    now,
                    tz,
                    flat_kw=self._flat_kw(),
                    temperatures=temperatures,
                    holidays=self.holidays,
                )
            )
            if self._closed:
                return
            self.forecast = forecast
            # Hours without a figure are left out: a site that knows nothing yet shows an empty list.
            self._attribute = [
                {"start": hour.start.isoformat(), "kw": round(hour.kw, 2)}
                for hour in forecast.hours[:ATTRIBUTE_HOURS]
                if hour.kw is not None
            ]
            self._log(forecast, hours, now, tz)
            self.refreshes += 1
            self._notify()
        except Exception:  # noqa: BLE001 - a forecast must never disturb the site
            _LOGGER.debug("SpotNav house forecast: the refresh failed", exc_info=True)
        finally:
            self._running = False

    def _log(self, forecast: LoadForecast, hours: Iterable[Any], now: datetime, tz: str) -> None:
        log = self.log
        log.tz = tz
        oldest = now - timedelta(days=KEEP_HOURS_DAYS)
        for hour in hours:
            if hour.start >= oldest and not hour.imported and hour.house_kw is not None:
                log.record_actual(KIND_HOUSE, hour.start, hour.house_kw)
        log.record_forecast(KIND_HOUSE, forecast.by_hour(), now)
        log.review(now)
        if self._store is not None:
            self._store.changed()

    # ---- what is shown

    @property
    def attribute(self) -> list[dict[str, Any]]:
        """The site state sensor's `forecast`: the next `ATTRIBUTE_HOURS` hours that have a figure, `[{start, kw}]`."""
        return self._attribute

    def diagnostics(self) -> dict[str, Any]:
        """The sources, the last forecast and the log with its reviews."""
        return {
            "weather_entity": self.weather_entity_id,
            "weather": self.weather_status,
            "forecast_temperatures": len(self._temperatures),
            "holidays": self.holiday_entity_id or HOLIDAYS_NONE,
            "holiday_dates": None if self.holidays is None else sorted(day.isoformat() for day in self.holidays),
            "refreshes": self.refreshes,
            "retry_at": None if self._retry_at is None else self._retry_at.isoformat(),
            "forecast": None if self.forecast is None else self.forecast.as_dict(),
            "log": self.log.diagnostics(),
        }
