"""The car-ended record and the connection in the core's session (the core end state plan, step 6): while the core
drives, when the car ended a charge by itself in this plug-in, its level read then, which car was read and what the plan
waits for are the session's (`car_ended`), and so is the connection the charger last stated (`plugged`); the controller
reads both from it. Today's `car_ended_*` keys are still written, derived from the session, so an older build reads
them as before; the session's own record is version 2. With the option off, today's fields stay the truth. Every test
here runs in both modes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.core import session as session_module
from custom_components.spotnav.core.session import CarEnded, CarEndedWait, ChargeSession, STORED_VERSION
from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.execution.controller import SESSION_STORE_KEY

from .helpers import install_schedule
from .pause_world import pause_world, two_windows, World
from .relay import FakeScheduler
from .test_car_ended_waits_for_level import _estimate, Car, TARGET

KEYS = ("car_ended_at", "car_ended_soc", "car_ended_vehicle", "car_ended_wait")
WAIT = {"target": TARGET, "level": 86.2, "estimated": True}


@pytest.fixture(params=[True, False], ids=["core", "previous"])
def drives(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", request.param)
    return request.param


async def _stored(world: World) -> dict[str, Any]:
    await world.hass.async_block_till_done()
    return await world.controller._store.async_load()  # noqa: SLF001


def _record(world: World) -> dict[str, Any]:
    """The car-ended record, through its accessor, as today's keys write it."""
    record = world.controller._car_ended  # noqa: SLF001 - the record, through its accessor
    return {
        "car_ended_at": None if record.at is None else record.at.isoformat(),
        "car_ended_soc": record.soc,
        "car_ended_vehicle": record.vehicle,
        "car_ended_wait": record.wait,
    }


def _agrees(world: World, drives: bool) -> dict[str, Any]:
    """The record the controller reads is the core's when it drives; the shadow saw no difference in it."""
    record = _record(world)
    shadow = world.controller.ownership_shadow
    if drives:
        ended = shadow.session.car_ended
        assert (None if ended is None else ended.at.isoformat()) == record["car_ended_at"]
        if ended is not None:
            assert (ended.soc, ended.vehicle) == (record["car_ended_soc"], record["car_ended_vehicle"])
            assert (None if ended.wait is None else ended.wait.to_dict()) == record["car_ended_wait"]
    assert shadow.counts["mirror_drift"] == 0
    return record


async def _plan_ended(hass: HomeAssistant, timers: FakeScheduler, car: Car) -> World:
    """The open window started the plan's charge and the car ended it by itself short of the target, on an estimate:
    the plan waits for the car's level."""
    world = await pause_world(hass, timers)
    world.controller._soc_reader = car  # noqa: SLF001
    world.controller._car_drawing = lambda: False  # type: ignore[method-assign]  # noqa: SLF001
    await install_schedule(world.controller, {**two_windows(), "target_soc_percent": TARGET})
    await hass.async_block_till_done()
    assert world.controller.charging
    await world.controller._async_plan_car_ended()  # noqa: SLF001 - the watch's five minutes at SuspendedEV
    await hass.async_block_till_done()
    return world


async def _next_window(world: World, freezer: Any) -> None:
    freezer.tick(timedelta(hours=2, minutes=1))
    await world.fire("_async_start_callback")


@pytest.mark.usefixtures("both_restarts")
async def test_the_plans_car_ended_record_survives_a_restart_whole(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any, drives: bool
) -> None:
    car = _estimate()
    world = await _plan_ended(hass, timers, car)
    record = _agrees(world, drives)
    assert record["car_ended_wait"] == WAIT and record["car_ended_soc"] is None, "an estimate is no level read"
    stored = await _stored(world)
    assert {key: stored[key] for key in KEYS} == record, "today's keys, written in both modes"
    if drives:
        kept = stored[SESSION_STORE_KEY]
        assert kept["version"] == STORED_VERSION == 2
        assert kept["car_ended"] == {"at": record["car_ended_at"], "soc": None, "vehicle": None, "wait": WAIT}
    else:
        assert SESSION_STORE_KEY not in stored

    restarted = await world.restart()
    restarted.controller._soc_reader = car  # noqa: SLF001
    assert _agrees(restarted, drives) == record
    await restarted.switch("off")  # the open window ends
    starts = len(restarted.starts)
    await _next_window(restarted, freezer)
    assert len(restarted.starts) == starts, "the plan still waits for the car's level after the restart"
    assert restarted.controller.car_ended_wait is not None
    await restarted.shutdown()


async def test_a_level_read_below_the_target_ends_the_wait_and_keeps_the_end(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any, drives: bool
) -> None:
    car = _estimate()
    world = await _plan_ended(hass, timers, car)
    ended_at = _agrees(world, drives)["car_ended_at"]
    await world.switch("off")
    freezer.tick(timedelta(minutes=30))
    car.reads(80.0)

    await world.controller._async_enforce_target()  # noqa: SLF001 - the reading's listener
    record = _agrees(world, drives)
    assert record["car_ended_wait"] is None and record["car_ended_at"] == ended_at
    stored = await _stored(world)
    assert stored["car_ended_wait"] is None and stored["car_ended_at"] == ended_at
    if drives:
        kinds = [item["event"]["kind"] for item in world.controller.ownership_shadow.events]
        assert "car_level_read" in kinds

    starts = len(world.starts)
    await _next_window(world, freezer)
    assert len(world.starts) == starts + 1, "the car is charged as planned again"
    await world.shutdown()


@pytest.mark.parametrize("how", ["unplug", "replug"])
async def test_the_record_ends_with_its_plug_in(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool, how: str
) -> None:
    world = await _plan_ended(hass, timers, _estimate())
    assert _agrees(world, drives)["car_ended_at"] is not None
    await world.plug.set(False, control="off")
    if how == "replug":
        await world.plug.set(True)
    record = _agrees(world, drives)
    assert record["car_ended_at"] is None and record["car_ended_wait"] is None
    stored = await _stored(world)
    assert (stored["car_ended_at"], stored["car_ended_wait"]) == (None, None), "saved at once, derived"
    if drives:
        assert stored[SESSION_STORE_KEY]["car_ended"] is None
    await world.shutdown()


async def test_a_persons_start_overrules_what_the_car_ended(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    world = await _plan_ended(hass, timers, _estimate())
    await world.switch("off")
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    assert _agrees(world, drives)["car_ended_at"] is None
    assert (await _stored(world))["car_ended_at"] is None
    await world.shutdown()


async def test_the_car_ending_a_persons_charge_keeps_its_level_and_its_car(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """The car ended a person's Start by itself: their pause ends, and the record keeps the level read then, so a
    later window of the plug-in skips a car known full."""
    world = await pause_world(hass, timers)
    world.controller._soc_reader = Car(100.0, estimated=False, read_at=dt_util.utcnow())  # noqa: SLF001
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    assert world.pause.manual

    await world.executor._async_manual_charge_ended()  # noqa: SLF001 - the watch saw the car end it
    await hass.async_block_till_done()
    assert not world.pause.manual
    record = _agrees(world, drives)
    assert record["car_ended_at"] is not None and record["car_ended_soc"] == 100.0 and record["car_ended_wait"] is None
    stored = await _stored(world)
    assert {key: stored[key] for key in KEYS} == record
    await world.shutdown()


async def test_the_connection_is_the_sessions_while_the_core_drives(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    world = await pause_world(hass, timers, connected=False)
    controller = world.controller
    shadow = controller.ownership_shadow
    assert controller.known_connected is False and shadow.session.plugged is False
    for connected in (True, False, True):
        await world.plug.set(connected)
        assert controller.known_connected is connected
        assert shadow.session.plugged is connected
    assert controller.plugged_in_since is not None
    # A status that says nothing keeps the last connection stated.
    await world.plug.set(None)
    assert controller.known_connected is True and shadow.session.plugged is True
    assert shadow.counts["mirror_drift"] == 0

    restarted = await world.restart()
    assert restarted.controller.ownership_shadow.session.plugged is None, "a restart clears it"
    assert restarted.controller.known_connected is None
    await restarted.plug.set(False)
    assert restarted.controller.known_connected is False
    await restarted.shutdown()


@pytest.mark.usefixtures("both_restarts")
async def test_an_older_build_reads_the_keys_when_it_cannot_read_the_record(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that knows only version 1 of the session's record refuses version 2 and reads today's keys, which are
    written beside it, derived from the session: the record comes back whole, and is written again."""
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", True)
    world = await _plan_ended(hass, timers, _estimate())
    record = _agrees(world, True)
    assert (await _stored(world))[SESSION_STORE_KEY]["version"] == 2

    monkeypatch.setattr(session_module, "READ_VERSIONS", (1,))
    restarted = await world.restart()
    assert _agrees(restarted, True) == record
    assert restarted.controller.ownership_shadow.counts["disagreements"] == 0
    await restarted.shutdown()


@pytest.mark.usefixtures("both_restarts")
async def test_a_version_1_record_is_read_with_the_instant_and_the_keys_fill_nothing_in(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record an earlier build wrote (version 1) keeps only when the car ended: read as a record of that instant
    alone (its level, car and wait not known), and written again as version 2."""
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", True)
    world = await pause_world(hass, timers, plan=two_windows())
    controller = world.controller
    ended = dt_util.utcnow() - timedelta(minutes=5)
    current = controller.ownership_shadow.session.to_store()
    v1 = {key: value for key, value in current.items() if key != "car_ended"}
    v1.update(version=1, car_ended_at=ended.isoformat())
    controller._cancel_session_save()  # noqa: SLF001
    disk = dict(await controller._store.async_load())  # noqa: SLF001
    disk[SESSION_STORE_KEY] = v1
    disk.pop("charge_session_order", None)
    await controller._store.async_save(disk)  # noqa: SLF001
    controller._session_saved = ChargeSession.from_store(v1)  # noqa: SLF001 - the old controller saves nothing more
    controller.ownership_shadow.session = controller._session_saved  # noqa: SLF001

    restarted = await world.restart()
    session = restarted.controller.ownership_shadow.session
    assert session.car_ended == CarEnded(at=ended)
    assert restarted.controller.car_ended_at == ended
    await restarted.controller.async_save_record()
    stored = await _stored(restarted)
    assert stored[SESSION_STORE_KEY]["version"] == 2
    assert stored[SESSION_STORE_KEY]["car_ended"] == {
        "at": ended.isoformat(), "soc": None, "vehicle": None, "wait": None
    }
    assert stored["car_ended_at"] == ended.isoformat()
    await restarted.shutdown()


def test_the_record_is_compared_as_one_with_one_instant_off_two_clocks() -> None:
    """A difference in the record (`CAR_ENDED_AND_PLUG`) is mirror drift but for its instant read off two clocks."""
    from custom_components.spotnav.core import events as ev

    now = dt_util.utcnow()
    record = CarEnded(at=now, soc=90.0, vehicle="car", wait=CarEndedWait(target=91.0))
    today = {"session": ChargeSession(plugged=True, car_ended=record)}
    shadow = ownership_shadow.OwnershipShadow(
        lambda: today["session"], owned_fields=ownership_shadow.CAR_ENDED_AND_PLUG
    )
    shadow.session = today["session"]
    later = CarEnded(at=now + timedelta(seconds=1), soc=90.0, vehicle="car", wait=CarEndedWait(target=91.0))
    today["session"] = today["session"].with_changes(car_ended=later)
    shadow.feed(ev.ConnectionUnknown())
    assert shadow.counts["mirror_drift"] == 0, "the same record, its instant a second apart"
    assert ownership_shadow._same(record, record) and not ownership_shadow._same(record, None)  # noqa: SLF001


@pytest.mark.shadow_disagreement_expected
async def test_the_record_and_the_connection_are_kept_as_the_core_decided_them_and_a_difference_is_mirror_drift(
    hass: HomeAssistant,
) -> None:
    from custom_components.spotnav.core import events as ev

    ended = CarEnded(at=dt_util.utcnow(), soc=90.0, vehicle="car")
    today = {"session": ChargeSession(plugged=True, car_ended=ended)}
    heard: list[str] = []
    ownership_shadow.LISTENERS.append(lambda kind, record: heard.append(kind))
    try:
        shadow = ownership_shadow.OwnershipShadow(
            lambda: today["session"], owned_fields=ownership_shadow.CAR_ENDED_AND_PLUG
        )
        shadow.session = today["session"]
        shadow.feed(ev.ConnectionUnknown())
        assert heard == [] and shadow.counts["mirror_drift"] == 0
        today["session"] = today["session"].with_changes(car_ended=None, plugged=False)
        shadow.feed(ev.ConnectionUnknown())
    finally:
        ownership_shadow.LISTENERS.pop()
    assert heard == ["mirror_drift"] and shadow.counts["mirror_drift"] == 1
    assert (shadow.session.car_ended, shadow.session.plugged) == (ended, True), "never taken"
    (record,) = [item for item in shadow.diagnostics()["drift"] if item.get("mirror")]
    assert set(record["fields"]) == {"car_ended", "plugged"}
    assert record["fields"]["car_ended"]["core"] == ended.to_dict()


async def test_a_car_ended_plug_in_sessions_recording_replays_with_no_line_up(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any, drives: bool
) -> None:
    """Every change of the car-ended record and the connection is fed: a recording of the plan's charge the car ended
    on an estimate, a level read below the target ending the wait, the next window, a person's Start, the unplug and
    a new plug-in replays from its first session alone, with no difference beyond observation drift."""
    import json

    from custom_components.spotnav.core.replay import replay_bundle

    from .test_core_replay import _bundle

    car = _estimate()
    world = await _plan_ended(hass, timers, car)
    await world.switch("off")
    freezer.tick(timedelta(minutes=30))
    car.reads(80.0)
    await world.controller._async_enforce_target()  # noqa: SLF001 - the reading's listener
    await _next_window(world, freezer)
    await world.executor.async_manual_start()
    await world.plug.set(False, control="off")
    await world.plug.set(True)
    await hass.async_block_till_done()
    diagnostics = world.controller.ownership_shadow.diagnostics()
    kinds = {item["event"]["kind"] for item in diagnostics["events"]}
    assert {"car_ended", "car_level_read", "unplug", "plug_in"} <= kinds
    bundle = json.loads(json.dumps(_bundle(diagnostics["events"]), default=str))
    alone = replay_bundle(bundle, line_up=False)["charger_a"]
    assert alone.events == len(diagnostics["events"]) and alone.skipped == 0
    assert alone.real == (), alone.real
    assert replay_bundle(bundle)["charger_a"].mismatches == ()
    await world.shutdown()
