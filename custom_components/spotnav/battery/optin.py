"""The brand's control entities that ship disabled, and enabling them on the person's confirm.

Some integrations ship the entities SpotNav's control writes disabled in the entity registry (Sigenergy's `sigen`:
`disabled_by=integration`). When the person turns "Styr batteriet" on, the card lists the disabled ones and asks; only
on that confirm does `async_enable` clear `disabled_by` for exactly those entities and reload the integration's config
entry once (Home Assistant would do it itself 30 s later; the person should not wait for that). The verification then
runs as usual. Turning control off leaves them enabled. Nothing here runs without the confirm, and never over a webhook.

`ENABLE_KEYS` is each adapter's list as `(domain, unique-id key)`, the keys `commands.py`'s roles match (a test keeps the
two together), plus what the per-phase control needs.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import CONF_BATTERY_AGGREGATE_POWER_ENTITY
from .adapters import Match, _key

_LOGGER = logging.getLogger(__name__)

#: Per brand, the entities the control needs enabled: `(domain, key)`.
ENABLE_KEYS: Final[Mapping[str, tuple[tuple[str, str], ...]]] = {
    "sigenergy": (
        ("switch", "plant_remote_ems_enable"),
        ("select", "plant_remote_ems_control_mode"),
        ("number", "plant_ess_max_charging_limit"),
        ("number", "plant_ess_max_discharging_limit"),
        # Read by SpotNav (the plant's own import limit under a grid charge).
        ("number", "plant_grid_maximum_import_limitation"),
        # The per-phase control: the switch for independent phase control and each phase's fixed target.
        ("switch", "plant_independent_phase_power_control_enable"),
        ("number", "plant_phase_a_active_power_fixed_target"),
        ("number", "plant_phase_b_active_power_fixed_target"),
        ("number", "plant_phase_c_active_power_fixed_target"),
        # The plant's total active power target (40001): acts in the same mode; kept at 0, and read by the plain
        # control so a value left there never runs.
        ("number", "plant_active_power_fixed_target"),
    ),
    "solaredge": (
        ("number", "storage_command_timeout"),
        ("number", "storage_charge_limit"),
        ("number", "storage_discharge_limit"),
        ("select", "storage_command_mode"),
        ("select", "storage_control_mode"),
        ("select", "ac_charge_policy"),
    ),
    "huawei": (
        ("sensor", "forcible_charge_discharge_write"),
        ("number", "storage_maximum_discharging_power"),
        ("number", "storage_maximum_charging_power"),
    ),
    "solax": (
        ("number", "remotecontrol_autorepeat_duration"),
        ("number", "remotecontrol_active_power"),
        ("select", "remotecontrol_power_control"),
        ("button", "remotecontrol_trigger"),
    ),
    # The Solinteg plugin of `solax_modbus` (enabled by default but for the three phase numbers).
    "solinteg": (
        ("select", "working_mode"),
        ("number", "battery_charge_discharge_power_target"),
        # The per-phase control: the AC power scheduling mode, Grid Unbalanced Output (read, never written) and each
        # phase's AC power.
        ("select", "ems_acctrl_mode"),
        ("select", "grid_unbalanced_output"),
        ("number", "ems_acctrl_phase_a_power"),
        ("number", "ems_acctrl_phase_b_power"),
        ("number", "ems_acctrl_phase_c_power"),
    ),
    # Nothing ships disabled in `ferroamp`; the sensor of the last request is the read-back.
    "ferroamp": (("sensor", "last_cmd"),),
    # `victron_gx` and `victron_mqtt` enable all of these by default; listed for a person who disabled one.
    "victron": (
        ("number", "system_ess_max_charge_power"),
        ("number", "system_ess_max_inverter_power_limit"),
        ("number", "system_ac_power_set_point"),
        # The charge per phase (external control): the ESS mode, both flags, a setpoint per phase, and the power on
        # each Multi's AC output and AC input.
        ("select", "system_ess_mode"),
        ("switch", "vebus_hub4_disable_charge"),
        ("switch", "vebus_hub4_disable_feed_in"),
        ("number", "vebus_ac_power_setpoint_l1"),
        ("number", "vebus_ac_power_setpoint_l2"),
        ("number", "vebus_ac_power_setpoint_l3"),
        ("sensor", "vebus_inverter_output_power_l1"),
        ("sensor", "vebus_inverter_output_power_l2"),
        ("sensor", "vebus_inverter_output_power_l3"),
        ("sensor", "vebus_inverter_input_power_l1"),
        ("sensor", "vebus_inverter_input_power_l2"),
        ("sensor", "vebus_inverter_input_power_l3"),
    ),
}

#: The reasons an entity is disabled that a person's confirm may lift (not the config entry's or Home Assistant's).
_LIFTABLE: Final = (er.RegistryEntryDisabler.INTEGRATION, er.RegistryEntryDisabler.USER)


def _matches(brand: str) -> list[Match]:
    return [_key(key, domain) for domain, key in ENABLE_KEYS.get(brand, ())]


def disabled_control_entities(
    hass: HomeAssistant, config: Mapping[str, Any], brand: str | None
) -> tuple[str | None, list[er.RegistryEntry]]:
    """The config entry of the site's battery and the brand's control entities in it that are disabled (and may be
    enabled), in the order of `ENABLE_KEYS`; `(None, [])` when the brand has none, the site has no battery entity or
    nothing is disabled. Registry reads only."""
    if brand is None or brand not in ENABLE_KEYS:
        return None, []
    registry = er.async_get(hass)
    entity_id = config.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY)
    battery = registry.async_get(entity_id) if isinstance(entity_id, str) and entity_id else None
    if battery is None or battery.config_entry_id is None:
        return None, []
    entries = [
        entry
        for entry in er.async_entries_for_config_entry(registry, battery.config_entry_id)
        if entry.platform == battery.platform
    ]
    found: list[er.RegistryEntry] = []
    for match in _matches(brand):
        candidates = sorted(
            (entry for entry in entries if match.matches(entry.domain, str(entry.unique_id or ""))),
            key=lambda entry: entry.entity_id,
        )
        own = [entry for entry in candidates if battery.device_id is not None and entry.device_id == battery.device_id]
        # The same pick as `facts.find_entity`: the one on the battery's device, else the only one.
        chosen = own[0] if own else (candidates[0] if len(candidates) == 1 else None)
        if chosen is not None and chosen.disabled_by in _LIFTABLE:
            found.append(chosen)
    return battery.config_entry_id, found


def describe(hass: HomeAssistant, config: Mapping[str, Any], brand: str | None) -> dict[str, Any] | None:
    """What the card asks about: the integration and the disabled entities, `None` when there are none."""
    entry_id, entities = disabled_control_entities(hass, config, brand)
    if entry_id is None or not entities:
        return None
    entry = hass.config_entries.async_get_entry(entry_id)
    return {
        "integration": (entry.title if entry is not None and entry.title else entities[0].platform),
        "entities": [
            {"entity_id": entity.entity_id, "name": entity.name or entity.original_name or entity.entity_id}
            for entity in entities
        ],
    }


async def async_enable(hass: HomeAssistant, config: Mapping[str, Any], brand: str | None) -> list[str]:
    """Enable exactly the disabled entities `disabled_control_entities` finds, then reload their config entry once
    (only when something was enabled). Answers the entity ids enabled. The caller has the person's confirm."""
    entry_id, entities = disabled_control_entities(hass, config, brand)
    if entry_id is None or not entities:
        return []
    registry = er.async_get(hass)
    for entity in entities:
        registry.async_update_entity(entity.entity_id, disabled_by=None)
    _LOGGER.info("SpotNav battery control: enabled %s, reloading the integration", [e.entity_id for e in entities])
    await hass.config_entries.async_reload(entry_id)
    return [entity.entity_id for entity in entities]
