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
the battery entity reads a power.

Only a brand's own keys name the brand. A key that many integrations share (`battery_soc`, `battery_power`,
`battery_voltage`: `_GENERIC_KEY`) or one that another adapter finds too is the brand's evidence for nothing; such
keys found SolaX's level under Solinteg's name on a SolaX whose other instance was not on solax_modbus's own keys.
The brand is the one with the most keys of its own (`specific` in the diagnostics), taken only when it has more than
every other (else `ambiguous:<brands>`). When no adapter has a key of its own, no brand is named (`generic_keys`):
the shared keys are still read (the level, the voltage, a counter), under `GENERIC_LABEL`. Such a battery's size
and ratings are then also looked for by names many integrations give them (`_GENERIC_ROLES`, on the battery's own stem
only, by unit): read after the brand is decided, they are never a brand's evidence. A size in Ah and currents in A are
turned into kWh and kW by the voltage read, as for Victron and SolaX (`facts.battery_facts`); a value that reads
nothing, 0 or beyond `_SANE` is left to be learnt.

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
from .facts import EST_CAPACITY_FROM_AH, EST_RATED_FROM_CURRENT, BatteryFacts, EntityFact, battery_facts, find_entity

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
GENERIC_KEYS: Final = "generic_keys"

#: The label of a mirrored battery whose brand its keys do not tell (`GENERIC_KEYS`).
GENERIC_LABEL: Final = "Mirrored battery (Remote Home-Assistant)"

#: An object id that ends in a name many battery integrations give their entities: it names no brand.
_GENERIC_KEY: Final = re.compile(
    r"(?:^|_)(?:battery_)?(?:soc|state_of_charge|level|power|voltage|current|temperature|charge_power"
    r"|discharge_power)$|(?:^|_)battery$"
)


#: A generic mirrored battery's size and ratings by names many integrations give them, each role's keys in order of
#: preference with the units it is read in (a unit is required). `battery_capacity` is a size only in energy units:
#: in solax_modbus it is the level (%). solax_modbus's `battery_charge_max_current` is a person's setting that
#: defaults to 20 A and says nothing of the battery, so it is not read.
_CAPACITY_KEYS: Final = (
    "battery_install_capacity", "battery_system_installed_capacity", "battery_installed_capacity",
    "battery_capacity_kwh", "battery_rated_capacity", "battery_nominal_capacity", "battery_total_capacity",
    "battery_capacity",
)
_GENERIC_ROLES: Final[Mapping[str, tuple[tuple[str, ...], frozenset[str]]]] = {
    "capacity": (_CAPACITY_KEYS, _ENERGY_UNITS),
    "capacity_ah": (("battery_capacity_ah", *_CAPACITY_KEYS), frozenset({"Ah"})),
    "rated_charge": (
        ("battery_max_charge_power", "max_charge_power", "battery_max_charging_power", "max_charging_power",
         "battery_rated_charge_power", "rated_charge_power"),
        _POWER_UNITS,
    ),
    "rated_discharge": (
        ("battery_max_discharge_power", "max_discharge_power", "battery_max_discharging_power",
         "max_discharging_power", "battery_rated_discharge_power", "rated_discharge_power"),
        _POWER_UNITS,
    ),
    "charge_current": (
        ("battery_max_charge_current", "max_charge_current", "bms_charge_max_current", "bms_max_charge_current",
         "battery_max_charging_current", "max_charging_current", "battery_charge_current_limit"),
        frozenset({"A"}),
    ),
    "discharge_current": (
        ("battery_max_discharge_current", "max_discharge_current", "bms_discharge_max_current",
         "bms_max_discharge_current", "battery_max_discharging_current", "max_discharging_current",
         "battery_discharge_current_limit"),
        frozenset({"A"}),
    ),
    "voltage": (("battery_voltage", "bms_battery_voltage", "battery_voltage_charge", "battery_1_voltage_charge"),
                frozenset({"V"})),
}
_GENERIC_DOMAINS: Final = ("sensor", "number")
#: The range a generic size (kWh) and rating (kW) is taken in; beyond it the reading is something else, and learnt.
_SANE: Final[Mapping[str, tuple[float, float]]] = {
    "capacity": (0.5, 500.0), "rated_charge": (0.1, 100.0), "rated_discharge": (0.1, 100.0),
}


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
    #: Each adapter that found the level, by brand: how many of its finds are its own (not shared, not generic).
    specific: Mapping[str, int] = field(default_factory=dict)
    #: For `GENERIC_KEYS`: the brandless adapter that reads the shared keys found (`_generic_reader`).
    reader: Adapter | None = None
    #: For `GENERIC_KEYS`: the size and rating roles found by common names (`_GENERIC_ROLES`), not by any adapter.
    generic_roles: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "via": MIRROR_PLATFORM,
            "reason": self.reason,
            "brand": None if self.adapter is None else self.adapter.brand,
            "prefix": self.prefix or None,
            "scores": dict(sorted(self.scores.items(), key=lambda item: (-item[1], item[0]))),
            "specific": dict(sorted(self.specific.items(), key=lambda item: (-item[1], item[0]))),
            "found": dict(self.found),
        } | ({"generic_roles": list(self.generic_roles)} if self.reason == GENERIC_KEYS else {})


def _is_power(battery: EntityFact) -> bool:
    return battery.unit in _POWER_UNITS or (battery.unit is None and battery.device_class == "power")


#: The entity domains an adapter reads (`adapters.Match.domains`); the rest of a remote's set is not looked at.
_READ_DOMAINS: Final = frozenset({"sensor", "select", "number", "switch", "binary_sensor"})


@dataclass(frozen=True, slots=True)
class _Scored:
    adapter: Adapter
    found: dict[str, str]
    #: The brand's own battery power row matches the battery entity's key.
    row: bool

    @property
    def has_level(self) -> bool:
        return "soc" in self.found or any(name.startswith("soc_pack:") for name in self.found)

    @property
    def score(self) -> int:
        return len(self.found) + (1 if self.row else 0)


def _generic(key: str) -> bool:
    return _GENERIC_KEY.search(key) is not None


def _score(battery: EntityFact, battery_key: str, pool: Sequence[EntityFact], prefix: str) -> list[_Scored]:
    """Every adapter that finds something in `pool`, with or without the level."""
    scored: list[_Scored] = []
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
        row = power_row_inverted(adapter, battery_key) is not None
        if found or row:
            scored.append(_Scored(adapter, found, row))
    return scored


def _specific(scored: Sequence[_Scored], battery_key: str, keys: Mapping[str, str]) -> dict[str, int]:
    """Each adapter with the level, by brand: its finds that are its own, an entity no other adapter finds and
    whose key is not a generic one (`_GENERIC_KEY`), and its power row when no other's matches the key either."""
    finders: dict[str, int] = {}
    for item in scored:
        for entity_id in set(item.found.values()):
            finders[entity_id] = finders.get(entity_id, 0) + 1
    rows = sum(1 for item in scored if item.row)
    out: dict[str, int] = {}
    for item in scored:
        if not item.has_level:
            continue
        own = {
            entity_id for entity_id in item.found.values()
            if finders[entity_id] == 1 and not _generic(keys.get(entity_id, entity_id))
        }
        out[item.adapter.brand] = len(own) + (1 if item.row and rows == 1 and not _generic(battery_key) else 0)
    return out


def _generic_roles(
    battery_key: str, pool: Sequence[EntityFact], prefix: str, taken: Mapping[str, str]
) -> dict[str, tuple[str, str]]:
    """The size and rating roles `taken` lacks, found among the entities of the battery's stem by the common names of
    `_GENERIC_ROLES` and their unit: by role, the entity id and the remote's key. Units only, never the value (a
    match is kept while the set's shape is; the value is checked each time it is read)."""
    stem = _stem(battery_key)
    keyed: dict[str, EntityFact] = {}
    for entity in pool:
        key = mirror_key(entity.entity_id, entity.unique_id, prefix)
        if entity.domain in _GENERIC_DOMAINS and _stem(key) == stem:
            keyed[key] = entity
    out: dict[str, tuple[str, str]] = {}
    for role, (names, units) in _GENERIC_ROLES.items():
        if role in taken:
            continue
        for name in names:
            hit = next(
                (key for key, entity in sorted(keyed.items()) if key.endswith(f"_{name}") and entity.unit in units),
                None,
            )
            if hit is not None:
                out[role] = (keyed[hit].entity_id, hit)
                break
    return out


def _generic_reader(item: _Scored, extra: Mapping[str, tuple[str, str]]) -> Adapter:
    """A brandless adapter that reads only the facts `item` found by shared keys (the level, the voltage, a
    counter) and the size and ratings found by common names (`extra`, `_generic_roles`): no modes, no controls, no
    plant entities, nothing that is a brand's behaviour."""
    facts: dict[Any, Match] = {role: match for role, match in item.adapter.facts.items() if role in item.found}
    for role, (_entity_id, key) in extra.items():
        facts[role] = Match(_GENERIC_DOMAINS, re.compile(rf"^{re.escape(key)}$"))
    return Adapter(brand=GENERIC_KEYS, label=GENERIC_LABEL, platforms=item.adapter.platforms[:1], facts=facts)


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
    scored: list[_Scored] = []
    pool: Sequence[EntityFact] = near
    if near:
        scored = _score(battery, battery_key, near, prefix)
    if not any(item.has_level for item in scored) and len(near) < len(readable):
        pool = readable
        scored = _score(battery, battery_key, readable, prefix)
    leveled = sorted((item for item in scored if item.has_level), key=lambda item: (-item.score, item.adapter.brand))
    if not leveled:
        return MirrorMatch(None, NO_BRAND_MATCH, prefix)
    keys = {entity.entity_id: mirror_key(entity.entity_id, entity.unique_id, prefix) for entity in pool}
    scores = {item.adapter.brand: item.score for item in leveled}
    specific = _specific(scored, battery_key, keys)
    pool_ids = tuple(sorted(entity.entity_id for entity in pool))
    best = max(specific.values())
    if best == 0:
        # Only keys many brands share: the readings are kept, the brand is not named.
        found = {role: entity_id for role, entity_id in leveled[0].found.items() if role in leveled[0].adapter.facts}
        extra = _generic_roles(battery_key, readable, prefix, found)
        reader = _generic_reader(leveled[0], extra)
        found |= {role: entity_id for role, (entity_id, _key) in extra.items()}
        return MirrorMatch(
            None, GENERIC_KEYS, prefix, scores, found, pool_ids, specific, reader, tuple(sorted(extra)),
        )
    top = sorted(brand for brand, count in specific.items() if count == best)
    if len(top) > 1:
        return MirrorMatch(None, f"{AMBIGUOUS}:{','.join(top)}", prefix, scores, specific=specific)
    chosen = next(item for item in leveled if item.adapter.brand == top[0])
    return MirrorMatch(chosen.adapter, MATCHED, prefix, scores, chosen.found, pool_ids, specific)


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
    if adapter is None and match.reader is not None:
        return _generic_facts(battery, entities, match, detail, power_kw, external_planner)
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
    _missing(facts, detail)
    return replace(
        facts,
        integration=MIRROR_PLATFORM,
        read_only=READ_ONLY_MIRRORED,
        mirror=detail,
        hold=None,
        grid_charge=None,
        preheat=None,
    )


def _missing(facts: BatteryFacts, detail: dict[str, Any]) -> None:
    missing = [role for role in ("soc", "capacity", "rated_charge", "rated_discharge") if role not in facts.sources]
    # The size and the ratings are learnt where they are not read (`learned.py`); the firmware is never mirrored.
    detail["missing"] = missing
    detail["learnt_if_missing"] = [role for role in missing if role != "soc"]


def _generic_facts(
    battery: EntityFact,
    entities: Sequence[EntityFact],
    match: MirrorMatch,
    detail: dict[str, Any],
    power_kw: float | None,
    external_planner: str | None,
) -> BatteryFacts:
    """A mirrored battery whose keys name no brand (`GENERIC_KEYS`): the shared keys read, no brand, its mode
    unknown as for a battery SpotNav has no adapter for, read only."""
    reader = match.reader
    assert reader is not None
    platform = reader.platforms[0]
    own = translate([battery], platform, match.prefix)[0]
    pool = set(match.found.values())
    translated = translate((entity for entity in entities if entity.entity_id in pool), platform, match.prefix)
    read = _sane(battery_facts(own, translated, adapter=reader, power_kw=power_kw))
    unknown = battery_facts(battery, [], power_kw=power_kw, external_planner=external_planner)
    detail["power_sign"] = "unknown"
    _missing(read, detail)
    return replace(
        read,
        integration=MIRROR_PLATFORM,
        brand=None,
        label=GENERIC_LABEL,
        # A generic charge current may be a setting, not the BMS' present limit; and nothing here is controlled.
        bms_charge_kw=None,
        battery_entity_id=battery.entity_id,
        management=unknown.management,
        read_only=READ_ONLY_MIRRORED,
        mirror=detail,
        hold=None,
        grid_charge=None,
        preheat=None,
    )


def _sane(facts: BatteryFacts) -> BatteryFacts:
    """A generic size or rating that reads nothing (0 included) or beyond `_SANE` dropped, with its source and
    estimate, so it is named missing and learnt instead."""
    changes: dict[str, Any] = {}
    sources = dict(facts.sources)
    for role, field_name in (
        ("capacity", "capacity_kwh"), ("rated_charge", "rated_charge_kw"), ("rated_discharge", "rated_discharge_kw"),
    ):
        value = getattr(facts, field_name)
        low, high = _SANE[role]
        if value is None or not low <= value <= high:
            if value is not None:
                changes[field_name] = None
            sources.pop(role, None)
    if not changes and sources == facts.sources:
        return facts
    derived = [source for role, source in sources.items() if role in ("rated_charge", "rated_discharge") and " x " in source]
    estimates = tuple(
        estimate for estimate in facts.estimates
        if not (estimate == EST_CAPACITY_FROM_AH and "capacity" not in sources)
        and not (estimate == EST_RATED_FROM_CURRENT and not derived)
    )
    return replace(facts, sources=sources, estimates=estimates, **changes)
