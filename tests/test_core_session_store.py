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
    ChargeSession,
    ManualPause,
    PendingCommand,
    SessionError,
    STORED_VERSION,
    TRANSIENT_FIELDS,
)
from custom_components.spotnav.execution import ownership_shadow
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
        car_ended_at=T0,
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
    assert (read.owner, read.manual, read.car_ended_at, read.paused_origin) == ("plan", session.manual, T0, "person")


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"version": None},
        {"pending": []},  # a transient field is not part of the record
        {"owner": "nobody"},
        {"manual": {"action": "stop"}},
        {"car_ended_at": "yesterday"},
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
def drives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", True)


@pytest.fixture
def shadows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", False)


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
    manual = world.controller.ownership_shadow.session.manual
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
    assert _but_the_plug(restarted.controller.ownership_shadow.session) == _but_the_plug(
        ChargeSession.from_store(stored)
    )

    # Once: the next restart reads the record and writes nothing for it.
    before, read = len(saves), len(reads)
    again = await restarted.restart()
    assert len(reads) == read + 1 and _but_the_plug(reads[-1]) == _but_the_plug(ChargeSession.from_store(stored))
    assert not [record for record in saves[before:] if record.get(SESSION_STORE_KEY, {}).get("plugged") is None]
    assert _intent(again) == (MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)
    assert again.controller.ownership_shadow.counts["disagreements"] == 0
    await again.shutdown()


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
    assert _but_the_plug(ChargeSession.from_store(stored)) == _but_the_plug(restarted.controller.ownership_shadow.session)
    assert _intent(restarted) == (MANUAL_STOP, MANUAL_SCOPE_PLUG_IN), "from today's keys and the stored pause"
    assert restarted.controller.ownership_shadow.counts["disagreements"] == 0
    await restarted.shutdown()


@pytest.mark.usefixtures("shadows")
@pytest.mark.usefixtures("both_restarts")
async def test_with_the_option_off_the_record_is_neither_written_nor_read(
    hass: HomeAssistant, timers: FakeScheduler, saves: list, reads: list, freezer: Any
) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_stop()
    await world.switch("on")
    await _settle(hass, freezer)
    assert saves and all(SESSION_STORE_KEY not in record for record in saves)

    # A record a release with the option on left behind is not read: today's restore, exactly.
    def leave(stored: dict[str, Any]) -> None:
        stored[SESSION_STORE_KEY] = ChargeSession(owner="person", manual=ManualPause("start", "plug_in")).to_store()

    await _rewrite(world, leave)
    restarted = await world.restart()
    assert reads == []
    assert _intent(restarted) == (MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)
    assert restarted.controller.ownership_shadow.session.owner == "none"
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
    (the core's owner moving between nobody and the charger itself at every report): the option on writes no more
    than the option off, but for at most one debounced save of its record."""
    written: dict[bool, tuple[int, int]] = {}
    for drives in (False, True):
        monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", drives)
        world = await pause_world(hass, timers, charging=not toggle)
        during = await _reports(hass, world, saves, count=200, toggle=toggle)
        before = len(saves)
        await _settle(hass, freezer)
        written[drives] = (during, len(saves) - before)
        await world.shutdown()
        hass.services.async_remove("switch", "turn_on")
        hass.services.async_remove("switch", "turn_off")
    assert written[True][0] == written[False][0], written
    assert written[True][1] <= 1 and written[False][1] == 0, written


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
    assert restarted.controller.ownership_shadow.counts["disagreements"] == 0
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
    assert restarted.controller.ownership_shadow.counts["disagreements"] == 0
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
    assert restarted.controller.ownership_shadow.session.owner == "person"
    hass.states.async_set(SWITCH, "on", {"report": "after the restart"})
    await hass.async_block_till_done()
    assert len(world.stops) == stops and restarted.controller.charging, "nothing automatic stops the person's Start"

    await restarted.plug.set(False)
    assert _intent(restarted) == (None, None)
    assert restarted.controller.ownership_shadow.counts["disagreements"] == 0
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
    shadow = world.controller.ownership_shadow
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
        patch.setattr(integration, "domain_data", lambda _hass: type("Domain", (), {"price_refresh": None})())
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
    shadow = controller.ownership_shadow
    async with controller._lock:  # noqa: SLF001 - a decision in flight
        down = hass.async_create_task(controller.async_shutdown())
        await asyncio.sleep(0)
        assert controller._shut_down  # noqa: SLF001
        shadow.session = shadow.session.with_changes(held=True)
        controller._persist_session(shadow.session)  # noqa: SLF001 - the decision ends
    await down
    assert (await _stored(world))[SESSION_STORE_KEY]["held"] is True
    await world.executor.async_shutdown()


@pytest.mark.shadow_disagreement_expected  # the record's owner against today's keys, compared at the restart
@pytest.mark.usefixtures("drives", "both_restarts")
@pytest.mark.parametrize("newer", ["keys", "record"])
async def test_at_a_restart_the_newer_of_the_record_and_todays_keys_wins(
    hass: HomeAssistant, timers: FakeScheduler, newer: str
) -> None:
    """On disk the record says nobody owns the charge and today's keys say the sun does. Written after the record's
    last change, today's keys win (the sun's charge is spared); written before it, the record wins (as the core
    decided last, today's owner is cleared)."""
    world = await pause_world(hass, timers, plan={"periods": [later_window()], "amps": 10})
    key = SESSION_ORDER_KEY

    def change(record: dict[str, Any]) -> None:
        record[SESSION_STORE_KEY] = {**record[SESSION_STORE_KEY], "owner": "none"}
        record["charge_origin"] = "solar"
        record[key] = {"session": 3, "keys": 4} if newer == "keys" else {"session": 4, "keys": 3}

    await world.controller.async_start(cause="solar")
    await hass.async_block_till_done()
    await _rewrite(world, change)
    world.controller._session_saved = ChargeSession.from_store(  # noqa: SLF001 - the old controller saves nothing more
        (await _stored(world))[SESSION_STORE_KEY]
    )
    world.controller.ownership_shadow.session = world.controller._session_saved  # noqa: SLF001
    restarted = await world.restart()
    expected = "solar" if newer == "keys" else None
    assert restarted.controller.charge_origin == expected
    await restarted.shutdown()
