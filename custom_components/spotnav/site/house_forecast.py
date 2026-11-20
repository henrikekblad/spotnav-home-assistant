"""The site's house load forecast in Home Assistant: its sources, its hourly refresh and its log.

The model is pure (`site/load_forecast.py`), and so is the log with its nightly review (`site/forecast_log.py`); this
is the thin shell around them. Nothing here is a setting, and nothing here decides what a charger is told: a plan
counts on it under the import target (`plan_kw_by_hour`, `plan_kw_at`), while the hold and fuse protection stay on
measurements; it is shown on the site's state sensor (`forecast`), in the diagnostics and in the debug bundle.

* **What the house is** (`basis`): the import less the chargers (`net`), as the hour tracker measures it, on a site
  with neither a home battery nor the sun's production in the Energy dashboard. On a site with a battery's power
  reading that figure counts what the battery charged and discharged (the owner's read ~0 kW while the battery
  covered the house), and on a site with solar production it is the house less the sun, so there the house is its
  whole load (`gross`): `grid - car - battery` per hour (the battery term 0 without one) plus the sun's production
  from the Energy dashboard's statistics (`site/house_history.py`). On a battery site that is the house figure from
  the start (`house_basis`): the import less the chargers there is the battery's timing, not the house (it read ~0
  while the battery covered the house and up to the fuse while it grid-charged, so its p75 per hour, scaled by the
  in-day adjustment, forecast 17 kW for a house drawing 1.5 kW); with few hours of the whole load the forecast falls
  back to their flat mean, and to nothing before the first. On a solar site without a battery it is the house figure
  once `MIN_GROSS_HOURS` such hours are kept, and an hour is filed only with its production (never as the house
  less the sun). The battery plan's own rows of before are taken over once (`async_take_rows`). The shown forecast,
  the log's house kinds and the battery plan's input (`battery_house_by_hour`, the median without the in-day
  adjustment) are then the whole load.
* **The expected solar surplus** (`surplus_house_by_hour`): the sun less the house's whole load, so it reads the
  whole load's median without the in-day adjustment, and nothing (with a reason) while there is no whole load with
  the sun in every hour: the import less the chargers already has the sun taken off.
* **The import target keeps the net figure** (`plan_kw_by_hour`): the target is about the hour's net import, and the
  import less the chargers as measured already holds what the battery and the sun really did in each hour (a battery
  in its own AI mode that grid-charges at night, the sun at noon). The whole load less an expected battery and sun
  would rest on a battery plan the battery does not follow (shadow) and on a sun forecast that runs optimistic, and
  a p75 house less a p50 sun is no p75 of the import: a car would be planned too much room. So on a site whose house
  figure is the whole load a second forecast runs on the net rows for the target (`import_forecast`, logged as
  `import_kw`); elsewhere it is the shown forecast itself. The hold and fuse protection stay on measurements.

* **Weather** (no setting): `weather.forecast_home` (the core Met.no default), else the only weather entity, else
  none. Its `temperature` is read on every reading of the site into the running hour (`HourMean.outdoor_c`); its
  hourly forecast is fetched once an hour (`weather.get_forecasts`). Fahrenheit is turned into Celsius.
* **Outdoor temperature now** (no setting): a heat pump's own outdoor sensor when exactly one is found
  (`OUTDOOR_SENSORS`: NIBE's BT1 through `nibe_heatpump` or `myuplink`, MELCloud's outside temperature, matched by
  the integration and the key in the unique id), else the weather entity's. A sensor found by its name or area is not
  counted: too often a sensor in the sun or indoors. Looked for at each refresh; the source is in the diagnostics
  (`outdoor`). The hourly forecast ahead is always the weather entity's.
* **Same-source offset** (`offset`, `site/temperature_history.py`): where the history is the heat pump's sensor and
  the forecast the weather entity's, the model learned its temperature term on the sensor's scale, so the forecast
  temperatures it is fed are moved by the median of (the sensor's hour − the weather entity's last forecast for it)
  over the last two weeks, from a week of such hours on, clamped to ±5 °C. Where both are the weather entity's,
  nothing is moved. In the diagnostics (`temperature_history.offset`); the log keeps the forecast as fetched.
* **Holidays** (no setting): a calendar of Home Assistant's core Holiday integration, the one for Home Assistant's
  country when there are several, else the only one; its dates are read once a day (`calendar.get_events`). Without
  one: weekday/weekend only, nothing guessed.
* **Refresh**: when the running hour changes (an hour completed, or after a start), in the background; the model runs
  in the executor. A refresh that could not read a source it found is tried again after `RETRY_S`.
* **History**: the measured hours of the last eight weeks (`site/house_history.py`), merged from the demand tracker on
  each refresh (its two months taken over on the first), so early in a month the forecast still reads six weeks.
* **Log**: each hour's first forecast, as used and before the in-day adjustment (`house_kw_raw`), the battery plan's
  median (`house_kw_median`, whose misses are its error band, `site/forecast_band.py`), the next day's as
  made once the day-ahead prices are out (`house_day_ahead`, 13:00 local, what that day's plans count on), and the
  outcome (the hour's measured house figure), reviewed per local day the night after; kept with the history in the
  site's own store (`spotnav.load_forecast.<entry>`). On a whole-load site the import target's forecast is logged
  beside them (`import_kw`), and when the house figure changes what it is, the house kinds' days not yet reviewed are
  dropped. The log is the site's one forecast log: the battery plan and the solar correction log their kinds in it
  too (`log_changed`).
* **Temperatures kept for a year** (`site/temperature_history.py`): each completed hour's outdoor temperature as the
  history holds it, where it was read from (change markers), and the weather entity's forecast temperature per hour as
  known at the day-ahead issue and at the last refresh before the hour; ~400 days, in a store of their own
  (`spotnav.temperature_history.<entry>`) written at most once an hour (`TEMPERATURE_SAVE_DELAY_S`) and at an unload.
  Nothing reads them back into the forecast yet: they are what cannot be fetched later. Counted in the diagnostics.
* **Heat** (`site/heat_meter.py`, shown only): where the heat pump's meter is found (or set), its hours are read from
  the recorder at each refresh and the house figure is forecast a second time split in two, heat (learned against
  the outdoor temperature by the same model) and the rest (the house less heat), each logged with its outcome
  (`heat_kw`, and their sum as `house_kw_split` beside `house_kw`) so the nightly review shows whether the split
  forecasts the house better; each reviewed day's comparison is written to the log. Plans keep the whole load's
  forecast: nothing a car or the battery is planned on changes, except for the next point.
* **The heat pump's relay** (`heat/runtime.py`, `heat_delta`): while the relay the heat settings name is closed, or
  expected to close (the SpotNav blueprint's closings ahead), the heat it takes away and the rebound after it change
  what the plans count on (`plan_kw_by_hour`, `battery_house_by_hour`, `surplus_house_by_hour`), never below 0 kW,
  so the cars and the battery plan around the lowered hours. The shown forecast and the log stay as learned.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, time as dt_time, timedelta, timezone
from functools import partial
import logging
import re
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
from .energy_statistics import async_energy_ids, async_statistics, summed
from .forecast_log import (
    ForecastLog,
    KEEP_HOURS_DAYS,
    KIND_HOUSE,
    KIND_HEAT,
    KIND_HOUSE_DAY_AHEAD,
    KIND_HOUSE_MEDIAN,
    KIND_HOUSE_RAW,
    KIND_HOUSE_SPLIT,
    KIND_IMPORT,
    DayReview,
    next_day_hours,
)
from .forecast_band import FORM_ADDITIVE, learn as learn_band, pairs_from_log, view as band_view
from .heat_meter import HeatSplit, SiteHeatMeter, heat_split
from .house_history import HouseHistory
from .load_forecast import forecast_house_load, LoadForecast, WINDOW_DAYS
from .temperature_history import NO_OFFSET, Offset, source_marker, SOURCE_SENSOR, SOURCE_WEATHER, TemperatureHistory

_LOGGER = logging.getLogger(__name__)

#: The core Met.no weather entity every new Home Assistant has.
DEFAULT_WEATHER: Final = "weather.forecast_home"
#: The core Holiday integration's platform name in the entity registry.
HOLIDAY_PLATFORM: Final = "holiday"
#: A refresh that could not read a source it found tries again after this long.
RETRY_S: Final = 600.0
#: How long a weather or calendar read may take before it counts as not read (s): Home Assistant has no timeout of
#: its own, and a hung integration must not hold the refresh.
READ_TIMEOUT_S: Final = 20.0
#: The sensor's `forecast` attribute shows this many hours.
ATTRIBUTE_HOURS: Final = 24
#: The log's store waits this long before it is written (it changes once an hour).
SAVE_DELAY_S: Final = 30.0
#: The kept temperatures' store waits this long before it is written: their hours change once an hour, and a year of
#: them is no reason to write more often (an unload writes what waits).
TEMPERATURE_SAVE_DELAY_S: Final = 3600.0
#: A forecast older than this (a refresh that keeps failing) is not counted on by a plan: the flat figure is.
PLAN_MAX_AGE_S: Final = 3 * 3600.0

#: The battery plan's house forecast: the profile's median, without the in-day adjustment. Planned again every hour
#: on the hours measured so far, the median cost the plan least (`scripts/forecast_gap_backtest.py`, 2026-11-13: about
#: 25 kr against p75 and 35 kr against the in-day adjustment over 73 days); the car's planner keeps the p75 (a car
#: short of room is worse than a dearer plan) and the in-day adjustment.
MEDIAN_QUANTILE: Final = 0.5

#: What the house figure is: the import less the chargers, or the house's whole load (see the module).
BASIS_NET: Final = "net"
BASIS_GROSS: Final = "gross"
#: The house kinds of the log: they log the house figure, whichever it is.
HOUSE_KINDS: Final = (KIND_HOUSE, KIND_HOUSE_RAW, KIND_HOUSE_DAY_AHEAD, KIND_HOUSE_MEDIAN)
PRODUCTION_OK: Final = "ok"
PRODUCTION_NONE: Final = "no_solar_statistics"
PRODUCTION_FAILED: Final = "unreadable"
#: Why the expected solar surplus has no house figure (`surplus_house_by_hour`), shown in the diagnostics.
SURPLUS_OK: Final = "ok"
SURPLUS_NO_FORECAST: Final = "no_house_forecast"
SURPLUS_NO_SOLAR_STATISTICS: Final = "no_solar_statistics"
SURPLUS_COLLECTING: Final = "whole_load_collecting"
SURPLUS_WITHOUT_SUN: Final = "whole_load_without_sun"

WEATHER_NONE: Final = "no_weather_entity"
WEATHER_OK: Final = "ok"
WEATHER_NO_FORECAST: Final = "no_hourly_forecast"
HOLIDAYS_NONE: Final = "weekday_weekend_only"
OUTDOOR_SENSOR: Final = "sensor"
OUTDOOR_WEATHER: Final = "weather"

#: A heat pump integration's outdoor temperature sensor, by the key at the end of its unique id: NIBE's BT1 (40004
#: on the F series, VVM and SMO, 30002 on the S series, `nibe_heatpump` `<entry>-<coil>`; myUplink's parameter
#: 40004, `<device>-<parameter>`) and MELCloud's `<serial>-<mac>-outside_temperature` (read 2026-11-13).
OUTDOOR_SENSORS: Final[dict[str, re.Pattern[str]]] = {
    "nibe_heatpump": re.compile(r"-(?:40004|30002)$"),
    "myuplink": re.compile(r"-40004$"),
    "melcloud": re.compile(r"-outside_temperature$"),
}


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.load_forecast.{entry_id}"


def temperature_store_key(entry_id: str) -> str:
    return f"{DOMAIN}.temperature_history.{entry_id}"


def house_basis(*, battery: bool, solar: bool, gross_ready: bool) -> str:
    """What the house figure is (see the module): on a battery site always the whole load, since the import less the
    chargers there counts the battery (0 while it covers the house, the fuse's worth while it grid-charges: the
    owner's read 13.7 kW at 14:00 on 13 Nov with the house at 1.2 kW); on a solar site without a battery the whole
    load once it has `MIN_GROSS_HOURS` hours (until then the import less the chargers is the house less the sun, too
    low at noon but never the battery); else the import less the chargers."""
    if battery:
        return BASIS_GROSS
    return BASIS_GROSS if solar and gross_ready else BASIS_NET


def house_forecasts(
    house: list[Any],
    net: list[Any] | None,
    now: datetime,
    tz: str,
    *,
    house_flat_kw: float | None,
    net_flat_kw: float | None,
    temperatures: Mapping[datetime, float],
    holidays: frozenset[date] | None,
) -> tuple[LoadForecast, LoadForecast, LoadForecast | None]:
    """The executor's part of a refresh: the house figure (p75 with the in-day adjustment), its median without it (the
    battery plan's), and, when the house figure is the whole load, the import less the chargers for the import target
    (`net`, `None` otherwise)."""
    shown = forecast_house_load(house, now, tz, flat_kw=house_flat_kw, temperatures=temperatures, holidays=holidays)
    median = forecast_house_load(
        house,
        now,
        tz,
        flat_kw=house_flat_kw,
        temperatures=temperatures,
        holidays=holidays,
        inday=False,
        quantile=MEDIAN_QUANTILE,
    )
    target = None
    if net is not None:
        target = forecast_house_load(net, now, tz, flat_kw=net_flat_kw, temperatures=temperatures, holidays=holidays)
    return shown, median, target


# ------------------------------------------------------------------------------------------------ pure helpers


def heat_comparison(reviewed: Iterable[tuple[str, DayReview]]) -> list[str]:
    """The comparison lines for the days just reviewed that have the split (`house_kw_split`) beside the whole
    (`house_kw`): each forecast's mean absolute error, and the heat forecast's own."""
    by_day: dict[date, dict[str, DayReview]] = {}
    for kind, review in reviewed:
        by_day.setdefault(review.day, {})[kind] = review
    lines = []
    for day, kinds in sorted(by_day.items()):
        whole, split = kinds.get(KIND_HOUSE), kinds.get(KIND_HOUSE_SPLIT)
        if whole is None or split is None:
            continue
        heat = kinds.get(KIND_HEAT)
        lines.append(
            f"{day.isoformat()}: house forecast as heat plus the rest MAE {split.all.mae:.3f} kW over {split.all.hours} h,"
            f" whole {whole.all.mae:.3f} kW over {whole.all.hours} h"
            + ("" if heat is None else f"; heat alone MAE {heat.all.mae:.3f} kW over {heat.all.hours} h")
        )
    return lines


def log_heat_comparison(reviewed: Iterable[tuple[str, DayReview]]) -> None:
    """Write `heat_comparison`'s lines to the log (info: one line a day, where a heat meter is)."""
    for line in heat_comparison(reviewed):
        _LOGGER.info("SpotNav house forecast: %s", line)


def pick_weather_entity(entity_ids: Iterable[str]) -> str | None:
    """`weather.forecast_home` when it exists, else the only weather entity, else `None`."""
    weather = sorted({entity_id for entity_id in entity_ids if entity_id.startswith("weather.")})
    if DEFAULT_WEATHER in weather:
        return DEFAULT_WEATHER
    return weather[0] if len(weather) == 1 else None


def pick_outdoor_sensor(candidates: Iterable[tuple[str, str, str]]) -> str | None:
    """The one heat pump outdoor sensor among `(entity_id, platform, unique_id)` (`OUTDOOR_SENSORS`), `None` when
    there is none or more than one (two heat pumps, or two ways to the same one: which is meant cannot be told)."""
    found = sorted(
        {
            entity_id
            for entity_id, platform, unique_id in candidates
            if entity_id.startswith("sensor.")
            and (pattern := OUTDOOR_SENSORS.get(platform)) is not None
            and pattern.search(unique_id or "")
        }
    )
    return found[0] if len(found) == 1 else None


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
        has_battery: Callable[[], bool] = lambda: False,
        heat_setting: Callable[[], str | None] = lambda: None,
        heat_zones: Callable[[], Sequence[str]] = lambda: (),
        heat_moved: Callable[[], Sequence[str]] = lambda: (),
    ) -> None:
        self.hass = hass
        self._demand = demand
        self._flat_kw = flat_kw
        self._has_battery = has_battery
        # Only what shows the forecast listens (the site state sensor): a refresh is no new reading, so nothing
        # that decides (a charger's solar evaluation) is woken by it.
        self._listeners: set[Callable[[], None]] = set()
        self.log = ForecastLog(tz=demand.tracker.tz)
        self.history = HouseHistory()
        #: The heat pump's meter and its hours (`site/heat_meter.py`), and the house forecast split into heat and the
        #: rest where it is (`None` without a meter or its hours). Shown and logged only.
        self.heat = SiteHeatMeter(hass, heat_setting, heat_zones, heat_moved)
        self.heat_split: HeatSplit | None = None
        #: The same split on the heat pump's own meter alone (the zones left out): what the heat pump's relay or SG
        #: Ready contacts change, read by the heat shadow for the relay's effect and the tight hours. The same object
        #: as `heat_split` without zones.
        self.pump_heat_split: HeatSplit | None = None
        #: The house figure's forecast (shown, logged), its median (the battery plan's and the expected surplus's)
        #: and, on a site whose house figure is the whole load, the import less the chargers for the import target
        #: (else `None`).
        self.forecast: LoadForecast | None = None
        self.median: LoadForecast | None = None
        self.import_forecast: LoadForecast | None = None
        #: What the house figure is (`BASIS_NET`, `BASIS_GROSS`), as of the last refresh; kept so a change is seen.
        self.basis: str = BASIS_NET
        #: The Energy dashboard's solar statistics read for the whole load, and how the last read went.
        self.solar_statistics: tuple[str, ...] = ()
        self.production_status: str | None = None
        self.weather_entity_id: str | None = None
        self.weather_status: str = WEATHER_NONE
        #: The heat pump's outdoor sensor when exactly one is found (looked for at each refresh).
        self.outdoor_entity_id: str | None = None
        #: Where the last outdoor reading came from: `OUTDOOR_SENSOR`, `OUTDOOR_WEATHER` or `None` (no reading).
        self.outdoor_source: str | None = None
        self._outdoor_looked = False
        self.holiday_entity_id: str | None = None
        self.holidays: frozenset[date] | None = None
        self._holidays_day: date | None = None
        self._temperatures: dict[datetime, float] = {}
        self._for_hour: datetime | None = None
        self._retry_at: datetime | None = None
        self._running = False
        self._closed = False
        #: The refresh in flight, cancelled at the close.
        self._task: asyncio.Task[None] | None = None
        #: The heat pump relay's change to the expected load per hour (`heat/runtime.HeatShadow.forecast_delta`), set
        #: by the site once its heat shadow exists; `None` without one.
        self.heat_delta: Callable[[datetime], Mapping[datetime, float]] | None = None
        self._attribute: list[dict[str, Any]] = []
        self._rows: list[tuple[datetime, float]] = []
        self.refreshes = 0
        self._loaded = entry_id is None
        #: The outdoor temperatures kept for a year (`site/temperature_history.py`), in their own store.
        self.temperature_history = TemperatureHistory()
        #: The same-source offset added to the forecast temperatures the model is fed, as of the last refresh.
        self.offset: Offset = NO_OFFSET
        self._store: DayStore | None = None
        self._temperature_store: DayStore | None = None
        if entry_id is not None:
            self._store = DayStore(hass, store_key(entry_id), self._stored, save_delay_s=SAVE_DELAY_S)
            self._temperature_store = DayStore(
                hass,
                temperature_store_key(entry_id),
                self.temperature_history.as_dict,
                save_delay_s=TEMPERATURE_SAVE_DELAY_S,
                off_loop=True,
            )

    def _stored(self) -> dict[str, Any]:
        """What the store keeps: the log, and the forecast's own history beside it."""
        return {**self.log.as_dict(), "history": self.history.as_dict(), "basis": self.basis}

    async def async_load(self) -> None:
        if self._store is not None:
            raw = await self._store.async_load()
            try:
                self.log.restore(raw)
            except Exception:  # noqa: BLE001 - a log that cannot be read starts again
                _LOGGER.debug("SpotNav house forecast: the kept log could not be read", exc_info=True)
            if isinstance(raw, dict) and raw.get("basis") in (BASIS_NET, BASIS_GROSS):
                self.basis = raw["basis"]
            try:
                self.history.restore(raw.get("history") if isinstance(raw, dict) else None)
            except Exception:  # noqa: BLE001 - a history that cannot be read is taken from the tracker again
                _LOGGER.debug("SpotNav house forecast: the kept history could not be read", exc_info=True)
        if self._temperature_store is not None:
            raw = await self._temperature_store.async_load()
            try:
                self.temperature_history.restore(raw, dt_util.utcnow())
            except Exception:  # noqa: BLE001 - temperatures that cannot be read start again
                self.temperature_history = TemperatureHistory()
                _LOGGER.debug("SpotNav house forecast: the kept temperatures could not be read", exc_info=True)
        # The heat meter at once (registries and the Energy dashboard, no statistics read): the house card's flow and
        # the heat's readers take `heat.meter` as found, and a read before the first refresh would otherwise count
        # the heat in the house (and keep that answer until the next 5-minute row).
        try:
            await self.heat.async_detect()
        except Exception:  # noqa: BLE001 - found by the first refresh instead
            _LOGGER.debug("SpotNav house forecast: the heat meter could not be found at load", exc_info=True)
        self._loaded = True

    def log_changed(self) -> None:
        """Another part logged into the site's forecast log (`log`, shared): save it with the next write."""
        if self._store is not None:
            self._store.changed()

    @callback
    def halt(self) -> None:
        """The site shuts down: no refresh from here (the stores are closed by `async_close`)."""
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def async_close(self) -> None:
        self.halt()
        if self._store is not None:
            await self._store.async_close()
        if self._temperature_store is not None:
            await self._temperature_store.async_close()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The site is gone for good: forget its log and its kept temperatures."""
        await DayStore.async_remove(hass, store_key(entry_id))
        await DayStore.async_remove(hass, temperature_store_key(entry_id))

    # ---- sources

    def _weather_entity(self) -> str | None:
        return pick_weather_entity(self.hass.states.async_entity_ids("weather"))

    def _outdoor_sensor(self) -> str | None:
        registry = er.async_get(self.hass)
        return pick_outdoor_sensor(
            (entry.entity_id, entry.platform, entry.unique_id)
            for entry in registry.entities.values()
            if entry.platform in OUTDOOR_SENSORS and entry.disabled_by is None
        )

    def _look_for_outdoor_sensor(self) -> None:
        self.outdoor_entity_id = self._outdoor_sensor()
        self._outdoor_looked = True

    def outdoor_c(self) -> float | None:
        """The outdoor temperature now (°C): the heat pump's outdoor sensor when one was found and reads, else the
        weather entity's; `None` without either."""
        if not self._outdoor_looked:
            self._look_for_outdoor_sensor()
        if self.outdoor_entity_id is not None:
            state = self.hass.states.get(self.outdoor_entity_id)
            celsius = None if state is None else to_celsius(state.state, state.attributes.get("unit_of_measurement"))
            if celsius is not None:
                self.outdoor_source = OUTDOOR_SENSOR
                return celsius
        entity_id = self._weather_entity()
        state = None if entity_id is None else self.hass.states.get(entity_id)
        celsius = None
        if state is not None:
            celsius = to_celsius(state.attributes.get("temperature"), state.attributes.get("temperature_unit"))
        self.outdoor_source = None if celsius is None else OUTDOOR_WEATHER
        return celsius

    def history_marker(self) -> str | None:
        """Where the outdoor temperature now is read from, as a source marker (`sensor:<id>`, `weather:<id>`), as of
        the last `outdoor_c`; `None` without a reading."""
        if self.outdoor_source == OUTDOOR_SENSOR:
            return source_marker(SOURCE_SENSOR, self.outdoor_entity_id)
        if self.outdoor_source == OUTDOOR_WEATHER:
            return source_marker(SOURCE_WEATHER, self._weather_entity())
        return None

    def forecast_marker(self) -> str | None:
        """The weather entity the forecast temperatures come from, as a source marker; `None` without one."""
        return source_marker(SOURCE_WEATHER, self.weather_entity_id)

    def _holiday_entity(self) -> str | None:
        return pick_holiday_calendar(self.holiday_candidates(), self.hass.config.country)

    def holiday_candidates(self) -> dict[str, str | None]:
        """The Holiday integration's enabled calendars, `{entity_id: its country}`."""
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
        return candidates

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
            async with asyncio.timeout(READ_TIMEOUT_S):
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
            async with asyncio.timeout(READ_TIMEOUT_S):
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
        # Short (a cached forecast, a calendar read and a few milliseconds in the executor): a tracked task.
        self._task = self.hass.async_create_task(self.async_refresh(), "spotnav_house_forecast", eager_start=False)

    async def async_refresh(self) -> None:
        """Read the sources, run the model in the executor, log the forecast and the hours' outcomes, review the
        days that ended. Never raises."""
        self._running = True
        try:
            now = self._demand.now()
            tracker = self._demand.tracker
            tz = tracker.tz
            self._look_for_outdoor_sensor()
            complete = await self._async_temperatures(tz)
            complete &= await self._async_holidays(now, tz)
            self._retry_at = None if complete else now + timedelta(seconds=RETRY_S)
            # The tracker's two months merged into the forecast's own eight weeks (`site/house_history.py`).
            tracked = (*tracker.previous, *tracker.hours)
            self.history.merge(tracked, now)
            battery = self._has_battery()
            solar = await self._async_has_solar_statistics()
            whole = battery or solar
            if whole:
                await self._async_merge_gross(tracked, now, sun_needed=not battery)
            basis = house_basis(battery=battery, solar=solar, gross_ready=self.history.gross_ready(now))
            net_hours = self.history.hours()
            hours = self.history.gross_hours() if basis == BASIS_GROSS else net_hours
            running_outdoor = self.outdoor_c()
            current = hour_start(now, tz)
            self._keep_temperatures(tracked, now, current, tz)
            self.offset = self._learn_offset(now)
            # The weather entity's forecast on the history's scale; the running hour's own reading is on it already.
            temperatures = self.offset.apply(self._temperatures)
            if running_outdoor is not None:
                temperatures.setdefault(current, running_outdoor)
            net_flat = self._flat_kw()
            forecast, median, import_forecast = await self.hass.async_add_executor_job(
                partial(
                    house_forecasts,
                    hours,
                    net_hours if basis == BASIS_GROSS else None,
                    now,
                    tz,
                    house_flat_kw=self.history.gross_flat_kw() if basis == BASIS_GROSS else net_flat,
                    net_flat_kw=net_flat,
                    temperatures=temperatures,
                    holidays=self.holidays,
                )
            )
            if self._closed:
                return
            if basis != self.basis:
                # The house kinds change what they measure: no day is scored half on one figure, half on the other.
                for kind in (*HOUSE_KINDS, KIND_HOUSE_SPLIT):
                    self.log.drop_unreviewed(kind)
                _LOGGER.info("SpotNav house forecast: the house figure is now the %s load", basis)
                self.basis = basis
            self.forecast, self.median, self.import_forecast = forecast, median, import_forecast
            # Hours without a figure are left out: a site that knows nothing yet shows an empty list.
            self._rows = [
                (hour.start, hour.kw) for hour in forecast.hours[:ATTRIBUTE_HOURS] if hour.kw is not None
            ]
            self._attribute = [{"start": start.isoformat(), "kw": round(kw, 2)} for start, kw in self._rows]
            if import_forecast is not None:
                self._log_import(import_forecast, net_hours, now)
            await self._async_heat(hours, now, tz, temperatures)
            self._log(forecast, median, hours, now, tz)
            self.refreshes += 1
            for listener in list(self._listeners):
                listener()
        except Exception:  # noqa: BLE001 - a forecast must never disturb the site
            _LOGGER.debug("SpotNav house forecast: the refresh failed", exc_info=True)
        finally:
            self._running = False

    def _keep_temperatures(self, tracked: Iterable[Any], now: datetime, current: datetime, tz: str) -> None:
        """Keep the completed hours' temperatures, their source, and the forecast just fetched (see the module); the
        store is told only when something changed. Never raises: the forecast goes on without them."""
        try:
            kept = self.temperature_history
            changed = kept.merge_measured(tracked, now)
            changed |= kept.mark_source(self.history_marker(), current)
            if self.weather_status == WEATHER_OK:
                # Fetched by this refresh: a failed fetch keeps the last forecast, which is no new issue.
                changed |= kept.record_forecast(self._temperatures, self.forecast_marker(), now, tz)
            if changed and self._temperature_store is not None:
                self._temperature_store.changed()
        except Exception:  # noqa: BLE001 - kept for a later model only
            _LOGGER.debug("SpotNav house forecast: the temperatures could not be kept", exc_info=True)

    def _learn_offset(self, now: datetime) -> Offset:
        """The same-source offset (see the module); none when it cannot be learned."""
        try:
            return self.temperature_history.learn_offset(now, self.history_marker(), self.forecast_marker())
        except Exception:  # noqa: BLE001 - the forecast goes on with the temperatures as fetched
            _LOGGER.debug("SpotNav house forecast: the temperature offset could not be learned", exc_info=True)
            return NO_OFFSET

    async def _async_heat(
        self, hours: list[Any], now: datetime, tz: str, temperatures: Mapping[datetime, float]
    ) -> None:
        """Read the heat meter's hours, split the house forecast into heat and the rest, and log both (see the
        module). Never raises: without a meter or its hours there is no split."""
        try:
            if await self.heat.async_refresh(now):
                for kind in (KIND_HEAT, KIND_HOUSE_SPLIT):
                    self.log.drop_unreviewed(kind)
            # The house card's heat: the meter and the chosen zones together (`site/heat_zones.py`).
            heat = self.heat.combined_by_hour()
            split = None
            if heat:
                split = await self.hass.async_add_executor_job(
                    partial(heat_split, hours, heat, now, tz, temperatures=temperatures, holidays=self.holidays)
                )
            # The pump alone: the relay and SG Ready's contacts change the pump, never the zones.
            pump = dict(self.heat.by_hour) if self.heat.meter.found else {}
            pump_split = None
            if pump and pump == heat:
                pump_split = split
            elif pump:
                pump_split = await self.hass.async_add_executor_job(
                    partial(heat_split, hours, pump, now, tz, temperatures=temperatures, holidays=self.holidays)
                )
            if self._closed:
                return
            self.heat_split = split
            self.pump_heat_split = pump_split
            if split is None:
                return
            oldest = now - timedelta(days=KEEP_HOURS_DAYS)
            for start, kw in heat.items():
                if start >= oldest:
                    self.log.record_actual(KIND_HEAT, start, kw)
            for hour in hours:
                if hour.start >= oldest and not hour.imported and hour.house_kw is not None:
                    self.log.record_actual(KIND_HOUSE_SPLIT, hour.start, hour.house_kw)
            self.log.record_forecast(KIND_HEAT, split.heat.by_hour(), now)
            self.log.record_forecast(KIND_HOUSE_SPLIT, split.by_hour(), now)
        except Exception:  # noqa: BLE001 - heat is shown only: the house forecast goes on without it
            _LOGGER.debug("SpotNav house forecast: the heat split failed", exc_info=True)

    def request_refresh(self) -> None:
        """Refresh at the next reading, not at the next hour (a setting the forecast reads was changed)."""
        self._for_hour = None
        self.tick()

    async def _async_production(self, first: datetime, now: datetime) -> tuple[bool, dict[datetime, float] | None]:
        """The Energy dashboard's solar production per completed hour from `first` on (kWh, UTC hour starts):
        `(True, None)` without solar statistics, `(False, None)` when they cannot be read (the hours wait)."""
        current = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        try:
            ids, _export = await async_energy_ids(self.hass)
            self.solar_statistics = ids
            if not ids:
                self.production_status = PRODUCTION_NONE
                return True, None
            production = summed(await async_statistics(self.hass, ids, first, current, "hour"), ids)
        except Exception:  # noqa: BLE001 - the hours wait for the next refresh
            _LOGGER.debug("SpotNav house forecast: the solar statistics could not be read", exc_info=True)
            self.production_status = PRODUCTION_FAILED
            return False, None
        self.production_status = PRODUCTION_OK
        return True, production

    async def _async_has_solar_statistics(self) -> bool:
        """Whether the Energy dashboard has solar production statistics (a site without a battery has a whole load
        only with them); when they cannot be read, what the last read found."""
        try:
            ids, _export = await async_energy_ids(self.hass)
        except Exception:  # noqa: BLE001 - the last read stands until the next refresh
            _LOGGER.debug("SpotNav house forecast: the Energy dashboard could not be read", exc_info=True)
            return bool(self.solar_statistics)
        self.solar_statistics = ids
        if not ids:
            self.production_status = PRODUCTION_NONE
        return bool(ids)

    async def _async_merge_gross(self, tracked: Iterable[Any], now: datetime, *, sun_needed: bool = False) -> None:
        """File the tracker's hours of the whole load that wait for their production (`HouseHistory.merge_gross`);
        with `sun_needed` (a site without a battery) never without it."""
        tracked = list(tracked)
        waiting = self.history.waiting(tracked, now)
        if not waiting:
            return
        read, production = await self._async_production(waiting[0].start.astimezone(timezone.utc), now)
        if read and (production is not None or not sun_needed):
            self.history.merge_gross(tracked, production, now)

    async def async_take_rows(self, rows: list[tuple[datetime, float]], now: datetime) -> int:
        """Take the battery plan's own rows (`grid - car - battery` per hour) over as the whole load, once
        (`HouseHistory.take_rows`); the next reading refreshes the forecast on them. How many were taken; none when
        they were taken before or the statistics cannot be read now (then it is tried again)."""
        if self.history.gross_migrated_at is not None or not self._loaded:
            return 0
        first = min((start for start, _kw in rows), default=None)
        production: dict[datetime, float] | None = None
        if first is not None:
            read, production = await self._async_production(first.astimezone(timezone.utc), now)
            if not read:
                return 0
        taken = self.history.take_rows(rows, production, now)
        _LOGGER.info("SpotNav house forecast: %d hours of the battery plan's own rows taken over", taken)
        self._for_hour = None
        if self._store is not None:
            self._store.changed()
        return taken

    def _log_import(self, forecast: LoadForecast, hours: Iterable[Any], now: datetime) -> None:
        """Log the import target's forecast (the import less the chargers) beside the house kinds."""
        oldest = now - timedelta(days=KEEP_HOURS_DAYS)
        for hour in hours:
            if hour.start >= oldest and not hour.imported and hour.house_kw is not None:
                self.log.record_actual(KIND_IMPORT, hour.start, hour.house_kw)
        self.log.record_forecast(KIND_IMPORT, forecast.by_hour(), now)

    def _log(
        self, forecast: LoadForecast, median: LoadForecast | None, hours: Iterable[Any], now: datetime, tz: str
    ) -> None:
        log = self.log
        log.tz = tz
        oldest = now - timedelta(days=KEEP_HOURS_DAYS)
        for hour in hours:
            if hour.start >= oldest and not hour.imported and hour.house_kw is not None:
                for kind in HOUSE_KINDS:
                    log.record_actual(kind, hour.start, hour.house_kw)
        log.record_forecast(KIND_HOUSE, forecast.by_hour(), now)
        log.record_forecast(KIND_HOUSE_RAW, forecast.raw_by_hour(), now)
        if median is not None:
            # The battery plan's figure: its misses are the error band (`site/forecast_band.py`).
            log.record_forecast(KIND_HOUSE_MEDIAN, median.by_hour(), now)
        # The next day as forecast once its prices are out: what that day's plans count on.
        next_day = next_day_hours(forecast.by_hour(), now, tz)
        if next_day:
            log.record_forecast(KIND_HOUSE_DAY_AHEAD, next_day, now)
        log_heat_comparison(log.review(now))
        if self._store is not None:
            self._store.changed()

    # ---- what plans count on

    def _fresh(self, now: datetime) -> LoadForecast | None:
        """The import target's forecast (`import_forecast` on a site whose house figure is the whole load, else the
        house figure's), when made in the last `PLAN_MAX_AGE_S`."""
        forecast = self.forecast if self.import_forecast is None else self.import_forecast
        if forecast is None or (now - forecast.made_at).total_seconds() > PLAN_MAX_AGE_S:
            return None
        return forecast

    def _with_heat(self, by_hour: dict[datetime, float], now: datetime) -> dict[datetime, float]:
        """`by_hour` with the heat pump relay's change (`heat_delta`) on the hours it has, never below 0 kW."""
        if self.heat_delta is None or not by_hour:
            return by_hour
        try:
            delta = self.heat_delta(now)
        except Exception:  # noqa: BLE001 - the relay's change is extra: the forecast goes on without it
            _LOGGER.debug("SpotNav house forecast: the heat relay's change could not be read", exc_info=True)
            return by_hour
        return {start: max(0.0, kw + delta.get(start, 0.0)) for start, kw in by_hour.items()}

    def plan_kw_by_hour(self, now: datetime, *, heat: bool = True) -> dict[datetime, float]:
        """The import less the chargers expected per hour from the running hour on (`{hour_start (UTC): kW}`): the
        shown forecast on a site whose house figure is the net one, `import_forecast` where it is the whole load (see
        the module); the hours the profile
        knows only (`LoadForecast.known_by_hour`); empty without a forecast made in the last `PLAN_MAX_AGE_S`. An hour
        left out is reckoned at the site's flat figure, as before the forecast. With `heat` the heat pump relay's
        lowered hours and rebound are counted (`heat_delta`)."""
        forecast = self._fresh(now)
        if forecast is None:
            return {}
        current = hour_start(now, self._demand.tracker.tz)
        by_hour = {start: kw for start, kw in forecast.known_by_hour().items() if start >= current}
        return self._with_heat(by_hour, now) if heat else by_hour

    def plan_kw_at(self, now: datetime) -> float | None:
        """The import less the chargers expected in the running hour, `None` when the profile does not know it (see
        `plan_kw_by_hour`)."""
        return self.plan_kw_by_hour(now).get(hour_start(now, self._demand.tracker.tz))

    def battery_house_by_hour(self) -> tuple[dict[datetime, float], str, bool]:
        """The battery plan's house: the house figure's median per hour without the in-day adjustment (the house
        figure's own forecast until a median is made), with what the figure is (`basis`) and whether the sun's
        production is in its hours (on the whole load: every hour had it added). Empty without a forecast."""
        forecast = self.median if self.median is not None else self.forecast
        by_hour = {} if forecast is None else self._with_heat(forecast.by_hour(), dt_util.utcnow())
        return by_hour, self.basis, self.basis == BASIS_GROSS and self.history.gross_with_sun()

    def surplus_house_by_hour(self, now: datetime) -> tuple[dict[datetime, float], str]:
        """The expected solar surplus's house: the whole load's median per hour without the in-day adjustment, from
        the running hour on, the hours the profile knows only, with `SURPLUS_OK`; empty with the reason when there is
        no whole load with the sun in every hour (the import less the chargers has the sun taken off already) or no
        median made in the last `PLAN_MAX_AGE_S`."""
        median = self.median
        if median is None or (now - median.made_at).total_seconds() > PLAN_MAX_AGE_S:
            return {}, SURPLUS_NO_FORECAST
        if self.basis != BASIS_GROSS:
            return {}, SURPLUS_COLLECTING if self.solar_statistics else SURPLUS_NO_SOLAR_STATISTICS
        if not self.history.gross_with_sun():
            return {}, SURPLUS_WITHOUT_SUN if self.solar_statistics else SURPLUS_NO_SOLAR_STATISTICS
        current = hour_start(now, self._demand.tracker.tz)
        by_hour = {start: kw for start, kw in median.known_by_hour().items() if start >= current}
        return self._with_heat(by_hour, now), SURPLUS_OK

    def temperatures(self) -> dict[datetime, float]:
        """The weather's hourly outdoor forecast (°C, UTC hour starts) as last read."""
        return dict(self._temperatures)

    # ---- what is shown

    def band_view(self, now: datetime) -> dict[str, Any]:
        """The median's learnt error band over its hours from the running one on, from the log's misses."""
        band = learn_band(
            pairs_from_log(self.log.hours(KIND_HOUSE_MEDIAN)), now, self.log.tz, FORM_ADDITIVE, holidays=self.holidays
        )
        current = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        hours = {} if self.median is None else self.median.by_hour()
        return band_view(band, {start: kw for start, kw in hours.items() if start >= current}).as_dict()

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` after each refresh; returns its removal."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @property
    def attribute(self) -> list[dict[str, Any]]:
        """The site state sensor's `forecast`: the next `ATTRIBUTE_HOURS` hours that have a figure, `[{start, kw}]`."""
        return self._attribute

    @property
    def rows(self) -> list[tuple[datetime, float]]:
        """What `attribute` shows as `(hour start, kW)`, for the House load forecast sensor."""
        return self._rows

    def diagnostics(self) -> dict[str, Any]:
        """The sources, the last forecast and the log with its reviews."""
        return {
            "weather_entity": self.weather_entity_id,
            "weather": self.weather_status,
            # The outdoor temperature now, recorded per hour: from the heat pump's sensor or the weather entity.
            "outdoor": {"source": self.outdoor_source, "sensor": self.outdoor_entity_id},
            "forecast_temperatures": len(self._temperatures),
            # The temperatures kept for a year: how many hours, from when, and from which sources.
            # ...and the offset the forecast temperatures are moved by (the same-source offset).
            "temperature_history": self.temperature_history.diagnostics(self.offset),
            "holidays": self.holiday_entity_id or HOLIDAYS_NONE,
            "holiday_dates": None if self.holidays is None else sorted(day.isoformat() for day in self.holidays),
            "refreshes": self.refreshes,
            "retry_at": None if self._retry_at is None else self._retry_at.isoformat(),
            # What the house figure is (`net`: the import less the chargers; `gross`: the house's whole load).
            "basis": self.basis,
            "battery": self._has_battery(),
            "solar_statistics": list(self.solar_statistics),
            "production": self.production_status,
            "forecast": None if self.forecast is None else self.forecast.as_dict(),
            # The battery plan's: the median without the in-day adjustment.
            "median": None if self.median is None else self.median.as_dict(),
            # Its learnt error band (`site/forecast_band.py`): per hour [p10, median, p90].
            "band": self.band_view(dt_util.utcnow()),
            # On a site whose house figure is the whole load: the import target's (the net figure).
            "import_forecast": None if self.import_forecast is None else self.import_forecast.as_dict(),
            "history": self.history.diagnostics(),
            # The heat pump's meter and the house forecast split into heat and the rest (shown and logged only).
            "heat": {
                "meter": self.heat.diagnostics(),
                "forecast": None if self.heat_split is None else self.heat_split.heat.as_dict(),
                "rest": None if self.heat_split is None else self.heat_split.rest.as_dict(),
            },
            "log": self.log.diagnostics(),
        }
