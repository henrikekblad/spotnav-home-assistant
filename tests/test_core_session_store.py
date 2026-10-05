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
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.core.session import (
    ChargeSession,
    ManualPause,
    PendingCommand,
    SessionError,
    STORED_VERSION,
    TRANSIENT_FIELDS,
)
from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.execution.controller import SESSION_SAVE_DELAY_S, SESSION_STORE_KEY
from custom_components.spotnav.planning.auto_settings import (
    MANUAL_SCOPE_NEXT_PLUG_IN,
    MANUAL_SCOPE_PLUG_IN,
    MANUAL_START,
    MANUAL_STOP,
)

from .pause_world import ENTRY, pause_world, SWITCH, two_windows, World
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


async def _settle(hass: HomeAssistant) -> None:
    """Let the debounced save of the core's session run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SESSION_SAVE_DELAY_S + 1))
    await hass.async_block_till_done()


@pytest.mark.usefixtures("drives")
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
async def test_with_the_option_off_the_record_is_neither_written_nor_read(
    hass: HomeAssistant, timers: FakeScheduler, saves: list, reads: list
) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_stop()
    await world.switch("on")
    await _settle(hass)
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
    hass: HomeAssistant, timers: FakeScheduler, saves: list, monkeypatch: pytest.MonkeyPatch, toggle: bool
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
        await _settle(hass)
        written[drives] = (during, len(saves) - before)
        await world.shutdown()
        hass.services.async_remove("switch", "turn_on")
        hass.services.async_remove("switch", "turn_off")
    assert written[True][0] == written[False][0], written
    assert written[True][1] <= 1 and written[False][1] == 0, written


@pytest.mark.usefixtures("drives")
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
    assert world.controller._session_save_cancel is not None  # noqa: SLF001
    await world.shutdown()
    assert SESSION_STORE_KEY in await _stored(world)
