"""The registry: each charger module registers its own factories and `build_adapter` looks them up."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import (
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    CURRENT_CONTROL_EASEE,
    CURRENT_CONTROL_NUMBER,
)
from custom_components.spotnav.execution.charger_profiles import PATH_BUTTONS, PATH_EASEE, PATH_SELECT, PATH_SWITCH
from custom_components.spotnav.execution.chargers import registry
from custom_components.spotnav.execution.chargers.adapter import build_adapter
from custom_components.spotnav.execution.chargers.easee import EaseeCommandPath
from custom_components.spotnav.execution.chargers.generic import SwitchPath
from custom_components.spotnav.execution.chargers.zaptec import single_charger_installation


def test_every_module_registers_its_kinds() -> None:
    assert {PATH_SWITCH, PATH_SELECT, PATH_BUTTONS, PATH_EASEE} <= set(registry._START_STOP)  # noqa: SLF001
    assert {
        CURRENT_CONTROL_CHANGE_CONFIGURATION,
        CURRENT_CONTROL_EASEE,
        CURRENT_CONTROL_NUMBER,
    } <= set(registry._CURRENT)  # noqa: SLF001
    assert registry.installation_check() is single_charger_installation


async def test_an_unknown_or_incomplete_path_falls_back_to_the_charge_control_switch(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.plain", "on")
    for raw in ({"kind": "nonsense"}, {"kind": PATH_EASEE}, {"kind": PATH_SELECT, "entity_id": "select.x"}):
        adapter = build_adapter(
            hass,
            {"charge_control": "switch.plain", "control_path": raw},
            ocpp_target=lambda: None,
            energy_entity_id=None,
        )
        assert isinstance(adapter.path, SwitchPath) and adapter.path.entity_id == "switch.plain"


async def test_the_easee_kind_builds_the_easee_path(hass: HomeAssistant) -> None:
    adapter = build_adapter(
        hass,
        {"charge_control": "switch.x", "control_path": {"kind": PATH_EASEE, "device_id": "dev"}},
        ocpp_target=lambda: None,
        energy_entity_id=None,
    )
    assert isinstance(adapter.path, EaseeCommandPath) and adapter.path.device_id == "dev"
