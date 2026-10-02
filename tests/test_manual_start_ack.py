"""The manual Start acknowledgement gate: serialization is not acknowledgement.

`switch.turn_on` returning tells the boundary that the *service call* completed, not that the charger
did anything. Home Assistant's state for the charge control is the only observation of the charger, and
an integration can report it a moment later -- so two concurrent Starts could both be admitted while
that state still reads `off`, and the second would send a duplicate command.

One narrow in-memory fact closes that: "a manual Start has been accepted and the charger has not
answered yet". It is set inside the admission lock (before another request can be admitted), cleared
by the charger's own report or by one bounded appointment, and it never claims the charger is
charging -- while it stands, the canonical decision offers *nothing* with `action_pending`, because
neither direction is honest yet.

Nothing here sleeps and nothing here sets an entity state on the boundary's behalf: the state-change
channel is recorded and driven by the test, exactly as the pause expiry appointment is.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution import controller as controller_module
from custom_components.spotnav.execution.auto_execution import (
    CONTROL_ACTION_PENDING,
    EXECUTION_ACTION_FAILED,
    EXECUTION_ACTION_UNAVAILABLE,
    MANUAL_START_ACK_TIMEOUT,
    AutoControlRefused,
    ControlDecision,
)

from custom_components.spotnav.planning.auto_settings import (
    PAUSE_NEXT_PERIOD,
    PAUSE_UNTIL_RESUMED,
    PAUSE_UNTIL_TOMORROW,
)
from tests.relay import serve
from tests.harness import Session


class RecordedStateListeners:
    """The platform's state-change channel, recorded and driven by the test.

    It stands in for `homeassistant.helpers.event.async_track_state_change_event` inside the charging
    controller, which owns the one subscription to the charge control: `__call__` is arming it,
    `report` is what the platform would do -- change Home Assistant's state, then call the listener
    that watches it -- and the cancel handle is the controller's own unsubscribe.
    """

    def __init__(self) -> None:
        self.armed: list[dict[str, Any]] = []
        self.cancelled = 0

    def __call__(self, hass: HomeAssistant, entity_ids: Any, action: Any) -> Any:
        entry = {"entities": tuple(entity_ids), "action": action}
        self.armed.append(entry)

        def cancel() -> None:
            self.cancelled += 1
            if entry in self.armed:
                self.armed.remove(entry)

        return cancel

    def report(self, hass: HomeAssistant, entity_id: str, state: str) -> None:
        """The charger's own report: the state changes, then every watcher of it is called."""
        hass.states.async_set(entity_id, state)
        for entry in list(self.armed):
            if entity_id in entry["entities"]:
                entry["action"](None)

    @property
    def watching(self) -> list[str]:
        return [entity for entry in self.armed for entity in entry["entities"]]

    def watching_by(self, controller: Any) -> list[str]:
        """Every entity one controller's own subscriptions name.

        The recorder deliberately knows which object armed a subscription: each action is the bound
        callback of the controller that holds it, so "A watches A and B watches B" is a real
        assertion rather than an inference from the entity ids.
        """
        return [
            entity
            for entry in self.armed
            if getattr(entry["action"], "__self__", None) is controller
            for entity in entry["entities"]
        ]


@pytest.fixture
def start_ack(monkeypatch: pytest.MonkeyPatch) -> RecordedStateListeners:
    recorder = RecordedStateListeners()
    monkeypatch.setattr(controller_module, "async_track_state_change_event", recorder)
    return recorder


@pytest.fixture(autouse=True)
def frozen_real_clock(monkeypatch: pytest.MonkeyPatch, clock: Any) -> None:
    """Pin Home Assistant's clock to this harness's instant, as the pause tests do.

    The controller's window rules and a pause's `until tomorrow` both compare against
    `dt_util.utcnow()` as well as the injected clock, and the acknowledgement bound is measured on the
    same clock, so a moving real clock would make this suite pass in the morning and fail at night.
    """
    now = clock()

    def frozen(*_args: Any, **_kwargs: Any) -> Any:
        return now

    monkeypatch.setattr(dt_util, "utcnow", frozen)


async def auto_with_a_plan(session: Session) -> None:
    """One charger in Auto, with prices served and a plan installed: the prerequisites, asserted."""
    serve(session.transport)
    await session.set_auto()
    assert session.executor is not None and session.executor.applied is not None


def executor_of(session: Session) -> Any:
    assert session.executor is not None
    return session.executor


def preview_of(session: Session) -> Any:
    assert session.preview is not None
    return session.preview


async def test_a_start_held_in_the_service_call_is_never_sent_twice(
    session: Session, start_ack: RecordedStateListeners, pause_appointments: Any
) -> None:
    """The first Start is inside `switch.turn_on`; the second arrives and must not send another.

    This is the interleaving the gate exists for: the service call has not returned, Home Assistant
    still reports the charger as off, and serialization alone would have admitted the second request
    on that stale reading.
    """
    serve(session.transport)
    await session.set_auto()
    gate = asyncio.Event()
    calls: list[Any] = []

    async def held_turn_on(call: Any) -> None:
        calls.append(call)
        await gate.wait()

    session.hass.services.async_register("switch", "turn_on", held_turn_on)
    first = asyncio.ensure_future(preview_of(session).async_manual_action("start"))
    for _ in range(3):
        await asyncio.sleep(0)
    assert calls, "the prerequisite: the first request reached the charger"
    second = asyncio.ensure_future(preview_of(session).async_manual_action("start"))
    for _ in range(3):
        await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert len(calls) == 1, "one command, however many requests arrived"
    accepted = [r for r in results if isinstance(r, ControlDecision)]
    refused = [r for r in results if isinstance(r, AutoControlRefused)]
    assert len(accepted) == 1 and accepted[0].action == "start"
    assert len(refused) == 1 and refused[0].code == EXECUTION_ACTION_UNAVAILABLE, results
    assert executor_of(session).manual_start_pending is True, "the charger has not answered yet"


async def test_no_second_start_is_offered_until_the_charger_reports_charging(
    session: Session, start_ack: RecordedStateListeners, pause_appointments: Any
) -> None:
    """Accepted but unanswered: the decision offers nothing, and says why; the report frees it."""
    await auto_with_a_plan(session)
    async_mock_service(session.hass, "switch", "turn_on")  # a call that changes no state
    state = session.hass.states.get(session.charge_control)
    assert state is not None and state.state == "off", "the prerequisite: the charger reads off"

    decision = await preview_of(session).async_manual_action("start")

    assert decision.action == "start", "the Start itself is admitted"
    executor = executor_of(session)
    assert executor.manual_start_pending is True
    assert start_ack.watching == [session.charge_control], "one listener, on the charge control"

    pending = executor.control_axes()
    assert pending.immediate.action == "none", "no second Start while the charger has not answered"
    assert pending.immediate.reason == CONTROL_ACTION_PENDING
    assert pending.automatic.action == "none" and pending.automatic.choices == (), "and nothing to choose"
    with pytest.raises(AutoControlRefused) as refusal:
        await preview_of(session).async_manual_action("start")
    assert refusal.value.code == EXECUTION_ACTION_UNAVAILABLE

    # The charger's own report is the acknowledgement: the pending fact goes and Stop appears.
    start_ack.report(session.hass, session.charge_control, "on")
    await session.hass.async_block_till_done()

    assert executor.manual_start_pending is False
    assert start_ack.armed == [], "no listener is left behind"
    settled = executor.immediate_decision()
    assert settled.action == "stop", "and the dashboard now truthfully offers Stop"


async def test_a_silent_charger_is_settled_by_the_one_acknowledgement_bound(
    session: Session, start_ack: RecordedStateListeners, pause_appointments: Any
) -> None:
    """A charger that never reports charging is not pretended about: one appointment, then a code."""
    await auto_with_a_plan(session)
    async_mock_service(session.hass, "switch", "turn_on")
    executor = executor_of(session)

    await preview_of(session).async_manual_action("start")
    assert pause_appointments.when is not None
    assert pause_appointments.when == executor._now() + MANUAL_START_ACK_TIMEOUT

    await pause_appointments.fire(pause_appointments.when)

    assert executor.manual_start_pending is False, "the wait is over"
    assert executor.last_error == EXECUTION_ACTION_FAILED, "reported as the failure it is"
    assert start_ack.watching == [session.charge_control], "one standing observation, not a poll"
    assert pause_appointments.armed == [], "and no second appointment"
    retry = executor.immediate_decision()
    assert retry.action == "start", "a person can try again -- the state really is still off"
    assert retry.reason is None


async def test_a_successful_retry_and_its_report_retire_the_timeout_error(
    session: Session, start_ack: RecordedStateListeners, pause_appointments: Any
) -> None:
    """Accepted, unanswered, then answered: the old timeout stops being the current outcome."""
    await auto_with_a_plan(session)
    async_mock_service(session.hass, "switch", "turn_on")
    executor = executor_of(session)
    await preview_of(session).async_manual_action("start")
    await pause_appointments.fire(pause_appointments.when)
    assert executor.last_error == EXECUTION_ACTION_FAILED

    retried = await preview_of(session).async_manual_action("start")

    assert retried.action == "start", "the retry itself is admitted"
    assert executor.manual_start_pending is True, "and it is the outstanding command now"
    # The choice, stated: the failure is *retained* until the charger answers. Clearing it at
    # admission would let a press claim recovery before anything has actually succeeded.
    assert executor.last_error == EXECUTION_ACTION_FAILED

    start_ack.report(session.hass, session.charge_control, "on")
    await session.hass.async_block_till_done()

    assert executor.last_error is None, "the recovered failure is gone"
    assert executor.manual_start_pending is False
    assert start_ack.armed == [], "nothing is left watching"
    settled = executor.immediate_decision()
    assert settled.action == "stop" and settled.reason is None


async def test_a_retry_that_fails_keeps_the_error_and_leaves_no_pending_handle(
    session: Session, start_ack: RecordedStateListeners, pause_appointments: Any
) -> None:
    """A retry that never went out recovers nothing: the failure stands, and so does the watch."""
    await auto_with_a_plan(session)
    async_mock_service(session.hass, "switch", "turn_on")
    executor = executor_of(session)
    await preview_of(session).async_manual_action("start")
    await pause_appointments.fire(pause_appointments.when)
    original = session.controller.async_start

    async def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the switch did not answer")

    session.controller.async_start = refuse  # type: ignore[method-assign]
    with pytest.raises(AutoControlRefused) as refusal:
        await preview_of(session).async_manual_action("start")
    session.controller.async_start = original  # type: ignore[method-assign]

    assert refusal.value.code == EXECUTION_ACTION_FAILED
    assert executor.last_error == EXECUTION_ACTION_FAILED, "nothing recovered it"
    assert executor.manual_start_pending is False, "no handle is left outstanding"
    assert start_ack.watching == [session.charge_control], "the honest report is still watched for"


async def test_a_late_report_without_a_retry_retires_the_timeout_error(
    session: Session, start_ack: RecordedStateListeners, pause_appointments: Any
) -> None:
    """The documented policy: the charger telling the truth later is recovery enough."""
    await auto_with_a_plan(session)
    async_mock_service(session.hass, "switch", "turn_on")
    executor = executor_of(session)
    await preview_of(session).async_manual_action("start")
    await pause_appointments.fire(pause_appointments.when)
    assert executor.last_error == EXECUTION_ACTION_FAILED

    start_ack.report(session.hass, session.charge_control, "on")
    await session.hass.async_block_till_done()

    assert executor.last_error is None, "the obsolete failure is retired"
    assert start_ack.armed == [], "and the standing observation ends with it"
    settled = executor.immediate_decision()
    assert settled.action == "stop", "a charging charger is never offered another Start"


async def test_a_successful_start_does_not_erase_an_unrelated_error(
    session: Session, start_ack: RecordedStateListeners, pause_appointments: Any
) -> None:
    """Only this state machine's own failure may be cleared by its recovery."""
    await auto_with_a_plan(session)
    async_mock_service(session.hass, "switch", "turn_on")
    executor = executor_of(session)
    await executor.async_note_reconcile_failed()
    assert executor.last_error == "reconcile_failed"

    await preview_of(session).async_manual_action("start")
    start_ack.report(session.hass, session.charge_control, "on")
    await session.hass.async_block_till_done()

    assert executor.last_error == "reconcile_failed", "an unrelated failure is not this one's to clear"
    assert executor.immediate_decision().action == "stop"


async def test_unload_during_recovery_leaves_nothing_and_a_late_report_is_inert(
    session: Session, start_ack: RecordedStateListeners, pause_appointments: Any
) -> None:
    """A recovery that is still owed when the boundary goes away is dropped with it."""
    await auto_with_a_plan(session)
    async_mock_service(session.hass, "switch", "turn_on")
    executor = executor_of(session)
    await preview_of(session).async_manual_action("start")
    await pause_appointments.fire(pause_appointments.when)
    assert executor.last_error == EXECUTION_ACTION_FAILED and start_ack.armed

    await executor.async_shutdown()
    assert start_ack.armed == [], "the listener is cancelled exactly here"
    assert pause_appointments.armed == [], "and no appointment outlives the boundary"

    start_ack.report(session.hass, session.charge_control, "on")
    await session.hass.async_block_till_done()

    assert executor.last_error == EXECUTION_ACTION_FAILED, "a late report mutates nothing"
    assert start_ack.armed == [], "and re-arms nothing"


async def test_a_start_to_an_unavailable_control_is_a_failed_command_with_nothing_pending(
    session: Session, start_ack: RecordedStateListeners, pause_appointments: Any
) -> None:
    """Home Assistant skips a service call to an unavailable entity and only logs it: the Start never
    went out, so it is refused with the stable failure and nothing is left awaiting an answer.
    """
    await auto_with_a_plan(session)
    turn_on = async_mock_service(session.hass, "switch", "turn_on")
    session.hass.states.async_set(session.charge_control, "unavailable")

    with pytest.raises(AutoControlRefused) as refusal:
        await preview_of(session).async_manual_action("start")

    assert refusal.value.code == EXECUTION_ACTION_FAILED
    assert turn_on == [], "no call was made to the unavailable entity"
    assert executor_of(session).manual_start_pending is False
    assert start_ack.armed == [] and pause_appointments.armed == []

    session.hass.states.async_set(session.charge_control, "off")
    decision = await preview_of(session).async_manual_action("start")
    assert decision.action == "start" and len(turn_on) == 1, "a retry goes out once it is back"


async def test_a_start_whose_command_fails_leaves_nothing_pending_and_can_be_retried(
    session: Session, start_ack: RecordedStateListeners, pause_appointments: Any
) -> None:
    """A command that was never accepted must not look like one that is awaiting an answer."""
    await auto_with_a_plan(session)
    executor = executor_of(session)
    original = session.controller.async_start
    failures = {"left": 1}

    async def sometimes(*args: Any, **kwargs: Any) -> bool:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise RuntimeError("the switch did not answer")
        return await original(*args, **kwargs)

    session.controller.async_start = sometimes  # type: ignore[method-assign]
    with pytest.raises(AutoControlRefused) as refusal:
        await preview_of(session).async_manual_action("start")

    assert refusal.value.code == EXECUTION_ACTION_FAILED, "the existing stable failure"
    assert executor.manual_start_pending is False, "nothing is outstanding"
    assert start_ack.armed == [], "and no listener was armed for a command that never went out"
    assert pause_appointments.armed == [], "nor an appointment"

    retried = await preview_of(session).async_manual_action("start")

    assert retried.action == "start", "a real retry is possible"
    assert executor.manual_start_pending is True, "and this one is awaiting the charger"


async def test_a_late_report_after_shutdown_is_inert(
    session: Session, start_ack: RecordedStateListeners, pause_appointments: Any
) -> None:
    """Unloading ends the watch: no fact, no listener, no appointment, and nothing resurrected."""
    await auto_with_a_plan(session)
    async_mock_service(session.hass, "switch", "turn_on")
    executor = executor_of(session)
    await preview_of(session).async_manual_action("start")
    assert executor.manual_start_pending is True and start_ack.armed

    await executor.async_shutdown()

    assert executor.manual_start_pending is False, "the watch ended with the boundary"
    assert start_ack.armed == [], "the state listener was cancelled exactly here"
    assert start_ack.cancelled >= 1
    assert pause_appointments.armed == [], "and so was the acknowledgement bound"

    # The platform delivering the transition afterwards finds nothing to resolve.
    start_ack.report(session.hass, session.charge_control, "on")
    await session.hass.async_block_till_done()

    assert executor.manual_start_pending is False
    assert executor.last_error is None, "nothing was recorded by a shut-down boundary"
    assert start_ack.armed == [], "and nothing re-armed a listener"


async def test_two_chargers_do_not_share_a_pending_start(
    session: Session,
    start_ack: RecordedStateListeners,
    pause_appointments: Any,
    clock: Any,
    timers: Any,
) -> None:
    """A pending Start is one charger's business: the neighbour is not blocked by it."""
    serve(session.transport)
    await session.set_auto()
    async_mock_service(session.hass, "switch", "turn_on")
    second = Session(
        session.hass,
        session.transport,
        clock,
        timers,
        entry_id="entry-b",
        charge_control="switch.charger_b",
    )
    await second.start()
    await second.set_auto()

    await preview_of(session).async_manual_action("start")

    assert executor_of(session).manual_start_pending is True
    assert executor_of(second).manual_start_pending is False, "B has no outstanding command"
    # A's own control is what the acknowledgement watches: the one subscription A's controller armed
    # through this recorder, because the vehicle-side observation every controller keeps for its own
    # charger (see execution/charge_progress.py) was armed earlier, when A was set up.
    assert start_ack.watching_by(session.controller) == [session.charge_control]
    # B was set up while this recorder was watching, and its controller subscribes to B's own control
    # and nothing else -- so a pending Start on A can neither be answered nor blocked by B's charger.
    assert start_ack.watching_by(second.controller) == [second.charge_control]

    admitted = await preview_of(second).async_manual_action("start")

    assert admitted.action == "start", "B may start while A waits for its charger"
    assert executor_of(second).manual_start_pending is True


async def test_choices_belong_to_the_action_that_accepts_them(
    session: Session, start_ack: RecordedStateListeners, pause_appointments: Any
) -> None:
    """Only Stop offers pause choices; Start, Resume and `none` offer none.

    Asserted where those choices really are available, so the empty tuple is a decision and not an
    accident of a charger that had nothing to offer.
    """
    await auto_with_a_plan(session)
    executor = executor_of(session)
    available = executor.pause_choices()
    assert available == (PAUSE_NEXT_PERIOD, PAUSE_UNTIL_TOMORROW, PAUSE_UNTIL_RESUMED)

    idle = executor.control_axes()
    assert idle.immediate.action == "start"
    assert idle.automatic.action == "pause" and idle.automatic.choices == available

    session.hass.states.async_set(session.charge_control, "on")
    charging = executor.control_axes()
    assert charging.immediate.action == "stop"
    assert charging.automatic.choices == available, "exactly the choices the charger can honour"

    await preview_of(session).async_pause(PAUSE_UNTIL_RESUMED)
    resume = executor.control_axes()
    assert resume.automatic.action == "resume" and resume.automatic.choices == ()

    await preview_of(session).async_resume()
