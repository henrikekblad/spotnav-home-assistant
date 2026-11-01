"""The core's charge session kept across restarts while it drives (`CONF_CORE_OWNERSHIP`): one versioned record per
charger (`charge_session`) beside today's keys, saved debounced (never at every report), without what a restart
clears anyway; read back on the option-on path only, migrated once from today's keys when it is missing or of a
version the core does not know. Today's keys are still written as before."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.spotnav.core.session import (
    CarEnded,
    CarEndedWait,
    ChargeSession,
    ManualPause,
    PendingCommand,
    SessionError,
    STORED_VERSION,
    TRANSIENT_FIELDS,
)
from custom_components.spotnav.execution.controller import SESSION_ORDER_KEY, SESSION_SAVE_DELAY_S, SESSION_STORE_KEY
from custom_components.spotnav.planning.auto_settings import (
    MANUAL_SCOPE_NEXT_PLUG_IN,
    MANUAL_SCOPE_PLUG_IN,
    MANUAL_START,
    MANUAL_STOP,
)

from .pause_world import ENTRY, later_window, pause_world, SWITCH, two_windows, World
from .relay import FakeScheduler

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
KEY = f"spotnav.{ENTRY}"


# ---------------------------------------------------------------------------------------------- the record


def test_the_stored_record_round_trips_without_what_a_restart_clears() -> None:
    session = ChargeSession(
        plugged=True,
        owner="plan",
        manual=ManualPause("stop", "next_plug_in"),
        held=True,
        car_ended=CarEnded(at=T0, soc=88.0, vehicle="car", wait=CarEndedWait(target=91.0, level=86.0, estimated=True)),
        balancing_paused=True,
        paused_origin="person",
        held_for_safety=True,
        safety_stopped_at=T0,
        hold_stop_times=(T0,),
        hold_tried_at=T0,
        hold_gave_up=True,
        hold_stop_pending=True,
        pending=(PendingCommand("stop", "hold"),),
    )
    record = session.to_store()
    assert record["version"] == STORED_VERSION
    assert not set(TRANSIENT_FIELDS) & set(record)
    read = ChargeSession.from_store(record)
    assert read == session.stored()
    assert read.pending == () and read.hold_stop_times == () and not read.hold_gave_up and not read.held_for_safety
    assert (read.owner, read.manual, read.car_ended, read.paused_origin) == (
        "plan", session.manual, session.car_ended, "person"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"version": 3},
        {"version": None},
        {"version": True},
        {"pending": []},  # a transient field is not part of the record
        {"owner": "nobody"},
        {"manual": {"action": "stop"}},
        {"car_ended": {"at": "yesterday", "soc": None, "vehicle": None, "wait": None}},
        {"car_ended_at": None},  # a field of version 1 in a record of version 2
    ],
)
def test_a_stored_record_of_another_version_or_shape_is_refused(change: dict[str, Any]) -> None:
    record = {**ChargeSession(owner="plan").to_store(), **change}
    with pytest.raises(SessionError):
        ChargeSession.from_store(record)
    with pytest.raises(SessionError):
        ChargeSession.from_store("not a record")


# ---------------------------------------------------------------------------------------------- in the controller


@pytest.fixture
def drives() -> None:
    """The core drives (for every charger now)."""


@pytest.fixture
def saves(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every record written to the charger's store."""
    written: list[dict[str, Any]] = []
    real = Store.async_save

    async def counting(self: Store, data: Any) -> None:
        if self.key == KEY:
            written.append(dict(data))
        await real(self, data)

    monkeypatch.setattr(Store, "async_save", counting)
    return written


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[ChargeSession]:
    """Every stored session record the core read back."""
    seen: list[ChargeSession] = []
    real = ChargeSession.from_store.__func__  # type: ignore[attr-defined]

    def reading(cls: type[ChargeSession], raw: Any) -> ChargeSession:
        session = real(cls, raw)
        seen.append(session)
        return session

    monkeypatch.setattr(ChargeSession, "from_store", classmethod(reading))
    return seen


async def _stored(world: World) -> dict[str, Any]:
    return await world.controller._store.async_load()  # noqa: SLF001


async def _rewrite(world: World, change: Any) -> None:
    """Change the charger's stored record by hand, as an older release or a damaged file left it: no debounced save
    of the running controller overwrites it on the way down."""
    world.controller._cancel_session_save()  # noqa: SLF001
    record = dict(await _stored(world))
    change(record)
    await world.controller._store.async_save(record)  # noqa: SLF001


def _but_the_plug(session: ChargeSession) -> ChargeSession:
    """A session as stored, but for the connection the charger states again after a restart (a restart clears it)."""
    return session.stored().with_changes(plugged=None)


def _intent(world: World) -> tuple[Any, Any]:
    manual = world.controller.session_runner.session.manual
    return (None, None) if manual is None else (manual.action, manual.scope)


async def _settle(hass: HomeAssistant, freezer: Any) -> None:
    """Let the debounced save of the core's session run: the next report at least its delay later saves it."""
    freezer.tick(timedelta(seconds=SESSION_SAVE_DELAY_S + 1))
    current = hass.states.get(SWITCH)
    hass.states.async_set(SWITCH, current.state, {**current.attributes, "report": "later"})
    await hass.async_block_till_done()


@pytest.mark.usefixtures("drives")
@pytest.mark.usefixtures("both_restarts")
async def test_todays_keys_are_migrated_once_into_the_cores_record(
    hass: HomeAssistant, timers: FakeScheduler, saves: list, reads: list
) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_stop()
    await _rewrite(world, lambda record: record.pop(SESSION_STORE_KEY, None))
    assert SESSION_STORE_KEY not in await _stored(world)
    before = len(saves)

    restarted = await world.restart()
    assert reads == [], "nothing of the core's to read: today's keys are"

    migrated = [record for record in saves[before:] if SESSION_STORE_KEY in record]
    assert migrated, "the core's record is written at the restart that read today's keys"
    stored = (await _stored(restarted))[SESSION_STORE_KEY]
    assert stored["version"] == STORED_VERSION
    assert (stored["manual"], stored["owner"]) == ({"action": "stop", "scope": "plug_in"}, "none")
    for key in ("plan", "charge_origin", "plan_charge", "hold"):
        assert key in await _stored(restarted), "today's keys are still written"
    assert _but_the_plug(restarted.controller.session_runner.session) == _but_the_plug(
        ChargeSession.from_store(stored)
    )

    # Once: the next restart reads the record and writes nothing for it.
    before, read = len(saves), len(reads)
    again = await restarted.restart()
    assert len(reads) == read + 1 and _but_the_plug(reads[-1]) == _but_the_plug(ChargeSession.from_store(stored))
    assert not [record for record in saves[before:] if record.get(SESSION_STORE_KEY, {}).get("plugged") is None]
    assert _intent(again) == (MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)
    await again.shutdown()


@pytest.mark.core_failure_expected  # a record it cannot read is a failure, told
@pytest.mark.usefixtures("drives")
@pytest.mark.parametrize("record", [{"version": 99, "owner": "plan"}, "garbled", {"version": 1}])
@pytest.mark.usefixtures("both_restarts")
async def test_a_record_of_an_unknown_version_falls_back_to_todays_keys(
    hass: HomeAssistant, timers: FakeScheduler, reads: list, record: Any
) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_stop()

    def damage(stored: dict[str, Any]) -> None:
        stored[SESSION_STORE_KEY] = record

    await _rewrite(world, damage)
    restarted = await world.restart()

    stored = (await _stored(restarted))[SESSION_STORE_KEY]
    assert stored["version"] == STORED_VERSION, "written again in the version the core knows"
    assert _but_the_plug(ChargeSession.from_store(stored)) == _but_the_plug(restarted.controller.session_runner.session)
    assert _intent(restarted) == (MANUAL_STOP, MANUAL_SCOPE_PLUG_IN), "from today's keys and the stored pause"
    await restarted.shutdown()


async def _reports(hass: HomeAssistant, world: World, saves: list, *, count: int, toggle: bool) -> int:
    before = len(saves)
    for index in range(count):
        state = ("on" if index % 2 else "off") if toggle else hass.states.get(SWITCH).state
        hass.states.async_set(SWITCH, state, {"report": index})
        await hass.async_block_till_done()
    return len(saves) - before


@pytest.mark.parametrize("toggle", [False, True])
async def test_reports_do_not_write_the_record_each_time(
    hass: HomeAssistant,
    timers: FakeScheduler,
    saves: list,
    monkeypatch: pytest.MonkeyPatch,
    freezer: Any,
    toggle: bool,
) -> None:
    """Two hundred reports of a charge running unchanged, or of a charger turning itself on and off with no plan
    (the core's owner moving between nobody and the charger itself at every report): nothing is written during them,
    and at most one debounced save of the record after them."""
    world = await pause_world(hass, timers, charging=not toggle)
    during = await _reports(hass, world, saves, count=200, toggle=toggle)
    before = len(saves)
    await _settle(hass, freezer)
    after = len(saves) - before
    await world.shutdown()
    assert during == 0 and after <= 1, (during, after)


@pytest.mark.usefixtures("drives")
@pytest.mark.usefixtures("both_restarts")
async def test_a_restart_with_a_persons_stop_for_the_plug_in_reads_it_from_the_record(
    hass: HomeAssistant, timers: FakeScheduler, reads: list
) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_stop()
    starts = len(world.starts)

    restarted = await world.restart()
    assert reads and reads[-1].manual == ManualPause(MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)
    assert _intent(restarted) == (MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)
    assert len(world.starts) == starts, "the open window does not start under the person's Stop"
    await restarted.switch("on")  # the charger begins by itself: stopped (C7)
    assert hass.states.get(SWITCH).state == "off"

    await restarted.plug.set(False)
    assert _intent(restarted) == (None, None) and restarted.pause.choice != "manual"
    await restarted.shutdown()


@pytest.mark.usefixtures("drives")
@pytest.mark.usefixtures("both_restarts")
async def test_a_restart_with_a_persons_stop_for_the_next_plug_in_reads_it_from_the_record(
    hass: HomeAssistant, timers: FakeScheduler, reads: list
) -> None:
    world = await pause_world(hass, timers, connected=False)
    await world.executor.async_manual_stop()
    assert _intent(world) == (MANUAL_STOP, MANUAL_SCOPE_NEXT_PLUG_IN)

    restarted = await world.restart()
    assert reads and reads[-1].manual == ManualPause(MANUAL_STOP, MANUAL_SCOPE_NEXT_PLUG_IN)
    assert _intent(restarted) == (MANUAL_STOP, MANUAL_SCOPE_NEXT_PLUG_IN)

    await restarted.plug.set(True)
    assert _intent(restarted) == (MANUAL_STOP, MANUAL_SCOPE_PLUG_IN), "the plug-in it was given for"
    assert restarted.pause.scope == MANUAL_SCOPE_PLUG_IN
    await restarted.plug.set(False)
    assert _intent(restarted) == (None, None)
    await restarted.shutdown()


@pytest.mark.usefixtures("drives")
@pytest.mark.usefixtures("both_restarts")
async def test_a_restart_with_a_persons_start_reads_it_from_the_record(
    hass: HomeAssistant, timers: FakeScheduler, reads: list
) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_start()
    assert world.controller.charging and _intent(world) == (MANUAL_START, MANUAL_SCOPE_PLUG_IN)
    stops = len(world.stops)

    restarted = await world.restart()
    assert reads and reads[-1].manual == ManualPause(MANUAL_START, MANUAL_SCOPE_PLUG_IN)
    assert reads[-1].owner == "person"
    assert _intent(restarted) == (MANUAL_START, MANUAL_SCOPE_PLUG_IN)
    assert restarted.controller.session_runner.session.owner == "person"
    hass.states.async_set(SWITCH, "on", {"report": "after the restart"})
    await hass.async_block_till_done()
    assert len(world.stops) == stops and restarted.controller.charging, "nothing automatic stops the person's Start"

    await restarted.plug.set(False)
    assert _intent(restarted) == (None, None)
    await restarted.shutdown()


@pytest.mark.usefixtures("drives")
async def test_a_change_waiting_for_its_debounced_save_is_saved_at_shutdown(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await pause_world(hass, timers)
    world.controller._session_saved = None  # noqa: SLF001 - as if nothing of it was saved yet
    hass.states.async_set(SWITCH, "off", {"report": "a change"})
    await hass.async_block_till_done()
    assert world.controller._session_dirty_since is not None  # noqa: SLF001
    await world.shutdown()
    assert SESSION_STORE_KEY in await _stored(world)


# ---------------------------------------------------------------------------------------------- saved in time


@pytest.mark.usefixtures("drives")
async def test_a_change_of_owner_is_saved_at_once(hass: HomeAssistant, timers: FakeScheduler, saves: list) -> None:
    """The sun's start makes the charge the sun's: the record says so on disk at once, with no later decision or
    shutdown to wait for (Home Assistant's stop unloads no entry)."""
    world = await pause_world(hass, timers)
    assert await world.controller.async_start(cause="solar")
    await hass.async_block_till_done()
    assert (await _stored(world))[SESSION_STORE_KEY]["owner"] == "solar"
    await world.shutdown()


@pytest.mark.usefixtures("drives")
async def test_a_change_of_the_persons_intent_is_saved_at_once(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers)
    await world.executor.async_manual_stop()
    await hass.async_block_till_done()
    assert (await _stored(world))[SESSION_STORE_KEY]["manual"] == {"action": MANUAL_STOP, "scope": MANUAL_SCOPE_PLUG_IN}
    await world.shutdown()


@pytest.mark.usefixtures("drives")
async def test_home_assistants_stop_saves_a_change_waiting_for_its_debounced_save(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home Assistant's stop (`__init__._async_stop`) unloads no entry: it saves each controller's waiting change."""
    import custom_components.spotnav as integration

    world = await pause_world(hass, timers)
    shadow = world.controller.session_runner
    shadow.session = shadow.session.with_changes(held=True)
    world.controller._persist_session(shadow.session)  # noqa: SLF001 - a decision changed only bookkeeping
    assert world.controller._session_dirty_since is not None  # noqa: SLF001 - waiting for its debounced save
    assert (await _stored(world))[SESSION_STORE_KEY]["held"] is False

    class Entry:
        entry_id = ENTRY

    class Data:
        controller, executor, preview = world.controller, world.executor, None

    with monkeypatch.context() as patch:
        patch.setattr(hass.config_entries, "async_entries", lambda domain=None: [Entry()])
        patch.setattr(integration, "charger_data", lambda _hass, entry_id: Data() if entry_id == ENTRY else None)
        patch.setattr(integration, "domain_data", lambda _hass: type("Domain", (), {"price_refresh": None, "price_repository": None, "fx_rates": None})())
        await integration._async_stop(hass)  # noqa: SLF001
    assert (await _stored(world))[SESSION_STORE_KEY]["held"] is True
    assert world.controller._session_dirty_since is None  # noqa: SLF001
    await world.shutdown()


@pytest.mark.usefixtures("drives")
async def test_a_decision_made_while_the_shutdown_waits_for_the_lock_is_saved(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """The shutdown marks the controller shut down before it takes the lock, and no decision saves after that mark:
    one that finishes while the shutdown waits for the lock is saved by the shutdown itself."""
    import asyncio

    world = await pause_world(hass, timers)
    controller = world.controller
    shadow = controller.session_runner
    async with controller._lock:  # noqa: SLF001 - a decision in flight
        down = hass.async_create_task(controller.async_shutdown())
        await asyncio.sleep(0)
        assert controller._shut_down  # noqa: SLF001
        shadow.session = shadow.session.with_changes(held=True)
        controller._persist_session(shadow.session)  # noqa: SLF001 - the decision ends
    await down
    assert (await _stored(world))[SESSION_STORE_KEY]["held"] is True
    await world.executor.async_shutdown()


# ---------------------------------------------------------------------------------------------- one writer (step 8)


def _keys_of(session: dict[str, Any]) -> dict[str, Any]:
    """Today's ownership keys as derived from a stored record of the core's (`_store_record` when it drives)."""
    from custom_components.spotnav.execution.controller import _CORE_OWNER_ORIGIN

    origin, plan_charge = _CORE_OWNER_ORIGIN.get(session["owner"], (None, False))
    ended = session["car_ended"] or {}
    paused = _CORE_OWNER_ORIGIN.get(session["paused_origin"] or "", (None, False))
    return {
        "charge_origin": origin,
        "plan_charge": plan_charge,
        "car_ended_at": ended.get("at"),
        "car_ended_soc": ended.get("soc"),
        "car_ended_vehicle": ended.get("vehicle"),
        "car_ended_wait": ended.get("wait"),
        "balancing_pause": {"origin": paused[0], "plan_charge": paused[1]} if session["balancing_paused"] else None,
        "hold": {"held": session["held"], "overridden": session["overridden"]},
    }


def _written_keys(record: dict[str, Any]) -> dict[str, Any]:
    from custom_components.spotnav.execution.controller import OWNERSHIP_KEYS

    return {key: record.get(key) for key in OWNERSHIP_KEYS}


@pytest.mark.usefixtures("drives")
async def test_every_record_written_keeps_todays_keys_derived_from_its_session_and_no_order_marks(
    hass: HomeAssistant, timers: FakeScheduler, saves: list
) -> None:
    """One writer: every save writes the core's record and today's keys together, the keys derived from the record,
    also the saves inside a start's or a stop's feed before its result is decided (the owner the command makes, in
    both). No order marks are written, so an earlier build reads the record as the newer (its restore takes a
    record with no marks for the newer), and an older build or the option off reads the same owner from the keys."""
    world = await pause_world(hass, timers, plan=two_windows())
    assert world.controller.charge_origin == "plan_window"
    await world.executor.async_manual_stop()
    await world.executor.async_manual_start()
    await world.executor.async_resume()
    assert await world.controller.async_start(cause="solar")
    await world.controller.async_stop()
    assert await world.controller.async_start()
    await world.plug.set(False)
    await hass.async_block_till_done()
    await world.shutdown()
    written = [record for record in saves if SESSION_STORE_KEY in record]
    assert len(written) >= 6
    owners = {record[SESSION_STORE_KEY]["owner"] for record in written}
    assert {"plan", "person", "solar", "charge_now", "none"} <= owners, owners
    for record in written:
        assert SESSION_ORDER_KEY not in record
        assert _written_keys(record) == _keys_of(record[SESSION_STORE_KEY]), record


@pytest.mark.usefixtures("drives")
@pytest.mark.parametrize("cause", ["solar", None])
async def test_a_restart_right_after_a_starts_save_and_before_its_result_reads_the_starts_owner(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, cause: str | None
) -> None:
    """The start's own save (`_start_locked`, inside its feed) is the last one on disk when Home Assistant dies before
    the feed's end decides the result: the record alone is read back, and it names the start's owner, as today's keys
    do (the order marks that once let the keys win here are gone). The sun's start and a Charge-now start; a person's
    Start keeps its intent in the settings store as well, which this test cannot take back to that instant."""
    world = await pause_world(hass, timers, plan={"periods": [later_window()], "amps": 10})
    controller = world.controller
    on_disk: list[dict[str, Any]] = []
    real_save = controller._store.async_save  # noqa: SLF001

    async def save(data: Any) -> None:
        if controller._command_unfed == "start" and not on_disk:  # noqa: SLF001 - the start's result not fed yet
            on_disk.append(dict(data))
        await real_save(data)

    monkeypatch.setattr(controller._store, "async_save", save)  # noqa: SLF001
    assert await controller.async_start(cause=cause)
    await hass.async_block_till_done()
    (record,) = on_disk
    expected = "solar" if cause == "solar" else "charge_now"
    assert record[SESSION_STORE_KEY]["owner"] == expected, "the start's own save, before its result"
    assert _written_keys(record) == _keys_of(record[SESSION_STORE_KEY])

    # The process dies there: what is on disk is that save alone.
    controller._cancel_session_save()  # noqa: SLF001
    await world.executor.async_shutdown()

    async def nothing(*_args: Any, **_kwargs: Any) -> None:
        return None

    await real_save(record)
    monkeypatch.setattr(controller._store, "async_save", nothing)  # noqa: SLF001
    await controller.async_shutdown()
    restarted = await world._start_again()  # noqa: SLF001
    assert restarted.controller.session_runner.session.owner == expected
    assert restarted.controller.charge_origin == (cause or "other")
    assert hass.states.get(SWITCH).state == "on", "the charge goes on"
    await restarted.shutdown()


@pytest.mark.usefixtures("drives")
async def test_a_crash_between_a_persons_starts_save_and_its_pause_leaves_the_charge_to_the_person(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    """The plan's charge runs in an open window and the person presses Start. The Start's own save (inside its feed)
    writes the person's owner; Home Assistant dies before the settings store's pause and the feed's end are saved.
    The restart reads a person's charge with no intent of theirs and the plan still there: the window's start the
    re-arm feeds does not take it over as the plan's, so the window's end does not stop it."""
    from dataclasses import replace

    from .pause_world import ENTRY as CHARGER

    world = await pause_world(hass, timers, plan=two_windows())
    controller = world.controller
    assert controller.charge_origin == "plan_window"
    on_disk: list[dict[str, Any]] = []
    real_save = controller._store.async_save  # noqa: SLF001

    async def save(data: Any) -> None:
        if controller._command_unfed == "start" and not on_disk:  # noqa: SLF001 - the Start's result not fed yet
            on_disk.append(dict(data))
        await real_save(data)

    monkeypatch.setattr(controller._store, "async_save", save)  # noqa: SLF001
    pause_before = world.store.settings(CHARGER).pause
    await world.executor.async_manual_start(10)
    await hass.async_block_till_done()
    (record,) = on_disk
    assert (record[SESSION_STORE_KEY]["owner"], record[SESSION_STORE_KEY]["manual"]) == ("person", None)
    assert record["plan"] is not None and record["charge_origin"] == "manual"

    # The process dies there: neither the pause nor any later save reached the disk.
    controller._cancel_session_save()  # noqa: SLF001
    await world.executor.async_shutdown()
    await world.store.async_update(CHARGER, mutate=lambda current: replace(current, pause=pause_before))

    async def nothing(*_args: Any, **_kwargs: Any) -> None:
        return None

    await real_save(record)
    monkeypatch.setattr(controller._store, "async_save", nothing)  # noqa: SLF001
    await controller.async_shutdown()
    restarted = await world._start_again()  # noqa: SLF001
    shadow = restarted.controller.session_runner
    assert (shadow.session.owner, shadow.session.manual) == ("person", None)
    assert restarted.controller.plan is not None and restarted.controller.charge_origin == "manual"
    assert hass.states.get(SWITCH).state == "on"

    freezer.tick(timedelta(minutes=51))
    await restarted.fire("_async_end_callback")
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == "on", "the window's end does not stop the person's charge"
    assert shadow.session.owner == "person" and restarted.controller.charge_origin == "manual"
    await restarted.shutdown()


@pytest.mark.parametrize("drives", [True])
async def test_a_storm_of_reports_that_change_nothing_writes_nothing(
    hass: HomeAssistant, timers: FakeScheduler, saves: list, freezer: Any, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    """A hundred reports of a plan charge running unchanged, a debounce delay apart: no save at all, in either mode."""
    world = await pause_world(hass, timers, plan=two_windows())
    assert world.controller.charging
    await _settle(hass, freezer)
    await hass.async_block_till_done()
    before = len(saves)
    for index in range(100):
        freezer.tick(timedelta(seconds=SESSION_SAVE_DELAY_S / 3))
        hass.states.async_set(SWITCH, "on", {"report": index})
        await hass.async_block_till_done()
    assert len(saves) == before, saves[before:]
    await world.shutdown()


@pytest.mark.usefixtures("drives", "both_restarts")
@pytest.mark.parametrize("marks", ["none", "record_newer", "keys_newer"])
async def test_at_a_restart_the_record_alone_is_read_but_for_an_earlier_builds_keys_written_after_it(
    hass: HomeAssistant, timers: FakeScheduler, marks: str
) -> None:
    """On disk the record says nobody owns the charge and today's keys say the sun does. A record of this build (no
    order marks) or one an earlier build saved after its keys is the truth: today's owner is cleared, and the keys are
    written again to follow it. An earlier build's record whose marks say the keys were written after it is migrated
    from the keys once, as that build read it (the sun's charge is spared), and written again without marks."""
    world = await pause_world(hass, timers, plan={"periods": [later_window()], "amps": 10})

    def change(record: dict[str, Any]) -> None:
        record[SESSION_STORE_KEY] = {**record[SESSION_STORE_KEY], "owner": "none"}
        record["charge_origin"] = "solar"
        record.pop(SESSION_ORDER_KEY, None)
        if marks != "none":
            record[SESSION_ORDER_KEY] = {"session": 3, "keys": 4} if marks == "keys_newer" else {"session": 4, "keys": 3}

    await world.controller.async_start(cause="solar")
    await hass.async_block_till_done()
    await _rewrite(world, change)
    world.controller._session_saved = ChargeSession.from_store(  # noqa: SLF001 - the old controller saves nothing more
        (await _stored(world))[SESSION_STORE_KEY]
    )
    world.controller.session_runner.session = world.controller._session_saved  # noqa: SLF001
    restarted = await world.restart()
    expected = "solar" if marks == "keys_newer" else None
    assert restarted.controller.charge_origin == expected
    stored = await _stored(restarted)
    assert SESSION_ORDER_KEY not in stored
    assert stored["charge_origin"] == expected and stored[SESSION_STORE_KEY]["owner"] == (expected or "none")
    await restarted.shutdown()


# ---------------------------------------------------------------------------------------------- an update mid-session


def _as_the_previous_logic_left_it(record: dict[str, Any]) -> None:
    """A charger an earlier release ran with the previous logic: today's keys alone, no record of the core's."""
    record.pop(SESSION_STORE_KEY, None)
    record.pop(SESSION_ORDER_KEY, None)


@pytest.mark.usefixtures("both_restarts")
async def test_an_update_during_a_running_plan_charge_hands_it_to_the_core(
    hass: HomeAssistant, timers: FakeScheduler, reads: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release before ran the previous logic (no record of the core's on disk); the update makes the core drive
    while a plan window's charge runs. The core's session is read from today's keys, its record written at once, the
    charge goes on untouched, and the window's end stops it as ever."""
    world = await pause_world(hass, timers, plan=two_windows())
    assert world.controller.charging and world.controller.charge_origin == "plan_window"
    await _rewrite(world, _as_the_previous_logic_left_it)
    stops = len(world.stops)

    restarted = await world.restart()
    controller = restarted.controller
    shadow = controller.session_runner
    assert shadow.drives and reads == [], "nothing of the core's to read: today's keys are"
    assert shadow.session.owner == "plan"
    assert controller.charging and controller.charge_origin == "plan_window"
    stored = await _stored(restarted)
    assert stored[SESSION_STORE_KEY]["owner"] == "plan" and stored[SESSION_STORE_KEY]["version"] == STORED_VERSION

    starts = len(world.starts)
    hass.states.async_set(SWITCH, "on", {"report": "after the update"})
    await hass.async_block_till_done()
    assert (len(world.starts), len(world.stops)) == (starts, stops), "the running charge is left as it is"

    await restarted.fire("_async_end_callback")
    assert len(world.stops) == stops + 1 and not controller.charging, "the window's end stops it"
    assert shadow.session.owner == "none"
    await restarted.shutdown()


@pytest.mark.usefixtures("both_restarts")
async def test_an_update_during_a_solar_charge_hands_it_to_the_core(
    hass: HomeAssistant, timers: FakeScheduler, reads: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same during the sun's charge with a plan window ahead: the core takes it as the sun's, nothing stops it,
    and the next restart reads the core's own record."""
    world = await pause_world(hass, timers, plan={"periods": [later_window()], "amps": 10})
    assert await world.controller.async_start(cause="solar")
    await hass.async_block_till_done()
    assert world.controller.charging and world.controller.charge_origin == "solar"
    await _rewrite(world, _as_the_previous_logic_left_it)
    stops = len(world.stops)

    restarted = await world.restart()
    controller = restarted.controller
    assert controller.session_runner.drives and reads == []
    assert controller.session_runner.session.owner == "solar"
    assert controller.charging and controller.charge_origin == "solar"
    assert (await _stored(restarted))[SESSION_STORE_KEY]["owner"] == "solar"
    hass.states.async_set(SWITCH, "on", {"report": "after the update"})
    await hass.async_block_till_done()
    assert len(world.stops) == stops and controller.charge_origin == "solar", "the sun's charge is spared"

    again = await restarted.restart()
    assert reads and reads[-1].owner == "solar", "read from the core's own record now"
    assert again.controller.charge_origin == "solar" and len(world.stops) == stops
    await again.shutdown()
