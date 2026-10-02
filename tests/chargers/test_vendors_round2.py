"""The vendor behaviours of the second charger audit, from the integrations' recorded entity shapes:
what Start and Stop call, what a current write may do, and what each charger's own modes and statuses
make of both.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_UNCHANGED,
    WRITE_REGULATOR,
    WRITE_RESEND,
    WRITE_SESSION_START,
)
from custom_components.spotnav.execution.charger_entities import option_for
from custom_components.spotnav.execution.charger_profiles import profile_for, ROLE_UNSUPPORTED
from custom_components.spotnav.flows.charger_detection import detect_charger

from ..charger_helpers import adapter_for, Clock
from ..charger_shapes import register_shape, SHAPES


def _adapter(hass: HomeAssistant, platform: str, clock: Clock | None = None):
    ids = register_shape(hass, SHAPES[platform])
    return adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=clock)


# --- simple start and stop calls ----------------------------------------------------------------

BUTTONS = {
    "chargepoint": ("button.chargepoint_start_charging_session", "button.chargepoint_stop_charging_session"),
    "defa_power": ("button.defa_power_start_charging", "button.defa_power_stop_charging"),
    "wattpilot": ("button.wattpilot_frc2", "button.wattpilot_frc1"),
}


async def test_the_button_pairs_press_their_own_buttons(hass: HomeAssistant) -> None:
    for platform, (start, stop) in BUTTONS.items():
        adapter = _adapter(hass, platform)
        presses = async_mock_service(hass, "button", "press")

        await adapter.async_start()
        await adapter.async_stop()

        assert [call.data["entity_id"] for call in presses] == [start, stop], platform


async def test_sma_starts_with_boost_charging_and_stops_with_charge_stop_never_with_optimised(
    hass: HomeAssistant,
) -> None:
    adapter = _adapter(hass, "smaev")
    calls = async_mock_service(hass, "select", "select_option")

    await adapter.async_start()
    await adapter.async_stop()

    assert [call.data["option"] for call in calls] == ["boost_charging", "charge_stop"]
    # PV-surplus charging is neither started nor stopped by us: the control reads neutral.
    assert adapter.enabled_state() is None


async def test_garo_stops_with_always_off_and_reads_its_own_status(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "garo_wallbox")
    calls = async_mock_service(hass, "select", "select_option")

    await adapter.async_start()
    await adapter.async_stop()

    assert [call.data["option"] for call in calls] == ["ALWAYS_ON", "ALWAYS_OFF"]
    assert adapter.charging_state() is False  # CHARGING_PAUSED
    hass.states.async_set("sensor.garo_wallbox_status", "CHARGING")
    assert adapter.charging_state() is True
    assert adapter.progress_status() == "Charging"
    hass.states.async_set("sensor.garo_wallbox_status", "CHARGING_FINISHED")
    assert adapter.progress_status() == "SuspendedEV"


async def test_the_charge_status_sensors_decide_charging_where_the_switch_only_allows_it(
    hass: HomeAssistant,
) -> None:
    for platform, entity_id, charging, idle in (
        ("goecharger", "sensor.goecharger_car_status", "charging", "Waiting for vehicle"),
        ("wattpilot", "sensor.wattpilot_car", "Charging", "Wait Car"),
        ("alfen_modbus", "sensor.alfen_modbus_mode_3_state", "D2", "C1"),
        ("heidelberg_energy_control", "sensor.heidelberg_energy_control_charging_state", "C", "B"),
        ("smartevse", "sensor.smartevse_smartevse_state", "Charging", "Connected to EV"),
        ("smaev", "sensor.smaev_charging_session_status", "active_mode", "sleep_mode"),
        ("defa_power", "sensor.defa_power_charging_state", "charging", "suspended_ev"),
        ("ohme", "sensor.ohme_status", "charging", "plugged_in"),
        ("webasto_next_modbus", "sensor.webasto_next_modbus_charging_state", "charging", "idle"),
        ("abb_terra_ac", "sensor.abb_terra_ac_charging_state", "State C2 - Charging", "State B2 - EV Plug in, charging complete"),
        ("openevse", "sensor.openevse_status", "charging", "connected"),
        ("peblar", "sensor.peblar_cp_state", "charging", "suspended"),
    ):
        adapter = _adapter(hass, platform)
        hass.states.async_set(entity_id, charging)
        assert adapter.charging_state() is True, platform
        hass.states.async_set(entity_id, idle)
        assert adapter.charging_state() is False, platform


async def test_go_e_api2_in_german_is_charging_too(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "goecharger_api2")
    hass.states.async_set("sensor.goecharger_api2_car", "Laden")

    assert adapter.charging_state() is True


async def test_wallbox_queue_statuses_are_the_chargers_own_hold(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "wallbox")

    hass.states.async_set("sensor.wallbox_status_description", "Waiting in queue by Power Sharing")
    assert adapter.held_by_charger() is True
    hass.states.async_set("sensor.wallbox_status_description", "Paused")
    assert adapter.held_by_charger() is False


def test_pod_point_is_not_driven() -> None:
    profile = profile_for("pod_point")

    assert profile is not None and profile.role == ROLE_UNSUPPORTED and "schedule" in profile.note


def test_a_profile_can_prefer_one_of_two_stop_options() -> None:
    assert option_for(["OFF", "NORMAL", "SOLAR", "SMART", "PAUSE"], ("pause", "off")) == "PAUSE"
    assert option_for(["OFF", "NORMAL"], ("pause", "off")) == "OFF"
    assert option_for(["Neutral"], ("charge",)) is None


# --- OpenEVSE: the number is not the setpoint ---------------------------------------------------


async def test_openevse_writes_again_though_the_number_shows_the_target(hass: HomeAssistant) -> None:
    """The number reads the stored maximum (32 A) while a write is a claim: after lowering to 10 A a
    request for the maximum would see "already 32" and never be written, and the claim stays at 10 A.
    """
    clock = Clock()
    adapter = _adapter(hass, "openevse", clock)
    calls = async_mock_service(hass, "number", "set_value")

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    clock.advance(11)
    assert await adapter.async_set_current(32, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED

    assert [call.data["value"] for call in calls] == [10, 32]
    assert adapter.current.setpoint_a() == 32


async def test_openevse_does_not_repeat_its_own_last_regulator_write(hass: HomeAssistant) -> None:
    clock = Clock()
    adapter = _adapter(hass, "openevse", clock)
    calls = async_mock_service(hass, "number", "set_value")

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    clock.advance(11)
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_UNCHANGED
    clock.advance(11)
    assert await adapter.async_set_current(10, reason=WRITE_RESEND) == ASSIGN_ASSIGNED

    assert [call.data["value"] for call in calls] == [10, 10]


async def test_openevse_sends_the_claim_again_after_a_plug_in(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "openevse")
    calls = async_mock_service(hass, "number", "set_value")
    await adapter.async_set_current(12, reason=WRITE_SESSION_START)
    current = adapter.current

    assert current.needs_resend("not_connected", "connected") is True
    assert current.needs_resend("unavailable", "charging") is True
    assert current.needs_resend("connected", "charging") is False
    assert current.needs_resend("charging", "not_connected") is False
    assert current.setpoint_a() is None  # the claim went with the session
    assert len(calls) == 1


async def test_a_plain_number_never_asks_for_a_resend(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "lektrico")

    assert adapter.current.needs_resend("disconnected", "connected") is False


# --- SmartEVSE: Start puts back the mode the person had ------------------------------------------


async def test_smartevse_start_restores_smart_after_a_stop(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "smartevse")
    options = {"options": ["OFF", "NORMAL", "SOLAR", "SMART", "PAUSE"]}
    calls = async_mock_service(hass, "select", "select_option")
    entity = "select.smartevse_smartevse_mode_id"
    hass.states.async_set(entity, "SMART", options)

    await adapter.async_stop()
    hass.states.async_set(entity, "PAUSE", options)
    assert adapter.enabled_state() is False
    await adapter.async_start()

    assert [call.data["option"] for call in calls] == ["PAUSE", "SMART"]
    hass.states.async_set(entity, "SMART", options)
    assert adapter.enabled_state() is True  # Smart is enabled, not "neutral"


async def test_smartevse_start_falls_back_to_normal_when_nothing_is_remembered_or_offered(
    hass: HomeAssistant,
) -> None:
    adapter = _adapter(hass, "smartevse")
    calls = async_mock_service(hass, "select", "select_option")
    entity = "select.smartevse_smartevse_mode_id"
    hass.states.async_set(entity, "PAUSE", {"options": ["OFF", "NORMAL", "SOLAR", "SMART", "PAUSE"]})

    await adapter.async_start()
    assert calls[-1].data["option"] == "NORMAL"

    # Smart is withdrawn while no mains meter is configured: Normal then.
    hass.states.async_set(entity, "SMART", {"options": ["OFF", "NORMAL", "PAUSE"]})
    await adapter.async_stop()
    hass.states.async_set(entity, "PAUSE", {"options": ["OFF", "NORMAL", "PAUSE"]})
    await adapter.async_start()
    assert calls[-1].data["option"] == "NORMAL"


async def test_smartevse_stops_with_pause_not_off(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "smartevse")
    calls = async_mock_service(hass, "select", "select_option")

    await adapter.async_stop()

    assert calls[0].data["option"] == "PAUSE"
    assert adapter.describe()["start_stop"]["kind"] == "select"


# --- ABB Terra AC: a pause is 0 A -------------------------------------------------------------


async def test_abb_stop_writes_zero_to_the_limit_and_never_presses_the_stop_button(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "abb_terra_ac")
    presses = async_mock_service(hass, "button", "press")
    writes = async_mock_service(hass, "number", "set_value")

    assert await adapter.async_stop() is True

    assert presses == []
    assert [(call.data["entity_id"], call.data["value"]) for call in writes] == [
        ("number.abb_terra_ac_current_limit", 0)
    ]


async def test_abb_start_writes_the_requested_current_and_resumes_at_the_old_limit_without_one(
    hass: HomeAssistant,
) -> None:
    adapter = _adapter(hass, "abb_terra_ac")
    writes = async_mock_service(hass, "number", "set_value")
    number = "number.abb_terra_ac_current_limit"
    attrs = {"unit_of_measurement": "A", "min": 0, "max": 32, "step": 1}

    await adapter.async_stop()  # remembers the 16 A it was running at
    hass.states.async_set(number, "0", attrs)
    assert adapter.enabled_state() is False

    await adapter.async_start(10)
    await adapter.async_start()
    await adapter.async_start(40)  # above the number's maximum

    assert [call.data["value"] for call in writes] == [0, 10, 16, 32]


async def test_abb_start_presses_the_start_button_only_while_authorization_is_pending(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "abb_terra_ac")
    presses = async_mock_service(hass, "button", "press")
    async_mock_service(hass, "number", "set_value")

    await adapter.async_start(10)
    assert presses == []

    hass.states.async_set("sensor.abb_terra_ac_charging_state", "State B1 - EV Plug in, pending authorization")
    await adapter.async_start(10)
    assert [call.data["entity_id"] for call in presses] == ["button.abb_terra_ac_start_charging"]


async def test_abb_is_enabled_by_the_limit_not_by_a_switch(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "abb_terra_ac")
    number = "number.abb_terra_ac_current_limit"

    hass.states.async_set(number, "0", {"unit_of_measurement": "A"})
    assert adapter.enabled_state() is False
    hass.states.async_set(number, "10", {"unit_of_measurement": "A"})
    assert adapter.enabled_state() is True
    hass.states.async_set(number, "unavailable")
    assert adapter.enabled_state() is None
    assert adapter.describe()["start_stop"]["kind"] == "number_pause"


# --- Webasto Next: the command register must change -------------------------------------------


async def test_webasto_start_changes_the_register_by_cancelling_first_unless_the_last_command_was_a_cancel(
    hass: HomeAssistant,
) -> None:
    adapter = _adapter(hass, "webasto_next_modbus")
    presses = async_mock_service(hass, "button", "press")
    start, stop = "button.webasto_next_modbus_start_session", "button.webasto_next_modbus_stop_session"

    await adapter.async_start()  # nothing known: the register may hold 1
    await adapter.async_start()  # the last command was a start
    await adapter.async_stop()
    await adapter.async_start()  # the last command was a cancel: 2 to 1 is a change already

    assert [call.data["entity_id"] for call in presses] == [stop, start, stop, start, stop, start]


async def test_webasto_does_not_start_when_the_cancel_cannot_be_sent(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "webasto_next_modbus")
    presses = async_mock_service(hass, "button", "press")
    hass.states.async_set("button.webasto_next_modbus_stop_session", "unavailable")

    assert await adapter.async_start() is False
    assert presses == []


# --- Ohme: approve a pending charge ----------------------------------------------------------


async def test_ohme_start_approves_a_pending_charge_first(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "ohme")
    presses = async_mock_service(hass, "button", "press")
    selects = async_mock_service(hass, "select", "select_option")
    hass.states.async_set("sensor.ohme_status", "pending_approval")
    hass.states.async_set("select.ohme_charge_mode", "unavailable")

    assert await adapter.async_start() is True

    assert [call.data["entity_id"] for call in presses] == ["button.ohme_approve"]
    assert selects == []  # the select is unavailable until the charger has a mode


async def test_ohme_start_selects_max_charge_otherwise(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, "ohme")
    presses = async_mock_service(hass, "button", "press")
    selects = async_mock_service(hass, "select", "select_option")

    await adapter.async_start()

    assert presses == [] and [call.data["option"] for call in selects] == ["max_charge"]


# --- Peblar: three pauses in ten minutes -------------------------------------------------------


async def test_peblar_pauses_three_times_in_ten_minutes_then_holds_at_the_floor(hass: HomeAssistant) -> None:
    clock = Clock()
    adapter = _adapter(hass, "peblar", clock)
    offs = async_mock_service(hass, "switch", "turn_off")
    numbers = async_mock_service(hass, "number", "set_value")
    switch = "switch.peblar_charge"

    for _ in range(3):
        hass.states.async_set(switch, "on")
        assert await adapter.async_stop() is True
        clock.advance(60)
    hass.states.async_set(switch, "on")
    assert await adapter.async_stop() is True

    assert len(offs) == 3
    assert [(call.data["entity_id"], call.data["value"]) for call in numbers] == [
        ("number.peblar_charge_current_limit", 6)
    ]

    clock.advance(600)  # the first pause has left the window
    hass.states.async_set(switch, "on")
    assert await adapter.async_stop() is True
    assert len(offs) == 4


async def test_peblar_counts_no_pause_for_a_stop_while_already_paused(hass: HomeAssistant) -> None:
    clock = Clock()
    adapter = _adapter(hass, "peblar", clock)
    offs = async_mock_service(hass, "switch", "turn_off")
    hass.states.async_set("switch.peblar_charge", "off")

    for _ in range(5):
        await adapter.async_stop()

    assert len(offs) == 5
    hass.states.async_set("switch.peblar_charge", "on")
    await adapter.async_stop()
    assert len(offs) == 6


# --- site detection helpers used by the shapes ------------------------------------------------


async def test_an_ampere_number_the_integration_disables_is_still_found_and_offered(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["smaev"])

    found = detect_charger(hass, ids["device_id"])

    assert found.current_limit == "number.smaev_charge_current_limit"
    assert found.disabled_useful == ["number.smaev_charge_current_limit"]


async def test_defa_lifetime_register_is_the_total_meter_value_not_the_transaction_counter(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["defa_power"])

    found = detect_charger(hass, ids["device_id"])

    assert found.energy_register == "sensor.defa_power_meter_value"
    assert found.session_energy_register is None


async def test_a_key_that_ends_another_does_not_make_the_longer_one_a_lifetime_register(
    hass: HomeAssistant,
) -> None:
    """Lektrico's `energy` (session) is the tail of `lifetime_energy`: the longer key is what it is."""
    ids = register_shape(hass, SHAPES["lektrico"])

    found = detect_charger(hass, ids["device_id"])

    assert found.energy_register == "sensor.lektrico_lifetime_energy"


async def test_a_wattpilot_22_kw_fork_is_detected_for_current(hass: HomeAssistant) -> None:
    from ..charger_shapes import E, NUMBER_A as AMPERES, Shape

    shape = Shape(
        "wattpilot",
        "WPF",
        (
            E("button", "WPF-frc2", "frc2"),
            E("button", "WPF-frc1", "frc1"),
            E("number", "WPF-amp_22kw", "amp_22kw", "32", {**AMPERES, "max": 32}),
            E("sensor", "WPF-car_state", "car_state", "Charging"),
        ),
        {},
    )
    ids = register_shape(hass, shape)

    found = detect_charger(hass, ids["device_id"])

    assert found.current_limit == "number.wattpilot_amp_22kw"
    assert found.charging_state is not None and found.charging_state["entity_id"] == "sensor.wattpilot_car_state"


async def test_a_dual_socket_alfen_modbus_is_detected_by_its_socket_keys(hass: HomeAssistant) -> None:
    from ..charger_shapes import E, NUMBER_A as AMPERES, Shape

    shape = Shape(
        "alfen_modbus",
        "AM2",
        (
            E("switch", "AM2_socket_1_chargerEnabled", "charger_enabled_socket", "on", translation_key=True),
            E("number", "AM2_maxCurrent_socket_1", "max_current_limit_socket", "16", {**AMPERES, "min": 0}, translation_key=True),
            E("sensor", "AM2_socket_1_mode3state", "mode_3_state_socket", "C2", translation_key=True),
        ),
        {},
    )
    ids = register_shape(hass, shape)

    found = detect_charger(hass, ids["device_id"])

    assert found.charge_control == "switch.alfen_modbus_charger_enabled_socket"
    assert found.current_limit == "number.alfen_modbus_max_current_limit_socket"
    assert found.charging_state is not None


async def test_the_start_is_not_acknowledged_for_a_service_that_was_never_sent(hass: HomeAssistant) -> None:
    """Every new path reports `False` for an unavailable control, as the generic ones do."""
    adapter = _adapter(hass, "abb_terra_ac")
    writes = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("number.abb_terra_ac_current_limit", "unavailable")

    assert await adapter.async_stop() is False
    assert await adapter.async_start(10) is False
    assert writes == []

