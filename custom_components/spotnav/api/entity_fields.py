"""Pure validation and field-description functions for a charger's and a site's entity choices,
shared by the options flow and the entity-config WebSocket commands (`api/entity_config.py`) so
they cannot validate differently.

Nothing here talks to the websocket layer or writes a config entry; every function reads loaded
Home Assistant state plus config data the caller hands it. `api/entity_config.py` turns a failure
into a wire refusal and a pass into a write.

* Charger fields (`CHARGER_FIELDS`): `charge_control` (`switch`, required), `current_limit`
  (`number`) and `energy_register_entity` (`sensor`). Their only server-side check is the
  cross-entry duplicate-target guard in `vehicles/entity_conflicts.py`. `vehicle_soc` is reported
  read-only: it is resolved system-wide per device (`vehicles/vehicle_discovery.py`), not stored in
  the charger's entry, so `update_entity_config` always refuses a write to it.
* Site fields (`SITE_FIELDS`): main fuse (A), measurement mode (`direct|derived`), the per-phase
  meters each mode needs (three `direct_L{n}`, or per phase in derived mode the required
  `derived_L{n}_{power,voltage}` and the optional `_power_export` (the export half of an import/export
  pair), `_reactive_power`, `_apparent_power` and `_current`; without the last three the fuse check
  estimates the current from power), the meter's total grid power (`grid_power_source_power` and the
  optional `grid_power_source_power_export`, the export half of an import/export pair; it is what
  solar and hybrid read on a direct site), the sign options (`site_current_signed`, `grid_power_inverted`,
  `battery_power_inverted`), the battery aggregate power sensor with its optional discharge half, and
  the maximum measurement age. `apply_detection` (write only) applies a detected meter or battery
  (`site/site_detection.py`) chosen by id; the server recomputes the detection and never takes a mapping
  from the caller. Not included: the
  generic current source (`CONF_SITE_CURRENT_SOURCE`, a wizard), membership, active control's
  opt-in and the regulator/yield/solar/hybrid settings (`api/site_settings.py`). Numeric bounds
  (`MAIN_FUSE_MIN_A`, `MAX_AGE_MIN_S`) are shared with the flow's schema. Entity existence and
  domain are checked here only; the interactive flow leaves that to its picker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import (
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_BATTERY_DISCHARGE_POWER_ENTITY,
    CONF_BATTERY_POWER_INVERTED,
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_ENTRY_IDS,
    CONF_CHARGER_PLATFORM,
    CONF_CONTROL_PATH,
    CONF_CURRENT_LIMIT,
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_GRID_POWER_INVERTED,
    CONF_GRID_POWER_SOURCE,
    CONF_MAIN_FUSE_A,
    CONF_MAX_AGE_S,
    CONF_MEASUREMENT_MODE,
    CONF_SITE_CURRENT_SIGNED,
    CONF_SITE_CURRENT_SOURCE,
    CONF_MODE,
    DEFAULT_MAX_AGE_S,
    DOMAIN,
    MEASUREMENT_MODE_DERIVED,
    MEASUREMENT_MODE_DIRECT,
    MODE_DETECTED,
)
from ..runtime import controller_for, site_controller_for
from ..site.site_detection import (
    apply_meter_candidate,
    BatteryCandidate,
    detect_site_from_hass,
    Detection,
    excluded_charger_devices,
    find_own_load_balancing_from_hass,
    freshness_warnings,
    MeterCandidate,
)
from ..site.measurement_source import grid_power_source_from_dict, source_from_dict
from ..execution.charger_entities import (
    charger_entries,
    CONFLICT_DISABLED,
    CONFLICT_OWN_MODE,
    control_path_for_entity,
    disabled_switches,
    own_mode_conflicts,
)
from ..execution.chargers.registry import external_balancer
from ..execution.charger_profiles import PATH_EASEE, profile_for
from ..vehicles.entity_conflicts import conflict_errors
from ..vehicles.vehicle_discovery import discover_vehicles, soc_choices, vehicle_soc_entity_id


Scope = Literal["charger", "site"]

#: The three phases every per-phase field is named after, in display order.
PHASES: Final = ("L1", "L2", "L3")
#: The readings derived mode takes per phase: `power` (signed, or the import half of a pair) and
#: `voltage` are required; the rest are optional and only sharpen the fuse check.
DERIVED_SUBFIELDS: Final = ("power", "voltage", "power_export", "reactive_power", "apparent_power", "current")
DERIVED_REQUIRED_SUBFIELDS: Final = ("power", "voltage")

FIELD_CHARGE_CONTROL: Final = "charge_control"
FIELD_CURRENT_LIMIT: Final = "current_limit"
FIELD_ENERGY_REGISTER: Final = "energy_register_entity"
#: Read-only here; see the module docstring.
FIELD_VEHICLE_SOC: Final = "vehicle_soc"
CHARGER_FIELDS: Final = (FIELD_CHARGE_CONTROL, FIELD_CURRENT_LIMIT, FIELD_ENERGY_REGISTER, FIELD_VEHICLE_SOC)
FIELD_MAIN_FUSE_A: Final = "main_fuse_a"
FIELD_MEASUREMENT_MODE: Final = "measurement_mode"
FIELD_MAX_AGE_S: Final = "max_age_s"
FIELD_BATTERY_AGGREGATE_POWER: Final = "battery_aggregate_power_entity"
FIELD_BATTERY_DISCHARGE_POWER: Final = "battery_discharge_power_entity"
#: The meter's total grid power (`CONF_GRID_POWER_SOURCE`): one signed entity, or with the second field
#: an import/export pair (import minus export). Solar and hybrid read it on a direct site.
FIELD_GRID_POWER_SOURCE_POWER: Final = "grid_power_source_power"
FIELD_GRID_POWER_SOURCE_EXPORT: Final = "grid_power_source_power_export"
GRID_POWER_SOURCE_FIELDS: Final = (FIELD_GRID_POWER_SOURCE_POWER, FIELD_GRID_POWER_SOURCE_EXPORT)
FIELD_SITE_CURRENT_SIGNED: Final = "site_current_signed"
FIELD_GRID_POWER_INVERTED: Final = "grid_power_inverted"
FIELD_BATTERY_POWER_INVERTED: Final = "battery_power_inverted"
#: Write only: a detected meter's or battery's id, applied by the server.
FIELD_APPLY_DETECTION: Final = "apply_detection"
#: Boolean site options and the config key each one is stored under.
FLAG_FIELDS: Final = {
    FIELD_SITE_CURRENT_SIGNED: CONF_SITE_CURRENT_SIGNED,
    FIELD_GRID_POWER_INVERTED: CONF_GRID_POWER_INVERTED,
    FIELD_BATTERY_POWER_INVERTED: CONF_BATTERY_POWER_INVERTED,
}


def direct_field(phase: str) -> str:
    """The `changes`/`fields` key for one direct-mode phase's current sensor."""
    return f"direct_{phase}"


def derived_field(phase: str, sub: str) -> str:
    """The `changes`/`fields` key for one derived-mode phase's power/reactive_power/voltage
    sensor."""
    return f"derived_{phase}_{sub}"


def site_fixed_fields() -> tuple[str, ...]:
    """Every site field name that is not phase-shaped -- always present, whatever the mode."""
    return (
        FIELD_MAIN_FUSE_A,
        FIELD_MEASUREMENT_MODE,
        FIELD_MAX_AGE_S,
        FIELD_BATTERY_AGGREGATE_POWER,
        FIELD_BATTERY_DISCHARGE_POWER,
        *GRID_POWER_SOURCE_FIELDS,
        *FLAG_FIELDS,
        FIELD_APPLY_DETECTION,
    )


def direct_fields() -> tuple[str, ...]:
    return tuple(direct_field(phase) for phase in PHASES)


def derived_fields() -> tuple[str, ...]:
    return tuple(derived_field(phase, sub) for phase in PHASES for sub in DERIVED_SUBFIELDS)


def required_derived_fields() -> tuple[str, ...]:
    return tuple(derived_field(phase, sub) for phase in PHASES for sub in DERIVED_REQUIRED_SUBFIELDS)


#: Optional per-phase fields: clearing one is allowed.
_OPTIONAL_PHASE_FIELDS: Final = frozenset(
    derived_field(phase, sub) for phase in PHASES for sub in DERIVED_SUBFIELDS if sub not in DERIVED_REQUIRED_SUBFIELDS
)


# Shared with the flow's `vol.Range` on these two fields so the numbers cannot drift.
MAIN_FUSE_MIN_A: Final = 0.1
MAX_AGE_MIN_S: Final = 1.0

# Stable per-field codes, looked up by the card, never shown as prose.
ERR_REQUIRED: Final = "required"
ERR_ENTITY_NOT_FOUND: Final = "entity_not_found"
ERR_WRONG_DOMAIN: Final = "wrong_domain"
ERR_INVALID_VALUE: Final = "invalid_value"
ERR_NOT_WRITABLE: Final = "not_writable"
#: A detected charger's new charge control cannot be told how to start and stop (see
#: `execution/charger_entities.control_path_for_entity`).
ERR_CONTROL_PATH_UNKNOWN: Final = "control_path_unknown"
ERR_UNKNOWN_FIELD: Final = "unknown_field"
ERR_UNKNOWN_VEHICLE: Final = "unknown_vehicle"
# Re-exported so a caller never has to import `entity_conflicts` just to compare a code.
ERR_CHARGE_CONTROL_IN_USE: Final = "charge_control_in_use"
ERR_CURRENT_LIMIT_IN_USE: Final = "current_limit_in_use"

_ENTITY_DOMAIN: Final[dict[str, str]] = {
    FIELD_CHARGE_CONTROL: "switch",
    FIELD_CURRENT_LIMIT: "number",
    FIELD_ENERGY_REGISTER: "sensor",
    FIELD_VEHICLE_SOC: "sensor",
    FIELD_BATTERY_AGGREGATE_POWER: "sensor",
    FIELD_BATTERY_DISCHARGE_POWER: "sensor",
    FIELD_GRID_POWER_SOURCE_POWER: "sensor",
    FIELD_GRID_POWER_SOURCE_EXPORT: "sensor",
}
for _phase in PHASES:
    _ENTITY_DOMAIN[direct_field(_phase)] = "sensor"
    for _sub in DERIVED_SUBFIELDS:
        _ENTITY_DOMAIN[derived_field(_phase, _sub)] = "sensor"
del _phase, _sub

#: Device classes offered to the picker; metadata only, not re-checked on submission.
_ENTITY_DEVICE_CLASSES: Final[dict[str, tuple[str, ...]]] = {
    FIELD_CHARGE_CONTROL: (),
    FIELD_CURRENT_LIMIT: (),
    FIELD_ENERGY_REGISTER: ("energy",),
    FIELD_VEHICLE_SOC: ("battery",),
    FIELD_BATTERY_AGGREGATE_POWER: ("power",),
    FIELD_BATTERY_DISCHARGE_POWER: ("power",),
    FIELD_GRID_POWER_SOURCE_POWER: ("power",),
    FIELD_GRID_POWER_SOURCE_EXPORT: ("power",),
}
for _phase in PHASES:
    _ENTITY_DEVICE_CLASSES[direct_field(_phase)] = ("current",)
    _ENTITY_DEVICE_CLASSES[derived_field(_phase, "power")] = ("power",)
    _ENTITY_DEVICE_CLASSES[derived_field(_phase, "power_export")] = ("power",)
    _ENTITY_DEVICE_CLASSES[derived_field(_phase, "reactive_power")] = ("reactive_power",)
    _ENTITY_DEVICE_CLASSES[derived_field(_phase, "apparent_power")] = ("apparent_power",)
    _ENTITY_DEVICE_CLASSES[derived_field(_phase, "current")] = ("current",)
    _ENTITY_DEVICE_CLASSES[derived_field(_phase, "voltage")] = ("voltage",)
del _phase


@dataclass(frozen=True, slots=True)
class FieldError:
    """One field's refusal: a stable code, never prose."""

    field: str
    code: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code}


#: A detected charger's charge control may be a select, a button or (Easee) the status sensor that
#: identifies it; every other charger's is a switch.
DETECTED_CHARGE_CONTROL_DOMAINS: Final = ("switch", "select", "button", "sensor")


def charge_control_is_fixed(entry: ConfigEntry) -> bool:
    """Whether the charger's start and stop is not an entity at all: Easee is paused and resumed
    through its own `action_command` service, and the entity stored as `charge_control` (its status
    sensor) only identifies the charger. Choosing another entity there could never change how it is
    started, so the field is read-only for it.
    """
    path = entry.data.get(CONF_CONTROL_PATH)
    return (
        entry.data.get(CONF_MODE) == MODE_DETECTED and isinstance(path, dict) and path.get("kind") == PATH_EASEE
    )


def _charge_control_domains(entry: ConfigEntry) -> tuple[str, ...]:
    if entry.data.get(CONF_MODE) == MODE_DETECTED:
        return DETECTED_CHARGE_CONTROL_DOMAINS
    return (_ENTITY_DOMAIN[FIELD_CHARGE_CONTROL],)


def _entity_ref(
    hass: HomeAssistant,
    field: str,
    entity_id: Any,
    domains: tuple[str, ...] | None = None,
) -> tuple[tuple[str, str] | None, FieldError | None]:
    """`((entity_id, friendly_name), None)` for a submitted value that exists, is enabled and has the
    required domain, or `(None, FieldError)`. Device class and unit are not checked.
    """
    if not isinstance(entity_id, str) or not entity_id:
        return None, FieldError(field, ERR_ENTITY_NOT_FOUND)
    registry_entry = er.async_get(hass).async_get(entity_id)
    if registry_entry is None:
        return None, FieldError(field, ERR_ENTITY_NOT_FOUND)
    if registry_entry.domain not in (domains or (_ENTITY_DOMAIN[field],)):
        return None, FieldError(field, ERR_WRONG_DOMAIN)
    state = hass.states.get(entity_id)
    friendly_name = state.name if state is not None else entity_id
    return (entity_id, friendly_name), None


def _current_entity_value(hass: HomeAssistant, entity_id: str | None) -> dict[str, Any] | None:
    """The `{"entity_id", "friendly_name", "exists"}` object `get_entity_config` reports for a stored
    entity id, or `None` for "nothing configured".

    A stored id that no longer resolves reports itself with `exists: false`. An entity that is merely
    unavailable (it has a state or is registered) still counts as existing.
    """
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    registered = er.async_get(hass).async_get(entity_id) is not None
    return {
        "entity_id": entity_id,
        "friendly_name": state.name if state is not None else entity_id,
        "exists": state is not None or registered,
    }


SOURCE_CONFIGURED: Final = "configured"
SOURCE_AUTOMATIC: Final = "automatic"


def _effective_value(hass: HomeAssistant, entity_id: str | None, source: str) -> dict[str, str] | None:
    """`get_entity_config`'s `effective` object: the entity actually read, its friendly name and where
    the choice came from, or `None`.
    """
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    return {
        "entity_id": entity_id,
        "friendly_name": state.name if state is not None else entity_id,
        "source": source,
    }


def _entity_exists(hass: HomeAssistant, entity_id: str) -> bool:
    return hass.states.get(entity_id) is not None or er.async_get(hass).async_get(entity_id) is not None


def _charger_effective(hass: HomeAssistant, entry: ConfigEntry, values: dict[str, str]) -> dict[str, dict[str, str] | None]:
    """The entity each charger field really resolves to, asked of the loaded controller
    (`ChargingController.energy_register_entity_id`, `.ocpp_controls`). Read-only.

    * `current_limit`: the configured entity while it exists, else the OCPP 0.12 session limit.
    * `energy_register_entity`: the configured one, else the controller's auto-resolved register.
    """
    controller = controller_for(hass, entry.entry_id)
    effective: dict[str, dict[str, str] | None] = {
        FIELD_CHARGE_CONTROL: _effective_value(hass, values[FIELD_CHARGE_CONTROL], SOURCE_CONFIGURED),
    }

    limit = values[FIELD_CURRENT_LIMIT]
    if limit and _entity_exists(hass, limit):
        effective[FIELD_CURRENT_LIMIT] = _effective_value(hass, limit, SOURCE_CONFIGURED)
    else:
        controls = getattr(controller, "ocpp_controls", None)
        effective[FIELD_CURRENT_LIMIT] = _effective_value(
            hass, getattr(controls, "session_limit_entity", None), SOURCE_AUTOMATIC
        )

    register = values[FIELD_ENERGY_REGISTER]
    if register:
        effective[FIELD_ENERGY_REGISTER] = _effective_value(hass, register, SOURCE_CONFIGURED)
    else:
        effective[FIELD_ENERGY_REGISTER] = _effective_value(
            hass, getattr(controller, "energy_register_entity_id", None), SOURCE_AUTOMATIC
        )
    return effective



def validate_charger_entities(
    hass: HomeAssistant,
    *,
    charge_control: str,
    current_limit: str | None,
    exclude_entry_id: str | None = None,
) -> dict[str, str]:
    """The cross-entry duplicate-target guard for a charger's charge-control switch and current-limit
    number (`entity_conflicts.conflict_errors`).

    Returns `{field: code}` (`ERR_CHARGE_CONTROL_IN_USE` / `ERR_CURRENT_LIMIT_IN_USE`) or `{}`.
    """
    return conflict_errors(
        hass,
        charge_control=charge_control,
        current_limit=current_limit,
        exclude_entry_id=exclude_entry_id,
    )


def current_charger_values(entry: ConfigEntry) -> dict[str, str]:
    """The three writable charger fields' stored values, `""` for "not configured" (as `entry.data`)."""
    return {
        FIELD_CHARGE_CONTROL: entry.data.get(CONF_CHARGE_CONTROL) or "",
        FIELD_CURRENT_LIMIT: entry.data.get(CONF_CURRENT_LIMIT) or "",
        FIELD_ENERGY_REGISTER: entry.data.get(CONF_ENERGY_REGISTER_ENTITY) or "",
    }


def charger_field_errors(
    hass: HomeAssistant,
    *,
    entry: ConfigEntry,
    changes: dict[str, Any],
) -> list[FieldError]:
    """Every field error one charger's proposed `changes` produces.

    Each touched field is checked for existence and domain. `charge_control` is required (empty is
    `ERR_REQUIRED`); the other two are optional and a falsy value clears them. Naming `vehicle_soc`
    fails with `ERR_NOT_WRITABLE`. The duplicate guard runs once over the resulting
    charge_control/current_limit pair, only when neither failed its own check.
    """
    errors: list[FieldError] = []
    resolved: dict[str, str] = {}
    for field in (FIELD_CHARGE_CONTROL, FIELD_CURRENT_LIMIT, FIELD_ENERGY_REGISTER):
        if field not in changes:
            continue
        value = changes[field]
        if (
            field == FIELD_CHARGE_CONTROL
            and charge_control_is_fixed(entry)
            and value != current_charger_values(entry)[FIELD_CHARGE_CONTROL]
        ):
            errors.append(FieldError(field, ERR_NOT_WRITABLE))
            continue
        if not value:
            if field == FIELD_CHARGE_CONTROL:
                errors.append(FieldError(field, ERR_REQUIRED))
            else:
                resolved[field] = ""
            continue
        ref, error = _entity_ref(
            hass,
            field,
            value,
            _charge_control_domains(entry) if field == FIELD_CHARGE_CONTROL else None,
        )
        if error is not None:
            errors.append(error)
        else:
            resolved[field] = ref[0]

    if FIELD_VEHICLE_SOC in changes:
        errors.append(FieldError(FIELD_VEHICLE_SOC, ERR_NOT_WRITABLE))

    if (
        FIELD_CHARGE_CONTROL in resolved
        and entry.data.get(CONF_MODE) == MODE_DETECTED
        and detected_control_path(hass, entry, resolved[FIELD_CHARGE_CONTROL]) is None
    ):
        # The entity exists but nothing says how to start a charge with it.
        errors.append(FieldError(FIELD_CHARGE_CONTROL, ERR_CONTROL_PATH_UNKNOWN))

    failed_fields = {error.field for error in errors}
    if (FIELD_CHARGE_CONTROL in changes or FIELD_CURRENT_LIMIT in changes) and not (
        FIELD_CHARGE_CONTROL in failed_fields or FIELD_CURRENT_LIMIT in failed_fields
    ):
        current = current_charger_values(entry)
        charge_control = resolved.get(FIELD_CHARGE_CONTROL, current[FIELD_CHARGE_CONTROL])
        current_limit = resolved.get(FIELD_CURRENT_LIMIT, current[FIELD_CURRENT_LIMIT]) or None
        conflicts = validate_charger_entities(
            hass,
            charge_control=charge_control,
            current_limit=current_limit,
            exclude_entry_id=entry.entry_id,
        )
        errors.extend(FieldError(field, code) for field, code in conflicts.items())
    return errors


def charger_field_descriptors(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    """`get_entity_config`'s `fields` entries for one charger, all scoped `"charger"`: `charge_control`,
    `current_limit`, `energy_register_entity`, `vehicle_soc`.
    """
    values = current_charger_values(entry)
    effective = _charger_effective(hass, entry, values)
    return [
        _entity_field_descriptor(
            hass,
            field=FIELD_CHARGE_CONTROL,
            scope="charger",
            required=True,
            writable=not charge_control_is_fixed(entry),
            current_entity_id=values[FIELD_CHARGE_CONTROL] or None,
            effective=effective[FIELD_CHARGE_CONTROL],
            domains=_charge_control_domains(entry),
        ),
        _entity_field_descriptor(
            hass,
            field=FIELD_CURRENT_LIMIT,
            scope="charger",
            required=False,
            writable=True,
            current_entity_id=values[FIELD_CURRENT_LIMIT] or None,
            effective=effective[FIELD_CURRENT_LIMIT],
        ),
        _entity_field_descriptor(
            hass,
            field=FIELD_ENERGY_REGISTER,
            scope="charger",
            required=False,
            writable=True,
            current_entity_id=values[FIELD_ENERGY_REGISTER] or None,
            effective=effective[FIELD_ENERGY_REGISTER],
        ),
        vehicle_soc_descriptor(hass),
    ]


def detected_control_path(hass: HomeAssistant, entry: ConfigEntry, charge_control: str) -> dict[str, Any] | None:
    """The control path a detected charger gets when its charge control is `charge_control`: the
    stored one while the entity is unchanged, else what the new entity's domain and options mean, or
    `None` when nothing can be told (the edit is then refused).
    """
    stored = entry.data.get(CONF_CONTROL_PATH)
    return control_path_for_entity(
        hass,
        charge_control,
        detected_path=stored if isinstance(stored, dict) else None,
        charge_control_is_identity=charge_control == (entry.data.get(CONF_CHARGE_CONTROL) or ""),
    )


def charger_control_descriptor(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any] | None:
    """`get_entity_config`'s `control` block: how this charger is started, stopped and given a
    current, what it reads its state from, the write policy every current write obeys, what the
    adapter supports now, and which of the charger's own modes are on. `None` while the charger is
    not loaded. Read only: the card shows it, it cannot change it.
    """
    controller = controller_for(hass, entry.entry_id)
    if controller is None:
        return None
    adapter = controller.adapter
    description = adapter.describe()
    profile = profile_for(adapter.platform)
    conflicts: list[dict[str, str]] = []
    if profile is not None:
        entries = charger_entries(hass, controller.charge_control, profile)
        conflicts = [
            {"kind": kind, "entity_id": found.entity_id, "label": found.label, "state": found.state}
            for kind, found in (
                *((CONFLICT_OWN_MODE, item) for item in own_mode_conflicts(hass, entries, profile)),
                *((CONFLICT_DISABLED, item) for item in disabled_switches(hass, entries, profile)),
            )
        ]
    return {
        "platform": description["platform"],
        "start_stop": description["start_stop"],
        "current": {**description["current"], "enabled": adapter.current_enabled},
        "charging_state": description["charging_state"],
        "policy": description["policy"],
        "capabilities": description["capabilities"],
        "conflicts": conflicts,
    }


def vehicle_soc_descriptor(hass: HomeAssistant) -> dict[str, Any]:
    """`get_entity_config`'s `vehicle_soc` field, system-wide.

    `current`/`effective` report an entity only for a household with exactly one detected vehicle;
    zero or several report "nothing chosen" rather than guess. `writable` stays false; the choice is
    made per vehicle by `spotnav/choose_vehicle_soc`.
    """
    vehicles = discover_vehicles(hass)
    entity_id = vehicle_soc_entity_id(hass, vehicles[0].id) if len(vehicles) == 1 else None
    return _entity_field_descriptor(
        hass,
        field=FIELD_VEHICLE_SOC,
        scope="charger",
        required=False,
        writable=False,
        current_entity_id=entity_id,
        effective=_effective_value(hass, entity_id, SOURCE_AUTOMATIC),
    )


def vehicle_soc_vehicles(hass: HomeAssistant) -> list[dict[str, Any]]:
    """The config block's `vehicles`: one `{id, name, selected, source, candidates}` row per vehicle
    whose state-of-charge sensor may be chosen. `selected` is the entity really used (`None` while
    ambiguous), `source` is `confirmed`, `automatic` or `None`, `candidates` the percent-shaped
    battery sensors.
    """

    def named(entity_id: str) -> dict[str, str]:
        state = hass.states.get(entity_id)
        return {"entity_id": entity_id, "friendly_name": state.name if state is not None else entity_id}

    return [
        {
            "id": choice.id,
            "name": choice.name,
            "selected": None if choice.selected_entity_id is None else named(choice.selected_entity_id),
            "source": choice.source,
            "candidates": [named(entity_id) for entity_id in choice.candidate_entity_ids],
        }
        for choice in soc_choices(hass)
    ]



def current_site_values(entry: ConfigEntry) -> dict[str, Any]:
    """Every site field's stored value in the shape `changes`/`expected` use: `""` for an unconfigured
    entity field, the plain value otherwise.
    """
    direct = entry.data.get(CONF_DIRECT_ENTITIES) or {}
    derived = entry.data.get(CONF_DERIVED_ENTITIES) or {}
    grid_total = grid_power_source_from_dict(entry.data.get(CONF_GRID_POWER_SOURCE))
    values: dict[str, Any] = {
        FIELD_GRID_POWER_SOURCE_POWER: "" if grid_total is None else grid_total.power,
        FIELD_GRID_POWER_SOURCE_EXPORT: "" if grid_total is None else (grid_total.power_export or ""),
        FIELD_MAIN_FUSE_A: entry.data.get(CONF_MAIN_FUSE_A),
        FIELD_MEASUREMENT_MODE: entry.data.get(CONF_MEASUREMENT_MODE, MEASUREMENT_MODE_DIRECT),
        FIELD_MAX_AGE_S: entry.data.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S),
        FIELD_BATTERY_AGGREGATE_POWER: entry.data.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY) or "",
        FIELD_BATTERY_DISCHARGE_POWER: entry.data.get(CONF_BATTERY_DISCHARGE_POWER_ENTITY) or "",
    }
    for field, key in FLAG_FIELDS.items():
        values[field] = bool(entry.data.get(key, False))
    for phase in PHASES:
        values[direct_field(phase)] = direct.get(phase) or ""
    for phase in PHASES:
        subvalues = derived.get(phase) or {}
        for sub in DERIVED_SUBFIELDS:
            values[derived_field(phase, sub)] = subvalues.get(sub) or ""
    return values


def _resulting_measurement_mode(entry: ConfigEntry, changes: dict[str, Any]) -> tuple[str, FieldError | None]:
    stored = entry.data.get(CONF_MEASUREMENT_MODE, MEASUREMENT_MODE_DIRECT)
    if FIELD_MEASUREMENT_MODE not in changes:
        return stored, None
    value = changes[FIELD_MEASUREMENT_MODE]
    if value not in (MEASUREMENT_MODE_DIRECT, MEASUREMENT_MODE_DERIVED):
        return stored, FieldError(FIELD_MEASUREMENT_MODE, ERR_INVALID_VALUE)
    return value, None


def _validate_bounded_number(changes: dict[str, Any], field: str, *, minimum: float) -> tuple[float | None, FieldError | None]:
    if field not in changes:
        return None, None
    try:
        value = float(changes[field])
    except (TypeError, ValueError):
        return None, FieldError(field, ERR_INVALID_VALUE)
    if value < minimum:
        return None, FieldError(field, ERR_INVALID_VALUE)
    return value, None


def site_field_errors(
    hass: HomeAssistant,
    *,
    entry: ConfigEntry,
    changes: dict[str, Any],
) -> list[FieldError]:
    """Every field error one site's proposed `changes` produces.

    Numbers are checked against the flow's bounds, `measurement_mode` against the two known values,
    and each touched entity field for existence and domain. Finally the resulting mode's whole
    per-phase set (submission plus stored values) must resolve to real entities, else `ERR_REQUIRED`
    on the missing field, so a mode switch or first save cannot leave per-phase fields half-filled.
    """
    errors: list[FieldError] = []
    for field, minimum in ((FIELD_MAIN_FUSE_A, MAIN_FUSE_MIN_A), (FIELD_MAX_AGE_S, MAX_AGE_MIN_S)):
        _, error = _validate_bounded_number(changes, field, minimum=minimum)
        if error is not None:
            errors.append(error)

    mode, mode_error = _resulting_measurement_mode(entry, changes)
    if mode_error is not None:
        errors.append(mode_error)

    for field in (FIELD_BATTERY_AGGREGATE_POWER, FIELD_BATTERY_DISCHARGE_POWER, *GRID_POWER_SOURCE_FIELDS):
        if field in changes:
            value = changes[field]
            if value:
                _, error = _entity_ref(hass, field, value)
                if error is not None:
                    errors.append(error)

    for field in FLAG_FIELDS:
        if field in changes and not isinstance(changes[field], bool):
            errors.append(FieldError(field, ERR_INVALID_VALUE))

    if FIELD_APPLY_DETECTION in changes and (
        len(changes) != 1 or resolve_detection_candidate(hass, entry, changes[FIELD_APPLY_DETECTION]) is None
    ):
        errors.append(FieldError(FIELD_APPLY_DETECTION, ERR_INVALID_VALUE))

    for field in direct_fields() + derived_fields():
        if field not in changes:
            continue
        value = changes[field]
        if not value:
            if field in _OPTIONAL_PHASE_FIELDS:
                continue
            errors.append(FieldError(field, ERR_REQUIRED))
            continue
        _, error = _entity_ref(hass, field, value)
        if error is not None:
            errors.append(error)

    failed_fields = {error.field for error in errors}
    current = current_site_values(entry)
    if (
        FIELD_GRID_POWER_SOURCE_POWER not in failed_fields
        and FIELD_GRID_POWER_SOURCE_EXPORT not in failed_fields
        and FIELD_APPLY_DETECTION not in changes
        and not changes.get(FIELD_GRID_POWER_SOURCE_POWER, current[FIELD_GRID_POWER_SOURCE_POWER])
        and changes.get(FIELD_GRID_POWER_SOURCE_EXPORT, current[FIELD_GRID_POWER_SOURCE_EXPORT])
    ):
        # The export half has no meaning alone: a pair is import minus export.
        errors.append(FieldError(FIELD_GRID_POWER_SOURCE_POWER, ERR_REQUIRED))
    if FIELD_APPLY_DETECTION in changes:
        # The detected candidate supplies the whole set; nothing else is required of this write.
        return errors
    required_fields = direct_fields() if mode == MEASUREMENT_MODE_DIRECT else required_derived_fields()
    for field in required_fields:
        if field in failed_fields:
            continue
        resulting = changes.get(field, current[field])
        if not resulting:
            errors.append(FieldError(field, ERR_REQUIRED))
    return errors


def _entity_field_descriptor(
    hass: HomeAssistant,
    *,
    field: str,
    scope: Scope,
    required: bool,
    writable: bool,
    current_entity_id: str | None,
    effective: dict[str, str] | None | Literal["configured"] = "configured",
    domains: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if effective == "configured":
        effective = _effective_value(hass, current_entity_id, SOURCE_CONFIGURED)
    return {
        "field": field,
        "scope": scope,
        "kind": "entity",
        "required": required,
        "writable": writable,
        "current": _current_entity_value(hass, current_entity_id),
        "effective": effective,
        "allowed_domains": list(domains or (_ENTITY_DOMAIN[field],)),
        "allowed_device_classes": list(_ENTITY_DEVICE_CLASSES.get(field, ())),
    }


def _flag_descriptor(field: str, values: dict[str, Any]) -> dict[str, Any]:
    return {
        "field": field,
        "scope": "site",
        "kind": "flag",
        "required": False,
        "writable": True,
        "value": bool(values[field]),
    }


def site_field_descriptors(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    """`get_entity_config`'s `fields` entries for one site, all scoped `"site"`: main fuse, measurement
    mode, that mode's own per-phase meters (never both modes), battery aggregate power, max age.
    """
    values = current_site_values(entry)
    mode = values[FIELD_MEASUREMENT_MODE]
    descriptors: list[dict[str, Any]] = [
        {
            "field": FIELD_MAIN_FUSE_A,
            "scope": "site",
            "kind": "number",
            "required": True,
            "writable": True,
            "value": values[FIELD_MAIN_FUSE_A],
            "minimum": MAIN_FUSE_MIN_A,
        },
        {
            "field": FIELD_MEASUREMENT_MODE,
            "scope": "site",
            "kind": "enum",
            "required": True,
            "writable": True,
            "value": mode,
            "choices": [MEASUREMENT_MODE_DIRECT, MEASUREMENT_MODE_DERIVED],
        },
    ]
    phase_fields = direct_fields() if mode == MEASUREMENT_MODE_DIRECT else derived_fields()
    for field in phase_fields:
        descriptors.append(
            _entity_field_descriptor(
                hass,
                field=field,
                scope="site",
                required=field not in _OPTIONAL_PHASE_FIELDS,
                writable=True,
                current_entity_id=values[field] or None,
            )
        )
    descriptors.append(_flag_descriptor(FIELD_SITE_CURRENT_SIGNED, values))
    # The meter's total grid power, in both modes so a mode switch can set it in the same save: a direct
    # site needs it for solar and hybrid, a derived site ignores it for the surplus.
    for field in GRID_POWER_SOURCE_FIELDS:
        descriptors.append(
            _entity_field_descriptor(
                hass,
                field=field,
                scope="site",
                required=False,
                writable=True,
                current_entity_id=values[field] or None,
            )
        )
    # Listed in both modes so a switch to derived mode can set it in the same save. It negates the
    # per-phase power of a derived site and the total grid power of either mode.
    descriptors.append(_flag_descriptor(FIELD_GRID_POWER_INVERTED, values))
    for field in (FIELD_BATTERY_AGGREGATE_POWER, FIELD_BATTERY_DISCHARGE_POWER):
        descriptors.append(
            _entity_field_descriptor(
                hass,
                field=field,
                scope="site",
                required=False,
                writable=True,
                current_entity_id=values[field] or None,
            )
        )
    descriptors.append(_flag_descriptor(FIELD_BATTERY_POWER_INVERTED, values))
    descriptors.append(
        {
            "field": FIELD_MAX_AGE_S,
            "scope": "site",
            "kind": "number",
            "required": False,
            "writable": True,
            "value": values[FIELD_MAX_AGE_S],
            "minimum": MAX_AGE_MIN_S,
        }
    )
    return descriptors


# ---- Detection, warnings and measurement state for the card ------------------------------------


def _site_excluded_devices(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    return excluded_charger_devices(hass, list(entry.data.get(CONF_CHARGER_ENTRY_IDS) or []))


def site_detection(hass: HomeAssistant, entry: ConfigEntry) -> Detection:
    """Meter and battery candidates for this site, charger devices excluded."""
    return detect_site_from_hass(hass, excluded_device_ids=_site_excluded_devices(hass, entry))


def resolve_detection_candidate(
    hass: HomeAssistant, entry: ConfigEntry, candidate_id: Any
) -> MeterCandidate | BatteryCandidate | None:
    """The candidate `candidate_id` names in a fresh detection, or `None` (never a caller-made one)."""
    if not isinstance(candidate_id, str):
        return None
    detection = site_detection(hass, entry)
    for candidate in (*detection.meters, *detection.batteries):
        if candidate.candidate_id == candidate_id:
            return candidate
    return None


def _measurement_entity_ids(entry: ConfigEntry) -> set[str]:
    data = entry.data
    entity_ids: set[str] = set()
    mode = data.get(CONF_MEASUREMENT_MODE)
    if mode == MEASUREMENT_MODE_DIRECT:
        entity_ids.update(value for value in (data.get(CONF_DIRECT_ENTITIES) or {}).values() if value)
        source = source_from_dict(data.get(CONF_SITE_CURRENT_SOURCE))
        if source is not None:
            entity_ids.update(
                {source.entity_id} if source.entity_id else set(source.entity_ids.values() if source.entity_ids else ())
            )
    elif mode == MEASUREMENT_MODE_DERIVED:
        for phase_entities in (data.get(CONF_DERIVED_ENTITIES) or {}).values():
            entity_ids.update(value for value in (phase_entities or {}).values() if value)
    grid_total = grid_power_source_from_dict(data.get(CONF_GRID_POWER_SOURCE))
    if grid_total is not None:
        entity_ids.update(grid_total.entity_ids)
    for key in (CONF_BATTERY_AGGREGATE_POWER_ENTITY, CONF_BATTERY_DISCHARGE_POWER_ENTITY):
        if data.get(key):
            entity_ids.add(data[key])
    return entity_ids


def _external_balancer_warnings(hass: HomeAssistant) -> list[dict[str, Any]]:
    """One warning when an integration that balances a charger's installation-wide limit through its own
    cloud (Perific for Zaptec) is set up beside a SpotNav charger of that platform: the charger is
    started and stopped only, and its limit is not written.
    """
    balancer = external_balancer(hass)
    if balancer is None:
        return []
    for charger in hass.config_entries.async_entries(DOMAIN):
        profile = profile_for(charger.data.get(CONF_CHARGER_PLATFORM))
        if profile is not None and profile.policy.installation_wide:
            return [
                {
                    "code": "external_current_balancer",
                    "integration": balancer,
                    "entity_id": None,
                    "interval_s": None,
                    "option": None,
                    "device_name": profile.name,
                }
            ]
    return []


def site_measurement_info(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """The `measurement`, `warnings` and `detection` blocks of `get_entity_config`'s `site`.

    * `measurement`: the mode, how each phase's current is obtained (`measured`, `apparent`,
      `reactive` or `estimated`) and whether any is an estimate, with the power factor assumed.
    * `warnings`: measurement sources whose integration updates more slowly than the site's maximum
      age (naming the integration's option where known), and devices with their own load balancing.
    * `detection`: the fresh meter and battery candidates and whether the stored setup is one.
    """
    controller = site_controller_for(hass, entry.entry_id)
    result = None if controller is None else controller.result
    measurement = {
        "mode": entry.data.get(CONF_MEASUREMENT_MODE, MEASUREMENT_MODE_DIRECT),
        "current_estimated": bool(result is not None and result.current_estimated),
        "assumed_power_factor": None if result is None else result.estimated_power_factor,
        "basis": {phase: (None if result is None else result.phase_current_basis.get(phase)) for phase in PHASES},
    }

    registry = er.async_get(hass)
    platforms: dict[str, str] = {}
    for entity_id in _measurement_entity_ids(entry):
        registered = registry.async_get(entity_id)
        if registered is not None:
            platforms[entity_id] = registered.platform
    max_age_s = float(entry.data.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
    warnings: list[dict[str, Any]] = [
        {
            "code": warning.code,
            "integration": warning.integration,
            "entity_id": warning.entity_id,
            "interval_s": warning.interval_s,
            "option": warning.option,
            "device_name": None,
        }
        for warning in freshness_warnings(platforms, max_age_s)
    ]
    for own in find_own_load_balancing_from_hass(hass):
        warnings.append(
            {
                "code": "own_load_balancing",
                "integration": own.integration,
                "entity_id": None,
                "interval_s": None,
                "option": None,
                "device_name": own.device_name,
            }
        )

    warnings.extend(_external_balancer_warnings(hass))
    detection = site_detection(hass, entry)
    return {
        "measurement": measurement,
        "warnings": warnings,
        "detection": {
            "meters": [_meter_row(hass, entry, candidate) for candidate in detection.meters],
            "batteries": [_battery_row(hass, candidate) for candidate in detection.batteries],
        },
    }


def _friendly(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    return state.name if state is not None else entity_id


def _meter_row(hass: HomeAssistant, entry: ConfigEntry, candidate: MeterCandidate) -> dict[str, Any]:
    applied = apply_meter_candidate(entry.data, candidate)
    active = all(
        _applied_value(key, applied.get(key)) == _applied_value(key, entry.data.get(key))
        for key in _APPLIED_KEYS
    )
    return {
        "id": candidate.candidate_id,
        "integration": candidate.integration,
        "title": candidate.title,
        "mode": candidate.mode,
        "confidence": candidate.confidence,
        "current_signed": candidate.signed_current,
        "power_inverted": candidate.power_inverted,
        "estimated": candidate.estimated,
        "disabled_entities": list(candidate.disabled_entity_ids),
        "entities": [
            {
                "role": item.role,
                "phase": item.phase,
                "entity_id": item.entity_id,
                "friendly_name": _friendly(hass, item.entity_id),
                "disabled": item.disabled,
            }
            for item in candidate.entities
        ],
        "warnings": list(candidate.warnings),
        "applied": active,
    }


def _battery_row(hass: HomeAssistant, candidate: BatteryCandidate) -> dict[str, Any]:
    return {
        "id": candidate.candidate_id,
        "integration": candidate.integration,
        "title": candidate.title,
        "entity_id": candidate.entity_id,
        "friendly_name": _friendly(hass, candidate.entity_id),
        "inverted": candidate.inverted,
        "discharge_entity_id": candidate.discharge_entity_id,
        "disabled_entities": list(candidate.disabled_entity_ids),
    }


# Flags a site stored before they existed read as off, so an absent flag equals False here.
_APPLIED_FLAG_KEYS: Final = frozenset({CONF_SITE_CURRENT_SIGNED, CONF_GRID_POWER_INVERTED})


def _applied_value(key: str, value: Any) -> Any:
    if key == CONF_GRID_POWER_SOURCE:
        # Absent, empty and unreadable all mean "no total".
        return grid_power_source_from_dict(value)
    return bool(value) if key in _APPLIED_FLAG_KEYS else value


_APPLIED_KEYS: Final = (
    CONF_MEASUREMENT_MODE,
    CONF_DIRECT_ENTITIES,
    CONF_DERIVED_ENTITIES,
    CONF_SITE_CURRENT_SIGNED,
    CONF_SITE_CURRENT_SOURCE,
    CONF_GRID_POWER_INVERTED,
    CONF_GRID_POWER_SOURCE,
)
