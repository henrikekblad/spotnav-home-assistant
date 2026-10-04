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
    decide,
    GATE_BALANCING_RESUME,
    GATE_PERSON_RESUME,
    GATE_START,
    GATE_STOP,
    Keep,
    Notify,
    NOTIFY_CHARGER_IGNORES_STOP,
    PERSON_HOLD_MAX_STOPS,
    PERSON_HOLD_STOP_GAP_S,
    PERSON_HOLD_WINDOW_S,
    SAFETY_RESUME_GAP_S,
    Start,
    Stop,
)
from custom_components.spotnav.core.session import (
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
    session, _ = run(ChargeSession(car_ended_at=T0), ev.PersonStart())
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
        car_ended_at=T0,
        hold_stop_times=(T0,),
        hold_gave_up=True,
    )
    session, _ = run(before, ev.Unplug(previous=True))
    assert session == ChargeSession(plugged=False)


def test_the_first_connection_after_a_restart_keeps_what_the_plug_in_had() -> None:
    before = ChargeSession(car_ended_at=T0, hold_stop_times=(T0,))
    session, _ = run(before, ev.PlugIn(previous=None))
    assert session.car_ended_at == T0 and session.hold_stop_times == (T0,)


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


# ---------------------------------------------------------------------------------------------- C7 and R5


def on(**facts) -> ev.ChargerReportedOn:
    return ev.ChargerReportedOn(**{"charging": True, "was_on": True, **facts})


def test_under_a_person_stop_a_charge_the_charger_begins_is_stopped_at_once() -> None:
    """C7: whatever the plan's windows say."""
    session, commands = run(ChargeSession(manual=STOP, plugged=True), on(window_open=True, in_window=True))
    assert commands == (Stop("person_hold"),)
    assert session.owner == "charger_self" and session.hold_stop_pending and session.hold_stop_times == (T0,)


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


def test_the_stops_under_a_person_stop_give_up_after_three_in_ten_minutes() -> None:
    """R5 cycling: every stop counts, taken or not; then the give-up and its notification."""
    session = ChargeSession(manual=STOP, plugged=True)
    sent = 0
    now = T0
    for _ in range(PERSON_HOLD_MAX_STOPS):
        session, commands = run(session, on(), now=now)
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
    assert commands == (Stop("person_hold"),) and session.hold_stop_times == (T0,)


def test_the_stops_start_afresh_once_no_person_stop_holds() -> None:
    session, _ = run(ChargeSession(hold_stop_times=(T0,), hold_gave_up=True), on())
    assert session.hold_stop_times == () and not session.hold_gave_up


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
    session = ChargeSession(car_ended_at=T0)
    assert (
        car_ended_holds_window(session, open_window_start=window_start, known_full=known_full, need_grew=need_grew)
        is holds
    )
    _, commands = run(
        session,
        ev.WindowStart(open_window_start=window_start, car_ended_known_full=known_full, need_grew=need_grew),
        now=T0 + timedelta(hours=2),
    )
    assert kinds(commands) == ([] if holds else ["start"])


def test_no_car_ended_record_holds_nothing() -> None:
    assert not car_ended_holds_window(ChargeSession(), open_window_start=T0, known_full=True, need_grew=False)


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


def test_a_plug_in_inside_a_window_claims_a_charge_the_charger_began() -> None:
    session, commands = run(ChargeSession(owner="charger_self"), ev.WindowStart(trigger="plug_in", control_on=True))
    assert commands == (Start("claim"),)
    assert run(session, ok("start", "claim"))[0].owner == "plan"
    _, commands = run(ChargeSession(owner="solar"), ev.WindowStart(trigger="plug_in", control_on=True))
    assert kinds(commands) == []


def test_a_balancing_pause_keeps_a_plug_in_from_starting() -> None:
    _, commands = run(ChargeSession(balancing_paused=True), ev.WindowStart(trigger="plug_in"))
    assert kinds(commands) == []


@pytest.mark.parametrize("owner", ["plan", "solar", "charge_now", "charger_self"])
def test_a_window_end_stops_the_charge_whoever_owns_it(owner: str) -> None:
    """Today's rule (finding I3, kept in step 1); the hybrid hand-off and a person's Start are spared."""
    _, commands = run(ChargeSession(owner=owner), ev.WindowEnd())
    assert commands == (Stop("window_end", False),)
    _, commands = run(ChargeSession(owner=owner), ev.WindowEnd(handed_off=True))
    assert kinds(commands) == []
    _, commands = run(ChargeSession(owner="person", manual=START), ev.WindowEnd())
    assert kinds(commands) == []


def test_the_last_window_end_ends_the_plan_or_lets_the_car_top_off() -> None:
    _, commands = run(ChargeSession(owner="plan"), ev.FinalWindowEnd())
    assert commands == (Stop("final_window_end", True),)
    session, commands = run(ChargeSession(owner="plan"), ev.FinalWindowEnd(top_off_wanted=True))
    assert session.owner == "top_off" and kinds(commands) == []


# ---------------------------------------------------------------------------------------------- the re-arm and the hold


@pytest.mark.parametrize(
    ("session", "spared"),
    [
        (ChargeSession(owner="plan"), False),
        (ChargeSession(owner="charge_now"), False),
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


def test_a_charge_inside_a_window_nobody_started_is_the_plans() -> None:
    _, commands = run(PLUGGED, on(window_open=True, in_window=True))
    assert commands == (Start("claim"),)
    _, commands = run(ChargeSession(car_ended_at=T0), on(window_open=True, open_window_start=T0 - timedelta(minutes=5)))
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
    session, _ = run(ChargeSession(owner="person"), ev.ChargerReportedOff())
    assert session.owner == "person", "a report alone ends only the charger's own charge"
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
    assert session.owner == "charge_now", "nobody's: today's start with no cause"
    session, _ = run(
        ChargeSession(balancing_paused=True, paused_origin="solar"),
        ev.BalancingResume(),
        failed("start", "balancing_resume"),
    )
    assert session.balancing_paused and session.paused_origin == "solar" and session.owner == "none"


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
        car_ended_at=T0,
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
        owner="person", manual=START, held=True, car_ended_at=T0, balancing_paused=True, paused_origin="person"
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
        car_ended_at=T0,
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
        ChargeSession.from_dict({**raw, "version": 2})
    with pytest.raises(SessionError):
        ChargeSession.from_dict({**raw, "owner": "nobody"})
    with pytest.raises(SessionError):
        ChargeSession.from_dict({**raw, "car_ended_at": "2026-10-04T22:00:00"})
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
    assert SAFETY_RESUME_GAP_S == controller.SAFETY_RESUME_GAP_S
    assert {item.name for item in dataclass_fields(ChargeSession)} >= {"owner", "manual", "span_pause"}
