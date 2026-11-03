"""The charge-ownership core's guest car (`core/ownership.py`, `guest_met`): once a guest's amount is delivered its
charge is stopped and nothing automatic starts the charger until the unplug, a charge the charger begins by itself
is held, and a person's Start or Charge now still charges. Pure: no Home Assistant."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.spotnav.core import events as ev
from custom_components.spotnav.core.ownership import (
    automatic_allowed,
    decide,
    GATE_START,
    GATE_STOP,
    Keep,
    REASON_GUEST_MET,
    REASON_HOLD,
    REASON_PERSON,
    Start,
    Stop,
)
from custom_components.spotnav.core.session import ChargeSession, ManualPause

T0 = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)
PLUGGED = ChargeSession(plugged=True)
MET = PLUGGED.with_changes(guest_met=True)


def run(session: ChargeSession, *events: ev.Event):
    commands: tuple = ()
    for event in events:
        session, commands = decide(session, event, T0)
    return session, commands


def kinds(commands) -> list[str]:
    return [command.kind for command in commands if not isinstance(command, Keep)]


def ok(command: str, reason: str) -> ev.CommandResult:
    return ev.CommandResult(command=command, reason=reason, executed=True)


@pytest.mark.parametrize("owner", ["plan", "solar", "top_off", "charger_self", "min_soc"])
def test_the_guests_amount_delivered_stops_an_automatic_charge_and_clears_the_plan(owner: str) -> None:
    session, commands = run(PLUGGED.with_changes(owner=owner), ev.GuestMet(control_on=True))
    assert commands == (Stop(REASON_GUEST_MET, clear_schedule=True),)
    assert session.guest_met
    session, _ = run(session, ok("stop", REASON_GUEST_MET))
    assert session.owner == "none" and session.guest_met


@pytest.mark.parametrize("owner", ["person", "charge_now"])
def test_a_charge_a_person_started_goes_on_when_the_guests_amount_is_delivered(owner: str) -> None:
    session, commands = run(PLUGGED.with_changes(owner=owner), ev.GuestMet(control_on=True))
    assert kinds(commands) == []
    assert session.guest_met and session.owner == owner


def test_nothing_is_stopped_when_nothing_charges_or_a_start_is_on_its_way() -> None:
    assert kinds(run(PLUGGED.with_changes(owner="plan"), ev.GuestMet(control_on=False))[1]) == []
    assert kinds(run(PLUGGED.with_changes(owner="plan"), ev.GuestMet(control_on=True, start_pending=True))[1]) == []


def test_after_the_amount_no_window_sun_floor_or_claim_starts_the_charger() -> None:
    assert not automatic_allowed(MET, GATE_START)
    assert automatic_allowed(MET, GATE_STOP)
    assert kinds(run(MET, ev.WindowStart(trigger=ev.TRIGGER_TIMER, connected=True))[1]) == []
    assert kinds(run(MET, ev.WindowStart(trigger=ev.TRIGGER_PLUG_IN, connected=True))[1]) == []
    assert kinds(run(MET, ev.SolarStart())[1]) == []
    assert kinds(run(MET, ev.MinSocStart())[1]) == []
    # A charge the charger began by itself inside an open window is not claimed: it is held.
    session, commands = run(
        MET, ev.ChargerReportedOn(was_on=False, window_open=True, in_window=True, plan_present=True)
    )
    assert commands == (Stop(REASON_HOLD),)
    assert session.held


def test_a_charge_the_charger_begins_by_itself_is_held_whatever_else_owns_the_charger() -> None:
    session, commands = run(MET, ev.ChargerReportedOn(was_on=False, solar_holds=True))
    assert commands == (Stop(REASON_HOLD),)
    # Its re-check under the lock stops it as well.
    session, commands = run(session, ev.Recheck(what=ev.RECHECK_HOLD, control_on=True, owned=False))
    assert commands == (Stop(REASON_HOLD),)
    # Begun again by nobody here after the hold: a person overrode it at the charger, and it goes on.
    session, _ = run(session, ok("stop", REASON_HOLD), ev.ChargerReportedOff())
    session, commands = run(session, ev.ChargerReportedOn(was_on=False))
    assert kinds(commands) == [] and session.overridden


def test_a_persons_start_still_charges_and_is_never_held() -> None:
    session, commands = run(MET, ev.PersonStart())
    assert commands == (Start(REASON_PERSON),)
    session, _ = run(session, ok("start", REASON_PERSON))
    assert session.owner == "person" and session.guest_met
    session, commands = run(session, ev.ChargerReportedOn(was_on=False))
    assert kinds(commands) == []


def test_a_charge_now_start_still_charges() -> None:
    session, commands = run(MET, ev.DirectStart(cause="other"))
    assert kinds(commands) == ["start"]


def test_load_balancing_gives_back_only_a_charge_a_person_started() -> None:
    held = PLUGGED.with_changes(balancing_paused=True, paused_origin="plan")
    session, _ = run(held, ev.GuestMet(control_on=False))
    assert not session.balancing_paused, "the plan's charge it held back is not wanted any more"
    assert kinds(run(session.with_changes(balancing_paused=True, paused_origin="plan"), ev.BalancingResume())[1]) == []
    person = MET.with_changes(balancing_paused=True, paused_origin="person", manual=ManualPause("start", "plug_in"))
    assert kinds(run(person, ev.BalancingResume())[1]) == ["start"]


def test_the_unplug_and_a_new_plug_in_end_it_and_a_restart_keeps_it() -> None:
    assert not run(MET, ev.Unplug())[0].guest_met
    assert not run(MET.with_changes(plugged=False), ev.PlugIn(previous=False))[0].guest_met
    restarted, _ = run(MET, ev.Restart())
    assert restarted.guest_met
    assert run(restarted.with_changes(plugged=None), ev.PlugIn(previous=None))[0].guest_met, (
        "the first connection known after a restart is the same plug-in"
    )
    assert not run(MET, ev.GuestMet(met=False))[0].guest_met, "the guest plan ended: a household car"


def test_the_flag_is_stored_only_when_set_so_an_older_build_reads_the_record() -> None:
    assert "guest_met" not in PLUGGED.to_store()
    assert ChargeSession.from_store(PLUGGED.to_store()) == PLUGGED.stored()
    assert MET.to_store()["guest_met"] is True
    assert ChargeSession.from_store(MET.to_store()).guest_met
    assert ChargeSession.from_dict(MET.to_dict()) == MET
    old = {key: value for key, value in PLUGGED.to_dict().items() if key != "guest_met"}
    assert ChargeSession.from_dict(old) == PLUGGED, "a debug bundle from before the guest is still read"


def test_the_event_round_trips_for_replay() -> None:
    event = ev.GuestMet(met=True, control_on=True)
    assert ev.event_from_dict(event.to_dict()) == event
