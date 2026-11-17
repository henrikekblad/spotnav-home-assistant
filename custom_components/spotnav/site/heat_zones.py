"""The heat zones' own electricity (SpotNav 3.0 heat): what each zone chosen in the heat settings used, so the house
card's "Heat" holds electric floor heating and an air-to-air heat pump beside the heat pump's meter. Seen and shown
only: nothing here controls a zone.

**Which sensor** (no setting): each zone is a climate entity (`heat/config.py` `zones`, every kind: an electric floor
thermostat or an air-to-air heat pump); its sensor is found on the climate's own device (`pick_zone_sensor`):

1. an energy sensor (device class `energy`, else a kWh/Wh/MWh unit) that keeps a total (state class `total` or
   `total_increasing`: the recorder has its hourly `change`), else
2. a power sensor (device class `power`, else a W/kW/MW unit) with the state class `measurement` (the recorder has
   its hourly `mean`, integrated as the heat pump meter's power sensor is, `site/heat_meter.period_kwh`).

Among several of one kind a cooling one (`cool` in its entity id or translation key: a Daikin's
`ac_cool_energy_consumption`) goes last, the one with `total_increasing` first, then the unqualified total before a
`heat` one, then the lowest entity id, so the pick is stable.

**Counted once** (`resolve_zones`): a zone is left out (`unmeasured`, with why) when it has no such sensor
(`no_sensor`), when its sensor is the heat pump's meter or on the heat pump meter's own device (`heat_meter`: the meter
already holds it), when the Energy dashboard nests its sensor inside the heat pump's meter (`in_heat_meter`), when an
earlier zone already counts the same sensor (`shared`: two thermostats of one device) or when the Energy dashboard
nests it inside another zone's sensor (`in_zone`), or when the Energy dashboard nests the heat pump's meter inside its
sensor (`holds_heat_meter`: a circuit meter moved to heat that the pump's meter is part of; counting both would count
the pump twice). Nothing is said to the household about a zone left out: it is
listed in the diagnostics only.

**A meter the household moved to heat**: an Energy dashboard individual device whose belonging the household set
to heat in the house pie (`site/house_loads.py`, `meter_belongs`; a Shelly in the fuse box on the floor heating's
circuit) is a source of its own too (`origin` `meter`, its energy statistic), after the zones and under the same
rules (`REASON_HEAT_METER` and so on), so it is never counted twice.

**Their energy** (`async_zone_periods`): from the recorder's statistics (`"hour"`, or `"5minute"` within its
short-term window), read in its executor, kWh per period start for each zone's sensor.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .heat_meter import KIND_ENERGY, KIND_POWER, PERIOD_HOURS, HeatMeter, meter_kind, period_kwh

REASON_NO_SENSOR: Final = "no_sensor"
REASON_HEAT_METER: Final = "heat_meter"
REASON_IN_HEAT_METER: Final = "in_heat_meter"
REASON_SHARED: Final = "shared"
REASON_IN_ZONE: Final = "in_zone"
REASON_HOLDS_HEAT_METER: Final = "holds_heat_meter"

#: The state classes an energy sensor's statistics keep a total by (its `change` per period).
ENERGY_STATE_CLASSES: Final = frozenset({"total", "total_increasing"})
ORIGIN_ZONE: Final = "zone"
ORIGIN_METER: Final = "meter"


@dataclass(frozen=True, slots=True)
class DeviceSensor:
    """A sensor on a zone's device: its id, device class, unit and state class (any may be `None`)."""

    entity_id: str
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    #: The integration's translation key (`None` without one).
    key: str | None = None


def _qualifier_rank(sensor: DeviceSensor) -> int:
    """0 for an unqualified total, 1 for a `heat` one, 2 for a `cool` one (by its entity id or translation key)."""
    text = f"{sensor.entity_id} {sensor.key or ''}".lower()
    if "cool" in text:
        return 2
    return 1 if "heat" in text else 0


@dataclass(frozen=True, slots=True)
class ZoneSource:
    """A zone counted in heat: the climate, the name it is shown by, and the sensor read with its kind."""

    entity_id: str
    name: str
    sensor_id: str
    kind: str
    #: `zone` (a climate chosen in the heat settings) or `meter` (an Energy dashboard device moved to heat; its
    #: `entity_id` is its statistic).
    origin: str = "zone"

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "sensor_id": self.sensor_id,
            "kind": self.kind,
            "origin": self.origin,
        }


@dataclass(frozen=True, slots=True)
class ZoneSources:
    """The zones counted (`sources`) and those left out (`unmeasured`: `(climate, reason)`)."""

    sources: tuple[ZoneSource, ...] = ()
    unmeasured: tuple[tuple[str, str], ...] = ()

    @property
    def sensor_ids(self) -> tuple[str, ...]:
        return tuple(source.sensor_id for source in self.sources)

    def signature(self) -> tuple[tuple[str, str], ...]:
        """What is read (another sensor or kind: the held figures are read anew)."""
        return tuple((source.sensor_id, source.kind) for source in self.sources)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "sources": [source.as_dict() for source in self.sources],
            "unmeasured": [{"entity_id": entity_id, "reason": reason} for entity_id, reason in self.unmeasured],
        }


NO_ZONES: Final = ZoneSources()


def pick_zone_sensor(sensors: Iterable[DeviceSensor]) -> tuple[str, str] | None:
    """The sensor a zone's energy is read from (see the module) as `(entity_id, kind)`; `None` without one."""
    energy: list[DeviceSensor] = []
    power: list[DeviceSensor] = []
    for sensor in sensors:
        if not sensor.entity_id.startswith("sensor."):
            continue
        kind = meter_kind(sensor.device_class, sensor.unit)
        if kind == KIND_ENERGY and sensor.state_class in ENERGY_STATE_CLASSES:
            energy.append(sensor)
        elif kind == KIND_POWER and sensor.state_class == "measurement":
            power.append(sensor)
    for found, kind in ((energy, KIND_ENERGY), (power, KIND_POWER)):
        if found:
            best = min(
                found,
                key=lambda s: (
                    _qualifier_rank(s) == 2,
                    s.state_class != "total_increasing",
                    _qualifier_rank(s),
                    s.entity_id,
                ),
            )
            return best.entity_id, kind
    return None


def resolve_zones(
    zones: Sequence[tuple[str, str, tuple[str, str] | None, str | None]],
    meter: HeatMeter,
    *,
    meter_ids: Iterable[str] = (),
    meter_device: str | None = None,
    nested: Mapping[str, str] | None = None,
    moved: Sequence[tuple[str, str, str | None]] = (),
) -> ZoneSources:
    """The zones counted, each sensor once (see the module). `zones`: `(climate, name, picked sensor or None, the
    climate's device)` in the settings' order; `meter_ids`: the heat pump meter's sensors (its power sensor and its
    energy statistic where the Energy dashboard pairs them); `nested`: the Energy dashboard's `{statistic: the
    statistic it is included in}`; `moved`: the Energy dashboard devices the household moved to heat, `(statistic,
    name, its device)`, after the zones."""
    nested = dict(nested or {})
    meters = {entity_id for entity_id in (*meter_ids, meter.entity_id if meter.found else None) if entity_id}
    # The statistics the heat pump's meter is nested in, all the way up (a sensor among them holds the pump).
    holders: set[str] = set()
    for meter_id in meters:
        parent = nested.get(meter_id)
        while parent is not None and parent not in holders and parent not in meters:
            holders.add(parent)
            parent = nested.get(parent)
    sources: list[ZoneSource] = []
    unmeasured: list[tuple[str, str]] = []
    rows = [(entity_id, name, picked, device_id, ORIGIN_ZONE) for entity_id, name, picked, device_id in zones]
    rows += [(stat, name, (stat, KIND_ENERGY), device_id, ORIGIN_METER) for stat, name, device_id in moved]
    for entity_id, name, picked, device_id, origin in rows:
        if picked is None:
            unmeasured.append((entity_id, REASON_NO_SENSOR))
            continue
        sensor_id, kind = picked
        parent = nested.get(sensor_id)
        if sensor_id in meters or (meter.found and meter_device is not None and device_id == meter_device):
            unmeasured.append((entity_id, REASON_HEAT_METER))
        elif parent is not None and parent in meters:
            unmeasured.append((entity_id, REASON_IN_HEAT_METER))
        elif sensor_id in holders:
            unmeasured.append((entity_id, REASON_HOLDS_HEAT_METER))
        elif any(source.sensor_id == sensor_id for source in sources):
            unmeasured.append((entity_id, REASON_SHARED))
        else:
            sources.append(ZoneSource(entity_id, name, sensor_id, kind, origin))
    counted = {source.sensor_id for source in sources}
    kept: list[ZoneSource] = []
    for source in sources:
        if nested.get(source.sensor_id) in counted:
            unmeasured.append((source.entity_id, REASON_IN_ZONE))
        else:
            kept.append(source)
    return ZoneSources(tuple(kept), tuple(unmeasured))


def summed_periods(by_sensor: Mapping[str, Mapping[datetime, float]]) -> dict[datetime, float]:
    """The zones' energy together per period start (kWh)."""
    out: dict[datetime, float] = {}
    for periods in by_sensor.values():
        for start, kwh in periods.items():
            out[start] = out.get(start, 0.0) + kwh
    return out


# ------------------------------------------------------------------------------------------------ Home Assistant


def device_sensors(hass: HomeAssistant, entity_id: str) -> tuple[str | None, list[DeviceSensor]]:
    """The climate's device and the sensors on it (their state's attributes, else their registry entry)."""
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.device_id is None:
        return None, []
    out: list[DeviceSensor] = []
    for other in er.async_entries_for_device(registry, entry.device_id):
        if not other.entity_id.startswith("sensor.") or other.disabled_by is not None:
            continue
        state = hass.states.get(other.entity_id)
        attributes = {} if state is None else state.attributes
        out.append(
            DeviceSensor(
                other.entity_id,
                attributes.get("device_class") or other.device_class or other.original_device_class,
                attributes.get("unit_of_measurement") or other.unit_of_measurement,
                attributes.get("state_class") or (other.capabilities or {}).get("state_class"),
                other.translation_key,
            )
        )
    return entry.device_id, out


def zone_name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if state is not None:
        return state.name
    entry = er.async_get(hass).async_get(entity_id)
    return (entry.name or entry.original_name or entity_id) if entry is not None else entity_id


def find_zones(
    hass: HomeAssistant,
    climates: Sequence[str],
    meter: HeatMeter,
    *,
    meter_ids: Iterable[str] = (),
    nested: Mapping[str, str] | None = None,
    moved: Sequence[tuple[str, str]] = (),
) -> ZoneSources:
    """The chosen zones' sources (`resolve_zones`) from the registries, and the meters moved to heat (`(statistic,
    name)`); never raises (a zone that cannot be looked up counts as one without a sensor)."""
    meter_device = None
    if meter.found and meter.entity_id:
        meter_entry = er.async_get(hass).async_get(meter.entity_id)
        meter_device = None if meter_entry is None else meter_entry.device_id
    rows: list[tuple[str, str, tuple[str, str] | None, str | None]] = []
    for climate in climates:
        try:
            device_id, sensors = device_sensors(hass, climate)
            rows.append((climate, zone_name(hass, climate), pick_zone_sensor(sensors), device_id))
        except Exception:  # noqa: BLE001 - a registry that cannot be read leaves the zone out
            rows.append((climate, climate, None, None))
    registry = er.async_get(hass)
    moved_rows: list[tuple[str, str, str | None]] = []
    for stat, name in moved:
        entry = registry.async_get(stat)
        moved_rows.append((stat, name, None if entry is None else entry.device_id))
    return resolve_zones(
        rows, meter, meter_ids=meter_ids, meter_device=meter_device, nested=nested, moved=moved_rows
    )


async def async_zone_periods(
    hass: HomeAssistant,
    zones: ZoneSources,
    start: datetime,
    end: datetime,
    period: str = "hour",
    cap_kw: float | None = None,
) -> dict[str, dict[datetime, float]]:
    """Each zone sensor's energy per `period` in `[start, end)` (kWh, UTC period starts), read in the recorder's
    executor in one go; empty without a zone. A period above `cap_kw` (the main fuse's capacity, as a mean over the
    period) is a broken statistic, not energy: it is left out (`site/house_loads.py`, hours that cannot be)."""
    if not zones.sources or end <= start:
        return {}
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415 - only when loaded
    from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

    energy = {source.sensor_id for source in zones.sources if source.kind == KIND_ENERGY}
    power = {source.sensor_id for source in zones.sources if source.kind == KIND_POWER}

    def read() -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
        by_energy = (
            statistics_during_period(hass, start, end, energy, period, {"energy": "kWh"}, {"change"}) if energy else {}
        )
        by_power = statistics_during_period(hass, start, end, power, period, {"power": "kW"}, {"mean"}) if power else {}
        return by_energy, by_power

    by_energy, by_power = await get_instance(hass).async_add_executor_job(read)
    hours = PERIOD_HOURS[period]
    out: dict[str, dict[datetime, float]] = {}
    for source in zones.sources:
        rows = (by_energy if source.kind == KIND_ENERGY else by_power).get(source.sensor_id) or []
        figures = period_kwh(list(rows), source.kind, hours)
        if cap_kw is not None:
            figures = {moment: kwh for moment, kwh in figures.items() if kwh <= cap_kw * hours}
        out[source.sensor_id] = figures
    return out
