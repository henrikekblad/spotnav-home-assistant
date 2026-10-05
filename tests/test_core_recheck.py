"""A background task a report or a timer spawned (the hold's stop, a stray charge's stop, the claim of a window charge,
the stop under a person's Stop) keeps its re-check under the boundary's lock, and the re-check asks the core: it decides
on its session lined up at that moment (`core_events.Recheck`), and what the task sent comes back to it as a
`CommandResult`. With `CONF_CORE_OWNERSHIP` off today's rule still acts, and the shadow compares the two."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.core import events as ev
from custom_components.spotnav.core.ownership import decide, Keep, Stop
from custom_components.spotnav.core.session import ChargeSession, ManualPause, PendingCommand
from custom_components.spotnav.execution import ownership_shadow

from .pause_world import later_window, open_window, pause_world, SWITCH
from .relay import FakeScheduler

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
PLUGGED = ChargeSession(plugged=True)
STOP = ManualPause("stop", "plug_in")
START = ManualPause("start", "plug_in")


def _kinds(commands) -> list[tuple[str, str]]:
    return [(command.kind, getattr(command, "reason", "")) for command in commands if not isinstance(command, Keep)]


# ---------------------------------------------------------------------------------------------- the core's rule


@pytest.mark.parametrize(
    ("session", "facts", "due"),
    [
        (PLUGGED, {"window_ahead_outside": True, "control_on": True}, True),
        (PLUGGED, {"window_ahead_outside": False, "control_on": True}, False),  # a window opened meanwhile
        (PLUGGED, {"window_ahead_outside": True, "control_on": False}, False),  # off by now
        (PLUGGED, {"window_ahead_outside": True, "control_on": True, "owned": True}, False),  # SpotNav started it
        (PLUGGED, {"window_ahead_outside": True, "control_on": True, "solar_holds": True}, False),  # the sun has it
        (PLUGGED.with_changes(manual=START), {"window_ahead_outside": True, "control_on": True}, False),
        (PLUGGED.with_changes(span_pause="until_resumed"), {"window_ahead_outside": True, "control_on": True}, False),
        (
            PLUGGED.with_changes(span_pause="until_resumed"),
            {"window_ahead_outside": True, "control_on": True, "plan_auto_owned": False},
            True,
        ),
    ],
)
def test_the_holds_stop_is_decided_again(session: ChargeSession, facts: dict, due: bool) -> None:
    session = session.with_changes(pending=(PendingCommand("stop", "hold"),))
    after, commands = decide(session, ev.Recheck(what="hold", **facts), T0)
    assert _kinds(commands) == ([("stop", "hold")] if due else [])
    assert [(item.command, item.reason) for item in after.pending] == ([("stop", "hold")] if due else [])


@pytest.mark.parametrize(
    ("owner", "facts", "due"),
    [
        ("plan", {"plan_present": True, "control_on": True}, True),
        ("plan", {"plan_present": True, "control_on": True, "in_window": True}, False),  # a plan with a window now
        ("plan", {"plan_present": False, "control_on": True}, False),
        ("plan", {"plan_present": True, "control_on": True, "top_off": True}, False),
        ("plan", {"plan_present": True, "control_on": True, "handed_off": True}, False),
        ("plan", {"plan_present": True, "control_on": False}, False),
        ("person", {"plan_present": True, "control_on": True}, False),
        ("top_off", {"plan_present": True, "control_on": True}, False),
    ],
)
def test_a_stray_charges_stop_is_decided_again(owner: str, facts: dict, due: bool) -> None:
    session = PLUGGED.with_changes(owner=owner, pending=(PendingCommand("stop", "stray", owner_before=owner),))
    _after, commands = decide(session, ev.Recheck(what="stray", **facts), T0)
    assert _kinds(commands) == ([("stop", "stray")] if due else [])


@pytest.mark.parametrize(
    ("session", "facts", "due"),
    [
        (PLUGGED, {"window_open": True, "control_on": True}, True),
        (PLUGGED.with_changes(owner="charger_self"), {"window_open": True, "control_on": True}, True),
        (PLUGGED, {"window_open": False, "control_on": True}, False),
        (PLUGGED, {"window_open": True, "control_on": False}, False),
        (PLUGGED.with_changes(owner="plan"), {"window_open": True, "control_on": True}, False),  # claimed already
        (PLUGGED.with_changes(span_pause="next_period"), {"window_open": True, "control_on": True}, False),
        (PLUGGED.with_changes(manual=STOP), {"window_open": True, "control_on": True}, False),
        (
            PLUGGED.with_changes(car_ended_at=T0 - timedelta(minutes=5)),
            {"window_open": True, "control_on": True, "open_window_start": T0 - timedelta(minutes=30)},
            False,
        ),
    ],
)
def test_the_claim_of_a_window_charge_is_decided_again(session: ChargeSession, facts: dict, due: bool) -> None:
    # The report's own claim awaits a result: it is this task's, so it keeps nothing from being claimed.
    session = session.with_changes(pending=(PendingCommand("start", "claim", owner_after="plan"),))
    after, commands = decide(session, ev.Recheck(what="claim", **facts), T0)
    assert _kinds(commands) == ([("start", "claim")] if due else [])
    if due:
        after, _ = decide(after, ev.CommandResult(command="start", reason="claim", executed=True), T0)
        assert after.owner == "plan" and after.pending == ()


def test_the_stop_under_a_persons_stop_is_decided_again_and_counted_when_it_goes_out() -> None:
    earlier = T0 - timedelta(minutes=2)
    session = PLUGGED.with_changes(
        manual=STOP,
        hold_stop_times=(earlier,),
        hold_stop_pending=True,
        pending=(PendingCommand("stop", "person_hold"),),
    )
    after, commands = decide(session, ev.Recheck(what="person_hold", control_on=True), T0)
    assert _kinds(commands) == [("stop", "person_hold")]
    assert after.hold_stop_times == (earlier, T0) and after.hold_tried_at == T0 and after.hold_stop_pending
    after, _ = decide(after, ev.CommandResult(command="stop", reason="person_hold", executed=True), T0)
    assert not after.hold_stop_pending and after.pending == ()


@pytest.mark.parametrize(
    ("session", "facts"),
    [
        # A person's Start replaced their Stop while the task waited for the lock.
        (PLUGGED.with_changes(manual=START), {"control_on": True}),
        (PLUGGED.with_changes(manual=STOP), {"control_on": False}),
        # A start of theirs on its way: the report of the charge beat its result (C7, review probe).
        (PLUGGED.with_changes(manual=STOP), {"control_on": True, "start_pending": True}),
        (
            PLUGGED.with_changes(manual=STOP, pending=(PendingCommand("start", "person", owner_after="person"),)),
            {"control_on": True},
        ),
    ],
)
def test_the_stop_under_a_persons_stop_is_not_sent_once_it_is_not_due(session: ChargeSession, facts: dict) -> None:
    session = session.with_changes(hold_stop_pending=True)
    after, commands = decide(session, ev.Recheck(what="person_hold", **facts), T0)
    assert _kinds(commands) == []
    assert not after.hold_stop_pending and after.hold_tried_at is None
    assert all(item.reason != "person_hold" for item in after.pending)


def test_a_person_start_under_their_stop_is_not_stopped_when_the_report_beats_the_result() -> None:
    """The review probe's sequence: the core's report asks for a C7 stop of the person's own Start; the task's
    re-check, lined up after the Start's result came back, refuses it."""
    session, _ = decide(PLUGGED.with_changes(manual=STOP), ev.PersonStart(connected=True), T0)
    session, _ = decide(session, ev.CommandResult(command="start", reason="person", executed=True), T0)
    assert session.manual == START
    _after, commands = decide(session, ev.Recheck(what="person_hold", control_on=True), T0)
    assert _kinds(commands) == []


def test_a_recheck_of_a_task_the_core_does_not_know_sends_nothing() -> None:
    _after, commands = decide(PLUGGED, ev.Recheck(what="other", control_on=True), T0)
    assert _kinds(commands) == []


def test_a_recheck_round_trips_as_a_recorded_event() -> None:
    event = ev.Recheck(what="claim", control_on=True, window_open=True, open_window_start=T0)
    assert ev.event_from_dict(event.to_dict()) == event


# ---------------------------------------------------------------------------------------------- the task asks the core


async def _held_world(hass: HomeAssistant, timers: FakeScheduler):
    """A plan whose window is two hours ahead, a car plugged in, and the charger begins by itself: the hold's stop."""
    world = await pause_world(hass, timers, plan=later_window())
    stops = len(world.stops)
    await world.switch("on")
    return world, stops


def _records(world, kind: str) -> list[dict]:
    return [record for record in world.controller.ownership_shadow.events if record["event"]["kind"] == kind]


@pytest.mark.parametrize("drives", [False, True])
async def test_the_holds_stop_is_sent_and_its_result_comes_back_to_the_core(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch, drives: bool
) -> None:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", drives)
    world, stops = await _held_world(hass, timers)
    assert len(world.stops) == stops + 1
    rechecks = _records(world, "recheck")
    assert rechecks and rechecks[-1]["event"]["what"] == "hold"
    assert rechecks[-1]["core"] == [{"kind": "stop", "reason": "hold"}] and rechecks[-1]["today"] == ["stop"]
    assert rechecks[-1]["result"]["command"] == "stop" and rechecks[-1]["result"]["executed"] is True
    shadow = world.controller.ownership_shadow
    assert shadow.session.pending == () and shadow.counts["disagreements"] == 0
    await world.shutdown()


@pytest.mark.shadow_disagreement_expected
@pytest.mark.parametrize(("drives", "stopped"), [(True, False), (False, True)])
async def test_the_holds_task_acts_on_the_cores_recheck_when_it_drives(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch, drives: bool, stopped: bool
) -> None:
    """A core whose re-check finds the hold's stop no longer due: driving, the task sends nothing (today's rule would
    have, counted as `verdict_differs`); shadowing, today's rule stops it and the shadow reports the disagreement."""
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", drives)
    real = ownership_shadow.decide

    def nothing_due(session, event, now):
        session, commands = real(session, event, now)
        if event.kind == "recheck":
            commands = tuple(command for command in commands if not isinstance(command, Stop))
        return session, commands

    monkeypatch.setattr(ownership_shadow, "decide", nothing_due)
    world, stops = await _held_world(hass, timers)
    assert (len(world.stops) > stops) is stopped
    shadow = world.controller.ownership_shadow
    assert (shadow.counts["verdict_differs"] > 0) is drives
    assert (shadow.counts["disagreements"] > 0) is (not drives)
    await world.shutdown()


@pytest.mark.parametrize("drives", [False, True])
async def test_the_claim_of_a_window_charge_comes_back_to_the_core(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch, drives: bool
) -> None:
    """The charger begins by itself inside a window of the plan: the claim's task asks the core again and the charge
    is the plan's, in both modes."""
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", drives)
    world = await pause_world(hass, timers, plan=open_window())
    await world.controller.async_stop()  # the window's own start, stopped: nobody's charge now
    await world.switch("on")
    rechecks = _records(world, "recheck")
    assert rechecks and rechecks[-1]["event"]["what"] == "claim"
    assert rechecks[-1]["result"] == {
        "kind": "command_result",
        "command": "start",
        "reason": "claim",
        "executed": True,
        "balancing_held": False,
        "unobserved": False,
    }
    assert world.controller.charge_origin == "plan_window"
    assert world.controller.ownership_shadow.session.owner == "plan"
    await world.shutdown()


@pytest.mark.parametrize("drives", [False, True])
async def test_the_stop_under_a_persons_stop_is_rechecked_by_the_core(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch, drives: bool
) -> None:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", drives)
    world = await pause_world(hass, timers)
    await world.executor.async_manual_stop()
    stops = len(world.stops)
    await world.switch("on")  # the charger begins by itself under the person's Stop (C7)
    assert len(world.stops) == stops + 1
    assert hass.states.get(SWITCH).state == "off"
    rechecks = _records(world, "recheck")
    assert rechecks and rechecks[-1]["event"]["what"] == "person_hold"
    assert rechecks[-1]["result"]["executed"] is True
    shadow = world.controller.ownership_shadow
    assert not shadow.session.hold_stop_pending and len(shadow.session.hold_stop_times) == 1
    await world.shutdown()
