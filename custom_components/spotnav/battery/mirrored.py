"""A home battery mirrored from another Home Assistant by Remote Home-Assistant: read only (pure).

Remote Home-Assistant (HACS `remote_homeassistant`, custom-components/remote_homeassistant 4.6, `__init__.py`
`state_changed`, read 2026-11-25) writes the other instance's states here with `hass.states.async_set` and registers
each entity under its own platform: unique id `<the remote's uuid, first 16 characters>_<local entity id>`, no config
entry, no device. The local entity id is the remote's, its object id after the entry's optional `entity_prefix`
(option `entity_prefix`, prepended as it is: `grid_` turns `sensor.solax_battery_capacity` into
`sensor.grid_solax_battery_capacity`). The attributes (unit, device class, state class, friendly name) are the
remote's, passed on unchanged; the remote's unique ids, devices and manufacturers are not.

So a battery seen only through it has no adapter by platform (`adapters.adapter_for`). Here every read adapter is
tried on the mirrored entities with its own key patterns, read against the remote's object id (the prefix taken off)
in place of the unique id: the integrations' entity ids are, by default, the device's name and the entity's, and for
most the entity's name is its key (`solax_modbus`: `<hub name>_<key>` for both). Each adapter is scored by how many of
its roles are found with a plausible unit, and by the site's battery power entity matching the brand's own battery
power row (`site_detection.BATTERY_ROWS`). An adapter counts only when its level (`soc`) is found as a percentage and
the battery entity reads a power; the best one is taken only when it scores more than every other, else the battery
stays unknown and the diagnostics say why (`ambiguous:<brands>`).

What cannot be read this way degrades: a renamed entity on the other instance is not found, an integration whose
entity names are not its keys is found in part or not at all, and the device's manufacturer and firmware are not
mirrored (the firmware stays unknown). A missing size and missing ratings are learnt from the plan's quarter hours as
for any brand (`learned.py`). The power's sign is the site's own setting; where the battery entity is the brand's
battery power row, the diagnostics say whether the setting agrees with the brand's convention.

Never controlled: a write through Remote Home-Assistant would be forwarded to the other instance as a service call,
outside its own integration's checks, and nothing here can read the result back from the integration itself. The
facts carry `read_only` and no controls; every recipe lookup goes through `BatteryFacts.control_brand`, which is
`None` for them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from ..site.site_detection import BATTERY_ROWS
from .adapters import ADAPTERS, Adapter, Match
from .facts import BatteryFacts, EntityFact, battery_facts, find_entity

#: The integration's domain, the platform of every entity it mirrors.
MIRROR_PLATFORM: Final = "remote_homeassistant"
#: `BatteryFacts.read_only` for a battery read through it.
READ_ONLY_MIRRORED: Final = MIRROR_PLATFORM
#: The battery control checklist's line for it (`control.BatteryControl._needs`).
NEED_MIRRORED_READ_ONLY: Final = "mirrored_read_only"

#: `<remote uuid[:16]>_<domain>.<object id>`, as the integration builds it.
_UNIQUE_ID: Final = re.compile(r"(?P<remote>[^_.]{1,16})_(?P<domain>[a-z0-9_]+)\.(?P<object>.+)")

_POWER_UNITS: Final = frozenset({"W", "kW", "MW"})
_ENERGY_UNITS: Final = frozenset({"Wh", "kWh", "MWh"})
#: The units a role's entity may read in (`None`: any); an entity with no unit is not ruled out, except the level.
_ROLE_UNITS: Final[Mapping[str, frozenset[str] | None]] = {
    "soc": frozenset({"%"}),
    "capacity": _ENERGY_UNITS,
    "rated_charge": _POWER_UNITS,
    "rated_discharge": _POWER_UNITS,
    "firmware": None,
    "capacity_ah": frozenset({"Ah", "A"}),
    "charge_current": frozenset({"A"}),
    "discharge_current": frozenset({"A"}),
    "voltage": frozenset({"V"}),
    "charged_total": _ENERGY_UNITS,
    "discharged_total": _ENERGY_UNITS,
}

#: Reasons, in `MirrorMatch.reason`.
MATCHED: Final = "matched"
NO_BRAND_MATCH: Final = "no_brand_match"
BATTERY_NOT_POWER: Final = "battery_not_power"
AMBIGUOUS: Final = "ambiguous"


def remote_of(unique_id: str) -> str | None:
    """The remote's id part of a mirrored entity's unique id, `None` when it is not of the integration's shape."""
    found = _UNIQUE_ID.fullmatch(unique_id.lower())
    return None if found is None else found.group("remote")


def mirror_key(entity_id: str, unique_id: str, prefix: str = "") -> str:
    """The remote's own object id of a mirrored entity, lower case: the one its unique id carries (the entity id may
    be renamed here), else the entity id's, with the entry's `entity_prefix` taken off."""
    found = _UNIQUE_ID.fullmatch(unique_id.lower())
    key = found.group("object") if found is not None else entity_id.split(".", 1)[-1].lower()
    lowered = prefix.lower()
    if lowered and key.startswith(lowered):
        key = key[len(lowered):]
    return key


def _stem(key: str) -> str:
    """The first word of an object id: the device's name where the entity ids are `<device>_<entity>` (a hub
    named "SolaX 1" is `solax_1_...`, so its stem is `solax`). Mirrored entities have no device; this stands in, so
    the battery's own set is preferred where two sets match."""
    return key.split("_", 1)[0]


def translate(entities: Iterable[EntityFact], platform: str, prefix: str) -> list[EntityFact]:
    """The mirrored entities as the adapter of `platform` reads them: its platform, the remote's object id as the
    unique id, and the object id's first word as the device (`_stem`)."""
    out: list[EntityFact] = []
    for entity in entities:
        key = mirror_key(entity.entity_id, entity.unique_id, prefix)
        out.append(replace(
            entity, platform=platform, unique_id=key, device_id=f"mirror:{_stem(key)}", config_entry_id=None,
        ))
    return out


def _plausible(entity: EntityFact, units: frozenset[str] | None, role: str) -> bool:
    if role == "soc":
        return entity.unit == "%" or (entity.unit is None and entity.device_class == "battery")
    return units is None or entity.unit is None or entity.unit in units


def _roles(adapter: Adapter) -> list[tuple[str, Match]]:
    """Every entity the adapter reads or writes, by a name for the diagnostics."""
    roles: list[tuple[str, Match]] = [(role, match) for role, match in adapter.facts.items()]
    roles += [(f"mode:{rule.label}", rule.match) for rule in adapter.modes]
    roles += [(f"reserve:{index}", source.match) for index, source in enumerate(adapter.reserves)]
    roles += [(f"soc_pack:{index}", pack.soc) for index, pack in enumerate(adapter.soc_packs)]
    for name, spec in (("hold", adapter.hold), ("grid_charge", adapter.grid_charge)):
        if spec is not None and spec.match is not None:
            roles.append((f"control:{name}", spec.match))
    for name, match in (
        ("plant_max", adapter.plant_max), ("dc_power", adapter.dc_power), ("plant_load", adapter.plant_load_power),
        ("plant_pv", adapter.plant_pv_power), ("grid_status", None if adapter.grid_status is None else adapter.grid_status[0]),
    ):
        if match is not None:
            roles.append((name, match))
    return roles


def _manufacturers(value: str | tuple[str, ...] | None) -> tuple[str, ...]:
    return () if value is None else ((value,) if isinstance(value, str) else value)


def power_row_inverted(adapter: Adapter, key: str) -> bool | None:
    """Whether the brand's own battery power row that matches `key` negates it (discharge-positive), `None` when no
    row of the brand matches: the sign the brand's integration reports, for the diagnostics."""
    wanted = set(_manufacturers(adapter.manufacturer))
    for row in BATTERY_ROWS:
        if not set(row.platforms) & set(adapter.platforms):
            continue
        row_makers = set(_manufacturers(row.device_manufacturer))
        if row_makers and wanted and not row_makers & wanted:
            continue
        if row_makers and not wanted:
            continue
        if row.reject is not None and row.reject.search(key):
            continue
        if row.regex.search(key):
            return row.inverted
    return None


@dataclass(frozen=True, slots=True)
class MirrorMatch:
    """Which adapter reads a mirrored battery, and why (for the diagnostics)."""

    adapter: Adapter | None
    reason: str
    prefix: str = ""
    #: Each adapter that found the level, by brand: its score.
    scores: Mapping[str, int] = field(default_factory=dict)
    #: The chosen adapter's roles found, by name: the entity id.
    found: Mapping[str, str] = field(default_factory=dict)
    #: The entities it was found among (the battery's own device's, else the whole set): the ones read.
    pool: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "via": MIRROR_PLATFORM,
            "reason": self.reason,
            "brand": None if self.adapter is None else self.adapter.brand,
            "prefix": self.prefix or None,
            "scores": dict(sorted(self.scores.items(), key=lambda item: (-item[1], item[0]))),
            "found": dict(self.found),
        }


def _is_power(battery: EntityFact) -> bool:
    return battery.unit in _POWER_UNITS or (battery.unit is None and battery.device_class == "power")


#: The entity domains an adapter reads (`adapters.Match.domains`); the rest of a remote's set is not looked at.
_READ_DOMAINS: Final = frozenset({"sensor", "select", "number", "switch", "binary_sensor"})


def _score(battery: EntityFact, battery_key: str, pool: Sequence[EntityFact], prefix: str) -> list[tuple[int, Adapter, dict[str, str]]]:
    scored: list[tuple[int, Adapter, dict[str, str]]] = []
    for adapter in ADAPTERS:
        if adapter.package is not None:
            # A YAML package is found by its own fixed ids over shared platforms (`adapters.PackageScope`): not here.
            continue
        platform = adapter.platforms[0]
        translated = translate(pool, platform, prefix)
        own = translate([battery], platform, prefix)[0]
        found: dict[str, str] = {}
        for name, match in _roles(adapter):
            entity = find_entity(match, translated, own)
            if entity is not None and _plausible(entity, _ROLE_UNITS.get(name), name):
                found[name] = entity.entity_id
        if "soc" not in found and not any(name.startswith("soc_pack:") for name in found):
            continue
        score = len(found) + (1 if power_row_inverted(adapter, battery_key) is not None else 0)
        scored.append((score, adapter, found))
    return scored


def match_adapter(battery: EntityFact, entities: Sequence[EntityFact], prefix: str = "") -> MirrorMatch:
    """The read adapter of a mirrored battery (see the module); `battery` and `entities` as registered here (the
    remote's set, the battery among them or not). The entities whose object id starts as the battery's (`_stem`, the
    same device on the other instance) are tried first; only when no brand is found among them, the whole set."""
    if not _is_power(battery):
        return MirrorMatch(None, BATTERY_NOT_POWER, prefix)
    battery_key = mirror_key(battery.entity_id, battery.unique_id, prefix)
    readable = [entity for entity in entities if entity.domain in _READ_DOMAINS]
    stem = _stem(battery_key)
    near = [entity for entity in readable if _stem(mirror_key(entity.entity_id, entity.unique_id, prefix)) == stem]
    scored: list[tuple[int, Adapter, dict[str, str]]] = []
    pool: Sequence[EntityFact] = near
    if near:
        scored = _score(battery, battery_key, near, prefix)
    if not scored and len(near) < len(readable):
        pool = readable
        scored = _score(battery, battery_key, readable, prefix)
    if not scored:
        return MirrorMatch(None, NO_BRAND_MATCH, prefix)
    scored.sort(key=lambda item: (-item[0], item[1].brand))
    scores = {adapter.brand: score for score, adapter, _found in scored}
    best = scored[0][0]
    top = [adapter.brand for score, adapter, _found in scored if score == best]
    if len(top) > 1:
        return MirrorMatch(None, f"{AMBIGUOUS}:{','.join(sorted(top))}", prefix, scores)
    return MirrorMatch(
        scored[0][1], MATCHED, prefix, scores, scored[0][2], tuple(sorted(entity.entity_id for entity in pool)),
    )


def mirrored_facts(
    battery: EntityFact,
    entities: Sequence[EntityFact],
    *,
    prefix: str = "",
    power_kw: float | None = None,
    power_inverted: bool | None = None,
    external_planner: str | None = None,
    match: MirrorMatch | None = None,
) -> BatteryFacts:
    """The facts of a mirrored battery, read only. `match`: an earlier `match_adapter` of the same set, else made
    here. `power_inverted`: the site's own sign setting, compared with the brand's (`power_row_inverted`)."""
    match = match or match_adapter(battery, entities, prefix)
    detail = match.as_dict()
    adapter = match.adapter
    if adapter is None:
        facts = battery_facts(battery, [], power_kw=power_kw, external_planner=external_planner)
        return replace(facts, read_only=READ_ONLY_MIRRORED, mirror=detail)
    platform = adapter.platforms[0]
    own = translate([battery], platform, prefix)[0]
    pool = set(match.pool)
    translated = translate((entity for entity in entities if entity.entity_id in pool), platform, prefix)
    facts = battery_facts(
        own, translated, adapter=adapter, power_kw=power_kw, external_planner=external_planner,
    )
    brand_inverted = power_row_inverted(adapter, own.unique_id)
    if brand_inverted is None or power_inverted is None:
        detail["power_sign"] = "unknown"
    else:
        detail["power_sign"] = "as_brand" if brand_inverted == power_inverted else "differs_from_brand"
    missing = [role for role in ("soc", "capacity", "rated_charge", "rated_discharge") if role not in facts.sources]
    # The size and the ratings are learnt where they are not read (`learned.py`); the firmware is never mirrored.
    detail["missing"] = missing
    detail["learnt_if_missing"] = [role for role in missing if role != "soc"]
    return replace(
        facts,
        integration=MIRROR_PLATFORM,
        read_only=READ_ONLY_MIRRORED,
        mirror=detail,
        hold=None,
        grid_charge=None,
        preheat=None,
    )
