"""A plan built from settings the person has since changed does not survive a new answer that says "wait".

The case from the field: a plan for Sunday 08:00 was installed for tonight, 02:30 to 08:00. The person moved the
departure to Monday 08:00 and lowered the need; the price history shows Monday night much cheaper, so the new answer
is to wait for Monday's prices (`waiting_for_history`), with nothing to install. The old plan stayed on the charger
and would have started at 02:30 against the new intent, the status saying the charge waits for that window.

Now: the plan built from the older settings is cleared, with its timers, and the status reads the wait. The plan's
own charge that one of its windows runs now is stopped at once (2.5.0; before, it ran to its window's end), with no
notification; a charge the plan does not own (a person's Start, the car's minimum level, the sun's) goes on. Nothing of
a superseded plan starts: no window, plug-in or load balancing's resume. A recalculation over unchanged settings that answers
"wait" (new prices, a new reading) keeps the plan: it is still what the person asked for, bought on the prices known
when it was made.

Real price repository, settings store, preview, execution boundary and charging controller; the wire, the clock and
the controller's window timers are the doubles. The charge-ownership core decides, and a test fails on any decision
it could not take (`conftest`).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution.auto_execution import (
    EXECUTION_ACTIVE,
    EXECUTION_NOT_APPLIED,
    EXECUTION_SCHEDULED,
)
from custom_components.spotnav.planning.auto_settings import STRATEGY_HYBRID

from .harness import Session
from .relay import AREA_TZ, SE4, Clock, day_body, hold_history, index_listing, serve

pytestmark = pytest.mark.usefixtures("offline_relay")

#: Thursday 2026-10-01, 20:00 in Stockholm (CEST, UTC+2): today's and Friday's prices are published.
THURSDAY = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
TODAY_DATE = date(2026, 10, 1)
FRIDAY = date(2026, 10, 2)
SUNDAY = date(2026, 10, 4)
#: Sunday's night hours, in the profile's local weekday-hour: much cheaper than anything published.
SUNDAY_NIGHT = {(7, hour): 0.03 for hour in range(8)}


@pytest.fixture(autouse=True)
def thursday_evening(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness clock on Thursday evening, and Home Assistant's own clock following it (the controller's plan
    rules and window checks read `dt_util.utcnow`)."""
    clock.now = THURSDAY
    monkeypatch.setattr(dt_util, "utcnow", lambda *_args, **_kwargs: clock())


def _two_dips(when: date) -> str:
    """A day expensive everywhere but two cheap hours, 00:00 and 03:00 local, apart from each other."""
    document = json.loads(day_body(SE4, when))
    document["tz"] = AREA_TZ[SE4]
    prices = [0.50] * len(document["prices"])
    for hour in (0, 3):
        prices[hour * 4 : (hour + 1) * 4] = [0.01] * 4
    document["prices"] = prices
    return json.dumps(document)


def _publish(session: Session, *, two_windows: bool = False) -> None:
    """Today and Friday published, and four weeks of history in which Sunday night is cheap. `two_windows`: tonight's
    plan has two windows, apart."""
    transport = session.transport
    serve(transport, days=(TODAY_DATE, FRIDAY))
    if two_windows:
        transport.serve(transport.day_path(SE4, TODAY_DATE), 200, _two_dips(TODAY_DATE))
        transport.serve(transport.day_path(SE4, FRIDAY), 200, _two_dips(FRIDAY))
    transport.serve("/v1/index.json", 200, index_listing(SE4, [TODAY_DATE.isoformat(), FRIDAY.isoformat()]))
    hold_history(session.repository, to=TODAY_DATE.isoformat(), cheap=SUNDAY_NIGHT)


async def _fire_due(session: Session) -> None:
    """Fire the controller's window timers that are due at the clock's instant, as Home Assistant would."""
    for timer in list(session.timers.pending):
        if timer.when <= session.clock():
            timer.fire()
    await session.hass.async_block_till_done()


async def _move_to_sunday(session: Session):
    """The person's change: departure on a date (Sunday 08:00) and a smaller need. The answer is to wait."""
    snapshot = await session.set_auto(departure=time(8, 0), departure_date=SUNDAY, requested_kwh=5.0)
    assert (snapshot.state, snapshot.reason) == ("waiting_for_publication", "waiting_for_history")
    assert snapshot.history is not None and snapshot.history.outcome == "wait"
    return snapshot


async def test_a_settings_change_answered_with_a_wait_clears_the_plan_built_from_the_old_settings(
    session: Session,
) -> None:
    _publish(session)
    await session.set_auto(departure=time(8, 0), requested_kwh=10.0)
    plan = session.controller.plan
    assert plan is not None and plan.auto_owned
    assert plan.auto_settings_revision == session.settings().revision
    first_start = plan.windows[0][0]
    assert first_start > session.clock(), "the plan waits for tonight's cheap hours"
    assert session.timers.pending, "its window timers are armed"
    assert session.executor.execution_state() == EXECUTION_SCHEDULED

    snapshot = await _move_to_sunday(session)

    assert session.controller.plan is None, "the plan built from the old settings is gone"
    assert session.timers.pending == [], "and so are its timers"
    assert session.executor.applied is None and session.executor.pending is None
    assert session.executor.execution_state() == EXECUTION_NOT_APPLIED
    assert session.controller.hold_until is None, "nothing is held for a window that will not come"
    current = session.preview.snapshot()
    assert (current.state, current.reason) == ("waiting_for_publication", "waiting_for_history")
    assert current.applied is False and current.execution == EXECUTION_NOT_APPLIED
    assert snapshot.proposal is None or not snapshot.proposal.has_plan

    # Tonight, at the old plan's start: nothing starts, and a recalculation still waits.
    session.clock.now = first_start + timedelta(minutes=1)
    await _fire_due(session)
    await session.tick()
    assert session.on_calls == [] and session.off_calls == []
    assert session.controller.plan is None


async def test_an_executable_answer_to_a_settings_change_still_replaces_the_plan(session: Session) -> None:
    """The other side of the rule: a change with a plan to install replaces the old one at once, as before."""
    _publish(session)
    await session.set_auto(departure=time(8, 0), requested_kwh=10.0)
    old = session.executor.applied
    assert old is not None

    await session.set_auto(departure=time(8, 0), requested_kwh=5.0)

    new = session.executor.applied
    assert new is not None and new.identity != old.identity
    assert new.settings_revision == session.settings().revision
    assert session.controller.plan is new.plan


async def test_a_wait_over_unchanged_settings_keeps_the_plan(session: Session) -> None:
    """A recalculation of the same settings that answers "wait" does not touch the plan. Also when the settings were
    written since, but a calculation of the written settings proposed the same plan again."""
    # A wait answer, from a second charger on the same prices whose departure is on Sunday.
    _publish(session)
    other = Session(
        session.hass, session.transport, session.clock, session.timers, entry_id="entry-b", charge_control="switch.b"
    )
    await other.start()
    waiting = await _move_to_sunday(other)

    await session.set_auto(departure=time(8, 0), requested_kwh=10.0)
    installed = session.executor.applied
    assert installed is not None

    session.executor.begin_attempt()
    await session.executor.async_reconcile(session.settings(), waiting, attempt=session.executor.attempt)
    assert session.controller.plan is installed.plan, "the same settings: the plan stands"

    # The same values written again: a new revision whose calculation proposes the same plan, so nothing installs.
    await session.set_auto(departure=time(8, 0), requested_kwh=10.0)
    assert session.settings().revision > installed.settings_revision
    assert session.controller.plan is installed.plan

    session.executor.begin_attempt()
    await session.executor.async_reconcile(session.settings(), waiting, attempt=session.executor.attempt)
    assert session.controller.plan is installed.plan, "the settings it stands for still want it"
    assert session.timers.pending


async def test_an_answer_that_says_nothing_about_the_settings_keeps_the_plan(session: Session) -> None:
    """Prices not in yet (after a start, say) are no answer for the settings: a plan from older settings stays, as a
    stale or unavailable price stream or incomplete settings leave it."""
    _publish(session)
    other = Session(
        session.hass, session.transport, session.clock, session.timers, entry_id="entry-b", charge_control="switch.b"
    )
    await other.start()
    path = session.transport.day_path(SE4, TODAY_DATE)
    session.transport.hold(path)
    await other.set_auto(departure=time(9, 0))
    not_in_yet = other.preview.snapshot()
    assert not_in_yet.state == "waiting_for_prices"
    session.transport.release(path)
    await session.hass.async_block_till_done()

    await session.set_auto(departure=time(8, 0), requested_kwh=10.0)
    installed = session.executor.applied
    assert installed is not None
    await session.store.async_update(session.entry_id, mutate=lambda settings: settings)
    assert session.settings().revision > installed.settings_revision

    session.executor.begin_attempt()
    await session.executor.async_reconcile(session.settings(), not_in_yet, attempt=session.executor.attempt)
    assert session.controller.plan is installed.plan


def _expects_nothing(session: Session) -> None:
    """Nothing is told of the plan's charge: no window of a plan is expected to charge (`plan_expects_charge`, the one
    judgement behind `plan_stopped`), and the core asked for no notice."""
    assert session.controller.plan_expects_charge is False
    notices = [
        command
        for record in session.controller.session_runner.events
        for command in record.get("core", ())
        if command.get("kind") == "notify"
    ]
    assert notices == []


async def _charging_window(session: Session, *, two_windows: bool = False):
    """A plan installed for tonight, and its first window open and charging."""
    _publish(session, two_windows=two_windows)
    await session.set_auto(departure=time(8, 0), requested_kwh=4.0 if two_windows else 10.0)
    plan = session.controller.plan
    assert plan is not None
    assert len(plan.windows) == (2 if two_windows else 1)
    session.clock.now = plan.windows[0][0]
    await _fire_due(session)
    assert len(session.on_calls) == 1, "the window opened and the charge started"
    session.hass.states.async_set(session.charge_control, "on")
    await session.hass.async_block_till_done()
    assert session.executor.execution_state() == EXECUTION_ACTIVE
    assert session.controller.charge_origin == "plan_window"
    return plan


async def _report_off(session: Session) -> None:
    """The charger reports the stop it was sent."""
    session.hass.states.async_set(session.charge_control, "off")
    await session.hass.async_block_till_done()


@pytest.mark.parametrize("two_windows", [True, False], ids=["a-window-left", "the-last-window"])
async def test_a_window_charging_now_is_stopped_and_the_plan_cleared_at_once(
    session: Session, two_windows: bool
) -> None:
    """The person moved the departure while a window of the plan charges, and the answer for the new settings is to
    wait: the plan's charge stops at once and the plan is cleared (before 2.5.0 the window ran to its end)."""
    plan = await _charging_window(session, two_windows=two_windows)

    await _move_to_sunday(session)

    assert len(session.off_calls) == 1, "the plan's charge is stopped at once"
    await _report_off(session)
    assert session.controller.plan is None
    assert session.timers.pending == []
    assert session.executor.applied is None and session.executor.pending is None
    assert session.executor.execution_state() == EXECUTION_NOT_APPLIED
    current = session.preview.snapshot()
    assert current.applied is False and current.applied_identity is None
    _expects_nothing(session)

    # The rest of the night: nothing starts, the old window's end stops nothing more.
    for when in (plan.windows[0][1], plan.windows[-1][0], plan.windows[-1][1]):
        session.clock.now = when
        await _fire_due(session)
        await session.tick()
    assert len(session.on_calls) == 1 and len(session.off_calls) == 1
    _expects_nothing(session)


async def test_an_open_window_with_the_charger_off_is_cleared_at_once_and_starts_nothing(session: Session) -> None:
    """An open window whose charge is not running (the charger switched off by itself) keeps no superseded plan: it is
    cleared at once, with nothing to stop, and nothing of it starts later."""
    plan = await _charging_window(session)
    await _report_off(session)
    assert not session.controller.charging

    await _move_to_sunday(session)

    assert session.controller.plan is None and session.timers.pending == []
    assert len(session.off_calls) == 0, "nothing ran, nothing is stopped"
    _expects_nothing(session)
    session.clock.now = plan.windows[0][0] + timedelta(minutes=30)
    await _fire_due(session)
    await session.tick()
    assert len(session.on_calls) == 1


async def test_a_balancing_pause_in_the_window_is_cleared_at_once_and_its_resume_starts_nothing(
    session: Session,
) -> None:
    """Load balancing paused the plan's charge in its open window: the superseded plan is cleared at once, and the
    regulator's resume gives nothing back."""
    await _charging_window(session)
    controller = session.controller
    await controller.async_apply_regulated_current(0, must_lower=True)
    await _report_off(session)
    assert controller.balancing_holds("plan_window")

    await _move_to_sunday(session)

    assert controller.plan is None and session.timers.pending == []
    assert not controller.balancing_holds("plan_window"), "the plan's charge is not wanted any more"
    assert await controller.async_battery_probe_start(10) is False
    await session.hass.async_block_till_done()
    assert len(session.on_calls) == 1
    _expects_nothing(session)


async def test_a_superseded_plan_starts_nothing_while_it_waits_to_be_cleared(session: Session) -> None:
    """Between the wait's answer and the plan's clear nothing of the plan may start: the boundary's gate refuses its
    window start, plug-in start, claim and load balancing's resume of its charge; a stop is still allowed."""
    from custom_components.spotnav.execution.controller import (
        AUTOMATIC_BALANCING_RESUME,
        AUTOMATIC_START,
        AUTOMATIC_STOP,
    )

    _publish(session)
    await session.set_auto(departure=time(8, 0), requested_kwh=10.0)
    installed = session.executor.applied
    assert installed is not None
    other = Session(
        session.hass, session.transport, session.clock, session.timers, entry_id="entry-b", charge_control="switch.b"
    )
    await other.start()
    waiting = await _move_to_sunday(other)
    assert session.executor.automatic_allowed(AUTOMATIC_START)

    # The settings change is stored and the preview's answer for them is to wait; the reconcile has not run yet.
    await session.store.async_update(session.entry_id, mutate=lambda settings: replace(settings, requested_kwh=5.0))
    session.executor._live_snapshot = lambda: replace(  # noqa: SLF001 - the answer for the stored settings
        waiting, settings_revision=session.settings().revision
    )
    assert session.controller.plan is installed.plan
    assert session.executor.superseded_by_wait()
    assert not session.executor.automatic_allowed(AUTOMATIC_START)
    assert session.executor.automatic_allowed(AUTOMATIC_STOP)
    assert session.executor.automatic_allowed(AUTOMATIC_BALANCING_RESUME), "no charge of the plan is held back"

    # Its window opens: nothing is started, and nothing is expected of it.
    session.clock.now = installed.plan.windows[0][0]
    await _fire_due(session)
    assert session.on_calls == []
    assert await session.controller.async_start_on_plug_in() is False
    assert session.on_calls == []
    assert session.controller.plan_expects_charge is False


async def test_a_persons_start_in_the_window_is_not_stopped_by_the_change(session: Session) -> None:
    await _charging_window(session)
    await session.executor.async_manual_start()
    await session.hass.async_block_till_done()
    assert session.controller.charge_origin == "manual"

    await session.set_auto(departure=time(8, 0), departure_date=SUNDAY, requested_kwh=5.0)

    assert session.off_calls == [], "a person's Start goes on"
    assert session.controller.charging and session.controller.charge_origin == "manual"


async def test_the_floors_charge_in_the_window_is_not_stopped_and_the_plan_is_cleared(session: Session) -> None:
    await _charging_window(session)
    await session.executor.async_min_soc_start(None)
    await session.hass.async_block_till_done()
    assert session.controller.charge_origin == "min_soc", "the car's minimum level took the charge over"

    await _move_to_sunday(session)

    assert session.off_calls == [], "the car's minimum level is not the plan's to stop"
    assert session.controller.charging and session.controller.charge_origin == "min_soc"
    assert session.controller.plan is None, "the plan is cleared all the same"


async def test_a_sun_charge_is_not_stopped_and_the_plan_is_cleared(session: Session) -> None:
    """Hybrid: the sun charges before the plan's window tonight; the settings change answered by a wait clears the plan
    and leaves the sun's charge alone."""
    _publish(session)
    await session.set_auto(departure=time(8, 0), requested_kwh=10.0)
    installed = session.executor.applied
    assert installed is not None and installed.plan.windows[0][0] > session.clock()
    other = Session(
        session.hass, session.transport, session.clock, session.timers, entry_id="entry-b", charge_control="switch.b"
    )
    await other.start()
    waiting = await _move_to_sunday(other)
    await session.store.async_update(
        session.entry_id, mutate=lambda settings: replace(settings, strategy=STRATEGY_HYBRID, requested_kwh=5.0)
    )
    assert await session.executor.async_solar_start(10)
    session.hass.states.async_set(session.charge_control, "on")
    await session.hass.async_block_till_done()
    assert session.controller.charge_origin == "solar"
    offs = len(session.off_calls)

    session.executor.begin_attempt()
    await session.executor.async_reconcile(session.settings(), waiting, attempt=session.executor.attempt)

    assert session.controller.plan is None, "the superseded plan is cleared"
    assert len(session.off_calls) == offs, "the sun's charge is not the plan's to stop"
    assert session.controller.charging and session.controller.charge_origin == "solar"
