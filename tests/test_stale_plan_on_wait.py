"""A plan built from settings the person has since changed does not survive a new answer that says "wait".

The case from the field: a plan for Sunday 08:00 was installed for tonight, 02:30 to 08:00. The person moved the
departure to Monday 08:00 and lowered the need; the price history shows Monday night much cheaper, so the new answer
is to wait for Monday's prices (`waiting_for_history`), with nothing to install. The old plan stayed on the charger
and would have started at 02:30 against the new intent, the status saying the charge waits for that window.

Now: the plan built from the older settings is cleared, with its timers, and the status reads the wait. The plan's
own charge that one of its windows runs now is stopped at once (2.5.0; before, it ran to its window's end), with no
notification; a charge the plan does not own (a person's Start, the car's minimum level, the sun's) goes on. Nothing of
a superseded plan starts: no window, plug-in or load balancing's resume. A restart decides the restored plan against
the answer for the stored settings before anything of it starts. A recalculation over unchanged settings that answers
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
from typing import Any

from homeassistant.util import dt as dt_util

from custom_components.spotnav import async_restore_execution
from custom_components.spotnav.execution.auto_execution import (
    AutoExecutor,
    EXECUTION_ACTIVE,
    EXECUTION_NOT_APPLIED,
    EXECUTION_SCHEDULED,
)
from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CURRENT_LIMIT, CONF_WEBHOOK_ID
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.planning.auto_settings import STRATEGY_HYBRID

from .harness import Session, start_preview
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
    waiting = await _waiting_answer(session)

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


async def _waiting_answer(session: Session):
    """A wait answer, from a second charger on the same prices whose departure is on Sunday. Its start registers the
    switch services again, so this charger's recorded calls are read from the new recordings from here on."""
    other = Session(
        session.hass, session.transport, session.clock, session.timers, entry_id="entry-b", charge_control="switch.b"
    )
    await other.start()
    waiting = await _move_to_sunday(other)
    assert other.on_calls == [] and other.off_calls == []
    session.on_calls, session.off_calls = other.on_calls, other.off_calls
    return waiting


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
    waiting = await _waiting_answer(session)
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
    waiting = await _waiting_answer(session)
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


# ------------------------------------------------------------------------------------------------- a restart


@pytest.fixture(params=["unload", "ha_stop"])
def both_restarts(request: pytest.FixtureRequest) -> str:
    """A restart after the entry was unloaded, and Home Assistant's own stop, which unloads nothing (as
    `conftest.both_restarts` runs `pause_world`'s)."""
    return request.param


async def _restart(session: Session, kind: str, *, meanwhile: Any = None) -> Session:
    """Restart the charger's stack in `async_setup_entry`'s order: the boundary, the preview and its first calculation,
    then the controller's restore (`async_restore_execution`). `meanwhile` runs while it is down."""
    hass = session.hass
    await session.preview.async_shutdown()
    await session.executor.async_shutdown()
    controller = session.controller
    if kind == "unload":
        await controller.async_shutdown()
    else:
        await controller.async_flush_session()
        await hass.async_block_till_done()
        disk = await controller._store.async_load()  # noqa: SLF001
        controller._cancel_session_save()  # noqa: SLF001
        await controller.async_shutdown()
        await controller._store.async_save(dict(disk) if disk else {})  # noqa: SLF001
    if meanwhile is not None:
        await meanwhile()
    restarted = Session(
        hass, session.transport, session.clock, session.timers, entry_id=session.entry_id,
        charge_control=session.charge_control,
    )
    restarted.repository, restarted.manager, restarted.store = session.repository, session.manager, session.store
    restarted.scheduler = session.scheduler
    restarted.on_calls, restarted.off_calls = session.on_calls, session.off_calls
    restarted.controller = ChargingController(
        hass,
        session.entry_id,
        {CONF_CHARGE_CONTROL: session.charge_control, CONF_CURRENT_LIMIT: "", CONF_WEBHOOK_ID: "webhook-a"},
    )
    restarted.executor = AutoExecutor(hass, restarted.controller, session.store, now=session.clock)
    await restarted.executor.async_start()
    restarted.preview = await start_preview(
        hass, session.entry_id, session.store, session.manager, executor=restarted.executor, now=session.clock
    )
    restarted.executor.attach_preview(restarted.preview)
    await async_restore_execution(restarted.controller, restarted.executor, restarted.preview)
    await hass.async_block_till_done()
    return restarted


def _wait_settings(settings: Any) -> Any:
    """`_move_to_sunday`'s change, written while Home Assistant is down (or just before it stops)."""
    return replace(settings, departure_enabled=True, departure=time(8, 0), departure_date=SUNDAY, requested_kwh=5.0)


@pytest.mark.usefixtures("both_restarts")
@pytest.mark.parametrize("charger", ["unavailable", "off"], ids=["not-ready", "ready"])
async def test_a_restart_in_the_window_with_the_charge_not_running_starts_nothing_and_clears_the_plan(
    session: Session, both_restarts: str, charger: str
) -> None:
    """The field case of 2026-11-08: the departure moved, Home Assistant restarted inside the old plan's window with the
    charger's integration not loaded yet, and the charger then became ready. The old plan's start went out. Now the
    restored plan is decided against the wait before anything starts it: no start (also not the restore's own re-arm,
    with the charger ready at once), the plan cleared, nothing told."""
    _publish(session, two_windows=True)
    await session.set_auto(departure=time(8, 0), requested_kwh=4.0)
    plan = session.controller.plan
    assert plan is not None and len(plan.windows) == 2
    session.clock.now = plan.windows[0][0] - timedelta(minutes=1)

    async def down() -> None:
        await session.store.async_update(session.entry_id, mutate=_wait_settings)
        session.clock.now = plan.windows[0][0] + timedelta(minutes=10)
        session.hass.states.async_set(session.charge_control, charger)

    restarted = await _restart(session, both_restarts, meanwhile=down)

    assert restarted.on_calls == []
    assert restarted.controller.plan is None, "the restored plan is cleared"
    assert restarted.timers.pending == []
    snapshot = restarted.preview.snapshot()
    assert (snapshot.state, snapshot.reason) == ("waiting_for_publication", "waiting_for_history")
    assert snapshot.applied is False and snapshot.applied_identity is None
    _expects_nothing(restarted)

    # The charger's integration loads, and later the old plan's second window would open: nothing starts.
    restarted.hass.states.async_set(restarted.charge_control, "off")
    await restarted.hass.async_block_till_done()
    assert await restarted.controller.async_start_on_plug_in() is False
    restarted.clock.now = plan.windows[1][0] + timedelta(minutes=1)
    await _fire_due(restarted)
    await restarted.tick()
    assert restarted.on_calls == [] and restarted.off_calls == []
    _expects_nothing(restarted)


@pytest.mark.usefixtures("both_restarts")
async def test_a_restart_in_the_window_with_the_charge_running_stops_it(session: Session, both_restarts: str) -> None:
    plan = await _charging_window(session)

    async def down() -> None:
        await session.store.async_update(session.entry_id, mutate=_wait_settings)

    restarted = await _restart(session, both_restarts, meanwhile=down)

    assert len(restarted.on_calls) == 1, "nothing more is started"
    assert len(restarted.off_calls) == 1, "the old plan's charge is stopped: the answer is to wait"
    await _report_off(restarted)
    assert restarted.controller.plan is None and restarted.timers.pending == []
    assert restarted.preview.snapshot().applied_identity is None
    _expects_nothing(restarted)
    restarted.clock.now = plan.windows[0][1]
    await _fire_due(restarted)
    assert len(restarted.on_calls) == 1 and len(restarted.off_calls) == 1


@pytest.mark.usefixtures("both_restarts")
async def test_a_restart_with_unchanged_settings_keeps_the_plan_and_publishes_it(
    session: Session, both_restarts: str
) -> None:
    """The other side: settings unchanged, the restored plan stands with its timers, and the published snapshot names
    it (`applied_identity`, null after a restart before)."""
    _publish(session)
    await session.set_auto(departure=time(8, 0), requested_kwh=10.0)
    plan = session.controller.plan
    assert plan is not None

    restarted = await _restart(session, both_restarts)

    restored = restarted.controller.plan
    assert restored is not None and restored.auto_identity == plan.auto_identity
    assert restarted.timers.pending, "its window timers are armed again"
    snapshot = restarted.preview.snapshot()
    assert snapshot.applied is True and snapshot.applied_identity == restored.auto_identity
    assert restarted.executor.execution_state() == EXECUTION_SCHEDULED
    assert restarted.on_calls == [] and restarted.off_calls == []


# ------------------------------------------------------------------------------------- a charge not seen yet


@pytest.mark.usefixtures("both_restarts")
async def test_a_restart_with_the_charge_running_and_the_control_unreadable_keeps_the_plan_until_it_can_stop_it(
    session: Session, both_restarts: str
) -> None:
    """The plan's charge runs, the settings change to a wait, and Home Assistant restarts with the charger's integration
    not loaded: the charge cannot be seen. The plan is not dropped then (its charge would run on with nothing to stop
    it); once the charger reports the charge on, it is stopped and the plan cleared."""
    plan = await _charging_window(session)

    async def down() -> None:
        await session.store.async_update(session.entry_id, mutate=_wait_settings)
        session.hass.states.async_set(session.charge_control, "unavailable")

    restarted = await _restart(session, both_restarts, meanwhile=down)

    assert restarted.controller.plan is not None, "kept while its charge cannot be seen"
    assert restarted.executor.superseded_by_wait(), "nothing of it starts meanwhile"
    assert len(restarted.on_calls) == 1 and restarted.off_calls == []

    restarted.hass.states.async_set(restarted.charge_control, "on")
    await restarted.hass.async_block_till_done()

    assert len(restarted.off_calls) == 1, "the plan's own charge is stopped once it is seen"
    await _report_off(restarted)
    assert restarted.controller.plan is None and restarted.timers.pending == []
    _expects_nothing(restarted)
    restarted.clock.now = plan.windows[0][1]
    await _fire_due(restarted)
    await restarted.tick()
    assert len(restarted.on_calls) == 1 and len(restarted.off_calls) == 1


async def test_a_window_start_answered_after_the_wait_is_stopped_once_it_is_seen(session: Session) -> None:
    """The window's start went out and the charger has not answered it when the settings change to a wait: the plan is
    kept until the answer, and the charge the answer reports is stopped."""
    _publish(session)
    await session.set_auto(departure=time(8, 0), requested_kwh=10.0)
    plan = session.controller.plan
    assert plan is not None
    session.clock.now = plan.windows[0][0]
    await _fire_due(session)
    assert len(session.on_calls) == 1 and not session.controller.charge_control_on

    await _move_to_sunday(session)

    assert session.controller.plan is not None, "kept while its start is unanswered"
    assert session.off_calls == []

    session.hass.states.async_set(session.charge_control, "on")
    await session.hass.async_block_till_done()

    assert len(session.off_calls) == 1, "the charge the start began is stopped"
    await _report_off(session)
    assert session.controller.plan is None and session.timers.pending == []
    session.clock.now = plan.windows[0][1]
    await _fire_due(session)
    await session.tick()
    assert len(session.on_calls) == 1 and len(session.off_calls) == 1
    _expects_nothing(session)


@pytest.mark.usefixtures("both_restarts")
async def test_a_plan_confirmed_by_newer_settings_is_still_theirs_after_a_restart(
    session: Session, both_restarts: str
) -> None:
    """The settings were written again and their calculation proposed the same plan: the plan stands for them. That
    survives a restart (the confirming revision is saved with the plan), so a later wait over those same settings keeps
    the plan and its running charge."""
    _publish(session)
    waiting = await _waiting_answer(session)
    plan = await _charging_window(session)
    await session.set_auto(departure=time(8, 0), requested_kwh=10.0)
    assert session.controller.plan is plan
    assert plan.auto_settings_revision == session.settings().revision, "the plan carries the confirming revision"

    restarted = await _restart(session, both_restarts)

    restored = restarted.controller.plan
    assert restored is not None and restored.auto_settings_revision == session.settings().revision
    restarted.executor.begin_attempt()
    await restarted.executor.async_reconcile(
        restarted.settings(), waiting, attempt=restarted.executor.attempt
    )
    assert restarted.controller.plan is restored, "a wait over the settings it stands for keeps it"
    assert restarted.off_calls == []
