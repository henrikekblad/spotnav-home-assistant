"""The heat pump's electricity meter (SpotNav 3.0 heat, step 1): which sensor it is, and its use per hour. Seen and
shown only: nothing here controls the heat pump or anything else.

**Which sensor** (fewest settings: found without asking where it can be told, `pick_heat_meter`):

1. **The site's setting** ("Heat pump meter", `CONF_HEAT_METER_ENTITY`): a power or an energy sensor the user chose.
   Offered in the card's site settings only while 2. and 3. find none or more than one (`needs_setting`), or once set.
2. **The Energy dashboard's individual devices** (`energy/get_prefs` `device_consumption`): a device whose statistic
   belongs to a heat pump integration (`HEAT_PUMP_PLATFORMS`), or whose name says heating (`HEAT_WORDS`: the name the
   user gave it there, else its entity's or its device's). Its power sensor (`stat_rate`) is read when it has one,
   else its energy statistic.
3. **A heat pump integration's own meter of the whole pump** (`INTEGRATION_METERS`, by the key in the unique id).
   Few have one: NIBE's (`nibe_heatpump`, `myuplink`) give the compressor's input and the immersion heater apart on
   the S series and neither on the F series, so a NIBE pump is found through 1. or 2.

Exactly one candidate in the first tier that has any is the meter; two or more is `ambiguous` (the setting is then
offered: which is meant cannot be told). A sensor found by its name alone, outside the Energy dashboard, is not
counted: a device called "NIBE" also holds the pump's partial sensors, and a meter's clamps are named like its total.

**Its use per hour** (`async_heat_hours`): from the recorder's hourly statistics, as the house card's other series:
a power sensor's mean (kW), an energy sensor's change (kWh in the hour, the same figure). Kept per site in memory
for the forecast's window (`SiteHeatMeter`) and read again from the last few hours on at each refresh (an hour's
statistics are compiled a few minutes after it).

**The heat zones** (`site/heat_zones.py`): the zones chosen in the heat settings with a sensor of their own on their
device (electric floor heating, an air-to-air heat pump) are held beside the meter (`SiteHeatMeter.zones`, their
hours `zone_by_hour`), each counted once. The house card's heat is the meter and the zones together
(`combined_by_hour`, `async_heat_periods`); the heat card's control, its pump state and the day record read the meter
alone (`by_hour`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import logging
import re
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..core.parse import is_number
from .energy_statistics import async_energy_prefs, summed
from .load_forecast import WINDOW_DAYS, LoadForecast, forecast_house_load

_LOGGER = logging.getLogger(__name__)

KIND_POWER: Final = "power"
KIND_ENERGY: Final = "energy"

SOURCE_SETTING: Final = "setting"
SOURCE_ENERGY_DASHBOARD: Final = "energy_dashboard"
SOURCE_INTEGRATION: Final = "integration"

STATUS_FOUND: Final = "found"
STATUS_NONE: Final = "none"
STATUS_AMBIGUOUS: Final = "ambiguous"
#: The chosen sensor is neither a power nor an energy sensor (or is gone): nothing is read until it is.
STATUS_UNUSABLE: Final = "unusable"

#: Heat pump integrations (their platform names in the entity registry): a statistic of one of their entities on the
#: Energy dashboard is the heat pump's (survey `plans/research_integrations/heat_pump_survey_2026-11-15.md`).
HEAT_PUMP_PLATFORMS: Final = frozenset(
    {
        "nibe_heatpump",
        "myuplink",
        "nibe",
        "melcloud",
        "melcloudhome",
        "luxtronik",
        "luxtronik2",
        "stiebel_eltron",
        "stiebel_eltron_isg",
        "daikin_onecta",
        "daikin_altherma",
        "mypyllant",
        "thermiagenesis",
        "thermia",
        "qvantum",
        "ctc_heat_pump",
        "hitachi_yutaki",
        "panasonic_cc",
        "aquarea",
        "heishamon",
        "ecodan",
        "bosch",
        "ngenic",
        "ebusd",
    }
)
#: A heat pump integration's own sensor of the whole pump's electrical input, by the key at the end of its unique
#: id: Luxtronik's current power consumption (`luxtronik2`, the unique id is its entity id, the key last).
INTEGRATION_METERS: Final[dict[str, re.Pattern[str]]] = {
    "luxtronik2": re.compile(r"current_power_consumption$"),
}
#: A name that says heating, in the card's languages (an Energy dashboard device's name only): a heat pump anywhere in
#: a word ("Poolvärmepump"), heating as a word of its own ("Värme", not "Golvvärme", a floor heater), or NIBE.
HEAT_WORDS: Final = re.compile(
    r"heat ?pump|v[äa]rmepump|varmepumpe|w[äa]rmepumpe|l[äa]mp[öo]pumppu|warmtepomp|pompe [àa] chaleur|"
    r"bomba de calor|\b(?:v[äa]rme|heating|heizung|l[äa]mmitys|verwarming|chauffage|calefacci[óo]n|nibe)\b",
    re.IGNORECASE,
)
#: Units each kind reads (an entity without a device class is told by its unit).
POWER_UNITS: Final = frozenset({"W", "kW", "MW"})
ENERGY_UNITS: Final = frozenset({"Wh", "kWh", "MWh"})
#: The hours read again at each refresh (the last hour's statistics are compiled a few minutes after it).
REREAD_HOURS: Final = 3
#: Hourly figures above this (kW) are a sensor's glitch, not a heat pump.
MAX_KW: Final = 100.0


def meter_kind(device_class: Any, unit: Any) -> str | None:
    """`KIND_POWER` or `KIND_ENERGY` from a sensor's device class, else its unit; `None` for anything else."""
    if device_class == KIND_POWER or (device_class is None and unit in POWER_UNITS):
        return KIND_POWER
    if device_class == KIND_ENERGY or (device_class is None and unit in ENERGY_UNITS):
        return KIND_ENERGY
    return None


@dataclass(frozen=True, slots=True)
class DashboardDevice:
    """One of the Energy dashboard's individual devices: its energy statistic, its power sensor (`None` without),
    the names it goes by (the dashboard's, its entity's, its device's) and the platforms of its entities."""

    stat_consumption: str
    stat_rate: str | None = None
    names: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()

    @property
    def heating(self) -> bool:
        return any(platform in HEAT_PUMP_PLATFORMS for platform in self.platforms) or any(
            HEAT_WORDS.search(name) for name in self.names if name
        )


@dataclass(frozen=True, slots=True)
class HeatMeter:
    """The meter as found: `status`, the sensor (or statistic) read and its kind, where it came from, and on an
    ambiguous find the candidates."""

    status: str
    entity_id: str | None = None
    kind: str | None = None
    source: str | None = None
    candidates: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return self.status == STATUS_FOUND and self.entity_id is not None and self.kind is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "entity_id": self.entity_id,
            "kind": self.kind,
            "source": self.source,
            "candidates": list(self.candidates),
        }


NO_METER: Final = HeatMeter(STATUS_NONE)


def dashboard_candidates(devices: Iterable[DashboardDevice]) -> list[tuple[str, str]]:
    """The Energy dashboard's heating devices as `(statistic or entity read, kind)`: the power sensor where the
    device has one, else its energy statistic."""
    found: dict[str, str] = {}
    for device in devices:
        if not device.heating:
            continue
        if device.stat_rate:
            found.setdefault(device.stat_rate, KIND_POWER)
        else:
            found.setdefault(device.stat_consumption, KIND_ENERGY)
    return sorted(found.items())


def integration_candidates(entities: Iterable[tuple[str, str, str, str | None, Any]]) -> list[tuple[str, str]]:
    """A heat pump integration's own whole-pump meters among `(entity_id, platform, unique_id, device_class, unit)`
    (`INTEGRATION_METERS`), as `(entity_id, kind)`."""
    found: dict[str, str] = {}
    for entity_id, platform, unique_id, device_class, unit in entities:
        pattern = INTEGRATION_METERS.get(platform)
        if pattern is None or not entity_id.startswith("sensor.") or not pattern.search(unique_id or ""):
            continue
        kind = meter_kind(device_class, unit)
        if kind is not None:
            found[entity_id] = kind
    return sorted(found.items())


def pick_heat_meter(
    setting: str | None,
    setting_kind: str | None,
    dashboard: Sequence[tuple[str, str]],
    integration: Sequence[tuple[str, str]],
) -> HeatMeter:
    """The meter (see the module): the setting, else the one Energy dashboard heating device, else the one
    integration meter; more than one in the first tier that has any is `ambiguous`."""
    if setting:
        if setting_kind is None:
            return HeatMeter(STATUS_UNUSABLE, setting, None, SOURCE_SETTING)
        return HeatMeter(STATUS_FOUND, setting, setting_kind, SOURCE_SETTING)
    for source, candidates in ((SOURCE_ENERGY_DASHBOARD, dashboard), (SOURCE_INTEGRATION, integration)):
        if len(candidates) == 1:
            entity_id, kind = candidates[0]
            return HeatMeter(STATUS_FOUND, entity_id, kind, source)
        if candidates:
            return HeatMeter(STATUS_AMBIGUOUS, source=source, candidates=tuple(entity_id for entity_id, _ in candidates))
    return NO_METER


def needs_setting(found_without: HeatMeter) -> bool:
    """Whether the site setting is offered: the meter found without it is not exactly one."""
    return found_without.status != STATUS_FOUND


def _row_start(row: Mapping[str, Any]) -> datetime | None:
    start = row.get("start")
    if isinstance(start, datetime):
        return start.astimezone(timezone.utc)
    if is_number(start):
        # The recorder's own rows carry seconds; the WebSocket's carry milliseconds.
        seconds = float(start) / 1000.0 if float(start) > 1e11 else float(start)
        return datetime.fromtimestamp(seconds, timezone.utc)
    return None


def hourly_kw(rows: Sequence[Mapping[str, Any]], kind: str) -> dict[datetime, float]:
    """The statistics' hourly figure as kW per UTC hour start: a power sensor's `mean` (kW), an energy sensor's
    `change` (kWh in the hour). Negative figures count as 0; non-numbers and glitches above `MAX_KW` are left out."""
    return period_kwh(rows, kind, 1.0)


def period_kwh(rows: Sequence[Mapping[str, Any]], kind: str, hours: float) -> dict[datetime, float]:
    """The statistics' figure as kWh per period start, `hours` long (1 for the hourly rows, 1/12 for the 5-minute
    ones): a power sensor's `mean` (kW) times the period, an energy sensor's `change`. Negative figures count as 0;
    non-numbers and glitches above `MAX_KW` (as a mean over the period) are left out."""
    if kind == KIND_ENERGY:
        return {start: kwh for start, kwh in summed({"meter": list(rows)}, ("meter",)).items() if kwh <= MAX_KW * hours}
    out: dict[datetime, float] = {}
    for row in rows:
        start, mean = _row_start(row), row.get("mean")
        if start is None or not is_number(mean) or mean > MAX_KW:
            continue
        out[start] = max(0.0, float(mean)) * hours
    return out


#: The recorder's statistics periods the heat meter is read by, and each one's length in hours.
PERIOD_HOURS: Final = {"hour": 1.0, "5minute": 5.0 / 60.0}


async def async_heat_hours(
    hass: HomeAssistant, meter: HeatMeter, start: datetime, end: datetime, period: str = "hour"
) -> dict[datetime, float]:
    """The meter's energy per `period` in `[start, end)` (kWh, UTC period starts; per hour that is the mean kW) from
    the recorder's statistics (`"hour"`, or `"5minute"` within its short-term window), in its executor; empty
    without a meter."""
    if not meter.found or meter.entity_id is None or end <= start:
        return {}
    if "recorder" not in hass.config.components:
        return {}
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415 - only when loaded
    from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

    entity_id, kind = meter.entity_id, meter.kind or KIND_POWER
    units = {"power": "kW"} if kind == KIND_POWER else {"energy": "kWh"}
    types = {"mean"} if kind == KIND_POWER else {"change"}

    def read() -> list[dict[str, Any]]:
        rows = statistics_during_period(hass, start, end, {entity_id}, period, units, types)
        return list(rows.get(entity_id) or [])

    return period_kwh(await get_instance(hass).async_add_executor_job(read), kind, PERIOD_HOURS[period])


# ------------------------------------------------------------------------------------------------ the forecast split


@dataclass(frozen=True, slots=True)
class HeatSplit:
    """The house forecast split where a heat meter is: heat (learned against the outdoor temperature by the house
    forecast's own model, `site/load_forecast.py`) and the rest (the house less heat, as the house forecast), each
    with the same quantile and in-day adjustment as the shown house forecast. Shown and logged only: plans keep the
    whole load's forecast."""

    heat: LoadForecast
    rest: LoadForecast

    def by_hour(self) -> dict[datetime, float]:
        """Heat plus the rest per hour, the hours both know (neither from the flat rung nor unknown)."""
        heat, rest = self.heat.known_by_hour(), self.rest.known_by_hour()
        return {start: heat[start] + rest[start] for start in heat if start in rest}


def split_rows(house: Sequence[Any], heat: Mapping[datetime, float]) -> tuple[list[Any], list[Any]]:
    """The house's hours with a heat figure, as heat rows and rest rows (the house less heat, never below 0), each
    keeping its outdoor temperature."""
    heat_rows: list[Any] = []
    rest_rows: list[Any] = []
    for row in house:
        kw = heat.get(row.start)
        if kw is None or row.house_kw is None:
            continue
        heat_rows.append(replace(row, kw=kw, house_kw=kw))
        rest = max(0.0, row.house_kw - kw)
        rest_rows.append(replace(row, kw=rest, house_kw=rest))
    return heat_rows, rest_rows


def heat_split(
    house: Sequence[Any],
    heat: Mapping[datetime, float],
    now: datetime,
    tz: str,
    *,
    temperatures: Mapping[datetime, float],
    holidays: frozenset[date] | None,
) -> HeatSplit | None:
    """The executor's part (see `HeatSplit`); `None` without an hour that has both figures."""
    heat_rows, rest_rows = split_rows(house, heat)
    if not heat_rows:
        return None
    return HeatSplit(
        heat=forecast_house_load(heat_rows, now, tz, temperatures=temperatures, holidays=holidays),
        rest=forecast_house_load(rest_rows, now, tz, temperatures=temperatures, holidays=holidays),
    )


# ------------------------------------------------------------------------------------------------ Home Assistant


def dashboard_nesting(prefs: Any) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """The Energy dashboard's individual devices as `({statistic: the statistic it is included in}, {power sensor:
    its energy statistic}, {statistic: the name the dashboard gives it, "" without})`; empty without the Energy
    integration."""
    devices = prefs.get("device_consumption") if isinstance(prefs, Mapping) else None
    nested: dict[str, str] = {}
    pairs: dict[str, str] = {}
    names: dict[str, str] = {}
    for device in devices if isinstance(devices, list) else []:
        if not isinstance(device, Mapping) or not isinstance(device.get("stat_consumption"), str):
            continue
        stat = device["stat_consumption"]
        names[stat] = device["name"].strip() if isinstance(device.get("name"), str) else ""
        if isinstance(device.get("included_in_stat"), str):
            nested[stat] = device["included_in_stat"]
        if isinstance(device.get("stat_rate"), str):
            pairs[device["stat_rate"]] = stat
            if stat in nested:
                nested[device["stat_rate"]] = nested[stat]
    return nested, pairs, names


async def async_heat_periods(
    hass: HomeAssistant,
    meter: HeatMeter,
    zones: Any,
    start: datetime,
    end: datetime,
    period: str = "hour",
    cap_kw: float | None = None,
) -> dict[datetime, float] | None:
    """The heat the house card counts per `period` in `[start, end)` (kWh): the meter's energy plus the zones'
    (`site/heat_zones.ZoneSources`), a period the meter lacks left out (its heat stays in the house); the zones' alone
    without a meter; `None` with neither. `cap_kw`: the main fuse's capacity, above which a zone's period is left
    out (`site/heat_zones.async_zone_periods`)."""
    from .heat_zones import async_zone_periods, summed_periods  # noqa: PLC0415 - it imports this module

    has_zones = zones is not None and bool(zones.sources)
    if not meter.found and not has_zones:
        return None
    pump = await async_heat_hours(hass, meter, start, end, period) if meter.found else None
    zone = (
        summed_periods(await async_zone_periods(hass, zones, start, end, period, cap_kw)) if has_zones else None
    )
    if pump is None:
        return zone
    if zone is None:
        return pump
    return {moment: kwh + zone.get(moment, 0.0) for moment, kwh in pump.items()}


def combine_hours(pump: Mapping[datetime, float] | None, zones: Mapping[datetime, float]) -> dict[datetime, float]:
    """The meter's hours plus the zones' (`async_heat_periods`' rule): the meter's hours with the zones' added, or the
    zones' alone without a meter (`pump` `None`)."""
    if pump is None:
        return dict(zones)
    return {moment: kw + zones.get(moment, 0.0) for moment, kw in pump.items()}


async def async_dashboard_devices(hass: HomeAssistant, prefs: Any = None) -> list[DashboardDevice]:
    """The Energy dashboard's individual devices with their names and platforms; none without the Energy integration."""
    if prefs is None:
        prefs = await async_energy_prefs(hass)
    devices = prefs.get("device_consumption") if isinstance(prefs, Mapping) else None
    if not isinstance(devices, list):
        return []
    entities = er.async_get(hass)
    device_registry = dr.async_get(hass)
    out: list[DashboardDevice] = []
    for device in devices:
        if not isinstance(device, Mapping) or not isinstance(device.get("stat_consumption"), str):
            continue
        stat = device["stat_consumption"]
        rate = device.get("stat_rate") if isinstance(device.get("stat_rate"), str) else None
        names: list[str] = [device["name"]] if isinstance(device.get("name"), str) else []
        platforms: list[str] = []
        for entity_id in (stat, rate):
            entry = None if entity_id is None else entities.async_get(entity_id)
            if entry is None:
                continue
            platforms.append(entry.platform)
            names.extend(name for name in (entry.name, entry.original_name) if isinstance(name, str))
            owner = None if entry.device_id is None else device_registry.async_get(entry.device_id)
            if owner is not None:
                names.extend(name for name in (owner.name_by_user, owner.name) if isinstance(name, str))
        out.append(DashboardDevice(stat, rate, tuple(names), tuple(platforms)))
    return out


def sensor_kind(hass: HomeAssistant, entity_id: str) -> str | None:
    """A sensor's kind (`meter_kind`) from its state, else its registry entry; `None` when it is neither or gone."""
    if not entity_id.startswith("sensor."):
        return None
    state = hass.states.get(entity_id)
    if state is not None:
        kind = meter_kind(state.attributes.get("device_class"), state.attributes.get("unit_of_measurement"))
        if kind is not None:
            return kind
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    return meter_kind(entry.device_class or entry.original_device_class, entry.unit_of_measurement)


def integration_entities(hass: HomeAssistant) -> list[tuple[str, str, str, str | None, Any]]:
    """The enabled entities of the integrations with a whole-pump meter, for `integration_candidates`."""
    return [
        (entry.entity_id, entry.platform, entry.unique_id, entry.device_class or entry.original_device_class,
         entry.unit_of_measurement)
        for entry in er.async_get(hass).entities.values()
        if entry.platform in INTEGRATION_METERS and entry.disabled_by is None
    ]


class SiteHeatMeter:
    """One site's heat meter (see the module): found again at each refresh, its hours kept for the forecast's window.
    Never raises into the site."""

    def __init__(
        self,
        hass: HomeAssistant,
        setting: Callable[[], str | None],
        zones: Callable[[], Sequence[str]] = lambda: (),
        moved: Callable[[], Sequence[str]] = lambda: (),
    ) -> None:
        self.hass = hass
        self._setting = setting
        self._zone_climates = zones
        #: The Energy dashboard devices the household moved to heat in the house pie (their statistics).
        self._moved = moved
        from .heat_zones import NO_ZONES  # noqa: PLC0415 - it imports this module

        #: The chosen zones counted in heat and those left out (`site/heat_zones.py`), as of the last detection.
        self.zones = NO_ZONES
        #: The zones' energy together per completed hour (kW, UTC hour starts), the forecast's window.
        self.zone_by_hour: dict[datetime, float] = {}
        self._zones_read_for: tuple[Any, ...] = ()
        self._nested: dict[str, str] = {}
        self._pairs: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self.meter: HeatMeter = NO_METER
        #: The meter found without the setting (`needs_setting` reads it).
        self.detected: HeatMeter = NO_METER
        #: The meter's figure per completed hour (kW, UTC hour starts), the forecast's window.
        self.by_hour: dict[datetime, float] = {}
        self._read_for: str | None = None
        self.read_failed = False
        self.reads = 0
        #: The main fuse's capacity (kW; `None` where it is not set), above which a zone's hour is a broken statistic
        #: and left out of the forecast's hours (`site/heat_zones.async_zone_periods`). Set by the site.
        self.cap_kw: Callable[[], float | None] = lambda: None

    async def async_detect(self) -> HeatMeter:
        """Find the meter (see the module) and keep it."""
        try:
            prefs = await async_energy_prefs(self.hass)
            dashboard = dashboard_candidates(await async_dashboard_devices(self.hass, prefs))
            self._nested, self._pairs, self._names = dashboard_nesting(prefs)
        except Exception:  # noqa: BLE001 - the Energy dashboard cannot be read now: the integration tier still counts
            _LOGGER.debug("SpotNav heat meter: the Energy dashboard could not be read", exc_info=True)
            dashboard = []
        integration = integration_candidates(integration_entities(self.hass))
        self.detected = pick_heat_meter(None, None, dashboard, integration)
        setting = self._setting()
        meter = self.detected
        if setting:
            meter = pick_heat_meter(setting, sensor_kind(self.hass, setting), dashboard, integration)
        if meter.entity_id != self.meter.entity_id or meter.status != self.meter.status:
            _LOGGER.info(
                "SpotNav heat meter: %s (%s, %s)", meter.entity_id or meter.status, meter.kind, meter.source
            )
        self.meter = meter
        self.zones = self.current_zones()
        return meter

    def current_zones(self) -> Any:
        """The chosen zones' sources as the settings and the registries are now (`site/heat_zones.find_zones`), the
        Energy dashboard's nesting as of the last detection; no zone when they cannot be read."""
        from .heat_zones import NO_ZONES, find_zones  # noqa: PLC0415 - it imports this module

        try:
            climates = list(self._zone_climates())
            # A moved meter counts only while it is still on the Energy dashboard.
            moved = [(stat, self._meter_name(stat)) for stat in self._moved() if stat in self._names]
            if not climates and not moved:
                return NO_ZONES
            meter = self.meter
            paired = [] if meter.entity_id is None else [
                other for rate, stat in self._pairs.items() for other in (rate, stat) if meter.entity_id in (rate, stat)
            ]
            return find_zones(self.hass, climates, meter, meter_ids=paired, nested=self._nested, moved=moved)
        except Exception:  # noqa: BLE001 - heat is shown only: without the zones it is the meter's
            _LOGGER.debug("SpotNav heat meter: the heat zones could not be read", exc_info=True)
            return NO_ZONES

    def zone_climates(self) -> tuple[str, ...]:
        """The climates chosen as zones in the heat settings; none when they cannot be read."""
        try:
            return tuple(self._zone_climates())
        except Exception:  # noqa: BLE001 - shown only
            _LOGGER.debug("zone_climates: ignored failure (shown only)", exc_info=True)
            return ()

    async def async_current_zones(self) -> Any:
        """`current_zones` with the Energy dashboard read again (a device just moved to heat counts at once)."""
        try:
            self._nested, self._pairs, self._names = dashboard_nesting(await async_energy_prefs(self.hass))
        except Exception:  # noqa: BLE001 - the dashboard as last read
            _LOGGER.debug("SpotNav heat meter: the Energy dashboard could not be read", exc_info=True)
        return self.current_zones()

    def live_power_ids(self, zones: Any) -> tuple[str, ...] | None:
        """The power sensors that tell the house card's heat now (the meter and each zone, `live_power_id`), `None`
        when one of them has none: the heat is then the last period's (`api/house_now.py`)."""
        sources: list[tuple[str, str]] = []
        if self.meter.found and self.meter.entity_id:
            sources.append((self.meter.entity_id, self.meter.kind))
        sources.extend((source.sensor_id, source.kind) for source in getattr(zones, "sources", ()))
        if not sources:
            return None
        ids = [live_power_id(self.hass, sensor_id, kind, self._pairs) for sensor_id, kind in sources]
        return None if any(i is None for i in ids) else tuple(i for i in ids if i)

    def _meter_name(self, stat: str) -> str:
        """A dashboard device's name: the dashboard's, else its sensor's, else the statistic id."""
        if self._names.get(stat):
            return self._names[stat]
        state = self.hass.states.get(stat)
        if state is not None:
            return state.name
        entry = er.async_get(self.hass).async_get(stat)
        return (entry.name or entry.original_name or stat) if entry is not None else stat

    def combined_by_hour(self) -> dict[datetime, float]:
        """The meter's hours plus the zones' (`combine_hours`): the house card's heat; empty with neither."""
        if not self.meter.found and not self.zones.sources:
            return {}
        return combine_hours(self.by_hour if self.meter.found else None, self.zone_by_hour)

    @property
    def source_count(self) -> int:
        """How many sources the house card's heat holds: the meter where found, and each zone counted."""
        return (1 if self.meter.found else 0) + len(self.zones.sources)

    async def async_refresh(self, now: datetime) -> bool:
        """Find the meter and the zones and read their completed hours not yet held (the last `REREAD_HOURS` again);
        whether their hours changed what they measure (another sensor, a zone added or gone: the old hours are
        dropped)."""
        meter = await self.async_detect()
        changed = meter.entity_id != self._read_for
        if changed:
            self.by_hour = {}
            self._read_for = meter.entity_id
        current = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        oldest = current - timedelta(days=WINDOW_DAYS)
        zones_changed = self.zones.signature() != self._zones_read_for
        if zones_changed:
            self.zone_by_hour = {}
            self._zones_read_for = self.zones.signature()
        await self._async_read_zones(current, oldest)
        changed = changed or zones_changed
        if not meter.found:
            return changed
        held = [start for start in self.by_hour if start >= oldest]
        start = max(oldest, max(held) - timedelta(hours=REREAD_HOURS)) if held else oldest
        try:
            read = await async_heat_hours(self.hass, meter, start, current)
        except Exception:  # noqa: BLE001 - the hours wait for the next refresh
            _LOGGER.debug("SpotNav heat meter: %s could not be read", meter.entity_id, exc_info=True)
            self.read_failed = True
            return changed
        self.read_failed = False
        self.reads += 1
        self.by_hour.update(read)
        for stale in [start for start in self.by_hour if start < oldest]:
            del self.by_hour[stale]
        return changed

    async def _async_read_zones(self, current: datetime, oldest: datetime) -> None:
        """The zones' completed hours not yet held, as the meter's."""
        from .heat_zones import async_zone_periods, summed_periods  # noqa: PLC0415 - it imports this module

        if not self.zones.sources:
            return
        held = [start for start in self.zone_by_hour if start >= oldest]
        start = max(oldest, max(held) - timedelta(hours=REREAD_HOURS)) if held else oldest
        try:
            read = summed_periods(
                await async_zone_periods(self.hass, self.zones, start, current, "hour", self.cap_kw())
            )
        except Exception:  # noqa: BLE001 - the hours wait for the next refresh
            _LOGGER.debug("SpotNav heat meter: the heat zones' hours could not be read", exc_info=True)
            return
        self.zone_by_hour.update(read)
        for stale in [moment for moment in self.zone_by_hour if moment < oldest]:
            del self.zone_by_hour[stale]

    def diagnostics(self) -> dict[str, Any]:
        return {
            "zones": {**self.zones.diagnostics(), "hours": len(self.zone_by_hour)},
            **self.meter.as_dict(),
            "detected": self.detected.as_dict(),
            "hours": len(self.by_hour),
            "first_hour": None if not self.by_hour else min(self.by_hour).isoformat(),
            "read_failed": self.read_failed,
            "reads": self.reads,
        }


def live_power_id(hass: HomeAssistant, sensor_id: str, kind: str, pairs: Mapping[str, str]) -> str | None:
    """The power sensor that tells what an energy source draws now: a power sensor itself, the Energy dashboard's
    power sensor paired with an energy statistic (`stat_rate`), else the one enabled power sensor on the energy
    sensor's device; `None` where there is none or more than one (which one is meant cannot be told)."""
    if kind == KIND_POWER:
        return sensor_id
    for rate, stat in pairs.items():
        if stat == sensor_id:
            return rate
    registry = er.async_get(hass)
    entry = registry.async_get(sensor_id)
    if entry is None or entry.device_id is None:
        return None
    found = [
        other.entity_id
        for other in er.async_entries_for_device(registry, entry.device_id)
        if other.domain == "sensor"
        and other.disabled_by is None
        and (other.device_class or other.original_device_class) == "power"
    ]
    return found[0] if len(found) == 1 else None
