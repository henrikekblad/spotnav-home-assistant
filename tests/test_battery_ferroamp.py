"""Ferroamp's control recipe (henricm/ha-ferroamp `b18097f`): the services, the read-back and the hand-back."""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.spotnav.battery.adapters import FERROAMP, adapter_for
from custom_components.spotnav.battery.commands import (
    FERROAMP_CONTROL,
    NORMAL,
    Role,
    SETPOINT_WRITES_UNSTATED,
    answered,
    control_for,
    ferroamp_last_cmd,
    service_call,
    table,
)
from custom_components.spotnav.battery.executor import Executor
from tests.battery_control_fakes import FERROAMP_STATES, T0, FakeBattery, events, run, written


def test_the_recipe_is_in_the_table_and_covers_the_house_with_auto() -> None:
    assert control_for("ferroamp") is not None and adapter_for("ferroamp") is FERROAMP
    actions = {row["action"] for row in table() if row["brand"] == "ferroamp"}
    assert actions == {"hold", "grid_charge", "discharge_house", "export_discharge", "self_consume"}
    assert FERROAMP_CONTROL.wanted("discharge_house", 3.0)["command"] == "auto"
    assert FERROAMP_CONTROL.write_cap("setpoint") == SETPOINT_WRITES_UNSTATED
    assert FERROAMP_CONTROL.write_cap("setting") == 96
    assert NORMAL["ferroamp"] == {"command": "auto"}


def test_the_power_is_the_whole_systems_in_whole_500_w_steps() -> None:
    assert FERROAMP_CONTROL.wanted("grid_charge", 3.2)["command"] == "charge:3000"
    assert FERROAMP_CONTROL.wanted("export_discharge", 5.0)["command"] == "discharge:5000"
    assert FERROAMP_CONTROL.wanted("hold", 0)["command"] == "charge:0"
    assert control_for("ferroamp", "discharge_zero").wanted("hold", 0)["command"] == "discharge:0"


def test_the_last_request_sensor_reads_as_a_comparable_value() -> None:
    assert ferroamp_last_cmd("charge (1000)") == "charge:1000"
    assert ferroamp_last_cmd("discharge (500)") == "discharge:500"
    assert ferroamp_last_cmd("auto") == "auto"
    assert ferroamp_last_cmd("unknown") == "auto" and ferroamp_last_cmd(None) == "auto"
    assert ferroamp_last_cmd("unavailable") is None


def test_the_services_take_the_hubs_device_as_target_and_a_request_has_no_duration() -> None:
    services = FERROAMP_CONTROL.role("command").services
    assert service_call("service", "sensor.x", "charge:3000", device_id="D", services=services) == (
        "ferroamp", "charge", {"target": "D", "power": 3000}
    )
    assert service_call("service", "sensor.x", "discharge:0", device_id="D", services=services)[2] == {
        "target": "D", "power": 0
    }
    assert service_call("service", "sensor.x", "auto", device_id="D", services=services) == (
        "ferroamp", "autocharge", {"target": "D"}
    )


def test_a_charge_is_sent_and_handed_back_with_auto() -> None:
    executor = Executor(FERROAMP_CONTROL)
    battery = FakeBattery(FERROAMP_CONTROL, dict(FERROAMP_STATES))
    now, results = run(executor, battery, T0, 4, "grid_charge", 3.0)
    assert written(results) == [("command", "charge:3000")]
    assert battery.states["command"] == "charge (3000)"
    assert battery.writes[0].device_id == "DEVICE"
    now, results = run(executor, battery, now, 4, "hold")
    assert written(results) == [("command", "charge:0")]
    _now, results = run(executor, battery, now, 6, "self_consume")
    assert written(results) == [("command", "auto")]
    assert ferroamp_last_cmd(battery.states["command"]) == "auto"


def test_a_request_left_by_someone_else_is_not_overwritten() -> None:
    executor = Executor(FERROAMP_CONTROL)
    battery = FakeBattery(FERROAMP_CONTROL, {"command": "charge (2000)"})
    _now, results = run(executor, battery, T0, 4, "grid_charge", 3.0)
    assert written(results) == []
    assert executor.state.phase != "active"


@pytest.mark.parametrize("action", ["hold", "grid_charge", "discharge_house", "export_discharge"])
def test_every_action_ends_with_the_hub_back_on_auto(action: str) -> None:
    executor = Executor(FERROAMP_CONTROL)
    battery = FakeBattery(FERROAMP_CONTROL, dict(FERROAMP_STATES))
    now, _ = run(executor, battery, T0, 8, action, 3.0)
    assert executor.state.phase == "active"
    run(executor, battery, now, 240, action, 3.0, stopping="stop")
    assert executor.state.phase in ("idle", "blocked") and ferroamp_last_cmd(battery.states["command"]) == "auto"


def test_with_the_days_requests_spent_a_rate_that_must_fall_hands_the_hub_back() -> None:
    """Every Ferroamp request is a setpoint: once the day's are spent, a lower rate is not left unwritten (the old
    rate would stand); the hub is handed back with `auto`, which is never counted."""
    executor = Executor(FERROAMP_CONTROL)
    battery = FakeBattery(FERROAMP_CONTROL, dict(FERROAMP_STATES))
    now, _ = run(executor, battery, T0, 4, "grid_charge", 3.0)
    assert battery.states["command"] == "charge (3000)"
    executor.state = replace(executor.state, setpoint_writes_today=SETPOINT_WRITES_UNSTATED)
    _now, results = run(executor, battery, now, 6, "grid_charge", 1.0)
    assert events(results, "hand_back")[0]["reason"] == "setpoint_write_cap"
    assert written(results) == [("command", "auto")] and battery.states["command"] == "auto"


def test_the_last_request_counts_only_once_the_hub_acked_it() -> None:
    role = FERROAMP_CONTROL.role("command")
    # No request since the integration started (or a restart: the attributes are not restored): the state as is.
    assert answered(role, "auto", {}) == "auto" and answered(role, "charge (1000)", None) == "charge (1000)"
    # Published, the answer awaited: no reading yet.
    assert answered(role, "auto", {"transId": "t1", "status": None, "message": None}) == "unavailable"
    assert answered(role, "auto", {"transId": "t1", "status": "ack"}) == "auto"
    assert answered(role, "auto", {"transId": "t1", "status": "nak", "message": "Other transaction in progress"}) == (
        "unavailable"
    )
    assert ferroamp_last_cmd(answered(role, "auto", {"transId": "t1", "status": "nak"})) is None
    # A role without an answer is read as before.
    assert answered(Role("x", "number", role.match), "5", {"transId": "t", "status": None}) == "5"


def test_a_refused_hand_back_is_not_confirmed_by_its_own_request_and_is_sent_again() -> None:
    """The hub naks `auto` (another transaction runs). The sensor shows `auto` all the same; the hand-back is not
    done until a later `auto` is acked."""
    executor = Executor(FERROAMP_CONTROL)
    battery = FakeBattery(FERROAMP_CONTROL, dict(FERROAMP_STATES))
    now, _ = run(executor, battery, T0, 4, "grid_charge", 3.0)
    assert executor.state.phase == "active"
    battery.refuse_next = 1
    now, results = run(executor, battery, now, 4, "self_consume")
    assert written(results) == [("command", "auto")] and battery.states["command"] == "auto"
    assert executor.state.phase == "restoring", "a nak'd hand-back is not confirmed by the request it shows"
    _now, results = run(executor, battery, now, 15, "self_consume")
    assert written(results) == [("command", "auto")] and battery.attributes["command"]["status"] == "ack"
    assert executor.state.phase == "idle"


def test_a_refused_charge_is_never_taken_as_running() -> None:
    executor = Executor(FERROAMP_CONTROL)
    battery = FakeBattery(FERROAMP_CONTROL, dict(FERROAMP_STATES), refuse_next=1)
    _now, results = run(executor, battery, T0, 12, "grid_charge", 3.0)
    assert written(results) == [("command", "charge:3000"), ("command", "auto")]
    assert events(results, "not_taken")[0]["why"] == "no_read_back"
    assert [event["read"] for event in events(results, "confirmed")] == ["auto"]
    assert executor.state.phase == "blocked"


def test_the_shell_reads_the_hubs_answer_from_the_attributes() -> None:
    from homeassistant.core import State

    from custom_components.spotnav.battery.control import stored_reads
    from custom_components.spotnav.battery.executor import role_reads
    from custom_components.spotnav.battery.facts import EntityFact

    attrs = {"transId": "t1", "status": "nak", "message": "Other transaction in progress"}
    states = {"sensor.hub_last_cmd": State("sensor.hub_last_cmd", "auto", attrs)}
    hass = type("Hass", (), {"states": type("States", (), {"get": staticmethod(states.get)})()})()
    executor = Executor(FERROAMP_CONTROL)
    executor.state = replace(executor.state, entities={"command": "sensor.hub_last_cmd"})
    assert stored_reads(hass, executor)["command"].state == "unavailable"  # type: ignore[arg-type]
    fact = EntityFact("sensor.hub_last_cmd", "ferroamp", unique_id="hub_last_cmd", state="auto", attributes=attrs)
    assert role_reads(FERROAMP_CONTROL, [fact], fact)["command"].state == "unavailable"
    acked = replace(fact, attributes=attrs | {"status": "ack"})
    assert role_reads(FERROAMP_CONTROL, [acked], acked)["command"].state == "auto"
