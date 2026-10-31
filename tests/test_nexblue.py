"""NexBlue (core `nexblue`): started and stopped through the `charging` switch Home Assistant 2026.10 adds, and read
only on 2026.9, which has no switch. No version can set the current. Detection and the adapter are run against the
registry entries the integration creates (`<serial>_<key>` unique ids; the three phase currents share the
translation key `current`).

The switch's state is assumed for up to 20 s after each command (an `assumed_state` attribute), and is read from the
status sensor meanwhile, by the switch's own rule: on while charging, waiting for power or waiting for the car.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service, MockConfigEntry

from custom_components.spotnav.execution.charger_profiles import (
    PATH_SWITCH_ASSUMED,
    profile_for,
    ROLE_CHARGER,
    ROLE_MEASUREMENT_ONLY,
)
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.vehicles.discovery import (
    async_discover_charger_current_sources,
    REASON_SEPARATE_ENTITIES_PROFILE_MATCH,
)

from .charger_helpers import adapter_for
from .test_charger_config_flow import _to_detected_entities

SERIAL = "NB2412345678"
PHASES = ("L1", "L2", "L3")
SWITCH = "switch.nexblue_charging"
STATUS = "sensor.nexblue_charging_state"
ASSUMED = {"assumed_state": True}


def _nexblue(hass: HomeAssistant, *, with_switch: bool) -> tuple[str, list[str]]:
    """The sensors of Home Assistant 2026.9 and, from 2026.10, the `charging` switch."""
    config = MockConfigEntry(domain="nexblue", entry_id="nexblue")
    config.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config.entry_id, identifiers={("nexblue", SERIAL)}, manufacturer="NexBlue", name=SERIAL
    )
    registry = er.async_get(hass)

    def add(domain: str, key: str, translation_key: str | None, state: str, attrs: dict) -> str:
        entry = registry.async_get_or_create(
            domain, "nexblue", f"{SERIAL}_{key}", config_entry=config, device_id=device.id,
            translation_key=translation_key, suggested_object_id=f"nexblue_{key}",
        )
        hass.states.async_set(entry.entity_id, state, attrs)
        return entry.entity_id

    add("sensor", "charging_state", "charging_state", "charging", {"device_class": "enum"})
    add("sensor", "power", None, "7.2", {"device_class": "power", "unit_of_measurement": "kW"})
    add("sensor", "energy", None, "3.1", {"device_class": "energy", "state_class": "total", "unit_of_measurement": "kWh"})
    add("sensor", "lifetime_energy", "lifetime_energy", "812.4", {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"})
    add("sensor", "current_limit", "current_limit", "16", {"device_class": "current", "unit_of_measurement": "A"})
    add("sensor", "cable_current_limit", "cable_current_limit", "32", {"device_class": "current", "unit_of_measurement": "A"})
    currents = [
        add("sensor", f"current_{n}", "current", value, {"device_class": "current", "unit_of_measurement": "A"})
        for n, value in zip((3, 1, 2), ("10.4", "10.1", "10.2"))
    ]
    if with_switch:
        add("switch", "charging", "charging", "on", {})
    # Registered L3 first, so the phase order comes from the keys and not from the registry.
    return device.id, [currents[1], currents[2], currents[0]]


async def test_without_the_charging_switch_nexblue_is_read_only(hass: HomeAssistant) -> None:
    """Home Assistant 2026.9: nothing to start or stop with, so it is read only, as it always was there."""
    device_id, _ = _nexblue(hass, with_switch=False)

    found = detect_charger(hass, device_id)

    assert found is not None
    assert found.role == ROLE_MEASUREMENT_ONLY
    assert found.charge_control is None and found.control_path is None
    assert found.energy_register is None and found.charging_state is None and found.current_entities == []
    profile = profile_for("nexblue")
    assert profile is not None and not profile.can_set_current


async def test_without_the_charging_switch_the_flow_refuses_it_as_read_only(hass: HomeAssistant) -> None:
    device_id, _ = _nexblue(hass, with_switch=False)

    result = await _to_detected_entities(hass, device_id)

    assert result["type"] == FlowResultType.ABORT and result["reason"] == "charger_not_controllable"


async def test_with_the_charging_switch_nexblue_is_a_start_stop_charger(hass: HomeAssistant) -> None:
    device_id, currents = _nexblue(hass, with_switch=True)

    found = detect_charger(hass, device_id)

    assert found is not None and found.role == ROLE_CHARGER
    assert found.charge_control == SWITCH
    assert found.control_path == {"kind": PATH_SWITCH_ASSUMED, "entity_id": SWITCH, "inverted": False}
    assert found.current_limit is None and found.current_control == ""
    assert found.energy_register == "sensor.nexblue_lifetime_energy"
    assert found.session_energy_register is None
    assert found.charging_state == {"entity_id": STATUS, "charging_values": ["charging"]}
    assert found.current_entities == currents


async def test_with_the_charging_switch_the_flow_goes_on_to_its_entities(hass: HomeAssistant) -> None:
    device_id, _ = _nexblue(hass, with_switch=True)

    result = await _to_detected_entities(hass, device_id)

    assert result["type"] == FlowResultType.FORM and result["step_id"] == "detected_entities"


async def test_start_and_stop_turn_the_charging_switch_on_and_off(hass: HomeAssistant) -> None:
    device_id, _ = _nexblue(hass, with_switch=True)
    adapter = adapter_for(hass, detect_charger(hass, device_id))
    ons = async_mock_service(hass, "switch", "turn_on")
    offs = async_mock_service(hass, "switch", "turn_off")

    assert await adapter.async_start() is True
    assert await adapter.async_stop() is True

    assert [call.data["entity_id"] for call in ons] == [SWITCH]
    assert [call.data["entity_id"] for call in offs] == [SWITCH]
    assert adapter.describe()["start_stop"]["kind"] == "switch"


async def test_a_start_the_charger_has_not_confirmed_is_read_from_its_status(hass: HomeAssistant) -> None:
    """Right after a start the switch shows on, assumed; the status still says ready, so it is not on yet."""
    device_id, _ = _nexblue(hass, with_switch=True)
    adapter = adapter_for(hass, detect_charger(hass, device_id))
    hass.states.async_set(STATUS, "connected")
    hass.states.async_set(SWITCH, "on", ASSUMED)

    assert adapter.enabled_state() is False
    assert adapter.charging_state() is False

    # The 3 s refresh: the charger took it.
    hass.states.async_set(STATUS, "charging")
    assert adapter.enabled_state() is True and adapter.charging_state() is True
    # Confirmed: the switch is read again.
    hass.states.async_set(SWITCH, "on")
    assert adapter.enabled_state() is True


async def test_a_stop_the_charger_has_not_confirmed_is_read_from_its_status(hass: HomeAssistant) -> None:
    device_id, _ = _nexblue(hass, with_switch=True)
    adapter = adapter_for(hass, detect_charger(hass, device_id))
    hass.states.async_set(STATUS, "charging")
    hass.states.async_set(SWITCH, "off", ASSUMED)

    assert adapter.enabled_state() is True and adapter.charging_state() is True

    hass.states.async_set(STATUS, "connected")
    assert adapter.enabled_state() is False and adapter.charging_state() is False


async def test_an_assumed_switch_with_no_status_says_nothing(hass: HomeAssistant) -> None:
    device_id, _ = _nexblue(hass, with_switch=True)
    adapter = adapter_for(hass, detect_charger(hass, device_id))
    hass.states.async_set(STATUS, "unavailable")
    hass.states.async_set(SWITCH, "on", ASSUMED)

    assert adapter.enabled_state() is None


async def test_the_charger_waiting_for_power_or_the_car_counts_as_started(hass: HomeAssistant) -> None:
    """The switch's own rule: waiting for available power or for the car is on. Its own load balancing or
    schedule holds the charge; a car that draws nothing is a connected, idle vehicle."""
    device_id, _ = _nexblue(hass, with_switch=True)
    adapter = adapter_for(hass, detect_charger(hass, device_id))
    hass.states.async_set(SWITCH, "on", ASSUMED)

    hass.states.async_set(STATUS, "lb_waiting")
    assert adapter.enabled_state() is True and adapter.held_by_charger() is True

    hass.states.async_set(STATUS, "ev_waiting")
    assert adapter.enabled_state() is True and adapter.held_by_charger() is False
    assert adapter.progress_status() == "SuspendedEV" and adapter.vehicle_connected() is True

    hass.states.async_set(STATUS, "delay_waiting")
    assert adapter.enabled_state() is False and adapter.held_by_charger() is True


async def test_no_cable_is_no_vehicle(hass: HomeAssistant) -> None:
    device_id, _ = _nexblue(hass, with_switch=True)
    adapter = adapter_for(hass, detect_charger(hass, device_id))

    hass.states.async_set(STATUS, "idle")
    assert adapter.vehicle_connected() is False
    hass.states.async_set(STATUS, "connected")
    assert adapter.vehicle_connected() is True


async def test_the_energy_register_and_phase_currents_are_read(hass: HomeAssistant) -> None:
    device_id, _ = _nexblue(hass, with_switch=True)
    found = detect_charger(hass, device_id)
    adapter = adapter_for(hass, found)

    assert adapter.energy_register_kwh() == 812.4
    assert adapter.measured_phase_currents_a() == (10.1, 10.2, 10.4)


async def test_nexblue_phase_currents_are_offered_in_phase_order_with_or_without_the_switch(
    hass: HomeAssistant,
) -> None:
    device_id, currents = _nexblue(hass, with_switch=False)

    candidates = await async_discover_charger_current_sources(hass, charger_device_id=device_id)

    by_profile = [c for c in candidates if c.reason_code == REASON_SEPARATE_ENTITIES_PROFILE_MATCH]
    assert len(by_profile) == 1
    assert dict(by_profile[0].mapping.entity_ids) == dict(zip(PHASES, currents))
