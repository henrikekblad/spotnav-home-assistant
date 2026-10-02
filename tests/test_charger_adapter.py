"""The charger adapter, per supported integration, from recorded entity shapes.

For each integration: how a charge is started and stopped, how the charging state is read (from the
status sensor, with the switch as the fallback), how a current is written, and what it supports.
Then the write policy on its own: rate limits, the flash guard, the pause rules.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_MODE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    MODE_OCPP,
)
from custom_components.spotnav.execution.charger_adapter import (
    ASSIGN_ASSIGNED,
    ASSIGN_BELOW_MINIMUM,
    ASSIGN_FLASH_GUARD,
    ASSIGN_IGNORED_WHILE_PAUSED,
    ASSIGN_INSTALLATION_SHARED,
    ASSIGN_RATE_LIMITED,
    ASSIGN_TARGET_UNAVAILABLE,
    ASSIGN_UNCHANGED,
    ASSIGN_UNIT_UNKNOWN,
    ASSIGN_UNSUPPORTED,
    build_adapter,
    WRITE_REGULATOR,
    WRITE_RESEND,
    WRITE_SESSION_START,
)
from custom_components.spotnav.vehicles.ocpp_identity import OcppConnectorTarget

from .charger_helpers import adapter_for, Clock, enable_easee_limit_sensor, set_easee_limit
from .charger_shapes import E, NUMBER_A, register_shape, Shape, SHAPES

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


async def test_easee_starts_and_stops_through_its_own_service_and_never_writes_a_max_limit(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    found = detect_charger(hass, ids["device_id"])
    adapter = adapter_for(hass, found)
    commands = async_mock_service(hass, "easee", "action_command")
    flash = [
        async_mock_service(hass, "easee", name)
        for name in ("set_charger_max_limit", "set_circuit_max_limit", "set_charger_offline_limit")
    ]
    async_mock_service(hass, "easee", "set_charger_dynamic_limit")

    await adapter.async_start()
    await adapter.async_stop()
    await adapter.async_set_current(12, reason=WRITE_SESSION_START)

    assert [dict(call.data) for call in commands] == [
        {"device_id": ids["device_id"], "action_command": "resume"},
        {"device_id": ids["device_id"], "action_command": "pause"},
    ]
    assert all(calls == [] for calls in flash)


# --------------------------------------------------------------------------- charging state


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


# ------------------------------------------------------------------------------ measurements


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


# ------------------------------------------------------------------------------ current writes

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


async def test_zaptec_writes_the_installation_limit_only_for_a_single_charger_installation(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["zaptec"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    calls = async_mock_service(hass, "number", "set_value")

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert [dict(call.data) for call in calls] == [{"entity_id": "number.zaptec_available_current", "value": 10}]
    assert adapter.policy.installation_wide and adapter.policy.min_interval_s == 900.0

    # A second charger under the same installation: the limit would lower it too.
    from homeassistant.helpers import device_registry as dr

    config_entry = hass.config_entries.async_get_entry("zaptec")
    devices = dr.async_get(hass)
    second = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("zaptec", "ZAP2")},
        name="zaptec second",
        via_device_id=ids["installation_device_id"],
    )
    er.async_get(hass).async_get_or_create(
        "switch", "zaptec", "ZAP2_charger_operation_mode", config_entry=config_entry, device_id=second.id
    )
    calls.clear()

    assert await adapter.async_set_current(8, reason=WRITE_SESSION_START) == ASSIGN_INSTALLATION_SHARED
    assert calls == []
    assert adapter.capabilities.set_current is False and adapter.capabilities.regulated_current is False


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


# ---------------------------------------------------------------------------------------- policy


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


async def test_peblar_ignores_a_write_while_paused_so_none_is_made(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["peblar"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    calls = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("switch.peblar_charge", "off")

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_IGNORED_WHILE_PAUSED
    assert calls == []

    hass.states.async_set("switch.peblar_charge", "on")
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert adapter.policy.ignored_while_paused and adapter.policy.zero_pauses


async def test_a_charger_just_started_counts_as_enabled_until_it_says_so(hass: HomeAssistant) -> None:
    """Its report lags the command: a current written straight after a Start must not be refused as
    "paused" (Peblar), but a charger that never came up is treated as paused again after a while.
    """
    ids = register_shape(hass, SHAPES["peblar"])
    clock = Clock()
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=clock)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    calls = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("switch.peblar_charge", "off")

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


# ------------------------------------------------------------------------------------------ Easee


async def _easee(hass: HomeAssistant, clock: Clock):
    ids = register_shape(hass, SHAPES["easee"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=clock)
    limits = async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    return ids, adapter, limits


async def test_easee_sets_the_dynamic_limit_with_no_expiry(hass: HomeAssistant) -> None:
    ids, adapter, limits = await _easee(hass, Clock())

    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED

    assert [dict(call.data) for call in limits] == [
        {"device_id": ids["device_id"], "current": 12, "time_to_live": 0}
    ]
    assert adapter.policy.max_writes_per_minute == 20 and adapter.policy.resend_after_plug_in
    assert adapter.capabilities.regulated_current is True


async def test_easee_does_not_trust_its_own_memory_of_a_write_after_a_plug_in(hass: HomeAssistant) -> None:
    """With no read-back the last value sent may have been cleared by the charger since."""
    clock = Clock()
    ids, adapter, limits = await _easee(hass, clock)
    hass.states.async_set("sensor.easee_status", "charging")
    assert await adapter.async_set_current(12, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    clock.advance(1)

    assert await adapter.async_set_current(12, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED

    assert [call.data["current"] for call in limits] == [12, 12]


async def test_easee_skips_a_value_the_charger_already_reports(hass: HomeAssistant) -> None:
    ids, adapter, limits = await _easee(hass, Clock())
    enable_easee_limit_sensor(hass, "12")

    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_UNCHANGED
    assert limits == []


async def test_easee_never_exceeds_twenty_settings_changes_a_minute(hass: HomeAssistant) -> None:
    clock = Clock()
    ids, adapter, limits = await _easee(hass, clock)
    enable_easee_limit_sensor(hass, "99")

    outcomes = []
    for index in range(25):
        outcomes.append(await adapter.async_set_current(6 + (index % 2) * 2, reason=WRITE_REGULATOR))
        clock.advance(1)

    assert outcomes.count(ASSIGN_ASSIGNED) == 20
    assert outcomes[20:] == [ASSIGN_RATE_LIMITED] * 5
    assert len(limits) == 20
    clock.advance(60)
    assert await adapter.async_set_current(9, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED


async def test_easee_commands_count_against_the_budget_but_are_never_refused(hass: HomeAssistant) -> None:
    clock = Clock()
    ids, adapter, limits = await _easee(hass, clock)
    commands = async_mock_service(hass, "easee", "action_command")
    enable_easee_limit_sensor(hass, "99")

    for _ in range(30):
        await adapter.async_stop()

    assert len(commands) == 30
    assert await adapter.async_set_current(9, reason=WRITE_REGULATOR) == ASSIGN_RATE_LIMITED


async def test_easee_sends_the_limit_again_after_a_plug_in_and_a_reboot(hass: HomeAssistant) -> None:
    ids, adapter, limits = await _easee(hass, Clock())
    current = adapter.current

    assert current.needs_resend("disconnected", "awaiting_start") is True
    assert current.needs_resend("unavailable", "charging") is True  # a reboot
    assert current.needs_resend("awaiting_start", "charging") is False
    assert current.needs_resend("charging", "disconnected") is False


async def test_a_resend_reaches_the_charger_even_though_the_service_skips_an_unchanged_value(
    hass: HomeAssistant,
) -> None:
    """The service short-circuits on its cached value, so a resend of the same number would do
    nothing: it is preceded by one amp lower, the safe direction, and then the wanted value.
    """
    clock = Clock()
    ids, adapter, limits = await _easee(hass, clock)
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    limits.clear()
    clock.advance(5)

    from custom_components.spotnav.execution.charger_adapter import WRITE_RESEND

    assert await adapter.async_set_current(12, reason=WRITE_RESEND) == ASSIGN_ASSIGNED

    assert [call.data["current"] for call in limits] == [11, 12]


async def test_an_easee_write_the_charger_never_reports_marks_the_cache_suspect(hass: HomeAssistant) -> None:
    clock = Clock()
    ids, adapter, limits = await _easee(hass, clock)
    enable_easee_limit_sensor(hass, "16")
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    limits.clear()

    # Well after the write the charger still reports 16: the service did nothing.
    clock.advance(60)
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert [call.data["current"] for call in limits] == [9, 10]


async def test_an_easee_limit_is_read_back_when_asked(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.spotnav.execution import charger_adapter

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(charger_adapter.asyncio, "sleep", no_wait)
    ids, adapter, limits = await _easee(hass, Clock())
    enable_easee_limit_sensor(hass, "16")

    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR, verify=True) == "unconfirmed"
    set_easee_limit(hass, "10")
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR, verify=True) == ASSIGN_UNCHANGED


# ------------------------------------------------------------------------------------------ OCPP


async def test_the_ocpp_adapter_is_the_original_path_with_no_policy_of_its_own(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.cp_charge_control", "off")
    config = {
        CONF_MODE: MODE_OCPP,
        CONF_CHARGE_CONTROL: "switch.cp_charge_control",
        CONF_CURRENT_LIMIT: "",
        CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
        CONF_OCPP_CHARGE_POINT_ID: "cp",
        CONF_OCPP_CONNECTOR_ID: 1,
    }
    adapter = build_adapter(
        hass, config, ocpp_target=lambda: OcppConnectorTarget("cp", 1), energy_entity_id=None
    )

    assert adapter.is_ocpp
    assert adapter.policy.min_interval_s == 0 and not adapter.policy.flash_stored
    assert adapter.capabilities.regulated_current is True
    assert adapter.describe()["current"]["service"] == "ocpp.configure"
    assert adapter.describe()["start_stop"]["kind"] == "switch"


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


async def test_an_ocpp_entry_with_the_number_path_writes_the_number_under_the_local_policy(hass) -> None:
    from custom_components.spotnav.const import (
        CONF_CHARGE_CONTROL,
        CONF_CURRENT_CONTROL,
        CONF_CURRENT_LIMIT,
        CONF_MODE,
        MODE_OCPP,
    )
    from custom_components.spotnav.execution.charger_adapter import NumberCurrent

    adapter = build_adapter(
        hass,
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: "switch.x_connector_1_charge_control",
            CONF_CURRENT_CONTROL: "number",
            CONF_CURRENT_LIMIT: "number.x_connector_1_session_current_limit",
        },
        ocpp_target=lambda: None,
        energy_entity_id=None,
    )

    assert isinstance(adapter.current, NumberCurrent)
    assert adapter.policy.min_interval_s == 10.0 and adapter.policy.zero_pauses is False


async def _easee_commands(hass: HomeAssistant, clock: Clock | None = None):
    ids, adapter, limits = await _easee(hass, clock or Clock())
    commands = async_mock_service(hass, "easee", "action_command")
    return adapter, commands, limits


def _names(commands) -> list[str]:
    return [call.data["action_command"] for call in commands]


async def test_easee_start_from_awaiting_start_resumes(hass: HomeAssistant) -> None:
    adapter, commands, _ = await _easee_commands(hass)
    hass.states.async_set("sensor.easee_status", "awaiting_start")

    await adapter.async_start()

    assert _names(commands) == ["resume"]


async def test_easee_start_from_awaiting_authorization_authorizes_then_resumes(hass: HomeAssistant) -> None:
    adapter, commands, _ = await _easee_commands(hass)
    hass.states.async_set("sensor.easee_status", "awaiting_authorization")

    await adapter.async_start()

    assert _names(commands) == ["start", "resume"]


async def test_easee_stop_pauses_and_never_deauthorizes(hass: HomeAssistant) -> None:
    adapter, commands, _ = await _easee_commands(hass)
    hass.states.async_set("sensor.easee_status", "charging")

    await adapter.async_stop()

    assert _names(commands) == ["pause"]


async def test_easee_writes_no_dynamic_limit_while_paused_and_sends_it_after_the_resume(
    hass: HomeAssistant,
) -> None:
    adapter, commands, limits = await _easee_commands(hass)
    hass.states.async_set("sensor.easee_status", "charging")
    await adapter.async_stop()
    hass.states.async_set("sensor.easee_status", "awaiting_start")

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_IGNORED_WHILE_PAUSED
    assert limits == []

    await adapter.async_start()
    hass.states.async_set("sensor.easee_status", "charging")
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert [call.data["current"] for call in limits] == [10]


async def test_easee_does_not_hold_back_a_limit_when_it_never_paused(hass: HomeAssistant) -> None:
    adapter, _, limits = await _easee_commands(hass)
    hass.states.async_set("sensor.easee_status", "awaiting_start")

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert len(limits) == 1


async def test_easee_resends_the_limit_after_a_plug_in_that_followed_a_pause(hass: HomeAssistant) -> None:
    adapter, _, limits = await _easee_commands(hass)
    hass.states.async_set("sensor.easee_status", "charging")
    await adapter.async_stop()
    hass.states.async_set("sensor.easee_status", "disconnected")
    hass.states.async_set("sensor.easee_status", "awaiting_start")
    assert adapter.current.needs_resend("disconnected", "awaiting_start") is True

    assert await adapter.async_set_current(8, reason=WRITE_RESEND) == ASSIGN_ASSIGNED
    assert limits[-1].data["current"] == 8
