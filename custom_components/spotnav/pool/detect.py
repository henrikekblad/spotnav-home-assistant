"""The pool's heat pump, circulation pump and water sensor, ranked from the registry (spec section 3; research
`plans/research_integrations/pool_heat_pump_control_2026-11-23.md` section 4). Read only and cheap: the entity and device
registries and the states, no I/O, nothing written. A suggestion under `ASK_BELOW` is asked once in the settings ("Är
detta poolens värmepump?"); nothing is stored until an administrator saves the pool block.

**Pool heat pump** (`heat_pump_score`): the best rule wins, then each support adds `SUPPORT` up to `SUPPORT_CAP`:

1. 0.95: a `tuya_local` climate whose registry `translation_key` is `pool_heatpump`; or a climate of an integration whose
   domain says pool heat pump (`POOL_DOMAINS`).
2. 0.9: core `tuya`'s pool heat pump without a climate (category `znrb`): a device with a `temp_set` number, an on/off
   switch and a compressor-strength sensor (`shape: switch_number`, read from core `tuya`'s source).
3. 0.8: a device whose manufacturer is a pool heat pump brand (`POOL_BRANDS`).
4. Supports: a range from at most 18 °C up to 40-45 °C; modes without `dry` and `fan_only`; a compressor, coil, outlet
   or flow sensor on the device. **Negative** (score 0, unless rule 1 or 2 holds: Aquatemp's climate has fan modes): a
   climate with `dry` or `fan_only` modes or with fan modes (an air-to-air unit).

The panels that also heat a spa (`iaqualink`, `screenlogic`, `intellicenter`) score 0.8 and are always asked (pool or
spa body). **Circulation**: no reliable signal; a `switch`/`input_boolean` on another device with "pool", "pump", "cirk",
"circ" or "filter" in its name or in the area "Pool" scores 0.5 (always asked); every other switch is listed at
0. **Water sensor**: the heat pump's own reading always comes first (`pool/runtime.py`); the standalone sensor is the
second choice. A temperature sensor with "pool", "vatten" or "water" in its name or area scores 0.5 (asked), unless its
name says air (ambient, outdoor, coil, vent, outlet) or it sits on the heat pump's device (its own ambient and pipe
readings); default none (the heat pump's reading alone).

**Grouping** (`suggest`): the best heat pump, with the circulation and the water sensor proposed on the same device or
area, as one row the person confirms.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ..pool.config import SHAPE_CLIMATE, SHAPE_SWITCH_NUMBER

#: A suggestion under this is asked once in the settings.
ASK_BELOW: Final = 0.9
#: A climate scoring under this (supports alone) is not offered as a pool heat pump.
MIN_OFFERED: Final = 0.2
SUPPORT: Final = 0.1
SUPPORT_CAP: Final = 0.95

#: Integrations whose domain is a pool heat pump's (research section 1).
POOL_DOMAINS: Final = frozenset({"aqua_temp", "warmlink", "fairland", "poolex_silverline", "poolsync_custom"})
#: Pool panels that heat a pool and a spa: the person says which body.
PANEL_DOMAINS: Final = frozenset({"iaqualink", "screenlogic", "intellicenter"})
#: Integrations read through the maker's cloud: slow planned actions only (spec section 6).
CLOUD_DOMAINS: Final = frozenset({"tuya", "aqua_temp", "warmlink", "fairland", "iaqualink"})
#: Pool heat pump brands in the device registry's manufacturer (research section 2).
POOL_BRANDS: Final = (
    "madimack", "poolex", "fairland", "aquark", "weau", "swim & fun", "mountfield", "henden", "brustec", "cbc",
    "giatsu", "aquastrong",
)
TUYA_LOCAL_KEY: Final = "pool_heatpump"

_DEVICE_SENSOR_WORDS: Final = re.compile(r"compressor|coil|outlet|effluent|flow|kompressor", re.IGNORECASE)
_CIRCULATION_WORDS: Final = re.compile(r"pool|pump|cirk|circ|filter|pumpe", re.IGNORECASE)
_WATER_WORDS: Final = re.compile(r"pool|vatten|water", re.IGNORECASE)
_AIR_WORDS: Final = re.compile(r"(?<![a-z])(ambient|air|luft|outdoor|ute|utomhus|coil|vent|exhaust|outlet|utlopp)", re.IGNORECASE)
POOL_AREA: Final = "pool"


@dataclass(frozen=True, slots=True)
class Seen:
    """An entity as detection sees it: its registry entry, its device and its state's attributes."""

    entity_id: str
    platform: str
    device_id: str | None = None
    name: str = ""
    area: str | None = None
    translation_key: str | None = None
    unique_id: str = ""
    manufacturer: str | None = None
    model: str | None = None
    hvac_modes: tuple[str, ...] = ()
    fan_modes: bool = False
    #: A climate's or water heater's range, in °C.
    min_c: float | None = None
    max_c: float | None = None
    device_class: str | None = None
    unit: str | None = None
    icon: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    def is_temperature(self) -> bool:
        return self.domain == "sensor" and (self.device_class == "temperature" or self.unit in ("°C", "°F", "K"))


@dataclass(frozen=True, slots=True)
class Candidate:
    """A suggested entity and how sure the detection is (`ask` below `ASK_BELOW`)."""

    entity_id: str
    score: float
    shape: str | None = None
    switch_id: str | None = None
    number_id: str | None = None
    device_id: str | None = None
    #: The device's own water reading (core `tuya`'s current temperature, read as the heat pump's), else `None`.
    water_sensor: str | None = None
    cloud: bool = False

    @property
    def ask(self) -> bool:
        return self.score < ASK_BELOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "score": round(self.score, 2),
            "ask": self.ask,
            "shape": self.shape,
            "switch_id": self.switch_id,
            "number_id": self.number_id,
            "cloud": self.cloud,
        }


def _by_device(seen: Iterable[Seen]) -> dict[str | None, list[Seen]]:
    out: dict[str | None, list[Seen]] = {}
    for item in seen:
        out.setdefault(item.device_id, []).append(item)
    return out


def _is_brand(manufacturer: str | None) -> bool:
    text = (manufacturer or "").casefold()
    return any(brand in text for brand in POOL_BRANDS)


def _supports(entity: Seen, siblings: Sequence[Seen]) -> int:
    count = 0
    if entity.min_c is not None and entity.max_c is not None and entity.min_c <= 18.0 and 40.0 <= entity.max_c <= 45.0:
        count += 1
    if entity.hvac_modes and not {"dry", "fan_only"} & set(entity.hvac_modes):
        count += 1
    if any(
        item.entity_id != entity.entity_id and item.domain in ("sensor", "binary_sensor") and _DEVICE_SENSOR_WORDS.search(
            f"{item.name} {item.translation_key or ''} {item.unique_id}"
        )
        for item in siblings
    ):
        count += 1
    return count


def heat_pump_score(entity: Seen, siblings: Sequence[Seen] = ()) -> float:
    """How sure the detection is that a climate is a pool heat pump (see the module); 0 when it is not one."""
    if entity.domain != "climate":
        return 0.0
    if (entity.platform == "tuya_local" and entity.translation_key == TUYA_LOCAL_KEY) or entity.platform in POOL_DOMAINS:
        base = 0.95
    else:
        if {"dry", "fan_only"} & set(entity.hvac_modes) or entity.fan_modes:
            return 0.0
        base = 0.8 if _is_brand(entity.manufacturer) or entity.platform in PANEL_DOMAINS else 0.0
    score = min(SUPPORT_CAP, base + SUPPORT * _supports(entity, siblings)) if base < SUPPORT_CAP else base
    return round(score, 2)


def _suffix(item: Seen, key: str) -> bool:
    return item.unique_id.endswith(key)


def tuya_heat_pump(device: Sequence[Seen]) -> Candidate | None:
    """Core `tuya`'s pool heat pump (category `znrb`, no climate): a `temp_set` number, the on/off switch and the
    compressor-strength sensor on one device (unique ids `tuya.<device><dp code>`)."""
    tuya = [item for item in device if item.platform == "tuya"]
    number = next((i for i in tuya if i.domain == "number" and _suffix(i, "temp_set")), None)
    switch = next((i for i in tuya if i.domain == "switch" and _suffix(i, "switch") and not _suffix(i, "child_lock")), None)
    compressor = next(
        (i for i in tuya if i.domain == "sensor" and (i.translation_key == "compressor_strength" or _suffix(i, "compressor_strength"))),
        None,
    )
    if number is None or switch is None or compressor is None or any(i.domain == "climate" for i in tuya):
        return None
    water = next((i for i in tuya if i.domain == "sensor" and _suffix(i, "temp_current")), None)
    return Candidate(
        entity_id=switch.entity_id,
        score=0.9,
        shape=SHAPE_SWITCH_NUMBER,
        switch_id=switch.entity_id,
        number_id=number.entity_id,
        device_id=switch.device_id,
        water_sensor=None if water is None else water.entity_id,
        cloud=True,
    )


def heat_pumps(seen: Iterable[Seen]) -> list[Candidate]:
    """Every pool heat pump candidate, best first."""
    found: list[Candidate] = []
    for device_id, items in _by_device(seen).items():
        siblings = items if device_id is not None else []
        for item in items:
            score = heat_pump_score(item, siblings)
            if score >= MIN_OFFERED:
                found.append(
                    Candidate(
                        item.entity_id, score, SHAPE_CLIMATE, device_id=device_id, cloud=item.platform in CLOUD_DOMAINS
                    )
                )
        if device_id is not None and (tuya := tuya_heat_pump(items)) is not None:
            found.append(tuya)
    return sorted(found, key=lambda c: (-c.score, c.entity_id))


def circulations(seen: Iterable[Seen], heat_pump_device: str | None = None) -> list[Candidate]:
    """Every switch that could be the circulation pump, a likely one (0.5) first; none on the heat pump's device."""
    found = []
    for item in seen:
        if item.domain not in ("switch", "input_boolean") or (heat_pump_device is not None and item.device_id == heat_pump_device):
            continue
        likely = bool(_CIRCULATION_WORDS.search(item.name)) or (item.area or "").casefold() == POOL_AREA
        found.append(Candidate(item.entity_id, 0.5 if likely else 0.0, device_id=item.device_id))
    return sorted(found, key=lambda c: (-c.score, c.entity_id))


def water_sensors(seen: Iterable[Seen], heat_pump_device: str | None = None) -> list[Candidate]:
    """Every temperature sensor that could be the pool's standalone water sensor, a likely one (0.5) first; one on the
    heat pump's device or named for the air is never likely."""
    found = []
    for item in seen:
        if not item.is_temperature():
            continue
        likely = (
            bool(_WATER_WORDS.search(f"{item.name} {item.area or ''}"))
            and not _AIR_WORDS.search(f"{item.name} {item.translation_key or ''} {item.entity_id}")
            and (heat_pump_device is None or item.device_id != heat_pump_device)
        )
        found.append(Candidate(item.entity_id, 0.5 if likely else 0.0, device_id=item.device_id))
    return sorted(found, key=lambda c: (-c.score, c.entity_id))


def suggest(seen: Sequence[Seen]) -> dict[str, Any] | None:
    """The one-time grouping: the best heat pump with a circulation and a water sensor proposed beside it (the same
    device or area); `None` without a heat pump candidate. Nothing is stored until an administrator saves it."""
    pumps = heat_pumps(seen)
    if not pumps:
        return None
    best = pumps[0]
    area = next((item.area for item in seen if item.entity_id == best.entity_id), None)
    near = [
        item
        for item in seen
        if item.device_id != best.device_id and (area is not None and item.area == area)
    ]
    circulation = next((c for c in circulations(near or seen, best.device_id) if c.score > 0), None)
    # The heat pump's own reading comes first at run time; the standalone sensor is proposed as the second.
    likely = [c for c in water_sensors(near or seen, best.device_id) if c.score > 0]
    sensor = likely[0].entity_id if likely else None
    return {
        "heat_pump": {
            "entity_id": best.entity_id,
            "shape": best.shape,
            "switch_id": best.switch_id,
            "number_id": best.number_id,
        },
        "circulation_id": None if circulation is None else circulation.entity_id,
        "water_sensor": sensor,
        "ask": best.ask or circulation is not None,
    }


# ------------------------------------------------------------------------------------------------ the registry shell


def _celsius(value: Any, fahrenheit: bool) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return (float(value) - 32.0) * 5.0 / 9.0 if fahrenheit else float(value)


DOMAINS: Final = frozenset({"climate", "water_heater", "switch", "input_boolean", "number", "sensor", "binary_sensor"})


def read_seen(hass: Any) -> list[Seen]:
    """Every entity detection looks at, from the registries and the states (read only, no I/O). SpotNav's own, a
    disabled one and a diagnostic or configuration one are left out."""
    from homeassistant.const import UnitOfTemperature  # noqa: PLC0415 - the pure part is read without Home Assistant
    from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er  # noqa: PLC0415

    from ..const import DOMAIN  # noqa: PLC0415

    fahrenheit = hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)

    def area_name(area_id: str | None) -> str | None:
        area = None if area_id is None else areas.async_get_area(area_id)
        return None if area is None else area.name

    seen: list[Seen] = []
    known: set[str] = set()
    for entry in entities.entities.values():
        if entry.domain not in DOMAINS or entry.platform == DOMAIN or entry.disabled_by is not None:
            continue
        if entry.entity_category is not None:
            continue
        device = None if entry.device_id is None else devices.async_get(entry.device_id)
        state = hass.states.get(entry.entity_id)
        attributes = {} if state is None else state.attributes
        modes = attributes.get("hvac_modes") or ()
        seen.append(
            Seen(
                entity_id=entry.entity_id,
                platform=entry.platform,
                device_id=entry.device_id,
                name=(state.name if state is not None else None) or entry.name or entry.original_name or entry.entity_id,
                area=area_name(entry.area_id or (None if device is None else device.area_id)),
                translation_key=entry.translation_key,
                unique_id=str(entry.unique_id or ""),
                manufacturer=None if device is None else device.manufacturer,
                model=None if device is None else device.model,
                hvac_modes=tuple(str(mode) for mode in modes) if isinstance(modes, (list, tuple)) else (),
                fan_modes=bool(attributes.get("fan_modes")),
                min_c=_celsius(attributes.get("min_temp"), fahrenheit),
                max_c=_celsius(attributes.get("max_temp"), fahrenheit),
                device_class=attributes.get("device_class") or entry.device_class or entry.original_device_class,
                unit=attributes.get("unit_of_measurement") or entry.unit_of_measurement,
                icon=attributes.get("icon") or entry.icon or entry.original_icon,
            )
        )
        known.add(entry.entity_id)
    # A helper or template without a registry entry (an `input_boolean` in YAML, a template sensor).
    for state in hass.states.async_all():
        if state.entity_id in known or state.domain not in DOMAINS:
            continue
        modes = state.attributes.get("hvac_modes") or ()
        seen.append(
            Seen(
                entity_id=state.entity_id,
                platform=state.domain,
                name=state.name,
                hvac_modes=tuple(str(mode) for mode in modes) if isinstance(modes, (list, tuple)) else (),
                fan_modes=bool(state.attributes.get("fan_modes")),
                min_c=_celsius(state.attributes.get("min_temp"), fahrenheit),
                max_c=_celsius(state.attributes.get("max_temp"), fahrenheit),
                device_class=state.attributes.get("device_class"),
                unit=state.attributes.get("unit_of_measurement"),
                icon=state.attributes.get("icon"),
            )
        )
    return seen
