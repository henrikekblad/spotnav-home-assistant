"""The authenticated, admin-only entity-configuration commands: `spotnav/get_entity_config`,
`spotnav/update_entity_config`, `spotnav/choose_vehicle_soc` (a vehicle's state-of-charge sensor,
recorded as the same discovery decision the `confirm_vehicle_soc` service records; `null` returns
it to automatic detection) and `spotnav/update_vehicle` (a vehicle's battery size, consumption and onboard charger,
written by `vehicles/vehicle_properties.py`):

    {type: "spotnav/update_vehicle", api_version: 1, charger_id, vehicle_id,
     changes: {capacity_kwh?: 1..500 | null, consumption_kwh_per_10km?: > 0 | null, onboard_phases?: 1 | 3 | null},
     expected: {capacity_kwh?, consumption_kwh_per_10km?, onboard_phases?}}

`null` clears a property. `expected` holds what the caller last saw for the keys it names; a
mismatch is `spotnav_conflict` and nothing is written. The answer is the shared envelope plus
`vehicle` (the dashboard's `vehicles` row, `null` when unknown). Refusals: `spotnav_unsupported_api_version`
(an error frame), `spotnav_not_admin`, `spotnav_unknown_charger`, `spotnav_conflict`, and
`spotnav_invalid_value` with `field_errors` (`vehicle_id`/`unknown_vehicle`, `changes`/`invalid_changes`,
`capacity_kwh`/`invalid_capacity`, `consumption_kwh_per_10km`/`invalid_consumption`,
`onboard_phases`/`invalid_onboard_phases`,
`<key>`/`unknown_field`, `expected`/`invalid_expected`).

They let the card choose every entity this integration uses (a charger's charge control, current
limit and energy register and, for a site charger, the site's main fuse, measurement mode,
per-phase meters, battery power sensor and maximum measurement age), validated by
`api/entity_fields.py`, the same pure functions the config/options flow calls.

* The charger names everything; the server resolves the site from it as `api/site_settings.py` does.
* A stale write is a `conflict`: `expected` maps field names to the last-seen values, and the
  current config travels back so the card converges.
* Failed validation answers `spotnav_invalid_value` with `field_errors` (`{"field", "code"}`);
  nothing is written unless every field passes.
* One write into `entry.data`, as the options flow does, then the same `async_reload`; a charger
  write re-resolves the OCPP connector target as `SpotNavChargingOptionsFlow` does. It never
  touches `active_control_enabled` (any unknown key is `unknown_field`). An unload never stops a
  charge or clears a stored plan or setting.
* Every outcome is one envelope, `not_admin` included, with the config re-read after any write.
* A charger's priority alone is written without a reload: the site reads it from the entry on every
  recompute. The webhook writes it through the same core as `update_charger_priority`
  (`async_webhook_update_charger_priority`), answering the dashboard's `charger_priority` block.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant

from ..const import (
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_BATTERY_DISCHARGE_POWER_ENTITY,
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_ENTRY_IDS,
    CHARGER_PRIORITIES,
    CONF_CHARGER_PRIORITY,
    DEFAULT_CHARGER_PRIORITY,
    CONF_CONTROL_PATH,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_CURRENT_LIMIT_NONE,
    CONF_ENERGY_REGISTER_NONE,
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_SITE_CURRENT_SOURCE,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_POWER_ENTITY,
    CONF_GRID_POWER_SOURCE,
    CONF_MAIN_FUSE_A,
    CONF_MAX_AGE_S,
    CONF_MEASUREMENT_MODE,
    CONF_MODE,
    CONF_SAFETY_MARGIN_A,
    CONF_CHARGER_PHASES,
    CONF_VOLTAGE_BETWEEN_PHASES_V,
    MODE_DETECTED,
    MODE_OCPP,
)
from ..repairs import async_clear_vehicle_soc, async_record_vehicle_soc
from ..runtime import domain_data, preview_for
from ..vehicles import vehicle_properties
from ..vehicles.ocpp_identity import apply_target, resolve_target
from ..vehicles.vehicle_discovery import resolve_target_vehicle, soc_choices, valid_soc_choice
from .common import (
    ERROR_NOT_ADMIN,
    ERROR_UNSUPPORTED_VERSION,
    is_admin,
    lookup_charger,
    send_unsupported_version,
)
from .dashboard import (
    capture_charger_priority,
    capture_vehicles,
    serialize_charger_priority,
    serialize_vehicle,
    site_binding,
)
from .entity_fields import (
    charger_control_descriptor,
    charger_field_descriptors,
    detected_control_path,
    charger_field_errors,
    CHARGER_FIELDS,
    current_charger_values,
    current_site_values,
    stored_site_current_source,
    derived_field,
    derived_fields,
    DERIVED_SUBFIELDS,
    direct_field,
    direct_fields,
    ERR_ENTITY_NOT_FOUND,
    ERR_INVALID_VALUE,
    ERR_UNKNOWN_FIELD,
    ERR_UNKNOWN_VEHICLE,
    FIELD_APPLY_DETECTION,
    FIELD_BATTERY_AGGREGATE_POWER,
    FIELD_BATTERY_DISCHARGE_POWER,
    FIELD_CHARGE_CONTROL,
    FIELD_CHARGER_PRIORITY,
    CURRENT_LIMIT_NONE,
    FIELD_CURRENT_LIMIT,
    FIELD_ENERGY_REGISTER,
    FIELD_POWER_ENTITY,
    FIELD_GRID_POWER_SOURCE_EXPORT,
    FIELD_GRID_POWER_SOURCE_POWER,
    FIELD_MAIN_FUSE_A,
    FIELD_MAX_AGE_S,
    FIELD_MEASUREMENT_MODE,
    FIELD_SAFETY_MARGIN_A,
    FIELD_VEHICLE_SOC,
    FIELD_CHARGER_PHASES,
    FIELD_VOLTAGE_BETWEEN_PHASES,
    FieldError,
    FLAG_FIELDS,
    PHASES,
    resolve_detection_candidate,
    site_field_descriptors,
    site_field_errors,
    site_fixed_fields,
    site_measurement_info,
    vehicle_soc_vehicles,
)
from ..site.site_detection import (
    apply_battery_candidate,
    apply_meter_candidate,
    BatteryCandidate,
    enable_disabled_entities,
)


_LOGGER = logging.getLogger(__name__)

ENTITY_CONFIG_API_VERSION: Final = 1

ERROR_NO_SITE: Final = "spotnav_no_site"
ERROR_CONFLICT: Final = "spotnav_conflict"
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"

_SCOPES: Final = ("charger", "site")


class EntityConfigRefusal(Exception):
    """A stable-code refusal, carrying the re-read config (`None` before any charger resolved)."""

    #: `spotnav/update_vehicle` only: the vehicle's row as it stands (`None` when unknown).
    vehicle: dict[str, Any] | None = None

    def __init__(
        self,
        code: str,
        config: dict[str, Any] | None = None,
        field_errors: list[FieldError] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.config = config
        self.field_errors = field_errors or []


def _resolve_charger(hass: HomeAssistant, charger_id: Any) -> ConfigEntry:
    """The loaded charger entry, or an `EntityConfigRefusal` carrying the dashboard's own code."""
    entry, code = lookup_charger(hass, charger_id)
    if entry is None:
        raise EntityConfigRefusal(code)
    return entry


def read_entity_config(hass: HomeAssistant, charger: ConfigEntry) -> dict[str, Any]:
    """The config block both commands answer with, read fresh right now."""
    site_entry = site_binding(hass, charger.entry_id)
    fields = charger_field_descriptors(hass, charger)
    site: dict[str, Any] | None = None
    if site_entry is not None:
        fields += site_field_descriptors(hass, site_entry)
        site = {
            "name": site_entry.title,
            "charger_count": len(site_entry.data.get(CONF_CHARGER_ENTRY_IDS) or []),
            **site_measurement_info(hass, site_entry),
        }
    return {
        "charger_id": charger.entry_id,
        "site": site,
        "fields": fields,
        "vehicles": vehicle_soc_vehicles(hass),
        "control": charger_control_descriptor(hass, charger),
    }


def _same(field: str, current: Any, expected: Any) -> bool:
    if field in (FIELD_MAIN_FUSE_A, FIELD_SAFETY_MARGIN_A, FIELD_MAX_AGE_S):
        try:
            return float(current) == float(expected)
        except (TypeError, ValueError):
            return False
    return current == expected


def _write_charger(hass: HomeAssistant, entry: ConfigEntry, changes: dict[str, Any]) -> bool:
    """Merge `changes` into the charger's data the way `SpotNavChargingOptionsFlow` does."""
    updated = dict(entry.data)
    if FIELD_CHARGE_CONTROL in changes:
        updated[CONF_CHARGE_CONTROL] = changes[FIELD_CHARGE_CONTROL]
    if FIELD_CURRENT_LIMIT in changes:
        if changes[FIELD_CURRENT_LIMIT] == CURRENT_LIMIT_NONE:
            # "None": no entity, no current control, and the automatic lookup is switched off.
            updated[CONF_CURRENT_LIMIT] = ""
            updated[CONF_CURRENT_CONTROL] = ""
            updated[CONF_CURRENT_LIMIT_NONE] = True
        else:
            updated[CONF_CURRENT_LIMIT] = changes[FIELD_CURRENT_LIMIT] or ""
            updated.pop(CONF_CURRENT_LIMIT_NONE, None)
    if FIELD_ENERGY_REGISTER in changes:
        if changes[FIELD_ENERGY_REGISTER] == CURRENT_LIMIT_NONE:
            # "None": no register, and none looked up or detected again (`energy_register.py`).
            updated[CONF_ENERGY_REGISTER_ENTITY] = ""
            updated[CONF_ENERGY_REGISTER_NONE] = True
        else:
            # An entity, or "" for the one SpotNav finds itself.
            updated[CONF_ENERGY_REGISTER_ENTITY] = changes[FIELD_ENERGY_REGISTER] or ""
            updated.pop(CONF_ENERGY_REGISTER_NONE, None)
    if FIELD_POWER_ENTITY in changes:
        # Stored only while set, so a charger without one keeps exactly its old data.
        if changes[FIELD_POWER_ENTITY]:
            updated[CONF_POWER_ENTITY] = changes[FIELD_POWER_ENTITY]
        else:
            updated.pop(CONF_POWER_ENTITY, None)
    if FIELD_VOLTAGE_BETWEEN_PHASES in changes:
        updated[CONF_VOLTAGE_BETWEEN_PHASES_V] = int(changes[FIELD_VOLTAGE_BETWEEN_PHASES])
    if FIELD_CHARGER_PHASES in changes:
        updated[CONF_CHARGER_PHASES] = int(changes[FIELD_CHARGER_PHASES])
    if FIELD_CHARGER_PRIORITY in changes:
        # Stored only while it differs from the default, so a charger left alone keeps exactly its old data.
        if changes[FIELD_CHARGER_PRIORITY] == DEFAULT_CHARGER_PRIORITY:
            updated.pop(CONF_CHARGER_PRIORITY, None)
        else:
            updated[CONF_CHARGER_PRIORITY] = changes[FIELD_CHARGER_PRIORITY]
    if entry.data.get(CONF_MODE) == MODE_OCPP:
        apply_target(
            updated,
            resolve_target(hass, charge_control=updated[CONF_CHARGE_CONTROL]),
        )
    if entry.data.get(CONF_MODE) == MODE_DETECTED and FIELD_CHARGE_CONTROL in changes:
        # The path follows the entity; `charger_field_errors` already refused one it cannot follow.
        updated[CONF_CONTROL_PATH] = detected_control_path(hass, entry, updated[CONF_CHARGE_CONTROL])
    if updated == dict(entry.data):
        return False
    hass.config_entries.async_update_entry(entry, data=updated)
    return True


def _apply_detection(
    hass: HomeAssistant, entry: ConfigEntry, data: dict[str, Any], candidate_id: Any
) -> dict[str, Any]:
    """`data` with the detected meter or battery `candidate_id` applied, enabling the entities the
    candidate uses that their integration ships disabled (never one a person disabled)."""
    candidate = resolve_detection_candidate(hass, entry, candidate_id)
    if candidate is None:  # validated just before; defensive
        return data
    enable_disabled_entities(hass, candidate.disabled_entity_ids)
    if isinstance(candidate, BatteryCandidate):
        return apply_battery_candidate(data, candidate)
    return apply_meter_candidate(data, candidate)


def _write_site(hass: HomeAssistant, entry: ConfigEntry, changes: dict[str, Any]) -> bool:
    """Merge `changes` into the site's data. Only the fields named are touched (unlike the wizard), so
    a narrow edit never discards the inactive mode's stored meters.
    """
    updated = dict(entry.data)
    if FIELD_APPLY_DETECTION in changes:
        updated = _apply_detection(hass, entry, updated, changes[FIELD_APPLY_DETECTION])
    if FIELD_MAIN_FUSE_A in changes:
        updated[CONF_MAIN_FUSE_A] = float(changes[FIELD_MAIN_FUSE_A])
    if FIELD_SAFETY_MARGIN_A in changes:
        updated[CONF_SAFETY_MARGIN_A] = float(changes[FIELD_SAFETY_MARGIN_A])
    if FIELD_MAX_AGE_S in changes:
        updated[CONF_MAX_AGE_S] = float(changes[FIELD_MAX_AGE_S])
    if FIELD_MEASUREMENT_MODE in changes:
        updated[CONF_MEASUREMENT_MODE] = changes[FIELD_MEASUREMENT_MODE]
    if FIELD_VOLTAGE_BETWEEN_PHASES in changes:
        updated[CONF_VOLTAGE_BETWEEN_PHASES_V] = int(changes[FIELD_VOLTAGE_BETWEEN_PHASES])
    if FIELD_BATTERY_AGGREGATE_POWER in changes:
        updated[CONF_BATTERY_AGGREGATE_POWER_ENTITY] = changes[FIELD_BATTERY_AGGREGATE_POWER] or ""
    if FIELD_BATTERY_DISCHARGE_POWER in changes:
        updated[CONF_BATTERY_DISCHARGE_POWER_ENTITY] = changes[FIELD_BATTERY_DISCHARGE_POWER] or ""
    for flag_field, key in FLAG_FIELDS.items():
        if flag_field in changes:
            updated[key] = bool(changes[flag_field])
    if FIELD_GRID_POWER_SOURCE_POWER in changes or FIELD_GRID_POWER_SOURCE_EXPORT in changes:
        current = current_site_values(entry)
        power = changes.get(FIELD_GRID_POWER_SOURCE_POWER, current[FIELD_GRID_POWER_SOURCE_POWER])
        export = changes.get(FIELD_GRID_POWER_SOURCE_EXPORT, current[FIELD_GRID_POWER_SOURCE_EXPORT])
        if power:
            updated[CONF_GRID_POWER_SOURCE] = {"power": power, **({"power_export": export} if export else {})}
        else:
            # Cleared: the export half goes with it (validation refused an export alone).
            updated.pop(CONF_GRID_POWER_SOURCE, None)
    if any(direct_field(phase) in changes for phase in PHASES):
        # Entities named for the phases replace a stored current source (validation required all three).
        replaced = stored_site_current_source(entry) is not None
        direct = {} if replaced else dict(entry.data.get(CONF_DIRECT_ENTITIES) or {})
        for phase in PHASES:
            if direct_field(phase) in changes:
                direct[phase] = changes[direct_field(phase)]
        updated[CONF_DIRECT_ENTITIES] = direct
        if replaced:
            updated.pop(CONF_SITE_CURRENT_SOURCE, None)
    if any(derived_field(phase, sub) in changes for phase in PHASES for sub in DERIVED_SUBFIELDS):
        derived = {phase: dict(values) for phase, values in (entry.data.get(CONF_DERIVED_ENTITIES) or {}).items()}
        for phase in PHASES:
            for sub in DERIVED_SUBFIELDS:
                key = derived_field(phase, sub)
                if key in changes:
                    if changes[key]:
                        derived.setdefault(phase, {})[sub] = changes[key]
                    else:
                        # An optional source cleared: drop the key, so the fuse check falls back
                        # to the next basis rather than reading an empty id.
                        derived.setdefault(phase, {}).pop(sub, None)
        updated[CONF_DERIVED_ENTITIES] = derived
    if updated == dict(entry.data):
        return False
    hass.config_entries.async_update_entry(entry, data=updated)
    return True


async def async_get_entity_config(hass: HomeAssistant, charger_id: Any) -> dict[str, Any]:
    charger = _resolve_charger(hass, charger_id)
    return read_entity_config(hass, charger)


async def async_update_entity_config(
    hass: HomeAssistant,
    charger_id: Any,
    *,
    scope: Any,
    expected: Any,
    changes: Any,
) -> dict[str, Any]:
    """Resolve, validate, compare-and-set, write at most once, answer with the re-read config."""
    charger = _resolve_charger(hass, charger_id)

    def _refuse(code: str, errors: list[FieldError] | None = None) -> EntityConfigRefusal:
        return EntityConfigRefusal(code, read_entity_config(hass, charger), errors)

    if scope not in _SCOPES or not isinstance(changes, dict) or not changes or not isinstance(expected, dict):
        raise _refuse(ERROR_INVALID_VALUE)

    if scope == "site":
        target = site_binding(hass, charger.entry_id)
        if target is None:
            raise EntityConfigRefusal(ERROR_NO_SITE)
        current = current_site_values(target)
        known = set(site_fixed_fields()) | set(direct_fields()) | set(derived_fields())
    else:
        target = charger
        current = current_charger_values(charger)
        known = set(CHARGER_FIELDS)

    unknown = [FieldError(str(key), ERR_UNKNOWN_FIELD) for key in changes if key not in known]
    unknown += [FieldError(str(key), ERR_UNKNOWN_FIELD) for key in expected if key not in known]
    if unknown:
        raise _refuse(ERROR_INVALID_VALUE, unknown)

    for key, value in expected.items():
        if key not in current or not _same(key, current[key], value):
            raise _refuse(ERROR_CONFLICT)

    if scope == "site":
        errors = site_field_errors(hass, entry=target, changes=changes)
    else:
        errors = charger_field_errors(hass, entry=target, changes=changes)
    if errors:
        raise _refuse(ERROR_INVALID_VALUE, errors)

    written = _write_site(hass, target, changes) if scope == "site" else _write_charger(hass, target, changes)
    if written and not (scope == "charger" and set(changes) == {FIELD_CHARGER_PRIORITY}):
        # Same reload the options flows perform: neither entry kind has an update listener.
        await hass.config_entries.async_reload(target.entry_id)
    return read_entity_config(hass, charger)


async def async_set_vehicle_soc(
    hass: HomeAssistant, charger_id: Any, *, vehicle_id: Any, entity_id: Any
) -> dict[str, Any]:
    """Choose which sensor is `vehicle_id`'s state of charge, or (`entity_id` `None`) go back to
    automatic detection, through the discovery decision itself.

    The charger only authenticates and receives the re-read config; the vehicle is a system-wide
    device (`vehicle_discovery.soc_choices`). Nothing is written unless the pair is valid.
    """
    charger = _resolve_charger(hass, charger_id)

    def _refuse(code: str, field_code: str) -> EntityConfigRefusal:
        return EntityConfigRefusal(
            code, read_entity_config(hass, charger), [FieldError(FIELD_VEHICLE_SOC, field_code)]
        )

    if not isinstance(vehicle_id, str) or not any(c.id == vehicle_id for c in soc_choices(hass)):
        raise _refuse(ERROR_INVALID_VALUE, ERR_UNKNOWN_VEHICLE)
    store = domain_data(hass).decision_store
    if store is None:
        raise _refuse(ERROR_INVALID_VALUE, ERR_UNKNOWN_VEHICLE)
    if entity_id is None:
        await async_clear_vehicle_soc(hass, store, vehicle_id)
    elif valid_soc_choice(hass, vehicle_id, entity_id):
        await async_record_vehicle_soc(hass, store, vehicle_id, entity_id)
    else:
        raise _refuse(ERROR_INVALID_VALUE, ERR_ENTITY_NOT_FOUND)
    return read_entity_config(hass, charger)


def _vehicle_row(hass: HomeAssistant, entry_id: str, vehicle_id: Any) -> dict[str, Any] | None:
    """The vehicle's row as the dashboard's `vehicles` states it, read now, or `None`."""
    store = domain_data(hass).auto_store
    settings = None if store is None else store.settings(entry_id)
    vehicles, _ = capture_vehicles(hass, entry_id, settings)
    return next((serialize_vehicle(v) for v in vehicles if v.id == vehicle_id), None)


async def async_update_vehicle(
    hass: HomeAssistant,
    charger_id: Any,
    *,
    vehicle_id: Any,
    changes: Any,
    expected: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Write a vehicle's own properties through `vehicle_properties`, or refuse by stable code.

    Returns `(config, vehicle_row)`. Validation and the conflict check come first. The charger
    authenticates and names the fallback consumption; a planned vehicle's chargers recalculate afterwards.
    """
    charger = _resolve_charger(hass, charger_id)
    entry_id = charger.entry_id

    def _refuse(code: str, errors: list[FieldError]) -> EntityConfigRefusal:
        refusal = EntityConfigRefusal(code, read_entity_config(hass, charger), errors)
        refusal.vehicle = _vehicle_row(hass, entry_id, vehicle_id)
        return refusal

    store = domain_data(hass).decision_store
    if store is None or not isinstance(vehicle_id, str) or not any(
        c.id == vehicle_id for c in resolve_target_vehicle(hass, None)[1]
    ):
        raise _refuse(ERROR_INVALID_VALUE, [FieldError("vehicle_id", ERR_UNKNOWN_VEHICLE)])
    errors = vehicle_properties.validate_changes(changes)
    if errors:
        raise _refuse(ERROR_INVALID_VALUE, [FieldError(k, c) for k, c in errors.items()])
    if expected is not None and (
        not isinstance(expected, dict) or set(expected) - set(vehicle_properties.PROPERTY_KEYS)
    ):
        raise _refuse(ERROR_INVALID_VALUE, [FieldError("expected", "invalid_expected")])
    row = _vehicle_row(hass, entry_id, vehicle_id) or {}
    seen = {
        vehicle_properties.KEY_CAPACITY: row.get("capacity_kwh"),
        vehicle_properties.KEY_CONSUMPTION: row.get("consumption_kwh_per_10km"),
        vehicle_properties.KEY_ONBOARD_PHASES: row.get("onboard_phases"),
    }
    if any(seen[key] != value for key, value in (expected or {}).items()):
        raise _refuse(ERROR_CONFLICT, [])
    before = vehicle_properties.stored_properties(hass, vehicle_id)
    after = await vehicle_properties.async_update_vehicle_properties(
        hass, store, vehicle_id, changes
    )
    if after != before:
        await _recalculate_planners_for(hass, vehicle_id)
    return read_entity_config(hass, charger), _vehicle_row(hass, entry_id, vehicle_id)


async def _recalculate_planners_for(hass: HomeAssistant, vehicle_id: str) -> None:
    """Plan again on every charger whose planned vehicle this is: its need or distance moved."""
    store = domain_data(hass).auto_store
    if store is None:
        return
    for entry_id in store.entry_ids():
        if vehicle_properties.resolved_vehicle_id(hass, entry_id) != vehicle_id:
            continue
        controller = preview_for(hass, entry_id)
        if controller is not None:
            hass.async_create_task(controller.async_recalculate())


def _envelope(config: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    return {
        "api_version": ENTITY_CONFIG_API_VERSION,
        "ok": True,
        "error": None,
        "field_errors": [],
        "config": config,
        **extra,
    }


def _failure(
    code: str,
    config: dict[str, Any] | None,
    field_errors: list[FieldError] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "api_version": ENTITY_CONFIG_API_VERSION,
        "ok": False,
        "error": code,
        "field_errors": [error.as_dict() for error in field_errors or []],
        "config": config,
        **extra,
    }


def _version_ok(connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> bool:
    if msg.get("api_version") == ENTITY_CONFIG_API_VERSION:
        return True
    send_unsupported_version(connection, msg, ENTITY_CONFIG_API_VERSION)
    return False


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_entity_config",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
    }
)
@websocket_api.async_response
async def websocket_get_entity_config(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Read the charger's (and its site's) entity choices. Admin only, in the envelope."""
    if not _version_ok(connection, msg):
        return
    if not is_admin(connection):
        connection.send_result(msg["id"], _failure(ERROR_NOT_ADMIN, None))
        return
    try:
        config = await async_get_entity_config(hass, msg.get("charger_id"))
    except EntityConfigRefusal as refusal:
        connection.send_result(msg["id"], _failure(refusal.code, refusal.config))
        return
    connection.send_result(msg["id"], _envelope(config))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/update_entity_config",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("scope"): object,
        vol.Optional("expected"): object,
        vol.Optional("changes"): object,
    }
)
@websocket_api.async_response
async def websocket_update_entity_config(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Change one scope's entity choices. Admin only; every outcome is the same envelope."""
    if not _version_ok(connection, msg):
        return
    if not is_admin(connection):
        connection.send_result(msg["id"], _failure(ERROR_NOT_ADMIN, None))
        return
    try:
        config = await async_update_entity_config(
            hass,
            msg.get("charger_id"),
            scope=msg.get("scope"),
            expected=msg.get("expected"),
            changes=msg.get("changes"),
        )
    except EntityConfigRefusal as refusal:
        connection.send_result(
            msg["id"], _failure(refusal.code, refusal.config, refusal.field_errors)
        )
        return
    connection.send_result(msg["id"], _envelope(config))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/choose_vehicle_soc",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("vehicle_id"): object,
        vol.Optional("entity_id"): object,
    }
)
@websocket_api.async_response
async def websocket_set_vehicle_soc(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Confirm (or, with `entity_id: null`, forget) a vehicle's state-of-charge sensor. Admin only."""
    if not _version_ok(connection, msg):
        return
    if not is_admin(connection):
        connection.send_result(msg["id"], _failure(ERROR_NOT_ADMIN, None))
        return
    try:
        config = await async_set_vehicle_soc(
            hass,
            msg.get("charger_id"),
            vehicle_id=msg.get("vehicle_id"),
            entity_id=msg.get("entity_id"),
        )
    except EntityConfigRefusal as refusal:
        connection.send_result(
            msg["id"], _failure(refusal.code, refusal.config, refusal.field_errors)
        )
        return
    connection.send_result(msg["id"], _envelope(config))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/update_vehicle",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("vehicle_id"): object,
        vol.Optional("changes"): object,
        vol.Optional("expected"): object,
    }
)
@websocket_api.async_response
async def websocket_update_vehicle(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Change a vehicle's battery size and/or consumption. Admin only; envelope plus `vehicle`."""
    if not _version_ok(connection, msg):
        return
    if not is_admin(connection):
        connection.send_result(msg["id"], _failure(ERROR_NOT_ADMIN, None, vehicle=None))
        return
    try:
        config, vehicle = await async_update_vehicle(
            hass,
            msg.get("charger_id"),
            vehicle_id=msg.get("vehicle_id"),
            changes=msg.get("changes"),
            expected=msg.get("expected"),
        )
    except EntityConfigRefusal as refusal:
        connection.send_result(
            msg["id"],
            _failure(refusal.code, refusal.config, refusal.field_errors, vehicle=refusal.vehicle),
        )
        return
    connection.send_result(msg["id"], _envelope(config, vehicle=vehicle))


def _webhook_status(code: str | None) -> int:
    """The HTTP status a webhook answer carries for an envelope's stable code."""
    if code is None:
        return 200
    return 409 if code == ERROR_CONFLICT else 400


async def _async_webhook_write(
    hass: HomeAssistant,
    entry: ConfigEntry,
    payload: dict[str, Any],
    *,
    with_vehicle: bool,
    write: Any,
) -> tuple[int, dict[str, Any]]:
    """Run one of this module's shared write cores for a webhook and shape its answer.

    The webhook twin of the WebSocket handlers: same core, same envelope, except that the charger is
    the webhook's own entry and `config` is always `null`, because the webhook secret must not read
    which entities this integration controls (entity configuration is admin-only, WebSocket-only).
    """
    version = payload.get("api_version")
    extra: dict[str, Any] = {"vehicle": None} if with_vehicle else {}
    if version is not None and (isinstance(version, bool) or version != ENTITY_CONFIG_API_VERSION):
        return 400, _failure(ERROR_UNSUPPORTED_VERSION, None, **extra)
    try:
        config, vehicle = await write(entry.entry_id)
    except EntityConfigRefusal as refusal:
        failure_extra = {"vehicle": refusal.vehicle} if with_vehicle else {}
        return _webhook_status(refusal.code), _failure(
            refusal.code, None, refusal.field_errors, **failure_extra
        )
    del config
    return 200, _envelope(None, **({"vehicle": vehicle} if with_vehicle else {}))


async def async_webhook_update_vehicle(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """`update_vehicle` over the webhook, through `async_update_vehicle`."""

    async def write(charger_id: str) -> tuple[Any, Any]:
        return await async_update_vehicle(
            hass,
            charger_id,
            vehicle_id=payload.get("vehicle_id"),
            changes=payload.get("changes"),
            expected=payload.get("expected"),
        )

    return await _async_webhook_write(hass, entry, payload, with_vehicle=True, write=write)


def _priority_answer(
    hass: HomeAssistant,
    entry: ConfigEntry,
    code: str | None,
    field_errors: list[FieldError] | None = None,
) -> tuple[int, dict[str, Any]]:
    """`update_charger_priority`'s envelope: the shared one without `config`, with the charger's
    `charger_priority` block re-read now (`null` for a charger on no site)."""
    block = serialize_charger_priority(capture_charger_priority(hass, entry), can_act=True)
    body = {
        "api_version": ENTITY_CONFIG_API_VERSION,
        "ok": code is None,
        "error": code,
        "field_errors": [error.as_dict() for error in field_errors or []],
        "charger_priority": block,
    }
    return _webhook_status(code), body


async def async_webhook_update_charger_priority(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """`update_charger_priority` over the webhook: `{"priority", "expected"}`, both one of "first",
    "normal" or "last", where `expected` is the value the caller last saw (a mismatch is
    `spotnav_conflict` and nothing is written). Validated and written by `async_update_entity_config`'s
    own core for the one field `charger_priority`; refused with `spotnav_no_site` for a charger on no
    site. Returns `(http_status, body)`.
    """
    version = payload.get("api_version")
    if version is not None and (isinstance(version, bool) or version != ENTITY_CONFIG_API_VERSION):
        return _priority_answer(hass, entry, ERROR_UNSUPPORTED_VERSION)
    if site_binding(hass, entry.entry_id) is None:
        return _priority_answer(hass, entry, ERROR_NO_SITE)
    expected = payload.get("expected")
    if not isinstance(expected, str) or expected not in CHARGER_PRIORITIES:
        return _priority_answer(
            hass, entry, ERROR_INVALID_VALUE, [FieldError("expected", ERR_INVALID_VALUE)]
        )
    if "priority" not in payload:
        return _priority_answer(
            hass, entry, ERROR_INVALID_VALUE, [FieldError(FIELD_CHARGER_PRIORITY, ERR_INVALID_VALUE)]
        )
    try:
        await async_update_entity_config(
            hass,
            entry.entry_id,
            scope="charger",
            expected={FIELD_CHARGER_PRIORITY: expected},
            changes={FIELD_CHARGER_PRIORITY: payload["priority"]},
        )
    except EntityConfigRefusal as refusal:
        return _priority_answer(hass, entry, refusal.code, refusal.field_errors)
    return _priority_answer(hass, entry, None)


@callback
def async_setup_entity_config_api(hass: HomeAssistant) -> None:
    """Register both commands once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_entity_config)
    websocket_api.async_register_command(hass, websocket_update_entity_config)
    websocket_api.async_register_command(hass, websocket_set_vehicle_soc)
    websocket_api.async_register_command(hass, websocket_update_vehicle)
