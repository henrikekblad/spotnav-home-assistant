"""Find a charger's controls from its device, by platform and key.

The device is chosen first; then the entities **of that device and of that integration** are matched
by the platform's known `translation_key` or `unique_id` tail (`execution/charger_profiles.py`),
never by entity id or friendly name, which change with language and version. A charger that lives
on both OCPP and its vendor cloud is common, so entities of any other integration on the same device
are ignored. The entity *registry* is read, not only states: an entity that is disabled by default
(Easee's current, Peblar's phase currents) is found and reported so the flow can offer to enable it.

Nothing here writes anything or talks to a charger. `detect_charger` returns what it found and what
it could not; every field is a suggestion the person confirms and can change in the card.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..const import CURRENT_CONTROL_EASEE, CURRENT_CONTROL_NUMBER
from ..execution.chargers.easee import EASEE_LIMIT_SENSOR_KEY
from ..execution.chargers.registry import external_balancer
from ..execution.chargers.zaptec import single_charger_installation
from ..execution.charger_entities import (
    EntityMatcher,
    option_for,
    own_mode_conflicts,
    OwnModeConflict,
)
from ..execution.charger_profiles import (
    entity_matches_keys,
    PlatformProfile,
    PATH_BUTTONS,
    PATH_BUTTONS_TOGGLE,
    PATH_EASEE,
    PATH_NUMBER_PAUSE,
    PATH_SELECT,
    PATH_SELECT_APPROVE,
    PATH_SELECT_RESTORE,
    PATH_SWITCH,
    PATH_SWITCH_BUDGET,
    profile_for,
    PROFILES,
    ROLE_CHARGER,
    ROLE_EXTERNAL_CONTROLLER,
)

_LOGGER = logging.getLogger(__name__)

_AMPERE_UNITS: Final = ("a", "amp", "amps", "ampere", "amperes", "ma")
_PHASE_LIMIT: Final = 3


@dataclass(slots=True)
class DetectedCharger:
    """What detection found for one device. Every entity field is an entity id or `None`."""

    device_id: str
    device_name: str
    platform: str | None
    role: str | None
    charge_control: str | None = None
    #: `{"kind": ...}`, as `CONF_CONTROL_PATH` stores it.
    control_path: dict[str, Any] | None = None
    current_limit: str | None = None
    #: `CURRENT_CONTROL_NUMBER`, `CURRENT_CONTROL_EASEE` or `""`: what to suggest, never applied
    #: without the person's confirmation.
    current_control: str = ""
    energy_register: str | None = None
    #: A per-session register (resets), offered only when no lifetime one exists.
    session_energy_register: str | None = None
    charging_state: dict[str, Any] | None = None
    current_entities: list[str] = field(default_factory=list)
    conflicts: list[OwnModeConflict] = field(default_factory=list)
    #: Entities found but disabled in the registry that would improve control or measurement.
    disabled_useful: list[str] = field(default_factory=list)
    #: Why something was not found or not suggested, as stable codes.
    notes: list[str] = field(default_factory=list)
    #: Another controller (evcc, openWB) owns or may own the charger: warn, suggest nothing.
    external_controller: bool = False
    #: The domain of an integration that balances this charger's installation-wide current limit through
    #: its own cloud (Perific for Zaptec): the current is not suggested, start and stop only.
    balanced_by: str | None = None

    @property
    def found_control(self) -> bool:
        return self.charge_control is not None and self.control_path is not None


def external_controller_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Config entries of integrations that are themselves a charger's controller (evcc, openWB)."""
    domains = {p.platform for p in PROFILES.values() if p.role == ROLE_EXTERNAL_CONTROLLER}
    return [entry for domain in sorted(domains) for entry in hass.config_entries.async_entries(domain)]


def identifier_domains(device: dr.DeviceEntry) -> list[str]:
    """The domains of a device's identifiers, sorted. An identifier is normally `(domain, id)`, but
    some integrations register longer tuples, so only the first element is read."""
    return sorted(
        {
            identifier[0]
            for identifier in device.identifiers
            if isinstance(identifier, tuple) and identifier and isinstance(identifier[0], str)
        }
    )


def _device_platform(hass: HomeAssistant, device: dr.DeviceEntry, entries: list[er.RegistryEntry]) -> str | None:
    """The integration the device is a charger of: the first of its identifiers' domains that has
    a profile, else the first entity platform that has one.
    """
    for domain in identifier_domains(device):
        if domain in PROFILES:
            return domain
    for entry in entries:
        if entry.platform in PROFILES:
            return entry.platform
    return None


def _unit_is_amps(hass: HomeAssistant, entry: er.RegistryEntry) -> bool:
    """Whether a number or sensor measures amperes (A or mA). An entity with no state yet (disabled,
    not loaded) is taken from its registry unit, and with neither is not assumed to.
    """
    state = hass.states.get(entry.entity_id)
    unit = state.attributes.get("unit_of_measurement") if state is not None else None
    unit = unit or entry.unit_of_measurement
    return bool(unit) and str(unit).strip().lower() in _AMPERE_UNITS


def _detect_start_stop(
    hass: HomeAssistant, found: DetectedCharger, matcher: EntityMatcher, device: dr.DeviceEntry
) -> None:
    rule = matcher.profile.start_stop
    if rule is None:
        found.notes.append("no_start_stop")
        return
    if rule.kind in (PATH_SWITCH, PATH_SWITCH_BUDGET):
        entry = matcher.first("switch", rule.keys)
        if entry is None:
            found.notes.append("no_charge_switch")
            return
        found.charge_control = entry.entity_id
        found.control_path = {"kind": rule.kind, "entity_id": entry.entity_id, "inverted": rule.inverted}
        if entry.disabled_by is not None:
            found.disabled_useful.append(entry.entity_id)
        return
    if rule.kind in (PATH_SELECT, PATH_SELECT_RESTORE, PATH_SELECT_APPROVE):
        entry = matcher.first("select", rule.keys)
        if entry is None:
            found.notes.append("no_charge_select")
            return
        state = hass.states.get(entry.entity_id)
        options = [str(o) for o in (state.attributes.get("options") or [])] if state is not None else []
        start = option_for(options, rule.start_options)
        stop = option_for(options, rule.stop_options)
        if start is None or stop is None:
            # The options are the integration's own words; never guess which one starts a charge.
            found.notes.append("select_options_not_recognised")
            return
        found.charge_control = entry.entity_id
        found.control_path = {
            "kind": rule.kind,
            "entity_id": entry.entity_id,
            "start_option": start,
            "stop_option": stop,
        }
        return
    if rule.kind in (PATH_BUTTONS, PATH_BUTTONS_TOGGLE):
        start_entry = matcher.first("button", rule.start_keys)
        stop_entry = matcher.first("button", rule.stop_keys)
        if start_entry is None or stop_entry is None:
            found.notes.append("no_button_pair")
            return
        found.charge_control = start_entry.entity_id
        found.control_path = {
            "kind": rule.kind,
            "start_entity_id": start_entry.entity_id,
            "stop_entity_id": stop_entry.entity_id,
        }
        return
    if rule.kind == PATH_NUMBER_PAUSE:
        # A pause is 0 A written to the current number, so the number is part of the path; the start
        # button (pressed only while the charger waits for authorization) is the charge control.
        number = matcher.first("number", rule.keys)
        start_entry = matcher.first("button", rule.start_keys)
        if number is None or start_entry is None:
            found.notes.append("no_number_pause_pair")
            return
        found.charge_control = start_entry.entity_id
        found.control_path = {
            "kind": PATH_NUMBER_PAUSE,
            "start_entity_id": start_entry.entity_id,
            "number_entity_id": number.entity_id,
        }
        return
    if rule.kind == PATH_EASEE:
        # No entity starts an Easee; the status sensor identifies the charger and is watched.
        status = matcher.first("sensor", matcher.profile.status_keys)
        found.charge_control = status.entity_id if status is not None else None
        found.control_path = {"kind": PATH_EASEE, "device_id": found.device_id}
        if status is None:
            found.notes.append("no_status_sensor")
            found.control_path = None


def _detect_current(
    hass: HomeAssistant, found: DetectedCharger, matcher: EntityMatcher, device: dr.DeviceEntry
) -> None:
    profile = matcher.profile
    if profile.easee_current:
        if found.control_path is not None:
            found.current_control = CURRENT_CONTROL_EASEE
            # The only read-back of the dynamic limit is this diagnostic sensor, disabled by default:
            # offered to be enabled (it gives confirmed writes), never required.
            limit = matcher.first("sensor", (EASEE_LIMIT_SENSOR_KEY,))
            if limit is not None and limit.disabled_by is not None:
                found.disabled_useful.append(limit.entity_id)
        return
    if not profile.current_keys:
        found.notes.append("no_current_control")
        return
    entry = matcher.first("number", profile.current_keys)
    if entry is None and profile.policy.installation_wide and device.via_device_id:
        # Zaptec: the limit sits on the Installation device above the charger.
        registry = er.async_get(hass)
        parent = [
            e
            for e in er.async_entries_for_device(registry, device.via_device_id, include_disabled_entities=True)
            if e.platform == profile.platform
        ]
        entry = EntityMatcher(hass, parent, profile).first("number", profile.current_keys)
    if entry is None:
        found.notes.append("no_current_number")
        return
    if not _unit_is_amps(hass, entry):
        found.notes.append("current_unit_not_amps")
        return
    balancer = external_balancer(hass) if profile.policy.installation_wide else None
    if balancer is not None:
        # Two writers on one installation-wide field, the other one holding the fuse: start and stop only.
        found.balanced_by = balancer
        found.notes.append("external_balancer")
        return
    if profile.policy.installation_wide and not single_charger_installation(hass, entry.entity_id):
        # A limit that caps every charger of the installation is never used for one of several.
        found.notes.append("installation_shared")
        return
    found.current_limit = entry.entity_id
    found.current_control = CURRENT_CONTROL_NUMBER
    if entry.disabled_by is not None:
        found.disabled_useful.append(entry.entity_id)


def _is_energy_register(
    hass: HomeAssistant, entry: er.RegistryEntry, state_classes: tuple[str, ...] = ("total_increasing",)
) -> bool:
    state = hass.states.get(entry.entity_id)
    device_class = (state.attributes.get("device_class") if state else None) or entry.device_class or entry.original_device_class
    state_class = (state.attributes.get("state_class") if state else None) or (entry.capabilities or {}).get("state_class")
    return device_class == "energy" and state_class in state_classes


def _register_owner(profile: PlatformProfile, entry: er.RegistryEntry) -> str | None:
    """Which list an energy sensor belongs to, `"lifetime"` or `"session"`, by the longest of the
    profile's keys it matches. A key can be the tail of another (Lektrico's `energy` ends
    `lifetime_energy`; DEFA's `meter_value` ends `transaction_meter_value`), and the longer one is
    what the entity is.
    """
    best_length = 0
    owner: str | None = None
    for name, keys in (("lifetime", profile.energy_keys), ("session", profile.session_energy_keys)):
        for key in keys:
            if len(key) > best_length and entity_matches_keys(
                translation_key=entry.translation_key,
                unique_id=entry.unique_id,
                keys=(key,),
                key_first=profile.key_first,
            ):
                best_length = len(key)
                owner = name
    return owner


def _detect_energy(hass: HomeAssistant, found: DetectedCharger, matcher: EntityMatcher) -> None:
    profile = matcher.profile
    sensors = [e for e in matcher.entries if e.domain == "sensor" and _is_energy_register(hass, e)]
    session_named = [
        e for e in matcher.find("sensor", profile.session_energy_keys) if _register_owner(profile, e) == "session"
    ]
    # A register the profile names may carry a state class the generic search would refuse (`total`).
    lifetime = next(
        (
            e
            for e in matcher.find("sensor", profile.energy_keys)
            if _register_owner(profile, e) == "lifetime"
            and _is_energy_register(hass, e, profile.energy_state_classes)
        ),
        None,
    )
    session = next((e for e in session_named if e in sensors), None)
    if lifetime is None:
        # Any other meter register on the device that is not a known session counter.
        lifetime = next((e for e in sensors if e is not session and e not in session_named), None)
    if lifetime is not None:
        found.energy_register = lifetime.entity_id
        if lifetime.disabled_by is not None:
            found.disabled_useful.append(lifetime.entity_id)
    elif session is not None:
        found.session_energy_register = session.entity_id
        found.notes.append("energy_register_is_session")
    else:
        found.notes.append("no_energy_register")


def _detect_status(hass: HomeAssistant, found: DetectedCharger, matcher: EntityMatcher) -> None:
    profile = matcher.profile
    if not profile.status_keys or not profile.charging_values:
        return
    entry = matcher.first("sensor", profile.status_keys)
    if entry is None:
        found.notes.append("no_status_sensor")
        return
    found.charging_state = {
        "entity_id": entry.entity_id,
        "charging_values": list(profile.charging_values),
    }
    if entry.disabled_by is not None:
        found.disabled_useful.append(entry.entity_id)


def _detect_measured_current(hass: HomeAssistant, found: DetectedCharger, matcher: EntityMatcher) -> None:
    profile = matcher.profile
    if not profile.current_sensor_keys:
        return
    entries = [
        entry
        for key in profile.current_sensor_keys
        for entry in matcher.find("sensor", (key,))[:1]
    ]
    # Measurements of another quantity that happen to share a key are refused by unit.
    usable = [entry for entry in entries if _is_current_sensor(hass, entry)]
    for entry in usable[:_PHASE_LIMIT]:
        found.current_entities.append(entry.entity_id)
        if entry.disabled_by is not None:
            found.disabled_useful.append(entry.entity_id)
    if not usable:
        found.notes.append("no_measured_current")


def _is_current_sensor(hass: HomeAssistant, entry: er.RegistryEntry) -> bool:
    """A current sensor by unit, or by device class when it has not loaded (disabled by default), or,
    for the few integrations that state neither (alfen_modbus, legacy go-e), by its key alone.
    """
    if _unit_is_amps(hass, entry):
        return True
    device_class = entry.device_class or entry.original_device_class
    if device_class == "current":
        return True
    state = hass.states.get(entry.entity_id)
    stated = state.attributes.get("unit_of_measurement") if state is not None else None
    return not (stated or entry.unit_of_measurement or device_class)


def detect_charger(hass: HomeAssistant, device_id: str) -> DetectedCharger | None:
    """Everything detection can say about one device, or `None` if the device is unknown."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    registry = er.async_get(hass)
    all_entries = er.async_entries_for_device(registry, device_id, include_disabled_entities=True)
    platform = _device_platform(hass, device, all_entries)
    profile = profile_for(platform)
    name = device.name_by_user or device.name or device_id
    found = DetectedCharger(device_id=device_id, device_name=name, platform=platform, role=None)
    if profile is None:
        found.notes.append("platform_not_known")
        return found
    found.role = profile.role
    if profile.role == ROLE_EXTERNAL_CONTROLLER:
        found.external_controller = True
        return found
    if profile.role != ROLE_CHARGER:
        return found
    # One integration only: the charger's twin on another integration is not this device's control.
    entries = [entry for entry in all_entries if entry.platform == profile.platform]
    matcher = EntityMatcher(hass, entries, profile)
    _detect_start_stop(hass, found, matcher, device)
    _detect_current(hass, found, matcher, device)
    _detect_energy(hass, found, matcher)
    _detect_status(hass, found, matcher)
    _detect_measured_current(hass, found, matcher)
    found.conflicts = own_mode_conflicts(hass, matcher.entries, profile)
    if external_controller_entries(hass):
        # evcc or openWB is installed: it may own this charger. Suggest nothing, say so.
        found.external_controller = True
    found.disabled_useful = list(dict.fromkeys(found.disabled_useful))
    return found
