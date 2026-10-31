"""A charger's lifetime energy register, found again after the charger was added.

Detection used to run once, in the config flow. A charger that was offline then (an OCPP charge point
after a reinstall) had no register to offer yet, and one its integration registered later was never
seen: the state-of-charge estimate and the requested-energy count then had nothing to count with.

Here the register is resolved again whenever the charger is set up (every start) and, while it still
has none, whenever a sensor of its own is registered or first reports. What is found is used as setup
would have used it:

* a detected charger stores it in its entry, as the flow stores what detection suggested;
* an OCPP connector's register stays automatic, as it always was: the controller uses it and the
  entry keeps no entity.

A person who chose "no energy register" (`CONF_ENERGY_REGISTER_NONE`) is never overruled; a charger
behind a smart plug keeps its power.

A detected charger's stored register that its profile now knows is not a lifetime one (a per-session or daily
counter, one phase of three, or any energy sensor of an integration that has no lifetime register) is replaced at
setup by the one detection finds now, or by none (then the charger's own power is counted, where its profile
names a power sensor), with an INFO line. Never one a person chose (`CONF_ENERGY_REGISTER_CHOSEN`, or a
per-session register taken knowingly in the flow, `CONF_ENERGY_REGISTER_IS_SESSION`). An entry from before
2.2 does not say whether its register was suggested or chosen: since a register of the charger's own
integration that is not its lifetime one stops a kWh need late (one phase of three counts a third) or loses
energy at every reset, such a register is taken for a suggestion and replaced; a person who wants it chooses
it again in the entity editor, and it is kept from then on. Only configuration is filled in: a register that appears in the
middle of a charge starts its own counts from its first reading and never adds energy to what was
counted before it (`soc_estimate`, `sessions.recorder`, Auto's requested-energy baseline).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback, Event, HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CHARGE_CONTROL,
    CONF_CONTROL_PATH,
    CONF_CHARGER_PLATFORM,
    CONF_ENERGY_REGISTER_CHOSEN,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_ENERGY_REGISTER_IS_SESSION,
    CONF_ENERGY_REGISTER_NONE,
    CONF_MODE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CONF_POWER_ENTITY,
    MODE_DETECTED,
    MODE_OCPP,
)
from .execution.charger_profiles import profile_for, register_owner
from .flows.charger_detection import charger_power_for_energy, lifetime_energy_register
from .vehicles.ocpp_identity import (
    energy_register_entity_for,
    energy_register_target_from_entity_id,
    OCPP_DOMAIN,
    OcppConnectorTarget,
    resolve_target,
)

if TYPE_CHECKING:
    from .execution.controller import ChargingController

_LOGGER = logging.getLogger(__name__)

_NO_VALUE = (STATE_UNAVAILABLE, STATE_UNKNOWN)


def register_wanted(data: dict[str, Any]) -> bool:
    """Whether a charger has no register and nobody chose that: neither an entity nor "none" stored, and
    no smart plug's power standing in for one.
    """
    return not (
        data.get(CONF_ENERGY_REGISTER_ENTITY)
        or data.get(CONF_ENERGY_REGISTER_NONE)
        or data.get(CONF_POWER_ENTITY)
    )


def _stored_target(data: dict[str, Any]) -> OcppConnectorTarget | None:
    charge_point = data.get(CONF_OCPP_CHARGE_POINT_ID)
    connector = data.get(CONF_OCPP_CONNECTOR_ID)
    if not charge_point or not connector:
        return None
    try:
        return OcppConnectorTarget(str(charge_point), int(connector))
    except (TypeError, ValueError):
        return None


def _charger_device_id(hass: HomeAssistant, data: dict[str, Any]) -> str | None:
    """The device a detected charger was found on: its control path's (Easee), else its charge control's."""
    path = data.get(CONF_CONTROL_PATH)
    if isinstance(path, dict) and isinstance(path.get("device_id"), str):
        return path["device_id"]
    charge_control = data.get(CONF_CHARGE_CONTROL)
    entry = er.async_get(hass).async_get(charge_control) if charge_control else None
    return entry.device_id if entry is not None else None


def detected_register(
    hass: HomeAssistant, data: dict[str, Any], *, ocpp_target: OcppConnectorTarget | None = None
) -> str | None:
    """The lifetime register detection finds for this charger now, whatever the person chose, or `None`.

    An OCPP connector's by its key (`ocpp_identity.energy_register_entity_for`), a detected charger's
    by its platform profile (`charger_detection.lifetime_energy_register`). A charger set up from plain
    entities has no device to look on.
    """
    if data.get(CONF_POWER_ENTITY):
        return None
    target = ocpp_target or _stored_target(data)
    if target is None and data.get(CONF_MODE) == MODE_OCPP:
        target = resolve_target(hass, charge_control=data.get(CONF_CHARGE_CONTROL)).target
    if target is not None:
        return energy_register_entity_for(hass, target)
    if data.get(CONF_MODE) == MODE_DETECTED:
        return lifetime_energy_register(hass, _charger_device_id(hass, data))
    return None


def _store(hass: HomeAssistant, entry: ConfigEntry, entity_id: str) -> None:
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ENERGY_REGISTER_ENTITY: entity_id}
    )
    _LOGGER.info("%s: found the energy register %s and stored it", entry.title, entity_id)


def stored_register_known_wrong(hass: HomeAssistant, data: dict[str, Any]) -> bool:
    """Whether a detected charger's stored register is one its integration's profile knows is not a lifetime
    register, and nobody chose it (see the module docstring)."""
    stored = data.get(CONF_ENERGY_REGISTER_ENTITY)
    if (
        data.get(CONF_MODE) != MODE_DETECTED
        or not stored
        or data.get(CONF_ENERGY_REGISTER_CHOSEN)
        or data.get(CONF_ENERGY_REGISTER_IS_SESSION)
    ):
        return False
    registered = er.async_get(hass).async_get(stored)
    profile = None if registered is None else (
        profile_for(data.get(CONF_CHARGER_PLATFORM)) or profile_for(registered.platform)
    )
    if registered is None or profile is None or registered.platform != profile.platform:
        # Another integration's meter (a separate energy meter) is a register detection never suggests.
        return False
    owner = register_owner(profile, translation_key=registered.translation_key, unique_id=registered.unique_id)
    if owner in ("session", "excluded"):
        return True
    return bool(profile.energy_power_keys) and owner != "lifetime"


def _replace_known_wrong(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = dict(entry.data)
    if not stored_register_known_wrong(hass, data):
        return
    stored = data[CONF_ENERGY_REGISTER_ENTITY]
    found = lifetime_energy_register(hass, _charger_device_id(hass, data))
    hass.config_entries.async_update_entry(entry, data={**data, CONF_ENERGY_REGISTER_ENTITY: found or ""})
    if found is not None:
        _LOGGER.info(
            "%s: the energy register %s is not the charger's lifetime register; using %s instead",
            entry.title, stored, found,
        )
    else:
        _LOGGER.info(
            "%s: the energy register %s is not the charger's lifetime register, and the charger has none; "
            "it is no longer used",
            entry.title, stored,
        )


def charger_power_counted(hass: HomeAssistant, data: dict[str, Any]) -> str | None:
    """The detected charger's own power sensor whose energy SpotNav counts, or `None`: only while the charger
    has no register and nobody chose that (`register_wanted`), and its integration has no lifetime register but
    a power sensor (`PlatformProfile.energy_power_keys`)."""
    if data.get(CONF_MODE) != MODE_DETECTED or not register_wanted(data):
        return None
    return charger_power_for_energy(hass, _charger_device_id(hass, data))


@callback
def async_store_detected_register(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """At setup, before the controller reads its config: a detected charger whose stored register is known
    wrong loses it, and one without a register gets the one detection finds now, stored in its entry. Returns
    it, or `None`."""
    _replace_known_wrong(hass, entry)
    if entry.data.get(CONF_MODE) != MODE_DETECTED or not register_wanted(dict(entry.data)):
        return None
    found = detected_register(hass, dict(entry.data))
    if found is not None:
        _store(hass, entry, found)
    return found


@callback
def async_watch_for_register(hass: HomeAssistant, entry: ConfigEntry, controller: ChargingController) -> None:
    """While the charger has no register, look again whenever one of its sensors is registered or first
    reports a value; stop at the first found, or when the entry unloads."""
    if controller.energy_register_entity_id is not None or not register_wanted(dict(entry.data)):
        return
    detected = entry.data.get(CONF_MODE) == MODE_DETECTED
    if not detected and controller.ocpp_target is None:
        # A charger set up from plain entities: no device and no connector to look on.
        return
    device_id = _charger_device_id(hass, dict(entry.data)) if detected else None
    registry = er.async_get(hass)
    unsubscribe: list[Callable[[], None]] = []

    def _stop() -> None:
        while unsubscribe:
            unsubscribe.pop()()

    def _relevant(entity_id: Any) -> bool:
        if not isinstance(entity_id, str) or not entity_id.startswith("sensor."):
            return False
        registered = registry.async_get(entity_id)
        if detected:
            return registered is not None and registered.device_id == device_id
        if registered is not None:
            return registered.platform == OCPP_DOMAIN
        return energy_register_target_from_entity_id(entity_id) is not None

    @callback
    def _registry_filter(data: Any) -> bool:
        return data.get("action") in ("create", "update") and _relevant(data.get("entity_id"))

    @callback
    def _state_filter(data: Any) -> bool:
        new, old = data.get("new_state"), data.get("old_state")
        return (
            new is not None
            and new.state not in _NO_VALUE
            and (old is None or old.state in _NO_VALUE)
            and _relevant(data.get("entity_id"))
        )

    @callback
    def _look(_event: Event | None = None) -> None:
        if not unsubscribe:
            return
        if controller.energy_register_entity_id is not None or not register_wanted(dict(entry.data)):
            _stop()
            return
        found = detected_register(hass, dict(entry.data), ocpp_target=controller.ocpp_target)
        if found is None:
            return
        _stop()
        if detected:
            _store(hass, entry, found)
        else:
            _LOGGER.info("%s: found the energy register %s", entry.title, found)
        controller.use_found_energy_register(found)

    unsubscribe.append(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _look, event_filter=_registry_filter))
    unsubscribe.append(hass.bus.async_listen(EVENT_STATE_CHANGED, _look, event_filter=_state_filter))
    entry.async_on_unload(_stop)
