"""The charge-ownership core's shadow (`execution/ownership_shadow.py`) and the replay of what it recorded
(`core/replay.py`): a synthetic debug bundle, a real charger's recording, and the shadow's own guarantees (bounded,
never raising into the real path, a disagreement reported)."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.core import events as ev
from custom_components.spotnav.core.replay import bundle_events, expand, main, replay, replay_bundle
from custom_components.spotnav.core.session import ChargeSession, ManualPause
from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.execution.ownership_shadow import (
    CommandOutcome,
    DISAGREEMENT_RING,
    EVENT_RING,
    OwnershipShadow,
)

from .pause_world import pause_world, two_windows
from .relay import FakeScheduler

AT = "2026-10-04T22:00:00+00:00"


def _record(event: ev.Event, *, core=(), today=(), result=None, pre=None, after=None, **extra):
    record = {"at": AT, "event": event.to_dict(), "core": [{"kind": kind} for kind in core], "today": list(today)}
    if result is not None:
        record["result"] = result.to_dict()
    if pre is not None:
        record["pre"] = pre
    if after is not None:
        record["today_after"] = after
    record.update(extra)
    return record


def _bundle(records) -> dict:
    return {
        "bundle_version": 5,
        "chargers": [
            {"entry_id": "charger_a", "diagnostics": {"controller": {"ownership_shadow": {"events": records}}}},
            {"entry_id": "charger_b", "diagnostics": {"controller": None}},
        ],
    }


STOPPED = {"owner": "none", "manual": {"action": "stop", "scope": "plug_in"}, "span_pause": None}


def _synthetic() -> list[dict]:
    """A person's Stop, a charge the charger begins under it (C7) whose stop goes out, and the unplug."""
    return [
        _record(
            ev.PersonStop(connected=True),
            core=["stop"],
            today=["stop"],
            result=ev.CommandResult(command="stop", reason="person", executed=True),
            pre={"version": 1, "plugged": True, "owner": "plan"},
            after=STOPPED,
        ),
        _record(
            ev.ChargerReportedOn(charging=True, was_on=False),
            core=["stop"],
            today=["stop"],
            pre={"version": 1, "plugged": True, "manual": {"action": "stop", "scope": "plug_in"}},
            after={**STOPPED, "owner": "charger_self"},
        ),
        _record(
            ev.CommandResult(command="stop", reason="person_hold", executed=True),
            after=STOPPED,
        ),
        _record(ev.Unplug(previous=True), after={"owner": "none", "manual": None, "span_pause": None}),
    ]


def test_a_synthetic_bundle_replays_as_recorded() -> None:
    bundle = json.loads(json.dumps(_bundle(_synthetic())))
    assert set(bundle_events(bundle)) == {"charger_a", "charger_b"}
    reports = replay_bundle(bundle)
    report = reports["charger_a"]
    assert report.events == 4 and report.mismatches == ()
    assert report.session.manual is None and report.session.owner == "none"
    assert reports["charger_b"].events == 0


def test_a_replay_says_where_the_core_or_todays_code_differs() -> None:
    records = _synthetic()
    records[1]["today"] = []
    records[1]["core"] = []
    records[3]["today_after"] = STOPPED
    report = replay(records)
    assert [(item.index, item.against) for item in report.differs_from_today] == [(1, "today"), (3, "today")]
    assert [(item.index, item.kind) for item in report.differs_from_recording] == [(1, "charger_reported_on")]


def test_a_queued_event_is_compared_only_through_the_state_after_it() -> None:
    records = [
        _record(
            ev.PersonStart(connected=True),
            core=["start"],
            today=["start"],
            result=ev.CommandResult(command="start", reason="person", executed=True),
            pre={"version": 1, "plugged": True},
            after={"owner": "none", "manual": {"action": "start", "scope": "plug_in"}, "span_pause": None},
        ),
        # Today's code decided this report mid-way through the Start, and spawned a stop it then dropped.
        _record(ev.ChargerReportedOff(notified=True), today=["stop"], queued=True, unchecked=True),
    ]
    assert replay(records).mismatches == ()


def test_the_compact_session_expands() -> None:
    assert expand({"version": 1, "manual": {"action": "stop", "scope": "next_plug_in"}}) == ChargeSession(
        manual=ManualPause("stop", "next_plug_in")
    )


def test_the_replay_command_line(tmp_path, capsys) -> None:
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(_bundle(_synthetic())), encoding="utf-8")
    assert main([str(path)]) == 0
    assert "charger_a: 4 events, 0 differ from the recording, 0 from today's code" in capsys.readouterr().out
    assert main([]) == 2


async def test_a_real_chargers_recording_replays_without_a_difference(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_stop()
    await world.switch("on")
    await world.executor.async_manual_start()
    await world.executor.async_manual_stop()
    await world.plug.set(False)
    await hass.async_block_till_done()
    diagnostics = world.controller.ownership_shadow.diagnostics()
    assert diagnostics["counts"]["events"] > 5 and diagnostics["counts"]["disagreements"] == 0
    bundle = json.loads(json.dumps(_bundle(diagnostics["events"]), default=str))
    report = replay_bundle(bundle)["charger_a"]
    assert report.events == len(diagnostics["events"])
    assert report.mismatches == ()
    await world.shutdown()


# ---------------------------------------------------------------------------------------------- the shadow itself


@pytest.mark.shadow_disagreement_expected
async def test_a_disagreement_is_reported_and_kept_with_the_events_before_it(hass: HomeAssistant) -> None:
    today = ChargeSession(owner="plan")
    heard: list[str] = []
    ownership_shadow.LISTENERS.append(lambda kind, record: heard.append(kind))
    try:
        shadow = OwnershipShadow(lambda: today, now=lambda: datetime(2026, 10, 4, 22, tzinfo=timezone.utc))
        shadow.session = today
        token = shadow.begin()
        # Today's code says it stopped, but its owner stays the plan's: the core says nobody's.
        shadow.end(token, ev.WindowEnd(), legacy=("stop",), outcome=CommandOutcome(True))
    finally:
        ownership_shadow.LISTENERS.pop()
    assert heard == ["disagreement"]
    (disagreement,) = shadow.diagnostics()["disagreements"]
    assert disagreement["where"] == "window_end"
    assert disagreement["fields"] == {"owner": {"core": "none", "today": "plan"}}
    assert disagreement["events"][-1]["event"]["kind"] == "window_end"
    assert shadow.session.owner == "plan", "today's state stays the truth"


@pytest.mark.shadow_disagreement_expected
async def test_the_shadow_never_raises_into_the_real_path_and_stays_bounded(hass: HomeAssistant) -> None:
    def broken() -> ChargeSession:
        raise RuntimeError("today's state could not be read")

    shadow = OwnershipShadow(broken)
    shadow.feed(ev.WindowEnd())
    assert shadow.counts["errors"] >= 1
    today = ChargeSession(owner="plan")
    shadow = OwnershipShadow(lambda: today)
    for _ in range(EVENT_RING + 50):
        shadow.end(shadow.begin(), ev.WindowEnd(), legacy=("stop",), outcome=CommandOutcome(True))
    diagnostics = shadow.diagnostics()
    assert len(diagnostics["events"]) == EVENT_RING
    assert len(diagnostics["disagreements"]) == DISAGREEMENT_RING
    assert diagnostics["counts"]["disagreements"] == EVENT_RING + 50
    json.dumps(diagnostics)


async def test_a_feed_inside_another_is_decided_after_it(hass: HomeAssistant) -> None:
    """A report that lands while a person's Start awaits its command is decided after the Start."""
    state = {"session": ChargeSession(plugged=True, manual=ManualPause("stop", "plug_in"))}
    shadow = OwnershipShadow(lambda: state["session"])
    outer = shadow.begin()
    inner = shadow.begin()
    shadow.end(inner, ev.ChargerReportedOn(charging=True, was_on=True), legacy=("stop",))
    state["session"] = ChargeSession(plugged=True, owner="person", manual=ManualPause("start", "plug_in"))
    shadow.end(outer, ev.PersonStart(connected=True), legacy=("start",), outcome=CommandOutcome(True))
    events = shadow.diagnostics()["events"]
    assert [record["event"]["kind"] for record in events] == ["person_start", "charger_reported_on"]
    assert events[1]["queued"] and events[1]["core"] == []
    assert shadow.counts["disagreements"] == 0


async def test_a_connection_read_that_raises_at_start_up_leaves_no_feed_open(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """The first connection read after a restart is taken before the shadow's feed opens: one that raises leaves
    nothing open behind it, so later feeds are still compared."""
    world = await pause_world(hass, timers, plan=two_windows())
    controller = world.controller
    shadow = controller.ownership_shadow
    real = controller.adapter.vehicle_connected
    cancel, controller._progress_listener_cancel = controller._progress_listener_cancel, None  # noqa: SLF001

    def broken() -> bool | None:
        raise RuntimeError("the connection could not be read")

    controller.adapter.vehicle_connected = broken
    try:
        with pytest.raises(RuntimeError):
            controller._async_arm_progress_listener()  # noqa: SLF001 - the start-up read under test
    finally:
        controller.adapter.vehicle_connected = real
    assert shadow.depth == 0 and not shadow._open  # noqa: SLF001
    leftover = controller._progress_listener_cancel  # noqa: SLF001
    if leftover is not None:
        leftover()
    controller._progress_listener_cancel = cancel  # noqa: SLF001
    compared = shadow.counts["compared"]
    await world.executor.async_manual_stop()
    assert shadow.counts["compared"] > compared
    await world.shutdown()


async def test_a_report_that_decides_nothing_keeps_the_plug_in_and_the_persons_actions_in_the_ring(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """A charger that reports its control every few seconds with nothing new: those reports are not recorded, so
    the event ring still holds the plug-in and the person's Stop after more of them than the ring holds."""
    from .pause_world import SWITCH

    world = await pause_world(hass, timers, plan=two_windows())
    await world.plug.set(False)
    await world.plug.set(True)
    await world.executor.async_manual_stop()
    shadow = world.controller.ownership_shadow
    for index in range(EVENT_RING + 20):
        hass.states.async_set(SWITCH, "off", {"report": index})
    await hass.async_block_till_done()
    kinds = [record["event"]["kind"] for record in shadow.diagnostics()["events"]]
    assert "plug_in" in kinds and "person_stop" in kinds, kinds
    assert shadow.counts["quiet"] >= EVENT_RING
    assert shadow.counts["disagreements"] == 0
    report = replay_bundle(json.loads(json.dumps(_bundle(shadow.diagnostics()["events"]), default=str)))["charger_a"]
    assert report.mismatches == ()
    await world.shutdown()


@pytest.mark.shadow_disagreement_expected
async def test_a_quiet_report_a_disagreement_comes_of_is_kept(hass: HomeAssistant) -> None:
    state = {"session": ChargeSession(plugged=True)}
    shadow = OwnershipShadow(lambda: state["session"], now=lambda: datetime(2026, 10, 4, 22, tzinfo=timezone.utc))
    shadow.session = state["session"]
    shadow.feed(ev.ChargerReportedOff(notified=False))
    assert shadow.counts["quiet"] == 1 and not shadow.diagnostics()["events"]
    token = shadow.begin()
    state["session"] = ChargeSession(plugged=True, owner="plan")
    shadow.end(token, ev.ChargerReportedOff(notified=False))
    (disagreement,) = shadow.diagnostics()["disagreements"]
    assert [record["event"]["kind"] for record in disagreement["events"]] == ["charger_reported_off"]
    assert len(shadow.diagnostics()["events"]) == 1
