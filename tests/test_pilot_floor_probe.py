"""The pilot-floor probe, a diagnostic that runs only when an administrator asks.

No socket: OCPP services are mocked, the clock is moved by hand and the thirty-second holds return at once.
Each test pins the dangerous part: nothing starts the probe but the request, nothing but the probe writes a
current while it runs, and the charger is always put back where it was found, also after a restart.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
)
from custom_components.spotnav.execution.controller import (
    ChargingController,
    PROBE_RESTORE_RETRY_S,
    PROBE_RESTORE_TRIES,
)
from tests.helpers import install_schedule
from custom_components.spotnav.execution.pilot_floor_probe import (
    FLOOR_FOR_S,
    PROBE_STEPS,
    PROBE_TOKEN,
    STORE_KEY,
    PilotFloorProbe,
    probe_refusal,
)

# The mocked slot: connector 1 at 16 A, connector 2 at 10 A. Every probe write must carry connector 2 through untouched, since `AssignedCurrent` is one string for the whole charger.
_ASSIGNED = "1.16,2.10"


def _controller(hass: HomeAssistant, entry_id: str = "entry_a") -> ChargingController:
    """An opted-in controller whose current-limit entity is per-connector OCPP."""
    return ChargingController(
        hass,
        entry_id,
        {
            CONF_CHARGE_CONTROL: "switch.a",
            CONF_CURRENT_LIMIT: "number.charger_connector_1_session_current_limit",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_CHARGE_POINT_ID: "charger",
            CONF_OCPP_CONNECTOR_ID: 1,
        },
    )


class _Clock:
    """A clock advanced by hand, so FLOOR_FOR_S costs no wall time."""

    def __init__(self) -> None:
        self._now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now


async def _no_wait(_seconds: float) -> None:
    """Stand-in for the thirty-second hold."""


def _mocked_ocpp(hass: HomeAssistant, response: object = {"value": _ASSIGNED}) -> list:
    """Mock the two OCPP services and return the configure call log."""
    async_mock_service(hass, "ocpp", "get_configuration", response=response)
    return async_mock_service(hass, "ocpp", "configure")


def _values(configure_calls: list) -> list[str]:
    return [call.data["value"] for call in configure_calls]


_GATE = {
    "interrupted": False,
    "current_control": CURRENT_CONTROL_CHANGE_CONFIGURATION,
    "has_current_limit": True,
    "plan_active": True,
    "charging": True,
    "at_or_above_floor_for_s": FLOOR_FOR_S,
}


def test_the_gate_opens_when_every_condition_holds() -> None:
    assert probe_refusal(**_GATE) is None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"interrupted": True}, "interrupted_probe_not_restored"),
        ({"current_control": ""}, "current_control_is_not_change_configuration"),
        ({"has_current_limit": False}, "no_current_limit_entity"),
        ({"plan_active": False}, "no_active_plan"),
        ({"charging": False}, "charge_not_running"),
        ({"at_or_above_floor_for_s": 0.0}, "drawing_below_5a_for_60s"),
        ({"at_or_above_floor_for_s": FLOOR_FOR_S - 1}, "drawing_below_5a_for_60s"),
    ],
)
def test_each_missing_condition_refuses_the_probe(change: dict, expected: str) -> None:
    """Each precondition is refused in turn and named in the log; the reason is a word so the log line says which condition stopped the probe."""
    assert probe_refusal(**{**_GATE, **change}) == expected


def test_a_car_drawing_half_an_amp_never_opens_the_gate() -> None:
    """A car sitting at half an amp: why the probe insists on a floor and a duration, not merely being plugged in."""
    clock = _Clock()
    probe = PilotFloorProbe(None, now=clock, sleep=_no_wait)  # type: ignore[arg-type]

    clock.advance(FLOOR_FOR_S * 4)
    probe.note_current_import(0.5)
    clock.advance(FLOOR_FOR_S * 4)

    assert probe.at_or_above_floor_for_s() == 0.0


def test_a_charger_held_at_the_pilot_floor_still_arms_the_clock() -> None:
    """A charger held at the pilot floor still arms the clock.

    A charger assigned the 6 A floor delivers slightly under it, so a gate set *at* the
    floor would never arm, yet that is exactly when the regulator wants to go lower and cannot.
    """
    clock = _Clock()
    probe = PilotFloorProbe(None, now=clock, sleep=_no_wait)  # type: ignore[arg-type]

    probe.note_current_import(5.9)
    clock.advance(FLOOR_FOR_S + 1)
    probe.note_current_import(5.89)

    assert probe.at_or_above_floor_for_s() > FLOOR_FOR_S


def test_the_floor_clock_restarts_when_the_car_drops_below_it() -> None:
    clock = _Clock()
    probe = PilotFloorProbe(None, now=clock, sleep=_no_wait)  # type: ignore[arg-type]

    probe.note_current_import(6.5)
    clock.advance(30)
    assert probe.at_or_above_floor_for_s() == 30.0

    # A dip resets the clock: sixty seconds means sixty unbroken seconds.
    probe.note_current_import(0.5)
    probe.note_current_import(6.5)
    clock.advance(30)
    assert probe.at_or_above_floor_for_s() == 30.0

    clock.advance(31)
    assert probe.at_or_above_floor_for_s() > FLOOR_FOR_S


async def test_a_probe_already_in_flight_cannot_be_started_again(
    hass: HomeAssistant,
) -> None:
    """Two reports in one event-loop iteration must not start two probes.

    The second would read the slot the first just wrote, remember that as the original and restore it.
    The flag is set by hand: it is the first probe's state between claiming the run and finishing it.
    """
    hass.states.async_set("switch.a", "on")
    configure_calls = _mocked_ocpp(hass)
    controller = _controller(hass)
    await controller.async_initialize()
    probe = PilotFloorProbe(controller, now=_Clock(), sleep=_no_wait)

    probe._in_flight = True

    assert probe.refusal() == "already_running"
    await probe.async_run()
    assert configure_calls == []

    await controller.async_shutdown()


async def test_the_probe_steps_down_and_restores_the_slot_verbatim(
    hass: HomeAssistant,
) -> None:
    """Three descending steps, then the remembered string written back as it was.

    Never rebuilt on the way back: the restore is the exact string read, since the charger's slot is
    the only authority on what its previous assignment meant.
    """
    hass.states.async_set("switch.a", "on")
    configure_calls = _mocked_ocpp(hass)
    controller = _controller(hass)
    await controller.async_initialize()
    probe = PilotFloorProbe(controller, now=_Clock(), sleep=_no_wait)

    await probe.async_run()

    assert _values(configure_calls) == [
        "1.5,2.10",
        "1.3,2.10",
        "1.1,2.10",
        _ASSIGNED,
    ]
    assert configure_calls[-1].data == {
        "devid": "charger",
        "ocpp_key": "AssignedCurrent",
        "value": _ASSIGNED,
    }
    # Only ever lowering: no step names a value above 16 A and each is below the one before.
    assert [int(value.split(",")[0].split(".")[1]) for value in _values(configure_calls)] == [
        5,
        3,
        1,
        16,
    ]
    assert probe.in_flight is False

    saved = await controller._store.async_load()
    assert saved is not None
    assert saved[STORE_KEY]["completed"] is True

    await controller.async_shutdown()


async def test_each_step_logs_the_token_and_every_reading(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """One INFO line per step, starting with the token and carrying the charger's own answer."""
    hass.states.async_set("switch.a", "on")
    for kind, value in (
        ("status_connector", "SuspendedEVSE"),
        ("current_import", "0.0"),
        ("transaction_id", "4711"),
    ):
        hass.states.async_set(f"sensor.charger_connector_1_{kind}", value)
    _mocked_ocpp(hass)
    controller = _controller(hass)
    await controller.async_initialize()
    probe = PilotFloorProbe(controller, now=_Clock(), sleep=_no_wait)

    with caplog.at_level(logging.INFO):
        await probe.async_run()

    step_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(PROBE_TOKEN) and "step=" in record.getMessage()
    ]
    assert len(step_lines) == len(PROBE_STEPS)
    for line, step in zip(step_lines, PROBE_STEPS):
        assert f"step={step}A" in line
        assert "read_back=" in line
        assert "status=SuspendedEVSE" in line
        assert "current_import=0.0" in line
        assert "transaction_id=4711" in line

    await controller.async_shutdown()


async def test_the_site_regulator_writes_nothing_while_the_probe_is_in_flight(
    hass: HomeAssistant,
) -> None:
    """The guard without which the probe would measure only the regulator.

    The apply loop calls `_async_assign_current` directly, many times a minute, so the probe's steps would be
    overwritten within seconds. The flag is set directly rather than by racing a task, which would pin timing.
    """
    hass.states.async_set("switch.a", "on")
    configure_calls = _mocked_ocpp(hass)
    controller = _controller(hass)
    await controller.async_initialize()

    controller._probe._in_flight = True
    await controller._async_assign_current(10)
    assert configure_calls == []

    controller._probe._in_flight = False
    await controller._async_assign_current(10)
    assert _values(configure_calls) == ["1.10,2.10"]

    await controller.async_shutdown()


async def test_a_step_that_raises_still_restores_the_slot(hass: HomeAssistant) -> None:
    """A probe that dies mid-run would otherwise leave the car at 1 A.

    The failure is injected into the second hold, so the restore (the `finally`) is all that
    stands between the car and the last value written.
    """

    class _Boom(Exception):
        pass

    hass.states.async_set("switch.a", "on")
    configure_calls = _mocked_ocpp(hass)
    controller = _controller(hass)
    await controller.async_initialize()

    sleeps: list[float] = []

    async def failing_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise _Boom("the hold broke")

    probe = PilotFloorProbe(controller, now=_Clock(), sleep=failing_sleep)
    await probe.async_run()  # must not raise: a broken probe is not a broken charge

    assert _values(configure_calls) == ["1.5,2.10", "1.3,2.10", _ASSIGNED]
    assert probe.in_flight is False
    saved = await controller._store.async_load()
    assert saved is not None and saved[STORE_KEY]["completed"] is True

    await controller.async_shutdown()


async def test_an_unreadable_slot_writes_nothing_and_is_not_retried(
    hass: HomeAssistant,
) -> None:
    """If the slot cannot be read there is nothing to restore, so the probe writes nothing and finishes for good."""
    hass.states.async_set("switch.a", "on")
    configure_calls = _mocked_ocpp(hass, response={"value": ""})
    controller = _controller(hass)
    await controller.async_initialize()
    probe = PilotFloorProbe(controller, now=_Clock(), sleep=_no_wait)

    await probe.async_run()

    assert configure_calls == []
    assert probe.in_flight is False
    saved = await controller._store.async_load()
    assert saved is not None and saved[STORE_KEY]["completed"] is True

    await controller.async_shutdown()


async def test_a_restart_never_runs_the_probe_and_a_finished_run_does_not_block_a_new_request(
    hass: HomeAssistant,
) -> None:
    """Run it, restart: nothing is written on its own. A finished run is no reason to refuse a person who asks
    again, and the new run's record starts afresh, so a crash in it is noticed."""
    hass.states.async_set("switch.a", "on")
    configure_calls = _mocked_ocpp(hass)
    controller = _controller(hass)
    await controller.async_initialize()
    probe = PilotFloorProbe(controller, now=_Clock(), sleep=_no_wait)
    await probe.async_run()
    assert len(configure_calls) == len(PROBE_STEPS) + 1
    await controller.async_shutdown()

    configure_calls.clear()
    restarted = _controller(hass)
    await restarted.async_initialize()
    await hass.async_block_till_done()
    assert configure_calls == []
    # Only the gates of a charge refuse it now, not the earlier run.
    assert restarted._probe.refusal() == "no_active_plan"

    await restarted.async_shutdown()


def _interrupted_record(**changes: Any) -> dict[str, Any]:
    return {
        "started": True,
        "remembered": _ASSIGNED,
        "last_written": "1.1,2.10",
        "devid": "charger",
        "connector": 1,
        **changes,
    }


async def _seed(hass: HomeAssistant, record: dict[str, Any]) -> None:
    await _controller(hass)._store.async_save({STORE_KEY: record})


def _flaky_ocpp(hass: HomeAssistant, answers: list[object]) -> list:
    """`get_configuration` answering each of `answers` in turn (an exception is raised), then the last for good."""

    async def get_configuration(call: ServiceCall) -> dict[str, Any]:
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return {"value": answer}

    hass.services.async_register(
        "ocpp", "get_configuration", get_configuration, supports_response=SupportsResponse.ONLY
    )
    return async_mock_service(hass, "ocpp", "configure")


async def test_an_interrupted_probe_is_put_back_when_the_entry_loads(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A restart in the middle left connector 1 at the probe's 1 A: the next load puts back its 16 A by
    itself, keeps connector 2 as it is now, and says at INFO what it wrote."""
    await _seed(hass, _interrupted_record())
    hass.states.async_set("switch.a", "on")
    configure_calls = _flaky_ocpp(hass, ["1.1,2.12"])

    restarted = _controller(hass)
    with caplog.at_level(logging.INFO):
        await restarted.async_initialize()
        await hass.async_block_till_done()

    assert _values(configure_calls) == ["1.16,2.12"]
    restored = [r for r in caplog.records if "restored charger connector 1" in r.getMessage()]
    assert restored and restored[0].levelno == logging.INFO
    assert "set it back by hand" not in caplog.text
    saved = await restarted._store.async_load()
    assert saved is not None and saved[STORE_KEY]["completed"] is True
    assert restarted._probe.refusal() != "interrupted_probe_not_restored"

    await restarted.async_shutdown()


async def test_an_interrupted_probe_whose_connector_was_assigned_since_writes_nothing(
    hass: HomeAssistant,
) -> None:
    """At or above the 6 A floor only the regulator can have written: nothing of the probe is left to undo."""
    await _seed(hass, _interrupted_record())
    hass.states.async_set("switch.a", "on")
    configure_calls = _flaky_ocpp(hass, ["1.10,2.10"])

    restarted = _controller(hass)
    await restarted.async_initialize()
    await hass.async_block_till_done()

    assert configure_calls == []
    saved = await restarted._store.async_load()
    assert saved is not None and saved[STORE_KEY]["completed"] is True

    await restarted.async_shutdown()


async def test_an_interrupted_probe_is_put_back_once_the_charger_becomes_ready(
    hass: HomeAssistant,
) -> None:
    """The charge point has not reconnected when Home Assistant starts: tried again a minute later, and put
    back then; until it is, a new run is refused."""
    await _seed(hass, _interrupted_record())
    hass.states.async_set("switch.a", "on")
    configure_calls = _flaky_ocpp(
        hass, [HomeAssistantError("not connected"), HomeAssistantError("not connected"), "1.3,2.10"]
    )

    restarted = _controller(hass)
    await restarted.async_initialize()
    await hass.async_block_till_done()
    assert configure_calls == []
    assert restarted._probe.refusal() == "interrupted_probe_not_restored"

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=PROBE_RESTORE_RETRY_S + 1))
    await hass.async_block_till_done()
    assert configure_calls == []

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2 * PROBE_RESTORE_RETRY_S + 2))
    await hass.async_block_till_done()
    assert _values(configure_calls) == [_ASSIGNED]
    assert restarted._probe_restore_cancel is None

    await restarted.async_shutdown()


async def test_a_charger_that_never_answers_is_given_up_on_until_the_next_start(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Bounded: after the last try a warning names both values, and the record stays for the next start."""
    await _seed(hass, _interrupted_record())
    hass.states.async_set("switch.a", "on")
    configure_calls = _flaky_ocpp(hass, [HomeAssistantError("not connected")])

    restarted = _controller(hass)
    with caplog.at_level(logging.WARNING):
        await restarted.async_initialize()
        await hass.async_block_till_done()
        for tick in range(1, PROBE_RESTORE_TRIES + 2):
            async_fire_time_changed(
                hass, dt_util.utcnow() + timedelta(seconds=tick * (PROBE_RESTORE_RETRY_S + 1))
            )
            await hass.async_block_till_done()

    assert configure_calls == []
    assert restarted._probe_restore_cancel is None
    assert "trying again at the next start" in caplog.text
    assert "1.1,2.10" in caplog.text and _ASSIGNED in caplog.text
    saved = await restarted._store.async_load()
    assert saved is not None and not saved[STORE_KEY].get("completed")

    await restarted.async_shutdown()


async def test_a_real_charge_never_starts_the_probe_and_the_request_does(
    hass: HomeAssistant,
) -> None:
    """The whole path: a plan, sensor reports, sixty seconds, and still nothing until it is asked for; then
    three steps and a restore. The listener only feeds the floor clock and goes away with the plan. The car
    starts drawing nothing, because a report of the value the sensor already holds is not a state change and
    would never start the floor clock.
    """
    hass.states.async_set("switch.a", "on")
    for kind, value in (
        ("status_connector", "SuspendedEVSE"),
        ("current_import", "0.0"),
        ("transaction_id", "4711"),
    ):
        hass.states.async_set(f"sensor.charger_connector_1_{kind}", value)

    configure_calls = _mocked_ocpp(hass)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    controller = _controller(hass)
    await controller.async_initialize()

    clock = _Clock()
    probe = PilotFloorProbe(controller, now=clock, sleep=_no_wait)
    controller._probe = probe

    # No plan, no charge to probe: refused with the gate's word, nothing written.
    assert controller.async_run_pilot_floor_probe() == "no_active_plan"

    now = dt_util.utcnow()
    await install_schedule(
        controller,
        {
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "amps": 16,
        }
    )
    await hass.async_block_till_done()
    configure_calls.clear()
    assert controller._probe_listener_cancel is not None
    assert controller.async_run_pilot_floor_probe() == "drawing_below_5a_for_60s"

    hass.states.async_set("sensor.charger_connector_1_current_import", "16.0")
    await hass.async_block_till_done()
    clock.advance(FLOOR_FOR_S + 1)
    hass.states.async_set("sensor.charger_connector_1_current_import", "16.1")
    await hass.async_block_till_done()

    # Every gate is open, and still nothing: the probe is never armed by itself.
    assert probe.refusal() is None
    assert configure_calls == []

    assert controller.async_run_pilot_floor_probe() is None
    await hass.async_block_till_done()
    assert _values(configure_calls) == ["1.5,2.10", "1.3,2.10", "1.1,2.10", _ASSIGNED]
    assert probe.in_flight is False

    await controller.async_stop(clear_schedule=True)
    await hass.async_block_till_done()
    assert controller._probe_listener_cancel is None

    await controller.async_shutdown()


async def test_a_shutdown_in_the_middle_of_a_requested_run_puts_the_slot_back(
    hass: HomeAssistant,
) -> None:
    """An unload while the current is held down cancels the run, and its `finally` restores the slot."""
    hass.states.async_set("switch.a", "on")
    configure_calls = _mocked_ocpp(hass)
    controller = _controller(hass)
    await controller.async_initialize()

    held = asyncio.Event()

    async def hold_forever(_seconds: float) -> None:
        held.set()
        await asyncio.Event().wait()

    controller._probe = PilotFloorProbe(controller, now=_Clock(), sleep=hold_forever)
    controller._probe.refusal = lambda: None  # type: ignore[method-assign]
    assert controller.async_run_pilot_floor_probe() is None
    await held.wait()
    assert _values(configure_calls) == ["1.5,2.10"]

    await controller.async_shutdown()

    assert _values(configure_calls) == ["1.5,2.10", _ASSIGNED]
    saved = await controller._store.async_load()
    assert saved is not None and saved[STORE_KEY]["completed"] is True
