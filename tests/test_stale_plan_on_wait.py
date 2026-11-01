"""A plan built from settings the person has since changed does not survive a new answer that says "wait".

The case from the field: a plan for Sunday 08:00 was installed for tonight, 02:30 to 08:00. The person moved the
departure to Monday 08:00 and lowered the need; the price history shows Monday night much cheaper, so the new answer
is to wait for Monday's prices (`waiting_for_history`), with nothing to install. The old plan stayed on the charger
and would have started at 02:30 against the new intent, the status saying the charge waits for that window.

Now: the plan built from the older settings is cleared, with its timers, and the status reads the wait. A charge
that one of its windows runs now is not cut: the plan is cleared at that window's boundary. A recalculation over
unchanged settings that answers "wait" (new prices, a new reading) keeps the plan: it is still what the person asked
for, bought on the prices known when it was made.

Real price repository, settings store, preview, execution boundary and charging controller; the wire, the clock and
the controller's window timers are the doubles. The charge-ownership core decides, and a test fails on any decision
it could not take (`conftest`).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

import pytest
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution.auto_execution import (
    EXECUTION_ACTIVE,
    EXECUTION_NOT_APPLIED,
    EXECUTION_SCHEDULED,
)

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


@pytest.mark.parametrize("two_windows", [True, False], ids=["a-window-left", "the-last-window"])
async def test_a_window_charging_now_is_not_cut_and_the_plan_is_cleared_at_its_boundary(
    session: Session, two_windows: bool
) -> None:
    _publish(session, two_windows=two_windows)
    await session.set_auto(departure=time(8, 0), requested_kwh=4.0 if two_windows else 10.0)
    plan = session.controller.plan
    assert plan is not None
    assert len(plan.windows) == (2 if two_windows else 1)
    # Tonight, its first window opens.
    session.clock.now = plan.windows[0][0]
    await _fire_due(session)
    assert session.executor.window_charging_now() is True
    assert len(session.on_calls) == 1, "the window opened and the charge started"
    session.hass.states.async_set(session.charge_control, "on")
    await session.hass.async_block_till_done()
    assert session.executor.execution_state() == EXECUTION_ACTIVE

    await _move_to_sunday(session)

    assert session.controller.plan is plan, "the window charging now is not cut"
    assert session.off_calls == []

    # A recalculation meanwhile (a price poll) changes nothing while the window runs.
    await session.tick()
    assert session.controller.plan is plan and session.off_calls == []

    # The window's boundary: its charge ends as planned, and the plan with it, its later windows never opening.
    first_end = plan.windows[0][1]
    session.clock.now = first_end
    await _fire_due(session)
    if session.off_calls:
        session.hass.states.async_set(session.charge_control, "off")
        await session.hass.async_block_till_done()

    assert len(session.off_calls) == 1, "the window ended as planned"
    assert session.controller.plan is None
    assert session.timers.pending == []
    assert session.executor.applied is None
    assert session.executor.execution_state() == EXECUTION_NOT_APPLIED

    # Later tonight nothing starts.
    session.clock.now = plan.windows[-1][1]
    await _fire_due(session)
    assert len(session.on_calls) == 1

