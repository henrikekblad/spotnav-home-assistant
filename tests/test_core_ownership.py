"""Table tests for the charge-ownership core (`core/ownership.py`): every rule of the manual-pause specs and their
review rounds, as (session, event) -> (session, commands). Pure: no Home Assistant."""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.core import events as ev
from custom_components.spotnav.core.ownership import (
    automatic_allowed,
    car_ended_holds_window,
    car_ended_known_full,
    CarFacts,
    decide,
    GATE_BALANCING_RESUME,
    GATE_PERSON_RESUME,
    GATE_START,
    GATE_STOP,
    Keep,
    KEEP,
    Notify,
    NOTIFY_CHARGER_IGNORES_STOP,
    owner_as_sent,
    PERSON_HOLD_MAX_STOPS,
    PERSON_HOLD_STOP_GAP_S,
    PERSON_HOLD_WINDOW_S,
    SAFETY_RESUME_GAP_S,
    SENT_START,
    SENT_STOP,
    Start,
    Stop,
    window_end_spared,
)
from custom_components.spotnav.core.session import (
    CarEnded,
    CarEndedWait,
    ChargeSession,
    ManualPause,
    PendingCommand,
    SessionError,
)

T0 = datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc)
START = ManualPause("start", "plug_in")
STOP = ManualPause("stop", "plug_in")
STOP_NEXT = ManualPause("stop", "next_plug_in")
PLUGGED = ChargeSession(plugged=True)
ENDED = CarEnded(at=T0)


def run(session: ChargeSession, *events: ev.Event, now: datetime = T0):
    """Feed events one after another; the commands of the last one."""
    commands: tuple = ()
    for event in events:
        session, commands = decide(session, event, now)
    return session, commands


def kinds(commands) -> list[str]:
    return [command.kind for command in commands if not isinstance(command, Keep)]


def ok(command: str, reason: str) -> ev.CommandResult:
    return ev.CommandResult(command=command, reason=reason, executed=True)


def failed(command: str, reason: str) -> ev.CommandResult:
    return ev.CommandResult(command=command, reason=reason, executed=False)


# ---------------------------------------------------------------------------------------------- the gate


@pytest.mark.parametrize(
    ("session", "allowed"),
    [
        (ChargeSession(), {GATE_START, GATE_STOP, GATE_BALANCING_RESUME, GATE_PERSON_RESUME}),
        (ChargeSession(span_pause="until_resumed"), {GATE_STOP, GATE_PERSON_RESUME}),
        (ChargeSession(span_pause="next_period"), {GATE_STOP, GATE_PERSON_RESUME}),
        (ChargeSession(manual=START), {GATE_BALANCING_RESUME, GATE_PERSON_RESUME}),
        (ChargeSession(manual=STOP), {GATE_STOP}),
        (ChargeSession(manual=STOP_NEXT), {GATE_STOP}),
    ],
)
def test_the_gate_every_automatic_decision_asks(session: ChargeSession, allowed: set[str]) -> None:
    """A: a pause holds Auto's starts; a stop agrees with a person's Stop and never overrules their Start; load
    balancing resumes a charge a person started under any pause but their Stop (C5, R4)."""
    for kind in (GATE_START, GATE_STOP, GATE_BALANCING_RESUME, GATE_PERSON_RESUME):
        assert automatic_allowed(session, kind) is (kind in allowed), kind


# ---------------------------------------------------------------------------------------------- a person's Stop


@pytest.mark.parametrize(
    ("before", "connected", "after"),
    [
        (None, True, STOP),
        (None, False, STOP_NEXT),
        # C2: the scope comes from the last definite connection; a status that says nothing says nothing.
        (None, None, STOP),
        (STOP_NEXT, None, STOP_NEXT),
        (STOP_NEXT, True, STOP),
        (STOP_NEXT, False, STOP_NEXT),
        # A Stop during a person's Start keeps the pause, now a Stop.
        (START, True, STOP),
    ],
)
def test_a_person_stop_pauses_auto_for_the_plug_in(before, connected, after) -> None:
    session, commands = run(ChargeSession(plugged=connected, owner="plan", manual=before), ev.PersonStop(connected=connected))
    assert session.manual == after
    assert commands == (Stop("person", True),)


def test_a_person_stop_goes_to_the_charger_first_and_the_owner_ends_only_when_it_went_out() -> None:
    """C3, I4: the pause is stored whatever becomes of the stop; a stop that never went out keeps the owner."""
    session, _ = run(ChargeSession(plugged=True, owner="plan"), ev.PersonStop(), failed("stop", "person"))
    assert session.manual == STOP and session.owner == "plan"
    session, _ = run(ChargeSession(plugged=True, owner="plan"), ev.PersonStop(), ok("stop", "person"))
    assert session.manual == STOP and session.owner == "none"


def test_a_person_stop_keeps_a_pause_they_chose_for_a_span() -> None:
    session, commands = run(ChargeSession(span_pause="until_tomorrow"), ev.PersonStop())
    assert session.span_pause == "until_tomorrow" and session.manual is None
    assert kinds(commands) == ["stop"]


def test_a_person_stop_starts_the_stops_under_it_afresh() -> None:
    """R5: the person acted, so a charger SpotNav gave up on is watched again."""
    given_up = ChargeSession(manual=STOP, hold_gave_up=True, hold_stop_times=(T0,), hold_tried_at=T0)
    session, _ = run(given_up, ev.PersonStop())
    assert not session.hold_gave_up and session.hold_stop_times == () and session.hold_tried_at is None


# ---------------------------------------------------------------------------------------------- a person's Start


def test_a_person_start_with_no_car_is_refused() -> None:
    """C6: nothing sent, nothing paused."""
    session, commands = run(ChargeSession(plugged=False, manual=STOP_NEXT), ev.PersonStart(connected=False))
    assert session.manual == STOP_NEXT and kinds(commands) == []


@pytest.mark.parametrize("connected", [True, None])
def test_a_person_start_owns_the_charge_and_pauses_auto(connected) -> None:
    """A, C6: an unknown connection goes out as ever; a Start never leaves a contradicting Stop pause."""
    session, commands = run(ChargeSession(manual=STOP, plugged=connected), ev.PersonStart(connected=connected))
    assert commands == (Start("person"),)
    session, _ = run(session, ok("start", "person"))
    assert session.owner == "person" and session.manual == START


def test_a_person_start_that_did_not_go_out_changes_nothing() -> None:
    session, _ = run(ChargeSession(manual=STOP), ev.PersonStart(), failed("start", "person"))
    assert session.owner == "none" and session.manual == STOP


def test_a_person_start_held_back_by_load_balancing_is_still_theirs() -> None:
    """C5: the regulator resumes it when there is room."""
    held = ev.CommandResult(command="start", reason="person", executed=False, balancing_held=True)
    session, _ = run(ChargeSession(), ev.PersonStart(), held)
    assert session.manual == START and session.balancing_paused and session.paused_origin == "person"
    assert session.owner == "none"


def test_a_person_start_keeps_a_pause_they_chose_for_a_span() -> None:
    session, _ = run(ChargeSession(span_pause="until_resumed"), ev.PersonStart(), ok("start", "person"))
    assert session.span_pause == "until_resumed" and session.manual is None and session.owner == "person"


def test_a_person_start_overrules_what_the_car_ended() -> None:
    session, _ = run(ChargeSession(car_ended=ENDED), ev.PersonStart())
    assert session.car_ended_at is None


# ---------------------------------------------------------------------------------------------- the plug-in session


@pytest.mark.parametrize(
    ("before", "event", "after"),
    [
        # The pause ends at the unplug, whatever the first known connection before it.
        (STOP, ev.Unplug(previous=True), None),
        (START, ev.Unplug(previous=True), None),
        (STOP, ev.Unplug(previous=None), None),
        # A Stop given with no car waits for the plug-in after it, and ends at the unplug after that.
        (STOP_NEXT, ev.Unplug(previous=True), STOP_NEXT),
        (STOP_NEXT, ev.Unplug(previous=None), STOP_NEXT),
        (STOP_NEXT, ev.PlugIn(previous=False), STOP),
        # C1: the first definite connection after a restart is the plug-in that starts the session.
        (STOP_NEXT, ev.PlugIn(previous=None), STOP),
        # A plug-in after a known unplug: the session the pause was given in is over (an unplug nobody saw).
        (STOP, ev.PlugIn(previous=False), None),
        (START, ev.PlugIn(previous=False), None),
        # The first connection known after a restart, a car: the same session.
        (STOP, ev.PlugIn(previous=None), STOP),
        # C2: a status that says nothing is never a plug-in or an unplug.
        (STOP, ev.ConnectionUnknown(), STOP),
        (STOP_NEXT, ev.ConnectionUnknown(), STOP_NEXT),
    ],
)
def test_a_manual_pause_lives_for_its_plug_in_session(before, event, after) -> None:
    session, commands = run(ChargeSession(manual=before), event)
    assert session.manual == after
    assert kinds(commands) == []


def test_a_span_pause_outlives_the_plug_in() -> None:
    session, _ = run(ChargeSession(span_pause="until_resumed"), ev.Unplug(), ev.PlugIn())
    assert session.span_pause == "until_resumed"


def test_an_unplug_ends_what_belonged_to_the_plug_in() -> None:
    """B6, P3: a balancing pause and a safety stop's hold; the hold's session; the car-ended record; the stops
    under a person's Stop."""
    before = ChargeSession(
        plugged=True,
        balancing_paused=True,
        paused_origin="plan",
        held_for_safety=True,
        safety_stopped_at=T0,
        held=True,
        overridden=True,
        car_ended=CarEnded(at=T0, soc=80.0, vehicle="car", wait=CarEndedWait(target=90.0)),
        hold_stop_times=(T0,),
        hold_gave_up=True,
    )
    session, _ = run(before, ev.Unplug(previous=True))
    assert session == ChargeSession(plugged=False)


def test_an_unplug_found_after_a_restart_ends_a_persons_start() -> None:
    """A person's Start holds until the car is unplugged, also when the unplug is the first connection known after a
    restart: a charge the charger begins by itself at the next plug-in is decided by the sun, not left as the
    person's (the field case: a Peblar charging at 11 kW under Solar with no surplus)."""
    before = ChargeSession(plugged=True, owner="person", manual=START)
    session, _ = run(before, ev.Restart(), ev.Unplug(previous=None))
    assert session.owner == "none" and session.manual is None
    session, commands = run(
        session,
        ev.PlugIn(previous=False),
        on(was_on=False),
        ev.SolarStop(take_over=True, charging=True),
    )
    assert session.owner == "charger_self"
    assert kinds(commands) == ["stop"]


def test_a_plug_in_after_a_known_unplug_does_not_inherit_a_persons_start() -> None:
    session, _ = run(ChargeSession(plugged=True, owner="person"), ev.Unplug(previous=True), ev.PlugIn(previous=False))
    assert session.owner == "none"


def test_a_persons_start_outlives_a_restart_while_the_car_stays_plugged_in() -> None:
    before = ChargeSession(plugged=True, owner="person", manual=START)
    session, commands = run(before, ev.Restart(), ev.PlugIn(previous=None), ev.SolarStop(take_over=True, charging=True))
    assert session.owner == "person" and session.manual == START
    assert kinds(commands) == []


def test_the_first_connection_after_a_restart_keeps_what_the_plug_in_had() -> None:
    before = ChargeSession(car_ended=CarEnded(at=T0, soc=80.0, vehicle="car"), hold_stop_times=(T0,))
    session, _ = run(before, ev.PlugIn(previous=None))
    assert session.car_ended == before.car_ended and session.hold_stop_times == (T0,)


# ---------------------------------------------------------------------------------------------- resume, follow, span pauses


@pytest.mark.parametrize(
    ("reason", "before", "manual", "span"),
    [
        ("resume", ChargeSession(manual=STOP), None, None),
        ("resume", ChargeSession(span_pause="until_resumed"), None, None),
        ("follow", ChargeSession(manual=START), None, None),
        ("expired", ChargeSession(span_pause="until_tomorrow"), None, None),
    ],
)
def test_a_pause_ends(reason, before, manual, span) -> None:
    session, commands = run(before, ev.Resume(reason=reason))
    assert (session.manual, session.span_pause) == (manual, span)
    assert kinds(commands) == []


@pytest.mark.parametrize("plan_applied", [True, False])
def test_a_pause_choice_replaces_a_manual_pause(plan_applied: bool) -> None:
    session, commands = run(
        ChargeSession(manual=START, owner="person"), ev.PauseChoiceMade(choice="next_period", plan_applied=plan_applied)
    )
    assert session.manual is None and session.span_pause == "next_period"
    assert kinds(commands) == (["stop"] if plan_applied else [])


def test_a_strategy_change_to_solar_stops_an_auto_plan() -> None:
    _, commands = run(ChargeSession(owner="plan"), ev.StrategyChange(strategy="solar", plan_applied=True))
    assert commands == (Stop("strategy", True),)
    _, commands = run(ChargeSession(owner="plan"), ev.StrategyChange(strategy="hybrid", plan_applied=True))
    assert kinds(commands) == []


def test_a_strategy_change_to_solar_hands_the_plans_charge_to_the_sun_that_keeps_it() -> None:
    """The hand-over plan → sun: no stop, and the charge is the sun's."""
    session, commands = run(
        ChargeSession(owner="plan"), ev.StrategyChange(strategy="solar", plan_applied=True, sun_keeps=True)
    )
    assert kinds(commands) == [] and session.owner == "solar"


# ---------------------------------------------------------------------------------------------- C7 and R5


def on(**facts) -> ev.ChargerReportedOn:
    return ev.ChargerReportedOn(**{"charging": True, "was_on": True, **facts})


def test_under_a_person_stop_a_charge_the_charger_begins_is_stopped_at_once() -> None:
    """C7: whatever the plan's windows say."""
    session, commands = run(ChargeSession(manual=STOP, plugged=True), on(window_open=True, in_window=True))
    assert commands == (Stop("person_hold"),)
    assert session.owner == "charger_self" and session.hold_stop_pending
    # Counted where it goes out (the task's `Recheck`), not where it is decided: once.
    assert session.hold_stop_times == () and session.hold_tried_at is None


@pytest.mark.parametrize(
    "session",
    [
        ChargeSession(manual=STOP, hold_stop_pending=True),
        ChargeSession(manual=STOP, hold_gave_up=True),
        ChargeSession(manual=STOP, hold_tried_at=T0 - timedelta(seconds=PERSON_HOLD_STOP_GAP_S - 1)),
        ChargeSession(manual=START),
        ChargeSession(span_pause="until_resumed"),
    ],
)
def test_no_stop_under_a_person_stop_is_sent_beside_one_or_after_the_give_up(session: ChargeSession) -> None:
    """R5: one at a time, one per gap, none after the give-up; and only under a person's Stop."""
    _, commands = run(session, on())
    assert kinds(commands) == []


def test_a_start_on_its_way_is_never_stopped_under_a_person_stop() -> None:
    _, commands = run(ChargeSession(manual=STOP), on(start_pending=True))
    assert kinds(commands) == []


def test_person_start_under_their_stop_is_not_stopped_by_c7_while_the_report_beats_the_result() -> None:
    """A person's Start under their Stop: the charger reports charging before the start's result comes back. The
    controller's `_core_start_pending` reads False once the charger charges, so the event says start_pending=False;
    the core's own pending Start keeps C7 from stopping the person's own Start."""
    s = ChargeSession(plugged=True, manual=ManualPause("stop", "plug_in"))
    s, cmds = decide(s, ev.PersonStart(connected=True), T0)
    assert [c.kind for c in cmds] == ["start"]
    s, cmds = decide(s, ev.ChargerReportedOn(charging=True, was_on=False, start_pending=False), T0)
    assert all(c.kind != "stop" for c in cmds), cmds


def test_the_stops_under_a_person_stop_give_up_after_three_in_ten_minutes() -> None:
    """R5 cycling: every stop counts, taken or not; then the give-up and its notification."""
    session = ChargeSession(manual=STOP, plugged=True)
    sent = 0
    now = T0
    for _ in range(PERSON_HOLD_MAX_STOPS):
        session, commands = run(session, on(), now=now)
        assert commands == (Stop("person_hold"),)
        # The report's stop goes out as a task of its own, decided again under the lock.
        session, commands = run(session, ev.Recheck(what="person_hold", control_on=True), now=now)
        assert commands == (Stop("person_hold"),)
        session, _ = run(session, ok("stop", "person_hold"), now=now)
        sent += 1
        now += timedelta(seconds=PERSON_HOLD_STOP_GAP_S)
    session, commands = run(session, on(), now=now)
    assert commands == (Notify(NOTIFY_CHARGER_IGNORES_STOP),) and session.hold_gave_up
    session, commands = run(session, on(), now=now + timedelta(minutes=30))
    assert kinds(commands) == []


def test_stops_older_than_the_window_do_not_count() -> None:
    old = T0 - timedelta(seconds=PERSON_HOLD_WINDOW_S + 1)
    session, commands = run(ChargeSession(manual=STOP, hold_stop_times=(old, old, old), hold_tried_at=old), on())
    assert commands == (Stop("person_hold"),) and session.hold_stop_times == ()


def test_the_stops_start_afresh_once_no_person_stop_holds() -> None:
    session, _ = run(ChargeSession(hold_stop_times=(T0,), hold_gave_up=True), on())
    assert session.hold_stop_times == () and not session.hold_gave_up


def test_a_persons_start_starts_the_stops_under_their_stop_afresh() -> None:
    """They acted: also when a report that landed while the start was on its way still saw the Stop."""
    session = ChargeSession(manual=STOP, plugged=True, hold_stop_times=(T0,), hold_tried_at=T0, hold_gave_up=True)
    session, commands = run(session, ev.PersonStart(connected=True))
    assert commands == (Start("person"),)
    session, _ = run(session, ok("start", "person"))
    assert session.hold_stop_times == () and session.hold_tried_at is None and not session.hold_gave_up


def test_an_awaited_stop_under_a_person_stop_that_is_overdue_holds_off_no_more() -> None:
    """Its task ended a gap ago and its result never came back (`hold_stop_overdue`): dropped, and the charge the
    charger runs is stopped again. Not overdue, it still holds the next stop off."""
    awaited = ChargeSession(
        manual=STOP, plugged=True, hold_stop_pending=True, pending=(PendingCommand("stop", "person_hold"),)
    )
    _, commands = run(awaited, on())
    assert kinds(commands) == []
    session, commands = run(awaited, on(hold_stop_overdue=True))
    assert commands == (Stop("person_hold"),) and session.hold_stop_pending
    assert [(item.command, item.reason) for item in session.pending] == [("stop", "person_hold")]
    session, commands = run(awaited, ev.Timer(control_on=True, hold_stop_overdue=True))
    assert commands == (Stop("person_hold"),)


def test_a_timer_looks_again_under_a_person_stop() -> None:
    later = T0 + timedelta(seconds=PERSON_HOLD_STOP_GAP_S)
    session = ChargeSession(manual=STOP, hold_stop_times=(T0,), hold_tried_at=T0)
    _, commands = run(session, ev.Timer(control_on=True), now=later)
    assert commands == (Stop("person_hold"),)
    _, commands = run(session, ev.Timer(control_on=False), now=later)
    assert kinds(commands) == []


# ---------------------------------------------------------------------------------------------- R3: the car ended a person's charge


def test_the_car_ending_a_person_start_ends_their_pause() -> None:
    """A.2, C8: Auto resumes; the car-ended record starts now."""
    session, commands = run(ChargeSession(manual=START, owner="person"), ev.CarEnded())
    assert session.manual is None and session.car_ended_at == T0 and kinds(commands) == []
    session, _ = run(ChargeSession(manual=STOP), ev.CarEnded())
    assert session.manual == STOP and session.car_ended_at is None


@pytest.mark.parametrize(
    ("window_start", "known_full", "need_grew", "holds"),
    [
        # Known full: every later window of the plug-in, unless the need grew.
        (T0 + timedelta(hours=2), True, False, True),
        (T0 + timedelta(hours=2), True, True, False),
        # Not known full (stopped drawing, or unread): only the window open when it ended.
        (T0 - timedelta(minutes=10), False, False, True),
        (T0 + timedelta(hours=2), False, False, False),
        # No window open.
        (None, True, False, False),
    ],
)
def test_r3_which_windows_the_car_ended_record_holds(window_start, known_full, need_grew, holds) -> None:
    # Known full: read at 90 % against a target of 90 %; the need grew: read 2 points lower since.
    session = ChargeSession(car_ended=CarEnded(at=T0, soc=90.0 if known_full else None, vehicle="car"))
    facts = CarFacts(plan_target=90.0, plan_vehicle="car", plan_full_at=90.0, car_level=88.0 if need_grew else 90.0)
    assert car_ended_holds_window(session, open_window_start=window_start, facts=facts) is holds
    _, commands = run(
        session,
        ev.WindowStart(open_window_start=window_start, **facts.facts()),
        now=T0 + timedelta(hours=2),
    )
    assert kinds(commands) == ([] if holds else ["start"])


@pytest.mark.parametrize("window_start", [T0 - timedelta(minutes=10), T0 + timedelta(hours=2)])
def test_a_plan_waiting_for_the_cars_level_holds_every_window(window_start) -> None:
    """The car ended the plan's charge short of the target on a level nobody read: no window starts it, the one open
    then nor any later, until a level read since says it is below the target (the car facts)."""
    session = ChargeSession(car_ended=CarEnded(at=T0, wait=CarEndedWait(target=91.0, level=86.0, estimated=True)))
    facts = CarFacts(plan_target=91.0, plan_full_at=91.0)
    assert car_ended_holds_window(session, open_window_start=window_start, facts=facts)
    for event in (
        ev.WindowStart(open_window_start=window_start, **facts.facts()),
        ev.Recheck(
            what=ev.RECHECK_CLAIM, control_on=True, window_open=True, in_window=True, plan_present=True,
            open_window_start=window_start, **facts.facts(),
        ),
    ):
        _, commands = run(session, event, now=T0 + timedelta(hours=2))
        assert kinds(commands) == []
    # Without a car-ended record there is nothing to wait for.
    assert not car_ended_holds_window(ChargeSession(), open_window_start=window_start, facts=facts)


def test_no_car_ended_record_holds_nothing() -> None:
    facts = CarFacts(plan_target=90.0, plan_full_at=90.0, car_level=90.0)
    assert not car_ended_holds_window(ChargeSession(), open_window_start=T0, facts=facts)


LATER = T0 + timedelta(hours=1)
WAITING = CarEnded(at=T0, vehicle="car", wait=CarEndedWait(target=91.0, level=86.0, estimated=True))
FULL = CarEnded(at=T0, soc=91.0, vehicle="car")
PLAN_ENDED = ev.CarEnded(plan=True, vehicle="car", wait_target=91.0, wait_level=86.0, wait_estimated=True)


@pytest.mark.parametrize(
    ("pre", "events", "ended"),
    [
        # Set: the car ended the plan's charge in an open window (the plan's watch), with what the shell read then (a
        # level read, never an estimate; the car; what the plan waits for), also one load balancing holds back
        # whose battery probes the car refused.
        (PLUGGED.with_changes(owner="plan"), (PLAN_ENDED,), WAITING),
        (
            PLUGGED.with_changes(balancing_paused=True, paused_origin="plan"),
            (ev.CarEnded(plan=True, soc=91.0, vehicle="car"),),
            FULL,
        ),
        # Not for a charge that is not the plan's (today's watch looks only at the plan's charge).
        (PLUGGED.with_changes(owner="solar"), (PLAN_ENDED,), None),
        (PLUGGED.with_changes(owner="person", manual=START), (PLAN_ENDED,), None),
        # Set: the car ended a person's Start (their pause ends), with the level read then; it waits for nothing.
        (PLUGGED.with_changes(owner="person", manual=START), (ev.CarEnded(soc=91.0, vehicle="car"),), FULL),
        (PLUGGED.with_changes(manual=STOP), (ev.CarEnded(soc=91.0, vehicle="car"),), None),
        # Not for a person's charge whose plug-in ended while its end was on its way (decided in step 6: today's code
        # wrote the record for a car already gone, kept until the next plug-in cleared it).
        (
            PLUGGED.with_changes(owner="person", manual=START),
            (ev.Unplug(previous=True), ev.CarEnded(soc=91.0, vehicle="car")),
            None,
        ),
        (PLUGGED, (ev.CarEnded(soc=91.0, vehicle="car"),), None),
        # A later end replaces the record whole (today's code writes all four fields at every end).
        (PLUGGED.with_changes(owner="plan", car_ended=FULL), (PLAN_ENDED,), WAITING),
        # The wait ends at a level read after the end that is not full for the plan: the end itself is kept (the
        # window open then stays skipped). Not on a level read before the end, a full one (the plan ends by the
        # target's own event), or with no level.
        (
            PLUGGED.with_changes(car_ended=WAITING),
            (ev.CarLevelRead(car_level=80.0, car_level_at=T0 + timedelta(minutes=30)),),
            CarEnded(at=T0, vehicle="car"),
        ),
        (
            PLUGGED.with_changes(car_ended=WAITING),
            (ev.CarLevelRead(car_level=80.0, car_level_at=T0 - timedelta(minutes=30)),),
            WAITING,
        ),
        (
            PLUGGED.with_changes(car_ended=WAITING),
            (ev.CarLevelRead(car_level=91.0, car_level_at=T0 + timedelta(minutes=30), car_level_full=True),),
            WAITING,
        ),
        (PLUGGED.with_changes(car_ended=WAITING), (ev.CarLevelRead(),), WAITING),
        (PLUGGED, (ev.CarLevelRead(car_level=80.0, car_level_at=LATER),), None),
        # Cleared: the unplug and a plug-in after a known one (the record belongs to its plug-in), not the first
        # connection known after a restart either way (nothing says the plug-in changed: today's
        # `_connection_changed` returns before it, so a car gone meanwhile keeps it until the next plug-in).
        (PLUGGED.with_changes(car_ended=WAITING), (ev.Unplug(previous=True),), None),
        (PLUGGED.with_changes(car_ended=WAITING), (ev.PlugIn(previous=False),), None),
        (ChargeSession(car_ended=WAITING), (ev.Unplug(previous=None),), WAITING),
        (ChargeSession(car_ended=WAITING), (ev.PlugIn(previous=None),), WAITING),
        (ChargeSession(car_ended=WAITING), (ev.Unplug(previous=None), ev.PlugIn(previous=False)), None),
        # Cleared: a person deciding to charge, also when the start then does not go out (today's code clears it
        # before the command); not a Start refused with no car, nor a start that is not a person's.
        (PLUGGED.with_changes(car_ended=WAITING), (ev.PersonStart(),), None),
        (PLUGGED.with_changes(car_ended=WAITING), (ev.PersonStart(), failed("start", "person")), None),
        (PLUGGED.with_changes(car_ended=WAITING), (ev.PersonStart(connected=False),), WAITING),
        (PLUGGED.with_changes(car_ended=WAITING), (ev.DirectStart(manual=True),), None),
        (PLUGGED.with_changes(car_ended=WAITING), (ev.DirectStart(cause="solar"), ok("start", "direct")), WAITING),
        # Kept: a restart (stored), a status that says nothing, the windows, a new plan or none, the target's stop, a
        # person's Stop, load balancing, a report of the charger.
        (PLUGGED.with_changes(car_ended=WAITING), (ev.Restart(),), WAITING),
        (PLUGGED.with_changes(car_ended=WAITING), (ev.ConnectionUnknown(),), WAITING),
        (PLUGGED.with_changes(car_ended=WAITING), (ev.WindowStart(open_window_start=T0),), WAITING),
        (PLUGGED.with_changes(car_ended=WAITING, owner="plan"), (ev.WindowEnd(),), WAITING),
        (PLUGGED.with_changes(car_ended=WAITING), (ev.PlanInstalled(window_open=True),), WAITING),
        (PLUGGED.with_changes(car_ended=WAITING), (ev.PlanDropped(),), WAITING),
        (PLUGGED.with_changes(car_ended=WAITING, owner="plan"), (ev.TargetReached(), ok("stop", "target")), WAITING),
        (PLUGGED.with_changes(car_ended=WAITING), (ev.PersonStop(), ok("stop", "person")), WAITING),
        (PLUGGED.with_changes(car_ended=WAITING), (ev.BalancingPause(), ok("stop", "balancing")), WAITING),
        (PLUGGED.with_changes(car_ended=WAITING), (on(connected=False), ev.ChargerReportedOff()), WAITING),
    ],
)
def test_where_the_car_ended_record_is_set_and_cleared(pre, events, ended) -> None:
    """The car-ended record (R3): when the car ended a charge by itself in this plug-in, the level read then, which
    car, and what the plan waits for. One record, set whole by the car's end and cleared whole with its plug-in."""
    session, _ = run(pre, *events)
    assert session.car_ended == ended


@pytest.mark.parametrize(
    ("pre", "events", "plugged"),
    [
        (ChargeSession(), (ev.PlugIn(previous=None),), True),
        (ChargeSession(), (ev.Unplug(previous=None),), False),
        (PLUGGED, (ev.Unplug(previous=True),), False),
        (ChargeSession(plugged=False), (ev.PlugIn(previous=False),), True),
        # A status that says nothing, a report and a command keep the connection the charger last stated.
        (PLUGGED, (ev.ConnectionUnknown(),), True),
        (PLUGGED, (on(connected=False), ev.ChargerReportedOff(connected=False)), True),
        (PLUGGED, (ev.ChargerReportedUnreadable(connected=False),), True),
        (PLUGGED, (ev.PersonStart(), ok("start", "person")), True),
        # A restart clears it: only the next status that states a car says.
        (PLUGGED, (ev.Restart(),), None),
    ],
)
def test_where_the_connection_is_set_and_cleared(pre, events, plugged) -> None:
    """`plugged`: only a plug-in or an unplug the charger stated in so many words changes it (C2)."""
    session, _ = run(pre, *events)
    assert session.plugged is plugged


@pytest.mark.parametrize(
    ("record", "facts", "holds"),
    [
        # A car read full at the end holds every later window, unless its level dropped by the sun's hysteresis since.
        (FULL, CarFacts(plan_target=91.0, plan_vehicle="car", plan_full_at=91.0, car_level=91.0), True),
        (FULL, CarFacts(plan_target=91.0, plan_vehicle="car", plan_full_at=91.0, car_level=89.0), False),
        # Not full for a plan of another car, or a raised target, or one that charges to the car's own ceiling above.
        (FULL, CarFacts(plan_target=91.0, plan_vehicle="other", plan_full_at=91.0), False),
        (FULL, CarFacts(plan_target=95.0, plan_vehicle="car", plan_full_at=95.0), False),
        (FULL, CarFacts(plan_target=100.0, plan_vehicle="car", plan_full_at=100.0), False),
        # A car known full is decided before a wait (today's `_car_ended_hold` asked in that order): one whose need
        # grew is started even if a wait was kept.
        (
            CarEnded(at=T0, soc=91.0, vehicle="car", wait=CarEndedWait(target=91.0)),
            CarFacts(plan_target=91.0, plan_vehicle="car", plan_full_at=91.0, car_level=89.0),
            False,
        ),
        # A wait holds every window until a level read since is below the target; one at the target still holds.
        (WAITING, CarFacts(plan_target=91.0, plan_vehicle="car", plan_full_at=91.0), True),
        (
            WAITING,
            CarFacts(plan_target=91.0, plan_full_at=91.0, car_level=80.0, car_level_at=T0 - timedelta(hours=1)),
            True,
        ),
        (WAITING, CarFacts(plan_target=91.0, plan_full_at=91.0, car_level=80.0, car_level_at=LATER), False),
        (
            WAITING,
            CarFacts(plan_target=91.0, plan_full_at=91.0, car_level=91.0, car_level_at=LATER, car_level_full=True),
            True,
        ),
        # A target raised above the one the car ended at, or another car's plan, waits for nothing.
        (WAITING, CarFacts(plan_target=95.0, plan_full_at=95.0), False),
        (WAITING, CarFacts(plan_target=91.0, plan_vehicle="other", plan_full_at=91.0), False),
        # Nothing known of the plan or the car: only the window open at the end.
        (WAITING, CarFacts(), False),
    ],
)
def test_r3_reads_the_record_and_the_car_facts(record, facts, holds) -> None:
    """The rule today's `controller._car_ended_holds_window` decided, on the session's record and the car facts the
    controller reads now (`CarFacts`), for a window that opened after the end."""
    session = ChargeSession(car_ended=record)
    assert car_ended_holds_window(session, open_window_start=LATER, facts=facts) is holds
    _, commands = run(session, ev.WindowStart(open_window_start=LATER, **facts.facts()), now=LATER)
    assert kinds(commands) == ([] if holds else ["start"])
    # The window open at the end holds whatever the facts say.
    assert car_ended_holds_window(session, open_window_start=T0 - timedelta(minutes=5), facts=facts) or (
        car_ended_known_full(record, facts)
    )


# ---------------------------------------------------------------------------------------------- windows


def test_a_window_start_starts_the_plans_charge() -> None:
    session, commands = run(ChargeSession(plugged=True, held=True, overridden=True), ev.WindowStart(open_window_start=T0))
    assert commands == (Start("plan_window"),)
    assert not session.held and not session.overridden, "the plug-in session a hold belonged to ends here"
    session, _ = run(session, ok("start", "plan_window"))
    assert session.owner == "plan"


@pytest.mark.parametrize(
    "session",
    [ChargeSession(manual=STOP), ChargeSession(manual=START), ChargeSession(span_pause="until_resumed")],
)
def test_no_window_starts_while_a_pause_holds_auto(session: ChargeSession) -> None:
    """Bug 8: a pause whose stop failed leaves its plan; its window timer starts nothing."""
    for trigger in ("timer", "rearm", "plug_in"):
        _, commands = run(session, ev.WindowStart(trigger=trigger, open_window_start=T0))
        assert kinds(commands) == [], trigger


@pytest.mark.parametrize(
    "event",
    [
        ev.WindowStart(target_reached=True),
        ev.WindowStart(target_stopping=True),
        ev.WindowStart(trigger="plug_in", window_open=False),
        ev.WindowStart(trigger="plug_in", top_off=True),
        ev.WindowStart(trigger="plug_in", connected=False),
        ev.WindowStart(trigger="plug_in", start_pending=True),
        ev.WindowStart(trigger="plug_in", solar_holds=True),
        ev.WindowStart(trigger="plug_in", target_reached=True),
    ],
)
def test_a_window_start_that_starts_nothing(event: ev.WindowStart) -> None:
    _, commands = run(PLUGGED, event)
    assert kinds(commands) == []


@pytest.mark.parametrize("trigger", ["timer", "rearm"])
def test_a_window_opening_at_an_empty_charger_starts_nothing(trigger: str) -> None:
    """The charger says no car is plugged in: the window's start waits for the plug-in, which starts it."""
    session, commands = run(
        ChargeSession(held=True, overridden=True), ev.WindowStart(trigger=trigger, open_window_start=T0, connected=False)
    )
    assert kinds(commands) == []
    assert not session.held and not session.overridden, "the plug-in session a hold belonged to still ends here"
    _, commands = run(session, ev.WindowStart(trigger=trigger, open_window_start=T0, connected=None))
    assert commands == (Start("plan_window"),), "a charger that cannot say is started as before"


def test_a_plug_in_inside_a_window_claims_a_charge_the_charger_began() -> None:
    session, commands = run(ChargeSession(owner="charger_self"), ev.WindowStart(trigger="plug_in", control_on=True))
    assert commands == (Start("claim"),)
    assert run(session, ok("start", "claim"))[0].owner == "plan"
    _, commands = run(ChargeSession(owner="solar"), ev.WindowStart(trigger="plug_in", control_on=True))
    assert kinds(commands) == []


def test_a_balancing_pause_keeps_a_plug_in_from_starting() -> None:
    _, commands = run(ChargeSession(balancing_paused=True), ev.WindowStart(trigger="plug_in"))
    assert kinds(commands) == []


@pytest.mark.parametrize(
    ("session", "trigger", "starts"),
    [
        # A re-arm inside an open window (a restart, a follow, an install) leaves a charge load balancing holds back
        # to its regulator, whoever's it was, and keeps the memory.
        (ChargeSession(balancing_paused=True, paused_origin="plan"), ev.TRIGGER_REARM, False),
        (ChargeSession(balancing_paused=True, paused_origin="person"), ev.TRIGGER_REARM, False),
        (ChargeSession(balancing_paused=True), ev.TRIGGER_REARM, False),
        # Nothing held back: the re-arm starts the window's charge as ever.
        (ChargeSession(), ev.TRIGGER_REARM, True),
        # A window's own timer is not a re-arm: it starts as before.
        (ChargeSession(balancing_paused=True, paused_origin="plan"), ev.TRIGGER_TIMER, True),
    ],
)
def test_a_rearm_leaves_a_charge_load_balancing_holds_back(
    session: ChargeSession, trigger: str, starts: bool
) -> None:
    after, commands = run(session, ev.WindowStart(trigger=trigger))
    if starts:
        assert commands == (Start("plan_window"),)
    else:
        assert kinds(commands) == []
        assert (after.balancing_paused, after.paused_origin) == (session.balancing_paused, session.paused_origin)


@pytest.mark.parametrize(
    ("session", "stops"),
    [
        # The plan's own charge: the window's, its top-off, one the charger began by itself, or none running.
        (ChargeSession(owner="plan"), True),
        (ChargeSession(owner="top_off"), True),
        (ChargeSession(owner="charger_self"), True),
        (ChargeSession(), True),
        # A person's Start, a Charge-now start and the sun's charge go on (I3).
        (ChargeSession(owner="person"), False),
        (ChargeSession(owner="person", manual=START), False),
        (ChargeSession(owner="charge_now"), False),
        (ChargeSession(owner="solar"), False),
        # Load balancing holds back a charge: it goes on when it was theirs, and stops when it was the plan's.
        (ChargeSession(balancing_paused=True, paused_origin="person"), False),
        (ChargeSession(balancing_paused=True, paused_origin="charge_now"), False),
        (ChargeSession(balancing_paused=True, paused_origin="solar"), False),
        (ChargeSession(owner="charger_self", balancing_paused=True, paused_origin="solar"), False),
        (ChargeSession(balancing_paused=True, paused_origin="plan"), True),
        (ChargeSession(balancing_paused=True), True),
        # What runs owns the window's end, not what balancing held back before it.
        (ChargeSession(owner="plan", balancing_paused=True, paused_origin="person"), True),
        # A held-back charge is only one while balancing holds it.
        (ChargeSession(paused_origin="person"), True),
    ],
)
def test_a_window_end_stops_only_the_plans_own_charge(session: ChargeSession, stops: bool) -> None:
    assert window_end_spared(session) is not stops
    _, commands = run(session, ev.WindowEnd())
    assert commands == ((Stop("window_end", False),) if stops else (KEEP,))
    _, commands = run(session, ev.WindowEnd(handed_off=True))
    assert kinds(commands) == []
    _, commands = run(session, ev.FinalWindowEnd())
    assert commands == ((Stop("final_window_end", True),) if stops else (KEEP,))


def test_the_last_window_end_ends_the_plan_or_lets_the_car_top_off() -> None:
    _, commands = run(ChargeSession(owner="plan"), ev.FinalWindowEnd())
    assert commands == (Stop("final_window_end", True),)
    session, commands = run(ChargeSession(owner="plan"), ev.FinalWindowEnd(top_off_wanted=True))
    assert session.owner == "top_off" and kinds(commands) == []


@pytest.mark.parametrize(
    "session",
    [
        ChargeSession(owner="person"),
        ChargeSession(owner="charge_now"),
        ChargeSession(owner="solar"),
        ChargeSession(balancing_paused=True, paused_origin="person"),
    ],
)
def test_the_last_window_end_spares_a_charge_not_the_plans_with_no_top_off(session: ChargeSession) -> None:
    """The plan ends (the shell clears it) with no stop, and a charge someone else owns is never a top-off."""
    after, commands = run(session, ev.FinalWindowEnd(top_off_wanted=True))
    assert commands == (KEEP,) and after == session
    after, commands = run(session, ev.FinalWindowEnd())
    assert commands == (KEEP,) and after == session


def test_a_pause_holds_the_window_ends_stop_of_a_person_start() -> None:
    _, commands = run(ChargeSession(owner="plan", manual=START), ev.WindowEnd())
    assert kinds(commands) == []


# ---------------------------------------------------------------------------------------------- the re-arm and the hold


@pytest.mark.parametrize(
    ("session", "spared"),
    [
        (ChargeSession(owner="plan"), False),
        # Only the plan's own charge is the plan's to stop: what a window's end spares, the re-arm spares too.
        (ChargeSession(owner="charge_now"), True),
        (ChargeSession(owner="none", balancing_paused=True, paused_origin="charge_now"), True),
        (ChargeSession(owner="none", balancing_paused=True, paused_origin="plan"), False),
        (ChargeSession(owner="charger_self"), False),
        (ChargeSession(owner="person"), True),
        (ChargeSession(owner="solar"), True),
        (ChargeSession(owner="charger_self", held=True, overridden=True), True),
        (ChargeSession(owner="person", manual=START), True),
        (ChargeSession(owner="plan", span_pause="until_resumed"), True),
    ],
)
def test_a_rearm_outside_the_windows_stops_what_is_not_spared(session: ChargeSession, spared: bool) -> None:
    _, commands = run(session, ev.Rearm(charging=True))
    assert kinds(commands) == ([] if spared else ["stop"])


def test_a_rearm_stop_of_a_running_charge_holds_the_rest_of_the_plug_in() -> None:
    session, _ = run(ChargeSession(owner="plan"), ev.Rearm(charging=True), ok("stop", "rearm"))
    assert session.owner == "none" and session.held and not session.overridden
    session, _ = run(ChargeSession(owner="plan"), ev.Rearm(charging=False), ok("stop", "rearm"))
    assert not session.held


def test_a_rearm_past_the_last_window() -> None:
    session, commands = run(ChargeSession(owner="top_off"), ev.Rearm(past_last=True, top_off_resumes=True))
    assert session.owner == "top_off" and kinds(commands) == []
    session, commands = run(ChargeSession(owner="top_off"), ev.Rearm(past_last=True, control_on=True))
    assert session.owner == "plan" and commands == (Stop("stray", False),)
    _, commands = run(ChargeSession(owner="person"), ev.Rearm(past_last=True, control_on=True))
    assert kinds(commands) == []


def test_the_hold_stops_a_charge_that_began_by_itself_outside_a_window_once() -> None:
    session, commands = run(PLUGGED, on(was_on=False, window_ahead_outside=True, plan_present=True))
    assert commands == (Stop("hold"),) and session.held
    session, _ = run(session, ok("stop", "hold"))
    session, commands = run(session, on(was_on=False, window_ahead_outside=True, plan_present=True))
    assert kinds(commands) == [] and session.overridden, "started again after the hold: a person's override"


HOLD_ON = {"was_on": False, "window_ahead_outside": True, "plan_present": True}
HELD_SESSION = ChargeSession(plugged=True, held=True)
OVERRIDDEN = ChargeSession(plugged=True, held=True, overridden=True)


@pytest.mark.parametrize(
    ("pre", "events", "held", "overridden"),
    [
        # Set: the hold, at a report of a charge that began by itself outside a window (`WindowHold.observe`).
        (PLUGGED, (on(**HOLD_ON),), True, False),
        # Set: a person's override, at the next such start after the hold.
        (HELD_SESSION, (on(**HOLD_ON),), True, True),
        # Not by a charge that is SpotNav's: its owner is, or a start of SpotNav's for one awaits its result (today's
        # `WindowHold.owned` is set as the start goes out, before its result).
        (PLUGGED.with_changes(owner="plan"), (on(**HOLD_ON),), False, False),
        (
            PLUGGED.with_changes(pending=(PendingCommand("start", "direct", owner_after="charge_now"),)),
            (on(**HOLD_ON),),
            False,
            False,
        ),
        # Nor one load balancing gives back with a start of SpotNav's, also one nobody here owns (the charger's own):
        # while the resume awaits its result, and after it until the charge is seen off or stopped (today's
        # `spotnav_started` at the resume). SpotNav does not stop a charge it did not stop before.
        (
            PLUGGED.with_changes(pending=(PendingCommand("start", "balancing_resume", owner_after="none"),)),
            (on(**HOLD_ON),),
            False,
            False,
        ),
        (
            PLUGGED.with_changes(balancing_paused=True),
            (ev.BalancingResume(), ok("start", "balancing_resume"), on(**HOLD_ON)),
            False,
            False,
        ),
        (
            PLUGGED.with_changes(balancing_paused=True),
            (ev.BalancingResume(), ok("start", "balancing_resume"), ev.ChargerReportedOff(), on(**HOLD_ON)),
            True,
            False,
        ),
        (
            PLUGGED.with_changes(balancing_paused=True),
            (ev.BalancingResume(), ok("start", "balancing_resume"), ev.Restart(), on(**HOLD_ON)),
            True,
            False,
        ),
        # Set: the re-arm's stop of a charge that ran outside the windows, once it went out (`held_now`); never one of
        # a charge that did not run, one that never went out, or one whose plug-in ended before its result (an unplug
        # and a new plug-in meanwhile).
        (PLUGGED.with_changes(owner="plan"), (ev.Rearm(charging=True), ok("stop", "rearm")), True, False),
        (PLUGGED.with_changes(owner="plan"), (ev.Rearm(charging=False), ok("stop", "rearm")), False, False),
        (PLUGGED.with_changes(owner="plan"), (ev.Rearm(charging=True), failed("stop", "rearm")), False, False),
        (
            PLUGGED.with_changes(owner="plan"),
            (ev.Rearm(charging=True), ev.Unplug(previous=True), ev.PlugIn(previous=False), ok("stop", "rearm")),
            False,
            False,
        ),
        (
            PLUGGED.with_changes(owner="plan"),
            (
                ev.Rearm(charging=True),
                ev.CommandResult(command="stop", reason="rearm", executed=True, earlier_plug_in=True),
            ),
            False,
            False,
        ),
        # The re-arm spares an overridden hold: nothing is stopped, and both stay.
        (OVERRIDDEN.with_changes(owner="charger_self"), (ev.Rearm(charging=True),), True, True),
        # Cleared: the override, when the charge is seen off; both, when it is seen off with no car.
        (OVERRIDDEN, (ev.ChargerReportedOff(),), True, False),
        (OVERRIDDEN, (ev.ChargerReportedOff(connected=False),), False, False),
        (OVERRIDDEN, (ev.ChargerReportedOff(notified=True),), True, True),
        # Cleared: both, at a report with no car (before the report's own hold), at the unplug, and at a new plug-in
        # after a known unplug (decided in step 5: today's code cleared them at any report saying no car, also one
        # whose control could not be read, which the core is not fed; the new plug-in is where that plug-in ends).
        (OVERRIDDEN, (on(connected=False),), False, False),
        (OVERRIDDEN, (on(connected=False, **HOLD_ON),), True, False),
        # A report whose control cannot be read is no state of the charge, but its "no car" still ends both (today's
        # `WindowHold.observe` ends them with a control it cannot read); with a car, or none stated, nothing changes.
        (OVERRIDDEN, (ev.ChargerReportedUnreadable(connected=False),), False, False),
        (OVERRIDDEN, (ev.ChargerReportedUnreadable(connected=True),), True, True),
        (OVERRIDDEN, (ev.ChargerReportedUnreadable(connected=None),), True, True),
        (PLUGGED, (on(connected=False, **HOLD_ON), ev.ChargerReportedUnreadable(connected=False)), False, False),
        (OVERRIDDEN, (ev.Unplug(previous=True),), False, False),
        (OVERRIDDEN, (ev.Unplug(previous=None),), False, False),
        (OVERRIDDEN, (ev.PlugIn(previous=False),), False, False),
        (OVERRIDDEN, (ev.PlugIn(previous=None),), True, True),
        # Cleared: both, where the next window starts (a timer's or a re-arm's), also when the target ends the plan
        # instead; a window started at plug-in or a start's retry is the same plug-in.
        (OVERRIDDEN, (ev.WindowStart(),), False, False),
        (OVERRIDDEN, (ev.WindowStart(trigger=ev.TRIGGER_REARM),), False, False),
        (OVERRIDDEN, (ev.WindowStart(target_reached=True),), False, False),
        (OVERRIDDEN, (ev.WindowStart(target_stopping=True),), False, False),
        (OVERRIDDEN, (ev.WindowStart(trigger=ev.TRIGGER_PLUG_IN, window_open=True),), True, True),
        (OVERRIDDEN, (ev.WindowStart(trigger=ev.TRIGGER_RETRY, window_open=True),), True, True),
        # Kept: a person's Start or Stop over a held window, a new plan, a window's end, load balancing, a restart.
        (OVERRIDDEN, (ev.PersonStart(), ok("start", "person")), True, True),
        (OVERRIDDEN, (ev.PersonStop(), ok("stop", "person")), True, True),
        (OVERRIDDEN, (ev.PlanInstalled(window_open=False),), True, True),
        (OVERRIDDEN, (ev.PlanDropped(),), True, True),
        (OVERRIDDEN.with_changes(owner="charger_self"), (ev.WindowEnd(),), True, True),
        (OVERRIDDEN, (ev.BalancingPause(), ok("stop", "balancing")), True, True),
        (OVERRIDDEN, (ev.Restart(),), True, True),
    ],
)
def test_where_the_holds_memory_is_set_and_cleared(pre, events, held, overridden) -> None:
    """The hold's memory (`window_hold.WindowHold`): `held` means "this plug-in session's charge that began by itself
    outside a window was stopped once" (or the re-arm stopped one that ran), `overridden` "a person started it again
    after that, and it goes on". `overridden` is set only while `held` (a person's override of a hold this plug-in)."""
    session, _ = run(pre, *events)
    assert (session.held, session.overridden) == (held, overridden)
    assert not session.overridden or session.held


@pytest.mark.parametrize(
    "event",
    [
        on(was_on=True, window_ahead_outside=True),
        on(was_on=False, window_ahead_outside=False),
        on(was_on=False, window_ahead_outside=True, owned=True),
        on(was_on=False, window_ahead_outside=True, solar_holds=True),
    ],
)
def test_the_hold_leaves_alone(event: ev.ChargerReportedOn) -> None:
    _, commands = run(PLUGGED, event)
    assert kinds(commands) == []


def test_a_pause_holds_the_hold_only_for_an_auto_plan() -> None:
    paused = ChargeSession(span_pause="until_resumed")
    _, commands = run(paused, on(was_on=False, window_ahead_outside=True, plan_auto_owned=True))
    assert kinds(commands) == []
    _, commands = run(paused, on(was_on=False, window_ahead_outside=True, plan_auto_owned=False))
    assert kinds(commands) == ["stop"]


@pytest.mark.parametrize("connected", [False, True, None])
def test_a_report_whose_control_cannot_be_read_decides_nothing_of_the_charge(connected) -> None:
    """Only the hold's memory may change (with no car); the owner, the person intent, load balancing's memory and
    what awaits a result stay, and nothing is sent."""
    pre = ChargeSession(
        plugged=True,
        owner="plan",
        manual=START,
        held=True,
        overridden=True,
        balancing_paused=True,
        paused_origin="plan",
        pending=(PendingCommand("stop", "hold"),),
    )
    session, commands = run(pre, ev.ChargerReportedUnreadable(connected=connected))
    assert kinds(commands) == []
    assert session == (pre.with_changes(held=False, overridden=False) if connected is False else pre)


def test_a_charge_inside_a_window_nobody_started_is_the_plans() -> None:
    _, commands = run(PLUGGED, on(window_open=True, in_window=True))
    assert commands == (Start("claim"),)
    _, commands = run(ChargeSession(car_ended=ENDED), on(window_open=True, open_window_start=T0 - timedelta(minutes=5)))
    assert kinds(commands) == [], "not after the car ended a charge in this window (R3)"


def test_a_plan_charge_that_strays_outside_every_window_is_stopped() -> None:
    _, commands = run(ChargeSession(owner="plan"), on(plan_present=True, in_window=False))
    assert commands == (Stop("stray"),)
    _, commands = run(ChargeSession(owner="plan"), on(plan_present=True, in_window=False, handed_off=True))
    assert kinds(commands) == []
    _, commands = run(ChargeSession(owner="person"), on(plan_present=True, in_window=False))
    assert kinds(commands) == []


def test_a_report_of_the_charger_on_or_off_is_its_own_charge_or_none() -> None:
    session, _ = run(ChargeSession(), on())
    assert session.owner == "charger_self"
    session, _ = run(ChargeSession(), on(start_pending=True))
    assert session.owner == "none"
    session, _ = run(ChargeSession(), on(stop_recent=True))
    assert session.owner == "none"
    session, _ = run(ChargeSession(owner="charger_self", overridden=True), ev.ChargerReportedOff())
    assert session.owner == "none" and not session.overridden
    for owner in ("person", "plan", "solar", "top_off", "charge_now"):
        session, _ = run(ChargeSession(owner=owner), ev.ChargerReportedOff())
        assert session.owner == "none", f"seen off with no start on its way, a {owner} charge is nobody's"
        session, _ = run(ChargeSession(owner=owner), ev.ChargerReportedOff(start_pending=True))
        assert session.owner == owner, "a start the charger has not answered keeps its owner"
    session, _ = run(ChargeSession(owner="person"), ev.ChargerReportedOff(notified=True))
    assert session.owner == "none", "the pass that tells readers does"
    session, _ = run(ChargeSession(owner="person"), ev.ChargerReportedOff(notified=True, start_pending=True))
    assert session.owner == "person"


# ---------------------------------------------------------------------------------------------- the sun


def test_the_suns_start_and_stop() -> None:
    _, commands = run(ChargeSession(span_pause="until_resumed"), ev.SolarStart())
    assert kinds(commands) == []
    _, commands = run(ChargeSession(), ev.SolarStart(strategy_ok=False))
    assert kinds(commands) == []
    session, commands = run(ChargeSession(), ev.SolarStart(), ok("start", "solar"))
    assert session.owner == "solar"
    _, commands = run(ChargeSession(owner="person", manual=START), ev.SolarStop())
    assert kinds(commands) == [], "never a charge a person started under their manual pause"
    session, commands = run(ChargeSession(owner="solar"), ev.SolarStop(), ok("stop", "solar"))
    assert session.owner == "none"


@pytest.mark.parametrize(
    ("session", "event", "stops"),
    [
        (ChargeSession(owner="charger_self"), ev.SolarStop(take_over=True, charging=True), True),
        (ChargeSession(owner="none"), ev.SolarStop(take_over=True, charging=True), True),
        (ChargeSession(owner="charger_self"), ev.SolarStop(take_over=True, charging=False), False),
        (ChargeSession(owner="charger_self"), ev.SolarStop(take_over=True, charging=True, start_pending=True), False),
        (ChargeSession(owner="charger_self"), ev.SolarStop(take_over=True, charging=True, stop_recent=True), False),
        (ChargeSession(owner="charger_self", overridden=True), ev.SolarStop(take_over=True, charging=True), False),
        (ChargeSession(owner="plan"), ev.SolarStop(take_over=True, charging=True), False),
        (ChargeSession(owner="person"), ev.SolarStop(take_over=True, charging=True), False),
        (ChargeSession(owner="charger_self", manual=STOP), ev.SolarStop(take_over=True, charging=True), False),
        (ChargeSession(owner="charger_self"), ev.SolarStop(take_over=True, charging=True, strategy_ok=False), False),
        (ChargeSession(owner="charger_self"), ev.SolarStop(take_over=True, charging=True, top_off_or_window=True), False),
    ],
)
def test_i4_a_self_started_charge_without_surplus_is_stopped_at_the_first_reading(session, event, stops) -> None:
    _, commands = run(session, event)
    assert kinds(commands) == (["stop"] if stops else [])


# ---------------------------------------------------------------------------------------------- load balancing


@pytest.mark.parametrize(
    ("owner", "code", "was_on", "remembered", "safety"),
    [
        ("plan", "pause", True, "plan", False),
        ("solar", "pause", True, "solar", False),
        ("charger_self", "pause", True, None, False),
        ("person", "safety_stop", True, "person", True),
        ("plan", "safety_stop", True, False, False),
        ("plan", "pause", False, False, False),
        # The owner already cleared (the window's end while a stop of ours was unanswered): nothing is held back.
        ("none", "pause", True, False, False),
    ],
)
def test_a_balancing_stop_remembers_the_charge_it_holds_back(owner, code, was_on, remembered, safety) -> None:
    """P3: a safety stop of a person's charge is remembered like the pause; nothing else's is."""
    session, commands = run(
        ChargeSession(owner=owner),
        ev.BalancingPause(code=code, was_on=was_on),
        ok("stop", "balancing"),
    )
    assert commands == (Keep(),) or kinds(commands) == []
    assert session.owner == "none"
    if remembered is False:
        assert not session.balancing_paused
    else:
        assert session.balancing_paused and session.paused_origin == remembered
        assert session.held_for_safety is safety
        assert session.safety_stopped_at == (T0 if safety else None)


@pytest.mark.parametrize(
    ("session", "resumes"),
    [
        (ChargeSession(balancing_paused=True, paused_origin="plan"), True),
        (ChargeSession(balancing_paused=True, paused_origin="plan", span_pause="until_resumed"), False),
        (ChargeSession(balancing_paused=True, paused_origin="plan", manual=START), True),
        # C5, R4: a person's charge, under any pause but their Stop.
        (ChargeSession(balancing_paused=True, paused_origin="person", span_pause="until_resumed"), True),
        (ChargeSession(balancing_paused=True, paused_origin="person", manual=START), True),
        (ChargeSession(balancing_paused=True, paused_origin="person", manual=STOP), False),
        (ChargeSession(balancing_paused=False, paused_origin="plan"), False),
        # P3: a safety stop's gap.
        (
            ChargeSession(
                balancing_paused=True,
                paused_origin="person",
                held_for_safety=True,
                safety_stopped_at=T0 - timedelta(seconds=SAFETY_RESUME_GAP_S - 1),
            ),
            False,
        ),
        (
            ChargeSession(
                balancing_paused=True,
                paused_origin="person",
                held_for_safety=True,
                safety_stopped_at=T0 - timedelta(seconds=SAFETY_RESUME_GAP_S),
            ),
            True,
        ),
    ],
)
def test_load_balancing_resumes_the_charge_it_held_back(session: ChargeSession, resumes: bool) -> None:
    _, commands = run(session, ev.BalancingResume())
    assert kinds(commands) == (["start"] if resumes else [])


def test_a_balancing_resume_gives_the_charge_back_what_it_was() -> None:
    session, _ = run(
        ChargeSession(balancing_paused=True, paused_origin="person", held_for_safety=True),
        ev.BalancingResume(),
        ok("start", "balancing_resume"),
    )
    assert session.owner == "person" and not session.balancing_paused and not session.held_for_safety
    session, _ = run(ChargeSession(balancing_paused=True), ev.BalancingResume(), ok("start", "balancing_resume"))
    assert session.owner == "none", "nobody's stays nobody's, not a Charge-now start a window's end spares"
    session, _ = run(session, ev.ChargerReportedOn(charging=True))
    assert session.owner == "charger_self", "the charger's own once it is seen charging"
    session, _ = run(
        ChargeSession(balancing_paused=True, paused_origin="solar"),
        ev.BalancingResume(),
        failed("start", "balancing_resume"),
    )
    assert session.balancing_paused and session.paused_origin == "solar" and session.owner == "none"


HELD = ChargeSession(plugged=True, balancing_paused=True, paused_origin="person")


@pytest.mark.parametrize(
    ("pre", "events", "paused", "origin"),
    [
        # Set: load balancing's pause, once its stop went out (the regulator's `_regulated_stop`).
        (ChargeSession(owner="plan"), (ev.BalancingPause(), ok("stop", "balancing")), True, "plan"),
        # A repeat pause of the charge it holds back is the same pause.
        (HELD, (ev.BalancingPause(was_on=False), ok("stop", "balancing")), True, "person"),
        # A pause whose stop never went out changes nothing (`_stop_request_locked` puts it back).
        (ChargeSession(owner="plan"), (ev.BalancingPause(), failed("stop", "balancing")), False, None),
        (HELD, (ev.BalancingPause(), failed("stop", "balancing")), True, "person"),
        # Set: a start held back below the floor (`_start_locked`), as the charge it would have been.
        (
            ChargeSession(plugged=True),
            (ev.SolarStart(), ev.CommandResult(command="start", reason="solar", executed=False, balancing_held=True)),
            True,
            "solar",
        ),
        # Set again: a resume (a battery probe) that did not take.
        (HELD, (ev.BalancingResume(), failed("start", "balancing_resume")), True, "person"),
        # Not set for a plug-in that ended while the command was on its way (an unplug decided before its result).
        (
            ChargeSession(owner="plan", plugged=True),
            (ev.BalancingPause(), ev.Unplug(previous=True), ok("stop", "balancing")),
            False,
            None,
        ),
        (HELD, (ev.BalancingResume(), ev.Unplug(previous=True), failed("start", "balancing_resume")), False, None),
        (
            ChargeSession(plugged=True),
            (
                ev.SolarStart(),
                ev.Unplug(previous=True),
                ev.CommandResult(command="start", reason="solar", executed=False, balancing_held=True),
            ),
            False,
            None,
        ),
        # Nor for one that ended and a new one began meanwhile (unplugged and plugged in again before the result):
        # the charge held back would be the earlier plug-in's, never the new one's to resume.
        (
            ChargeSession(owner="person", plugged=True),
            (ev.BalancingPause(), ev.Unplug(previous=True), ev.PlugIn(previous=False), ok("stop", "balancing")),
            False,
            None,
        ),
        (
            HELD,
            (
                ev.BalancingResume(),
                ev.Unplug(previous=True),
                ev.PlugIn(previous=False),
                failed("start", "balancing_resume"),
            ),
            False,
            None,
        ),
        (
            ChargeSession(plugged=True),
            (
                ev.PersonStart(connected=True),
                ev.Unplug(previous=True),
                ev.PlugIn(previous=False),
                ev.CommandResult(command="start", reason="person", executed=False, balancing_held=True),
            ),
            False,
            None,
        ),
        # The shell decided the start before the plug-in ended but fed it after: its result says so.
        (
            ChargeSession(plugged=True),
            (
                ev.SolarStart(),
                ev.CommandResult(
                    command="start", reason="solar", executed=False, balancing_held=True, earlier_plug_in=True
                ),
            ),
            False,
            None,
        ),
        # A plug-in first known after a restart is no new plug-in: still that charge.
        (
            ChargeSession(owner="person", plugged=None),
            (ev.BalancingPause(), ev.PlugIn(previous=None), ok("stop", "balancing")),
            True,
            "person",
        ),
        # Cleared: every other start, as soon as it is asked for (also one that then does not go out).
        (HELD, (ev.PersonStart(),), False, None),
        (HELD, (ev.BalancingResume(),), False, None),
        (HELD, (ev.BalancingResume(), ok("start", "balancing_resume")), False, None),
        (HELD.with_changes(manual=None), (ev.DirectStart(cause="solar"),), False, None),
        # Cleared: every other stop, once it went out.
        (HELD, (ev.PersonStop(), ok("stop", "person")), False, None),
        (HELD, (ev.PersonStop(), failed("stop", "person")), True, "person"),
        (HELD, (ev.DirectStop(), ok("stop", "direct")), False, None),
        # Cleared: the plug-in it held a charge for is over.
        (HELD, (ev.Unplug(previous=True),), False, None),
        (HELD, (ev.Unplug(previous=None),), False, None),
        (HELD, (ev.PlugIn(previous=False),), False, None),
        (HELD, (ev.PlugIn(previous=None),), True, "person"),
        # Cleared: a new plan with no window open now, whose charge it was too (decided in step 4: today's code kept
        # the origin with the flag down, which nothing read).
        (HELD, (ev.PlanInstalled(window_open=False),), False, None),
        (HELD, (ev.PlanInstalled(window_open=True),), True, "person"),
        # Cleared: the regulator finds it no longer wanted (`forget_balancing_pause`, fed since step 4).
        (HELD, (ev.BalancingDropped(),), False, None),
        # A window's start is a start like any other; one at plug-in leaves the charge to the regulator.
        (HELD.with_changes(manual=None), (ev.WindowStart(),), False, None),
        (HELD.with_changes(manual=None), (ev.WindowStart(trigger=ev.TRIGGER_PLUG_IN),), True, "person"),
        # Kept: a restart, a report of the charger off, a window's end, the person's pause choice.
        (HELD, (ev.Restart(),), True, "person"),
        (HELD, (ev.ChargerReportedOff(),), True, "person"),
        (HELD.with_changes(manual=None), (ev.WindowEnd(),), True, "person"),
        (HELD, (ev.PauseChoiceMade(choice="until_resumed"),), True, "person"),
    ],
)
def test_where_load_balancings_memory_is_set_and_cleared(pre, events, paused, origin) -> None:
    """Load balancing's memory means "the last thing done to the charge control was the regulator's pause, and this
    was the charge it paused": set where its pause (or a start held back below the floor) is done, cleared by every
    other start and every other stop that went out, by the end of the plug-in, by a new plan outside its windows and
    when the regulator finds the charge no longer wanted. `paused_origin` is set only while `balancing_paused`."""
    session, _ = run(pre, *events)
    assert (session.balancing_paused, session.paused_origin) == (paused, origin)


def test_a_resume_while_charging_does_nothing() -> None:
    _, commands = run(ChargeSession(balancing_paused=True, paused_origin="plan"), ev.BalancingResume(charging=True))
    assert kinds(commands) == []


# ---------------------------------------------------------------------------------------------- the plan's own ends


def test_the_target_and_the_need() -> None:
    _, commands = run(ChargeSession(owner="plan"), ev.TargetReached())
    assert commands == (Stop("target", True),)
    _, commands = run(ChargeSession(owner="person", manual=START), ev.TargetReached(gated=True))
    assert kinds(commands) == []
    _, commands = run(ChargeSession(owner="person", manual=START), ev.TargetReached(gated=False))
    assert kinds(commands) == ["stop"], "a window start or an install decides it without the gate (today)"
    _, commands = run(ChargeSession(owner="plan"), ev.NeedMet())
    assert commands == (Stop("need_met", True),)
    for event in (ev.NeedMet(control_on=False), ev.NeedMet(handed_off=True), ev.NeedMet(solar_holds=True)):
        assert kinds(run(ChargeSession(owner="plan"), event)[1]) == []
    _, commands = run(ChargeSession(owner="person"), ev.NeedMet())
    assert kinds(commands) == []
    session, _ = run(ChargeSession(owner="top_off"), ev.NeedMet())
    assert session.owner == "plan"


def test_the_top_off_end() -> None:
    _, commands = run(ChargeSession(owner="top_off"), ev.TopOffEnd(reason="full"))
    assert commands == (Stop("top_off_end", True),)
    _, commands = run(ChargeSession(owner="top_off"), ev.TopOffEnd(valid=False))
    assert kinds(commands) == []


def test_a_new_or_dropped_plan_ends_a_top_off() -> None:
    session, _ = run(ChargeSession(owner="top_off", balancing_paused=True), ev.PlanInstalled(window_open=False))
    assert session.owner == "plan" and not session.balancing_paused
    session, _ = run(ChargeSession(balancing_paused=True), ev.PlanInstalled(window_open=True))
    assert session.balancing_paused
    session, _ = run(ChargeSession(owner="top_off"), ev.PlanDropped())
    assert session.owner == "plan"


def test_a_start_or_stop_with_no_boundary() -> None:
    session, _ = run(ChargeSession(), ev.DirectStart(manual=True), ok("start", "direct"))
    assert session.owner == "person" and session.manual is None
    session, _ = run(ChargeSession(), ev.DirectStart(cause="solar"), ok("start", "direct"))
    assert session.owner == "solar"
    session, _ = run(ChargeSession(), ev.DirectStart(), ok("start", "direct"))
    assert session.owner == "charge_now"
    session, _ = run(ChargeSession(owner="charge_now"), ev.DirectStop(), ok("stop", "direct"))
    assert session.owner == "none"


# ---------------------------------------------------------------------------------------------- results


def test_a_stop_that_sent_nothing_to_an_unreadable_charger_keeps_the_owner() -> None:
    unobserved = ev.CommandResult(command="stop", reason="window_end", executed=True, unobserved=True)
    session, _ = run(ChargeSession(owner="plan"), ev.WindowEnd(), unobserved)
    assert session.owner == "plan"
    unobserved = ev.CommandResult(command="stop", reason="final_window_end", executed=True, unobserved=True)
    session, _ = run(ChargeSession(owner="plan"), ev.FinalWindowEnd(), unobserved)
    assert session.owner == "none", "unless the plan was cleared with it"


def test_a_result_with_no_pending_command_still_names_its_owner() -> None:
    session, _ = run(ChargeSession(), ok("start", "claim"))
    assert session.owner == "plan"


# ---------------------------------------------------------------------------------------------- restart


def test_a_restart_forgets_what_today_keeps_in_memory_only() -> None:
    before = ChargeSession(
        plugged=True,
        owner="person",
        manual=START,
        held=True,
        car_ended=CarEnded(at=T0, soc=90.0, vehicle="car"),
        balancing_paused=True,
        paused_origin="person",
        held_for_safety=True,
        safety_stopped_at=T0,
        hold_stop_times=(T0,),
        hold_gave_up=True,
        hold_stop_pending=True,
        pending=(PendingCommand("stop", "person"),),
    )
    session, commands = run(ChargeSession.from_json(before.to_json()), ev.Restart())
    assert kinds(commands) == []
    assert session == ChargeSession(
        owner="person",
        manual=START,
        held=True,
        car_ended=CarEnded(at=T0, soc=90.0, vehicle="car"),
        balancing_paused=True,
        paused_origin="person",
    )


def test_a_person_stop_an_older_release_stored_is_a_manual_pause() -> None:
    """C4."""
    session, _ = run(ChargeSession(), ev.Restart(legacy_person_stop=True))
    assert session.manual == STOP
    session, _ = run(ChargeSession(span_pause="until_resumed"), ev.Restart(legacy_person_stop=True))
    assert session.manual is None and session.span_pause == "until_resumed"


# ---------------------------------------------------------------------------------------------- the record


def test_the_session_round_trips_and_refuses_what_it_cannot_read() -> None:
    session = ChargeSession(
        plugged=False,
        owner="top_off",
        manual=STOP_NEXT,
        held=True,
        overridden=True,
        car_ended=CarEnded(at=T0, soc=88.5, vehicle="car", wait=CarEndedWait(target=91.0, level=86.0, estimated=True)),
        balancing_paused=True,
        paused_origin="plan",
        held_for_safety=True,
        safety_stopped_at=T0,
        hold_stop_times=(T0, T0 + timedelta(seconds=30)),
        hold_tried_at=T0,
        hold_gave_up=True,
        hold_stop_pending=True,
        pending=(
            PendingCommand("start", "balancing_resume", "none", "plan", "plan", True, "pause", True),
            PendingCommand("stop", "hold"),
        ),
    )
    assert ChargeSession.from_json(session.to_json()) == session
    raw = session.to_dict()
    with pytest.raises(SessionError):
        ChargeSession.from_dict({**raw, "version": 3})
    with pytest.raises(SessionError):
        ChargeSession.from_dict({**raw, "owner": "nobody"})
    with pytest.raises(SessionError):
        ChargeSession.from_dict({**raw, "car_ended": {**raw["car_ended"], "at": "2026-10-04T22:00:00"}})
    with pytest.raises(SessionError):
        ChargeSession.from_dict({**raw, "car_ended": {**raw["car_ended"], "wait": {"target": 91.0}}})
    with pytest.raises(SessionError):
        ChargeSession.from_dict({**raw, "car_ended": {**raw["car_ended"], "soc": True}})
    with pytest.raises(SessionError):
        ChargeSession.from_dict({**raw, "car_ended_at": None})
    with pytest.raises(SessionError):
        ChargeSession.from_dict({key: value for key, value in raw.items() if key != "held"})
    with pytest.raises(SessionError):
        ChargeSession(manual=STOP, span_pause="until_resumed")
    with pytest.raises(SessionError):
        ChargeSession.from_json("{")


def test_every_event_has_a_rule_and_round_trips() -> None:
    for kind, cls in ev.EVENT_TYPES.items():
        event = cls()
        assert ev.event_from_dict(event.to_dict()) == event, kind
        decide(ChargeSession(), event, T0)
    event = ev.WindowStart(open_window_start=T0)
    assert ev.event_from_dict(event.to_dict()) == event
    with pytest.raises(SessionError):
        ev.event_from_dict({"kind": "nothing"})
    with pytest.raises(TypeError):
        decide(ChargeSession(), ev.Event(), T0)


def test_the_core_has_no_home_assistant_import() -> None:
    import pathlib

    core = pathlib.Path(__file__).parent.parent / "custom_components" / "spotnav" / "core"
    for path in core.glob("*.py"):
        assert "homeassistant" not in path.read_text(encoding="utf-8"), path.name


def test_the_cores_constants_are_todays() -> None:
    from custom_components.spotnav.execution import controller

    assert PERSON_HOLD_STOP_GAP_S == controller.PERSON_HOLD_STOP_GAP_S
    assert PERSON_HOLD_MAX_STOPS == controller.PERSON_HOLD_MAX_STOPS
    assert PERSON_HOLD_WINDOW_S == controller.PERSON_HOLD_WINDOW_S
    assert {item.name for item in dataclass_fields(ChargeSession)} >= {"owner", "manual", "span_pause"}


@pytest.mark.parametrize("event", [ev.WindowEnd(continued=True), ev.FinalWindowEnd(continued=True)])
def test_a_window_end_the_next_plan_takes_over_stops_nothing(event: ev.Event) -> None:
    """The next plan, waiting for this boundary, has a window open now: the plan's charge goes on (no stop, no
    top-off), and the owner stays the plan's."""
    session, commands = run(ChargeSession(owner="plan"), event)
    assert kinds(commands) == [] and session.owner == "plan"
    session, commands = run(ChargeSession(owner="plan"), ev.FinalWindowEnd(continued=True, top_off_wanted=True))
    assert kinds(commands) == [] and session.owner == "plan"


def test_a_command_from_an_earlier_plug_in_round_trips_and_an_older_record_reads_as_this_one() -> None:
    session = ChargeSession(plugged=True, pending=(PendingCommand("stop", "balancing", earlier_plug_in=True),))
    assert ChargeSession.from_dict(session.to_dict()) == session
    raw = ChargeSession(pending=(PendingCommand("stop", "balancing"),)).to_dict()
    del raw["pending"][0]["earlier_plug_in"]  # a debug bundle written before it was kept
    assert ChargeSession.from_dict(raw).pending[0].earlier_plug_in is False
    assert "pending" not in session.to_store(), "transient: a restart clears every command awaiting a result"


# ---------------------------------------------------------------------------------------------- the owner (step 7)

PLAN_CHARGE = PLUGGED.with_changes(owner="plan")


def _unobserved(reason: str) -> ev.CommandResult:
    return ev.CommandResult(command="stop", reason=reason, executed=True, unobserved=True)


@pytest.mark.parametrize(
    ("pre", "events", "owner"),
    [
        # Set: by a start's result that went out, to the owner the start was for (today's `_start_locked` writes
        # `charge_origin` and `plan_charge` as the command goes out, and takes them back if it never did).
        (PLUGGED, (ev.WindowStart(), ok("start", "plan_window")), "plan"),
        (PLUGGED, (ev.PersonStart(), ok("start", "person")), "person"),
        (PLUGGED, (ev.DirectStart(manual=True), ok("start", "direct")), "person"),
        (PLUGGED, (ev.DirectStart(), ok("start", "direct")), "charge_now"),
        (PLUGGED, (ev.DirectStart(cause="other"), ok("start", "direct")), "charge_now"),
        (PLUGGED, (ev.DirectStart(cause="solar"), ok("start", "direct")), "solar"),
        (PLUGGED, (ev.DirectStart(cause="plan_window"), ok("start", "direct")), "plan"),
        (PLUGGED, (ev.DirectStart(cause="min_soc"), ok("start", "direct")), "min_soc"),
        (PLUGGED, (ev.SolarStart(), ok("start", "solar")), "solar"),
        (PLUGGED, (ev.MinSocStart(), ok("start", "min_soc")), "min_soc"),
        (PLUGGED, (on(window_open=True), ok("start", "claim")), "plan"),
        # Never by a start that did not go out (I4: today's rollback), nor one load balancing held back (its memory
        # keeps whose it is, the owner stays as it was).
        (PLUGGED, (ev.WindowStart(), failed("start", "plan_window")), "none"),
        (PLUGGED, (ev.DirectStart(), failed("start", "direct")), "none"),
        (
            PLUGGED,
            (ev.WindowStart(), ev.CommandResult(command="start", reason="plan_window", executed=False, balancing_held=True)),
            "none",
        ),
        (PLAN_CHARGE, (ev.PersonStart(), failed("start", "person")), "plan"),
        # A balancing resume gives the charge back as what it was; today's record of the plan's charge as
        # `(plan_window, False)` (a stop after `async_drop_plan`) reads back as the plan's (`ORIGIN_OWNER`), so it
        # resumes as `plan`, and nobody's stays nobody's.
        (
            PLUGGED.with_changes(balancing_paused=True, paused_origin="plan"),
            (ev.BalancingResume(), ok("start", "balancing_resume")),
            "plan",
        ),
        (
            PLUGGED.with_changes(balancing_paused=True, paused_origin="person"),
            (ev.BalancingResume(), ok("start", "balancing_resume")),
            "person",
        ),
        (PLUGGED.with_changes(balancing_paused=True), (ev.BalancingResume(), ok("start", "balancing_resume")), "none"),
        # Handed over with no command: the plan's charge to the sun, the floor's to the plan or the sun, the plan's
        # past the last window to its top-off and back.
        (PLAN_CHARGE, (ev.StrategyChange(plan_applied=True, sun_keeps=True),), "solar"),
        (PLUGGED.with_changes(owner="min_soc"), (ev.MinSocEnd(handed_to=ev.HAND_TO_PLAN),), "plan"),
        (PLUGGED.with_changes(owner="min_soc"), (ev.MinSocEnd(handed_to=ev.HAND_TO_SOLAR),), "solar"),
        (PLAN_CHARGE, (ev.FinalWindowEnd(top_off_wanted=True),), "top_off"),
        (PLUGGED.with_changes(owner="top_off"), (ev.NeedMet(control_on=False),), "plan"),
        (PLUGGED.with_changes(owner="top_off"), (ev.PlanInstalled(),), "plan"),
        (PLUGGED.with_changes(owner="top_off"), (ev.PlanDropped(),), "plan"),
        (PLUGGED.with_changes(owner="top_off"), (ev.Rearm(past_last=True),), "plan"),
        # Observed: a charge nobody here started is the charger's own once reported on.
        (PLUGGED, (on(),), "charger_self"),
        # Kept: a plan dropped by a person's Start (today's `async_drop_plan` clears `plan_charge` and keeps
        # `charge_origin`, which reads as the plan's), a window end the next plan takes over, a restart.
        (PLAN_CHARGE, (ev.PlanDropped(),), "plan"),
        (PLAN_CHARGE, (ev.WindowEnd(continued=True),), "plan"),
        (PLAN_CHARGE, (ev.FinalWindowEnd(continued=True),), "plan"),
        (PLAN_CHARGE, (ev.Restart(),), "plan"),
        (PLUGGED.with_changes(owner="person"), (ev.Restart(),), "person"),
        # Kept: seen off while a start of ours awaits the charger's answer (also in the pass that tells readers).
        (PLAN_CHARGE, (ev.ChargerReportedOff(start_pending=True),), "plan"),
        (PLAN_CHARGE, (ev.ChargerReportedOff(notified=True, start_pending=True),), "plan"),
        # Kept: a stop that never went out (I4), and one that sent nothing because the control said nothing.
        (PLAN_CHARGE, (ev.WindowEnd(), failed("stop", "window_end")), "plan"),
        (PLAN_CHARGE, (ev.WindowEnd(), _unobserved("window_end")), "plan"),
        # Cleared: a stop that went out, also one a stop of ours still on its way answered for, and one that sent
        # nothing to a control that said nothing when it also cleared the plan.
        (PLAN_CHARGE, (ev.WindowEnd(), ok("stop", "window_end")), "none"),
        (
            PLAN_CHARGE,
            (ev.WindowEnd(), ev.CommandResult(command="stop", reason="window_end", executed=True, settled=True)),
            "none",
        ),
        (PLAN_CHARGE, (ev.TargetReached(), _unobserved("target")), "none"),
        (PLUGGED.with_changes(owner="person"), (ev.PersonStop(), ok("stop", "person")), "none"),
        (PLUGGED.with_changes(owner="solar"), (ev.SolarStop(), ok("stop", "solar")), "none"),
        (PLAN_CHARGE, (ev.BalancingPause(), ok("stop", "balancing")), "none"),
        # Cleared: seen off with no start of ours on its way, at the report and in the pass that tells readers.
        (PLAN_CHARGE, (ev.ChargerReportedOff(),), "none"),
        (PLAN_CHARGE, (ev.ChargerReportedOff(notified=True),), "none"),
        (PLUGGED.with_changes(owner="charger_self"), (ev.ChargerReportedOff(),), "none"),
    ],
)
def test_where_the_owner_is_set_and_cleared(pre, events, owner) -> None:
    """The owner (the core end state plan, step 7): set only by a start's result that went out, a hand-over with no
    command, or a report of a charge nobody here started; cleared by a stop that went out or a report of the charger
    off with no start of ours on its way. Today's `charge_origin`/`plan_charge` are derived from it when the core
    drives (`controller._CORE_OWNER_ORIGIN`)."""
    session, _ = run(pre, *events)
    assert session.owner == owner


def test_a_top_off_is_the_plans_charge_past_the_last_window() -> None:
    """Owner `top_off` is the plan's charge with a top-off running: only the last window's end sets it, and only for
    the plan's own charge; today's code reads it as `charge_origin` `plan_window` with `top_off_until` set (and a
    plan present). A charge not the plan's gets no top-off."""
    for owner in ("person", "solar", "charge_now", "min_soc", "charger_self", "none"):
        session, commands = run(PLUGGED.with_changes(owner=owner), ev.FinalWindowEnd(top_off_wanted=True))
        assert session.owner != "top_off", owner
    from custom_components.spotnav.core.migration import legacy_owner

    unseen = {"charging": False, "start_pending": False, "stop_recent": False}
    assert legacy_owner(origin="plan_window", top_off=True, **unseen) == "top_off"
    assert legacy_owner(origin="plan_window", top_off=False, **unseen) == "plan"
    assert legacy_owner(origin="manual", top_off=True, **unseen) == "person"


@pytest.mark.parametrize("cause", [None, "other", "solar", "plan_window", "min_soc"])
def test_the_suns_take_over_never_stops_a_start_still_awaiting_its_result(cause) -> None:
    """A Charge-now start (any cause) is decided where it begins: while its command is on its way the charger may
    report charging (the shell's own `start_pending` then reads false), and the sun's take-over must not take that
    charge for one the charger began by itself."""
    session, commands = run(PLUGGED, ev.DirectStart(cause=cause))
    assert kinds(commands) == ["start"] and session.start_pending and session.owner == "none"
    session, commands = run(session, ev.SolarStop(take_over=True, charging=True))
    assert kinds(commands) == []
    session, _ = run(session, ok("start", "direct"))
    assert session.owner == run(PLUGGED, ev.DirectStart(cause=cause), ok("start", "direct"))[0].owner


@pytest.mark.parametrize(
    ("events", "owner", "paused"),
    [
        # Load balancing pauses the plan's charge, and its stop comes back before anything else is asked for.
        ((ev.BalancingPause(), ok("stop", "balancing")), "none", ("plan",)),
        # A person's Start asked for while the pause's stop is on its way: the stop comes back first, then the start.
        # The start ends what balancing held back, as `_start_locked` clears it when it goes out.
        ((ev.BalancingPause(), ev.PersonStart(), ok("stop", "balancing"), ok("start", "person")), "person", ()),
        # The same start that did not go out: nothing held back either (it was asked for after the pause).
        ((ev.BalancingPause(), ev.PersonStart(), ok("stop", "balancing"), failed("start", "person")), "none", ()),
        # A person's Start that went out before the pause: the pause holds the person's charge back.
        ((ev.PersonStart(), ok("start", "person"), ev.BalancingPause(), ok("stop", "balancing")), "none", ("person",)),
    ],
    ids=["pause", "start_after_the_pause", "start_after_the_pause_failed", "pause_after_the_start"],
)
def test_a_balancing_pause_meeting_a_persons_start_in_either_order(
    events: tuple[ev.Event, ...], owner: str, paused: tuple[str, ...]
) -> None:
    """The commands come back in the order they went out: a balancing pause holds back the charge that ran when it
    was asked for, unless a start asked for after it, still on its way, follows it."""
    session, _ = run(PLUGGED.with_changes(owner="plan"), *events)
    assert session.owner == owner
    assert (session.balancing_paused, session.paused_origin) == ((True, paused[0]) if paused else (False, None))


# ---------------------------------------------------------------------------------------------- the stored owner (step 8)


@pytest.mark.parametrize(
    ("pre", "sent", "owner"),
    [
        # Nothing on its way: the session's own owner.
        (PLAN_CHARGE, None, "plan"),
        (PLUGGED, None, "none"),
        # A start that went out, its result not decided yet: the owner it is for, as today's `_start_locked` writes
        # `charge_origin` before the command and saves it once the command went out.
        (run(PLUGGED, ev.PersonStart())[0], SENT_START, "person"),
        (run(PLUGGED, ev.DirectStart())[0], SENT_START, "charge_now"),
        (run(PLUGGED, ev.DirectStart(cause="solar"))[0], SENT_START, "solar"),
        (run(PLUGGED, ev.WindowStart())[0], SENT_START, "plan"),
        (run(PLUGGED, on(window_open=True))[0], SENT_START, "plan"),
        # A person's Start over the plan's charge: the person's from the moment it went out.
        (run(PLAN_CHARGE, ev.PersonStart())[0], SENT_START, "person"),
        # A start that names no owner (a balancing resume of nobody's charge), or none awaiting its result: as it was.
        (run(PLUGGED.with_changes(balancing_paused=True), ev.BalancingResume())[0], SENT_START, "none"),
        (PLAN_CHARGE, SENT_START, "plan"),
        # A stop that went out (or one of ours already on its way answered for it): nobody's, as today's stop paths
        # clear `charge_origin` before the stop's result is fed.
        (run(PLAN_CHARGE, ev.WindowEnd())[0], SENT_STOP, "none"),
        (PLUGGED.with_changes(owner="person"), SENT_STOP, "none"),
    ],
)
def test_the_owner_saved_while_a_command_of_ours_is_on_its_way(pre, sent, owner) -> None:
    """Step 8: the record is read back alone at a restart, so a save between a start or a stop going out and its
    result being decided (today's code saves there) writes the owner the command makes, in the record and in today's
    keys alike. A restart then keeps that owner (`Restart` keeps the owner), and the result, once decided, names the
    same one."""
    assert owner_as_sent(pre, sent) == owner
    restarted, _ = run(pre.stored().with_changes(owner=owner_as_sent(pre, sent)), ev.Restart())
    assert restarted.owner == owner and restarted.pending == ()
    if sent == SENT_START and pre.start_pending:
        reason = next(item.reason for item in pre.pending if item.command == "start")
        decided, _ = run(pre, ok("start", reason))
        assert decided.owner == owner


# ---------------------------------------------------------------------------------------------- a charge started by hand


@pytest.mark.parametrize("owner", ["person", "charge_now"])
@pytest.mark.parametrize(
    "event",
    [
        # A window's start by its timer, by the re-arm, at plug-in or at a start's retry, the charger off or on.
        ev.WindowStart(trigger=ev.TRIGGER_TIMER, connected=True),
        ev.WindowStart(trigger=ev.TRIGGER_TIMER, connected=True, control_on=True),
        ev.WindowStart(trigger=ev.TRIGGER_REARM, connected=True),
        ev.WindowStart(trigger=ev.TRIGGER_PLUG_IN, connected=True),
        ev.WindowStart(trigger=ev.TRIGGER_PLUG_IN, connected=True, control_on=True),
        ev.WindowStart(trigger=ev.TRIGGER_RETRY, connected=True),
        # A report of the charge in an open window (the claim), and the claim's task deciding again.
        on(window_open=True),
        ev.Recheck(what=ev.RECHECK_CLAIM, control_on=True, window_open=True, plan_present=True),
        # The stray charge's stop, outside every window, and the re-arm's stop, before the last window and past it.
        ev.Recheck(what=ev.RECHECK_STRAY, control_on=True, plan_present=True),
        ev.Rearm(control_on=True, charging=True),
        ev.Rearm(past_last=True, control_on=True, charging=True),
        # The window's end, also the last one's.
        ev.WindowEnd(),
        ev.FinalWindowEnd(top_off_wanted=True),
    ],
)
def test_the_plan_never_takes_over_or_stops_a_charge_started_by_hand(owner, event) -> None:
    """A person's or a Charge-now charge with no intent of the person's in the session: what a restart reads when
    Home Assistant died between the Start's own save and the settings store's pause (step 8). Nothing of the plan's
    takes it over as the plan's (its window's end would then stop it) or stops it: the owner is kept, nothing sent.
    Normally the person's intent holds these back already; with the option off today's code takes such a charge over
    at a window's start, so the core's verdict differs from today's there."""
    pre = PLUGGED.with_changes(owner=owner)
    session, commands = run(pre, event)
    assert kinds(commands) == [], (event, kinds(commands))
    assert session.owner == owner


@pytest.mark.parametrize("owner", ["none", "charger_self", "plan"])
def test_a_window_start_still_starts_what_is_not_a_charge_started_by_hand(owner) -> None:
    session, commands = run(PLUGGED.with_changes(owner=owner), ev.WindowStart(trigger=ev.TRIGGER_TIMER, connected=True))
    assert kinds(commands) == ["start"]
    session, _ = run(session, ok("start", "plan_window"))
    assert session.owner == "plan"
