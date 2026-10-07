"""The charge-ownership core's floor charge (`core/ownership.py`, `min_soc_start`/`min_soc_end`): a car below its
minimum charge level is charged at once as its own owner, a person's pause or Stop wins, and at the floor the
strategy takes the charge over. Pure: no Home Assistant."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.spotnav.core import events as ev
from custom_components.spotnav.core.ownership import (
    decide,
    Keep,
    ORIGIN_OWNER,
    REASON_MIN_SOC,
    Start,
    Stop,
    window_end_spared,
)
from custom_components.spotnav.core.session import (
    ChargeSession,
    ManualPause,
    OWNER_MIN_SOC,
    OWNERS,
    SPOTNAV_OWNERS,
)

T0 = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)
START = ManualPause("start", "plug_in")
STOP = ManualPause("stop", "plug_in")
PLUGGED = ChargeSession(plugged=True)


def run(session: ChargeSession, *events: ev.Event):
    commands: tuple = ()
    for event in events:
        session, commands = decide(session, event, T0)
    return session, commands


def kinds(commands) -> list[str]:
    return [command.kind for command in commands if not isinstance(command, Keep)]


def ok(command: str, reason: str) -> ev.CommandResult:
    return ev.CommandResult(command=command, reason=reason, executed=True)


def test_the_floor_is_an_owner_of_its_own() -> None:
    assert OWNER_MIN_SOC in OWNERS
    assert OWNER_MIN_SOC in SPOTNAV_OWNERS
    assert ORIGIN_OWNER["min_soc"] == OWNER_MIN_SOC
    assert ChargeSession.from_dict(ChargeSession(owner=OWNER_MIN_SOC).to_dict()).owner == OWNER_MIN_SOC
    assert ChargeSession.from_store(ChargeSession(owner=OWNER_MIN_SOC).to_store()).owner == OWNER_MIN_SOC


@pytest.mark.parametrize("owner", ["none", "charger_self", "plan", "solar", "charge_now", "top_off"])
def test_below_the_floor_the_charge_starts_at_once_as_the_floors(owner: str) -> None:
    session, commands = run(PLUGGED.with_changes(owner=owner), ev.MinSocStart())
    assert commands == (Start(REASON_MIN_SOC),)
    session, _ = run(session, ok("start", REASON_MIN_SOC))
    assert session.owner == OWNER_MIN_SOC
    assert session.pending == ()


def test_a_floor_charge_that_runs_is_not_started_again() -> None:
    session = PLUGGED.with_changes(owner=OWNER_MIN_SOC)
    assert kinds(run(session, ev.MinSocStart())[1]) == []


def test_no_car_starts_nothing() -> None:
    assert kinds(run(ChargeSession(plugged=False), ev.MinSocStart(connected=False))[1]) == []


@pytest.mark.parametrize(
    "session",
    [
        PLUGGED.with_changes(manual=STOP),
        PLUGGED.with_changes(span_pause="until_resumed"),
        PLUGGED.with_changes(span_pause="next_period"),
        PLUGGED.with_changes(manual=START, owner="person"),
    ],
)
def test_a_persons_pause_or_stop_wins_over_the_floor(session: ChargeSession) -> None:
    after, commands = run(session, ev.MinSocStart())
    assert kinds(commands) == []
    assert after == session


def test_a_persons_own_charge_stays_theirs() -> None:
    session = PLUGGED.with_changes(owner="person")
    assert kinds(run(session, ev.MinSocStart())[1]) == []


def test_a_floor_charge_load_balancing_holds_back_is_not_started_again() -> None:
    session = PLUGGED.with_changes(balancing_paused=True, paused_origin=OWNER_MIN_SOC)
    assert kinds(run(session, ev.MinSocStart())[1]) == []
    held = ev.CommandResult(command="start", reason=REASON_MIN_SOC, executed=False, balancing_held=True)
    after, _ = run(PLUGGED, ev.MinSocStart(), held)
    assert (after.balancing_paused, after.paused_origin, after.owner) == (True, OWNER_MIN_SOC, "none")


def test_load_balancing_gives_a_floor_charge_back_as_the_floors() -> None:
    session = PLUGGED.with_changes(balancing_paused=True, paused_origin=OWNER_MIN_SOC)
    session, commands = run(session, ev.BalancingResume())
    assert kinds(commands) == ["start"]
    session, _ = run(session, ok("start", "balancing_resume"))
    assert session.owner == OWNER_MIN_SOC


@pytest.mark.parametrize(("handed_to", "owner"), [("plan", "plan"), ("solar", "solar")])
def test_at_the_floor_the_strategy_takes_the_charge_over_with_no_command(handed_to: str, owner: str) -> None:
    session, commands = run(PLUGGED.with_changes(owner=OWNER_MIN_SOC), ev.MinSocEnd(handed_to=handed_to))
    assert kinds(commands) == []
    assert session.owner == owner


def test_at_the_floor_with_nothing_to_take_it_over_the_charge_stops_once() -> None:
    session, commands = run(PLUGGED.with_changes(owner=OWNER_MIN_SOC), ev.MinSocEnd())
    assert commands == (Stop(REASON_MIN_SOC),)
    session, _ = run(session, ok("stop", REASON_MIN_SOC))
    assert session.owner == "none"
    # Ended: no second stop, and nothing starts it again from the end itself.
    assert kinds(run(session, ev.MinSocEnd())[1]) == []


def test_a_pause_chosen_while_the_floor_charges_stops_it() -> None:
    session = PLUGGED.with_changes(owner=OWNER_MIN_SOC, span_pause="until_resumed")
    assert run(session, ev.MinSocEnd())[1] == (Stop(REASON_MIN_SOC),)


@pytest.mark.parametrize("owner", ["none", "plan", "solar", "person", "charger_self"])
def test_the_floors_end_touches_no_charge_that_is_not_the_floors(owner: str) -> None:
    session = PLUGGED.with_changes(owner=owner)
    after, commands = run(session, ev.MinSocEnd())
    assert kinds(commands) == []
    assert after == session


def test_no_flap_at_the_floor_the_strategy_charging_anyway_just_continues() -> None:
    session, _ = run(PLUGGED, ev.MinSocStart(), ok("start", REASON_MIN_SOC))
    session, commands = run(session, ev.MinSocEnd(handed_to="plan"))
    assert kinds(commands) == []
    # The plan's charge from here: its window's end stops it as any plan charge.
    assert kinds(run(session, ev.WindowEnd())[1]) == ["stop"]


def test_a_window_end_and_a_rearm_spare_the_floor_charge() -> None:
    session = PLUGGED.with_changes(owner=OWNER_MIN_SOC)
    assert window_end_spared(session)
    assert kinds(run(session, ev.WindowEnd())[1]) == []
    assert kinds(run(session, ev.FinalWindowEnd())[1]) == []
    assert kinds(run(session, ev.Rearm(charging=True))[1]) == []


def test_the_sun_does_not_stop_the_floor_charge() -> None:
    session = PLUGGED.with_changes(owner=OWNER_MIN_SOC)
    assert kinds(run(session, ev.SolarStop())[1]) == []
    assert kinds(run(session, ev.SolarStop(take_over=True, charging=True))[1]) == []


def test_a_strategy_change_to_the_sun_keeps_the_floor_charge_its_own() -> None:
    session = PLUGGED.with_changes(owner=OWNER_MIN_SOC)
    for sun_keeps in (False, True):
        after, commands = run(session, ev.StrategyChange(strategy="solar", plan_applied=True, sun_keeps=sun_keeps))
        assert kinds(commands) == []
        assert after.owner == OWNER_MIN_SOC


def test_the_hold_and_the_stray_stop_leave_the_floor_charge_alone() -> None:
    session = PLUGGED.with_changes(owner=OWNER_MIN_SOC)
    report = ev.ChargerReportedOn(was_on=False, window_ahead_outside=True, plan_present=True)
    assert kinds(run(session, report)[1]) == []


def test_a_persons_stop_ends_the_floor_charge_and_holds_it_off() -> None:
    session, commands = run(PLUGGED.with_changes(owner=OWNER_MIN_SOC), ev.PersonStop())
    assert kinds(commands) == ["stop"]
    session, _ = run(session, ok("stop", "person"))
    assert session.owner == "none"
    assert kinds(run(session, ev.MinSocStart())[1]) == []


def test_a_persons_start_takes_the_floor_charge_over() -> None:
    session, _ = run(PLUGGED.with_changes(owner=OWNER_MIN_SOC), ev.PersonStart(), ok("start", "person"))
    assert (session.owner, session.manual) == ("person", START)
    assert kinds(run(session, ev.MinSocEnd())[1]) == []


def test_a_plan_window_opening_takes_the_charge_and_the_floor_claims_it_back() -> None:
    session, _ = run(PLUGGED.with_changes(owner=OWNER_MIN_SOC), ev.WindowStart(), ok("start", "plan_window"))
    assert session.owner == "plan"
    session, commands = run(session, ev.MinSocStart())
    assert commands == (Start(REASON_MIN_SOC),)


def test_the_floor_events_round_trip() -> None:
    for event in (ev.MinSocStart(connected=None), ev.MinSocEnd(handed_to="solar")):
        assert ev.event_from_dict(event.to_dict()) == event
