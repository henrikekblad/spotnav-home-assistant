"""The vendor-neutral paths from recorded entity shapes: how each integration is started, stopped, read and
given a current, and the write policy on its own (rate limits, the flash guard, the pause rules).
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CURRENT_LIMIT, CONF_MODE
from custom_components.spotnav.execution.chargers.adapter import build_adapter
from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_BELOW_MINIMUM,
    ASSIGN_FLASH_GUARD,
    ASSIGN_IGNORED_WHILE_PAUSED,
    ASSIGN_RATE_LIMITED,
    ASSIGN_TARGET_UNAVAILABLE,
    ASSIGN_UNCHANGED,
    ASSIGN_UNIT_UNKNOWN,
    ASSIGN_UNSUPPORTED,
    WRITE_REGULATOR,
    WRITE_SESSION_START,
    WRITE_SOLAR,
)
from custom_components.spotnav.execution.chargers.generic import ButtonPath, SelectPath, SwitchPath
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.flows.charger_detection import detect_charger

from ..charger_helpers import adapter_for, Clock, detected_config
from ..charger_shapes import E, NUMBER_A, register_shape, Shape, SHAPES


CONTROLLABLE = [name for name, shape in SHAPES.items() if shape.expect["path"] is not None]


#: What starting and stopping each integration's charger must call, per its recorded shape.
START_STOP_CALLS = {
    "wallbox": (("switch", "turn_on", {"entity_id": "switch.wallbox_pause_resume"}), ("switch", "turn_off", {"entity_id": "switch.wallbox_pause_resume"})),
    "goecharger_api2": (
        ("select", "select_option", {"entity_id": "select.goecharger_api2_frc", "option": "Charge"}),
        ("select", "select_option", {"entity_id": "select.goecharger_api2_frc", "option": "Don't charge"}),
    ),
    "peblar": (("switch", "turn_on", {"entity_id": "switch.peblar_charge"}), ("switch", "turn_off", {"entity_id": "switch.peblar_charge"})),
    "nrgkick": (("switch", "turn_on", {"entity_id": "switch.nrgkick_charging_enabled"}), ("switch", "turn_off", {"entity_id": "switch.nrgkick_charging_enabled"})),
    "hypervolt_charger": (("switch", "turn_on", {"entity_id": "switch.hypervolt_charger_charging"}), ("switch", "turn_off", {"entity_id": "switch.hypervolt_charger_charging"})),
    "chargeamps": (("switch", "turn_on", {"entity_id": "switch.chargeamps_enable"}), ("switch", "turn_off", {"entity_id": "switch.chargeamps_enable"})),
    "monta": (("switch", "turn_on", {"entity_id": "switch.monta_charger"}), ("switch", "turn_off", {"entity_id": "switch.monta_charger"})),
    "zaptec": (("switch", "turn_on", {"entity_id": "switch.zaptec_charger_operation_mode"}), ("switch", "turn_off", {"entity_id": "switch.zaptec_charger_operation_mode"})),
    # V2C's switch means paused: starting turns it OFF.
    "v2c": (("switch", "turn_off", {"entity_id": "switch.v2c_paused"}), ("switch", "turn_on", {"entity_id": "switch.v2c_paused"})),
    "myenergi": (
        ("select", "select_option", {"entity_id": "select.myenergi_charge_mode", "option": "Fast"}),
        ("select", "select_option", {"entity_id": "select.myenergi_charge_mode", "option": "Stopped"}),
    ),
    "lektrico": (("button", "press", {"entity_id": "button.lektrico_charge_start"}), ("button", "press", {"entity_id": "button.lektrico_charge_stop"})),
    "openevse": (
        ("select", "select_option", {"entity_id": "select.openevse_override_state", "option": "active"}),
        ("select", "select_option", {"entity_id": "select.openevse_override_state", "option": "disabled"}),
    ),
}


@pytest.mark.parametrize("platform", sorted(START_STOP_CALLS))
async def test_start_and_stop_call_the_integrations_own_control(hass: HomeAssistant, platform: str) -> None:
    ids = register_shape(hass, SHAPES[platform])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    (start_domain, start_service, start_data), (stop_domain, stop_service, stop_data) = START_STOP_CALLS[platform]
    starts = async_mock_service(hass, start_domain, start_service)
    stops = starts if (start_domain, start_service) == (stop_domain, stop_service) else async_mock_service(hass, stop_domain, stop_service)

    await adapter.async_start()
    await adapter.async_stop()

    calls = [(call.domain, call.service, dict(call.data)) for call in (starts if starts is stops else starts + stops)]
    assert calls[0] == (start_domain, start_service, start_data)
    assert calls[-1] == (stop_domain, stop_service, stop_data)
    assert adapter.capabilities.start_stop is True


async def test_the_charging_state_is_the_status_sensor_not_the_switch(hass: HomeAssistant) -> None:
    """Wallbox's switch means "not paused": it is on while the charger waits for the car."""
    ids = register_shape(hass, SHAPES["wallbox"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))

    assert adapter.charging_state() is True
    hass.states.async_set("sensor.wallbox_status_description", "Waiting for car demand")
    assert adapter.charging_state() is False
    assert adapter.enabled_state() is True
    assert adapter.progress_status() == "SuspendedEV"


async def test_an_unreadable_status_falls_back_to_the_switch(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    hass.states.async_set("sensor.wallbox_status_description", "unavailable")

    assert adapter.charging_state() is True
    assert adapter.progress_status() is None
    hass.states.async_set("switch.wallbox_pause_resume", "off")
    assert adapter.charging_state() is False


async def test_a_charger_without_a_status_sensor_uses_its_control(hass: HomeAssistant) -> None:
    """V2C (inverted switch): off means not paused, so it is charging."""
    ids = register_shape(hass, SHAPES["v2c"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))

    assert adapter.charging_state() is True
    hass.states.async_set("switch.v2c_paused", "on")
    assert adapter.charging_state() is False
    assert adapter.enabled_state() is False


async def test_a_select_reports_enabled_only_for_its_start_option(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["myenergi"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))

    assert adapter.enabled_state() is False  # Stopped
    hass.states.async_set("select.myenergi_charge_mode", "Fast", {"options": ["Fast", "Eco", "Eco+", "Stopped"]})
    assert adapter.enabled_state() is True
    hass.states.async_set("select.myenergi_charge_mode", "Eco", {"options": ["Fast", "Eco", "Eco+", "Stopped"]})
    assert adapter.enabled_state() is None  # the zappi's own mode: not ours to call on or off


@pytest.mark.parametrize(
    ("platform", "entity", "charging", "not_charging"),
    [
        ("goecharger_api2", "sensor.goecharger_api2_car", "Charging", "Wait for car"),
        ("peblar", "sensor.peblar_cp_state", "charging", "no_ev_connected"),
        ("nrgkick", "sensor.nrgkick_status", "charging", "connected"),
        ("chargeamps", "sensor.chargeamps_status", "Charging", "SuspendedEV"),
        ("monta", "sensor.monta_charger_state", "busy-charging", "busy-non-charging"),
        ("zaptec", "sensor.zaptec_charger_operation_mode", "connected_charging", "connected_finished"),
        ("easee", "sensor.easee_status", "charging", "ready_to_charge"),
        ("lektrico", "sensor.lektrico_state", "charging", "available"),
    ],
)
async def test_each_integrations_status_values_say_charging(
    hass: HomeAssistant, platform: str, entity: str, charging: str, not_charging: str
) -> None:
    ids = register_shape(hass, SHAPES[platform])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))

    hass.states.async_set(entity, charging)
    assert adapter.charging_state() is True
    hass.states.async_set(entity, not_charging)
    assert adapter.charging_state() is False


async def test_a_vehicle_that_asks_for_no_current_is_suspended_ev_to_the_progress_check(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))

    hass.states.async_set("sensor.easee_status", "completed")
    assert adapter.progress_status() == "SuspendedEV"
    hass.states.async_set("sensor.easee_status", "charging")
    assert adapter.progress_status() == "Charging"
    hass.states.async_set("sensor.easee_status", "awaiting_start")
    assert adapter.progress_status() == "awaiting_start"


async def test_measured_current_is_the_highest_phase_in_amps_and_milliamps_are_converted(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["peblar"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    # The phase sensors are disabled by default: nothing to read until they are enabled.
    assert adapter.measured_current_a() is None

    for phase, milliamps in ((1, "9800"), (2, "9900"), (3, "10000")):
        hass.states.async_set(f"sensor.peblar_current_phase_{phase}", milliamps, {"unit_of_measurement": "mA", "device_class": "current"})
    assert adapter.measured_current_a() == pytest.approx(10.0)


async def test_measured_current_in_amps(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["nrgkick"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))

    assert adapter.measured_current_a() == pytest.approx(12.1)
    hass.states.async_set("sensor.nrgkick_l3_current", "unavailable")
    assert adapter.measured_current_a() == pytest.approx(12.0)


async def test_the_energy_register_is_read_in_kwh_whatever_the_unit(hass: HomeAssistant) -> None:
    wh = adapter_for(hass, detect_charger(hass, register_shape(hass, SHAPES["peblar"])["device_id"]))
    kwh = adapter_for(hass, detect_charger(hass, register_shape(hass, SHAPES["nrgkick"])["device_id"]))

    assert wh.energy_register_kwh() == pytest.approx(88.0)
    assert kwh.energy_register_kwh() == pytest.approx(512.0)
    assert wh.capabilities.reads_energy_register and wh.capabilities.reads_measured_current


NUMBER_WRITES = {
    "wallbox": ("number.wallbox_maximum_charging_current", 10),
    "goecharger_api2": ("number.goecharger_api2_amp", 10),
    "peblar": ("number.peblar_charge_current_limit", 10),
    "nrgkick": ("number.nrgkick_current_set", 10),
    "hypervolt_charger": ("number.hypervolt_charger_max_current", 10),
    "chargeamps": ("number.chargeamps_max_current", 10),
    "v2c": ("number.v2c_intensity", 10),
    "lektrico": ("number.lektrico_dynamic_limit", 10),
    "openevse": ("number.openevse_charge_rate", 10),
}


@pytest.mark.parametrize("platform", sorted(NUMBER_WRITES))
async def test_a_current_is_written_through_the_integrations_number(hass: HomeAssistant, platform: str) -> None:
    ids = register_shape(hass, SHAPES[platform])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    calls = async_mock_service(hass, "number", "set_value")
    entity, value = NUMBER_WRITES[platform]

    outcome = await adapter.async_set_current(10, reason=WRITE_SESSION_START)

    assert outcome == ASSIGN_ASSIGNED
    assert [dict(call.data) for call in calls] == [{"entity_id": entity, "value": value}]
    assert adapter.capabilities.set_current is True
    assert adapter.current.last_written_a == 10


async def test_a_charger_without_a_current_path_supports_none(hass: HomeAssistant) -> None:
    """myenergi and Monta start and stop only; the regulator and the plan must never assume more."""
    for platform in ("myenergi", "monta"):
        ids = register_shape(hass, SHAPES[platform])
        adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
        calls = async_mock_service(hass, "number", "set_value")

        assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_UNSUPPORTED
        assert calls == []
        assert adapter.capabilities.set_current is False and adapter.capabilities.regulated_current is False


async def test_without_the_opt_in_no_current_is_ever_written(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), current_control="")
    calls = async_mock_service(hass, "number", "set_value")

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_UNSUPPORTED
    assert calls == []
    assert adapter.capabilities.set_current is False


async def test_the_minimum_interval_refuses_an_early_write_and_allows_it_later(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    clock = Clock()
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=clock)
    calls = async_mock_service(hass, "number", "set_value")

    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    clock.advance(30)
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_RATE_LIMITED
    assert adapter.current.wait_s() == pytest.approx(60.0)
    clock.advance(59)
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_RATE_LIMITED
    clock.advance(2)
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED

    assert [call.data["value"] for call in calls] == [12, 10]


async def test_a_failed_call_still_counts_against_the_budget(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    clock = Clock()
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=clock)

    async def boom(call):
        raise RuntimeError("cloud down")

    hass.services.async_register("number", "set_value", boom)

    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == "write_failed"
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_RATE_LIMITED


async def test_a_flash_stored_setting_is_written_at_a_session_start_and_never_by_the_regulator(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["alfen_wallbox"])
    found = detect_charger(hass, ids["device_id"])
    # Alfen's socket setting is a stored parameter; it has no start/stop of its own, so the path is a
    # plain switch the person chose.
    hass.states.async_set("switch.alfen_enable", "off")
    found.charge_control = "switch.alfen_enable"
    found.control_path = {"kind": "switch", "entity_id": "switch.alfen_enable", "inverted": False}
    adapter = adapter_for(hass, found)
    calls = async_mock_service(hass, "number", "set_value")

    assert adapter.policy.flash_stored is True and adapter.policy.regulator_writes is False
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_FLASH_GUARD
    # The sun's own modulation is a repeated write as well: never to a flash-stored setting.
    assert await adapter.async_set_current(10, reason=WRITE_SOLAR) == ASSIGN_FLASH_GUARD
    assert calls == []
    assert adapter.capabilities.set_current is True and adapter.capabilities.regulated_current is False

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert [call.data["value"] for call in calls] == [10]


async def test_a_flash_setting_that_already_holds_the_value_is_not_written_at_all(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["alfen_wallbox"])
    found = detect_charger(hass, ids["device_id"])
    hass.states.async_set("switch.alfen_enable", "off")
    found.charge_control = "switch.alfen_enable"
    found.control_path = {"kind": "switch", "entity_id": "switch.alfen_enable", "inverted": False}
    adapter = adapter_for(hass, found)
    calls = async_mock_service(hass, "number", "set_value")

    assert await adapter.async_set_current(16, reason=WRITE_SESSION_START) == ASSIGN_UNCHANGED
    assert calls == []


async def test_alfen_modbus_ignores_a_write_while_paused_so_none_is_made(hass: HomeAssistant) -> None:
    """Its switch and its current share one register: a current written while stopped restarts it."""
    ids = register_shape(hass, SHAPES["alfen_modbus"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    calls = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("switch.alfen_modbus_charger_enabled", "off")

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_IGNORED_WHILE_PAUSED
    assert calls == []

    hass.states.async_set("switch.alfen_modbus_charger_enabled", "on")
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert adapter.policy.ignored_while_paused and adapter.policy.zero_pauses


async def test_peblar_takes_a_write_while_paused_because_the_integration_only_stores_it(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["peblar"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    calls = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("switch.peblar_charge", "off")

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED

    assert [call.data["value"] for call in calls] == [10]
    assert adapter.policy.ignored_while_paused is False and adapter.policy.zero_pauses


async def test_a_charger_just_started_counts_as_enabled_until_it_says_so(hass: HomeAssistant) -> None:
    """Its report lags the command: a current written straight after a Start must not be refused as
    "paused" (Alfen Modbus), but a charger that never came up is treated as paused again after a while.
    """
    ids = register_shape(hass, SHAPES["alfen_modbus"])
    clock = Clock()
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=clock)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    calls = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("switch.alfen_modbus_charger_enabled", "off")

    await adapter.async_start()
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED

    clock.advance(31)
    assert await adapter.async_set_current(12, reason=WRITE_SESSION_START) == ASSIGN_IGNORED_WHILE_PAUSED
    await adapter.async_start()
    await adapter.async_stop()
    assert await adapter.async_set_current(12, reason=WRITE_SESSION_START) == ASSIGN_IGNORED_WHILE_PAUSED
    assert [call.data["value"] for call in calls] == [10]


@pytest.mark.parametrize("platform", ["peblar", "lektrico", "zaptec", "goecharger_api2"])
async def test_a_current_below_the_floor_is_never_written_so_a_write_cannot_pause_the_charger(
    hass: HomeAssistant, platform: str
) -> None:
    ids = register_shape(hass, SHAPES[platform])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    calls = async_mock_service(hass, "number", "set_value")

    for amps in (0, 1, 5):
        assert await adapter.async_set_current(amps, reason=WRITE_REGULATOR) == ASSIGN_BELOW_MINIMUM
    assert calls == []


async def test_a_value_is_rounded_down_to_the_step_clamped_to_the_maximum_and_never_raised_to_the_minimum(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["nrgkick"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    calls = async_mock_service(hass, "number", "set_value")

    await adapter.async_set_current(20, reason=WRITE_SESSION_START)  # above the entity's 16 A maximum
    assert calls[-1].data["value"] == 16

    # A minimum above the request is refused, not satisfied: writing 8 A for a 6 A ask would exceed it.
    hass.states.async_set("number.nrgkick_current_set", "12.0", {**NUMBER_A, "min": 8, "step": 0.1})
    clock_free = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    assert await clock_free.async_set_current(6, reason=WRITE_SESSION_START) == ASSIGN_BELOW_MINIMUM


async def test_a_number_in_milliamps_is_written_in_milliamps(hass: HomeAssistant) -> None:
    shape = Shape(
        "abb_terra_ac",
        "ABB1",
        (
            E("button", "ABB1_start_charging", "start_charging"),
            E("button", "ABB1_stop_charging", "stop_charging"),
            E("number", "ABB1_current_limit", "current_limit", "16000", {"unit_of_measurement": "mA", "min": 6000, "max": 16000, "step": 100}),
        ),
        {},
    )
    ids = register_shape(hass, shape)
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    calls = async_mock_service(hass, "number", "set_value")

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert calls[0].data["value"] == 10000
    assert adapter.current.setpoint_a() == 16


async def test_a_number_with_no_ampere_unit_is_never_assumed_to_be_amps(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    calls = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("number.wallbox_maximum_charging_current", "16", {"min": 6, "max": 32})

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_UNIT_UNKNOWN
    hass.states.async_set("number.wallbox_maximum_charging_current", "unavailable")
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_TARGET_UNAVAILABLE
    assert calls == []


async def test_a_plain_generic_charger_keeps_its_switch_as_control_and_state(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.plain", "on")
    adapter = build_adapter(
        hass,
        {CONF_MODE: "generic", CONF_CHARGE_CONTROL: "switch.plain", CONF_CURRENT_LIMIT: "number.x"},
        ocpp_target=lambda: None,
        energy_entity_id=None,
    )
    calls = async_mock_service(hass, "number", "set_value")

    assert adapter.charging_state() is True
    assert adapter.capabilities.set_current is False
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_UNSUPPORTED
    assert calls == []


async def _paths(hass: HomeAssistant):
    for entity_id in ("switch.s", "select.x", "button.start", "button.stop"):
        hass.states.async_set(entity_id, "unavailable")
    return (
        SwitchPath(hass, "switch.s"),
        SwitchPath(hass, "switch.s", inverted=True),
        SelectPath(hass, "select.x", start_option="on", stop_option="off"),
        ButtonPath(hass, "button.start", "button.stop"),
    )


async def test_a_switch_that_is_unavailable_is_not_commanded(hass: HomeAssistant) -> None:
    on = async_mock_service(hass, "switch", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")
    plain, inverted, _, _ = await _paths(hass)

    for path in (plain, inverted):
        assert await path.async_start() is False
        assert await path.async_stop() is False
    assert on == [] and off == []

    hass.states.async_set("switch.s", "off")
    assert await plain.async_start() is True
    assert await plain.async_stop() is True
    assert len(on) == 1 and len(off) == 1


async def test_a_select_that_is_unavailable_is_not_commanded(hass: HomeAssistant) -> None:
    calls = async_mock_service(hass, "select", "select_option")
    _, _, select, _ = await _paths(hass)

    assert await select.async_start() is False
    assert await select.async_stop() is False
    assert calls == []

    hass.states.async_set("select.x", "off")
    assert await select.async_start() is True
    assert [call.data["option"] for call in calls] == ["on"]


async def test_a_button_that_is_unavailable_is_not_pressed(hass: HomeAssistant) -> None:
    presses = async_mock_service(hass, "button", "press")
    _, _, _, buttons = await _paths(hass)

    assert await buttons.async_start() is False
    assert await buttons.async_stop() is False
    assert presses == []

    hass.states.async_set("button.start", "unknown")  # a button is `unknown` until pressed: available
    assert await buttons.async_start() is True
    assert await buttons.async_stop() is False, "the stop button is still unavailable"
    assert [call.data["entity_id"] for call in presses] == ["button.start"]


async def test_a_missing_entity_is_not_commanded_either(hass: HomeAssistant) -> None:
    calls = async_mock_service(hass, "switch", "turn_on")

    assert await SwitchPath(hass, "switch.gone").async_start() is False
    assert calls == []


async def test_the_controller_does_not_claim_a_start_that_never_went_out(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    found = detect_charger(hass, ids["device_id"])
    turn_on = async_mock_service(hass, "switch", "turn_on")
    controller = ChargingController(hass, "entry_wallbox", detected_config(found))
    await controller.async_initialize()
    hass.states.async_set("sensor.wallbox_status_description", "Paused")
    hass.states.async_set("switch.wallbox_pause_resume", "unavailable")

    assert await controller.async_start() is False

    assert turn_on == []
    assert controller.start_pending is False, "nothing is awaiting an answer"
    hass.states.async_set("switch.wallbox_pause_resume", "off")
    assert await controller.async_start() is True
    assert len(turn_on) == 1 and controller.start_pending is True
    await controller.async_shutdown()


async def test_the_wallbox_idle_status_is_the_one_home_assistant_reports(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=Clock())

    hass.states.async_set("sensor.wallbox_status_description", "Waiting for car demand")
    assert adapter.progress_status() == "SuspendedEV"
    hass.states.async_set("sensor.wallbox_status_description", "waiting for car demand")
    assert adapter.progress_status() == "SuspendedEV", "the match ignores case"
    hass.states.async_set("sensor.wallbox_status_description", "Connected: waiting car demand")
    assert adapter.progress_status() == "connected: waiting car demand", "the portal wording is not it"
