"""A charging decision the core could not take is surfaced, never silent (`OwnershipShadow.failed`): the core raised,
its facts could not be read, or its stored session could not be read. Nothing automatic is sent for that event (the
core's `Keep`), and the next event decides again; a person's own command still goes out. A warning once per
(charger, where, kind) per run, then only a count (diagnostics and the debug bundle); a Repairs issue only when three
fall within a day on one charger, gone after a day without one and at the charger's unload."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_fire_time_changed, MockConfigEntry

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.core.session import STORED_VERSION
from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.execution.controller import SESSION_STORE_KEY
from custom_components.spotnav.repairs import (
    async_sync_resolution_repairs,
    CORE_DECISION_FAILED_ISSUE,
    core_decision_issue_id,
)

from .pause_world import ENTRY, open_window, pause_world, SWITCH, World
from .helpers import install_schedule
from .relay import FakeScheduler

ISSUE = core_decision_issue_id(ENTRY)
TITLE = "Garage charger"

#: Every test here makes the core fail on purpose: the shadow's errors are what is tested, not a regression.
pytestmark = pytest.mark.core_failure_expected


def _break(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """The core raises for every event of `kind`, and decides every other as ever."""
    real = ownership_shadow.decide

    def broken(session: Any, event: Any, now: Any) -> Any:
        if event.kind == kind:
            raise RuntimeError(f"the core cannot decide {kind}")
        return real(session, event, now)

    monkeypatch.setattr(ownership_shadow, "decide", broken)


async def _world(hass: HomeAssistant, timers: FakeScheduler, **kwargs: Any) -> World:
    MockConfigEntry(domain=DOMAIN, entry_id=ENTRY, title=TITLE).add_to_hass(hass)
    return await pause_world(hass, timers, **kwargs)


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "charging decision could not be taken" in record.getMessage()
    ]


async def _person_start(world: World) -> None:
    """A person's Start, then their Stop: one `person_start` feed, whose start goes out."""
    before = len(world.starts)
    await world.executor.async_manual_start()
    await world.hass.async_block_till_done()
    assert len(world.starts) == before + 1, "a person's own command still goes out"
    await world.executor.async_manual_stop()
    await world.hass.async_block_till_done()


async def _later(hass: HomeAssistant, freezer: Any, delta: timedelta) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------------------------- the core raises


async def test_a_failing_decision_warns_once_and_raises_the_issue_at_the_third(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _break(monkeypatch, "person_start")
    world = await _world(hass, timers)
    shadow = world.controller.ownership_shadow

    await _person_start(world)
    await _person_start(world)
    assert _issue(hass) is None, "one or two failures are a log line and a count only"
    await _person_start(world)

    # The core decides a person's Start before anything acts.
    assert shadow.counts["failures"] == {"decide_early/person_start": 3}
    assert shadow.counts["last_failure"] is not None
    assert shadow.diagnostics()["counts"]["failures"] == {"decide_early/person_start": 3}
    (warning,) = _warnings(caplog)
    assert ENTRY in warning.getMessage() and warning.exc_info is not None
    assert "nothing automatic was sent" in warning.getMessage()
    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == CORE_DECISION_FAILED_ISSUE
    assert issue.translation_placeholders == {"charger": TITLE}
    assert issue.severity == ir.IssueSeverity.WARNING and not issue.is_fixable
    await world.shutdown()


async def test_failures_spread_over_more_than_a_day_raise_no_issue(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    _break(monkeypatch, "person_start")
    world = await _world(hass, timers)
    await _person_start(world)
    await _later(hass, freezer, timedelta(hours=13))
    await _person_start(world)
    await _later(hass, freezer, timedelta(hours=13))
    await _person_start(world)
    assert world.controller.ownership_shadow.counts["failures"] == {"decide_early/person_start": 3}
    assert _issue(hass) is None, "never three within one day"
    await _person_start(world)
    assert _issue(hass) is not None, "the last three fall within a day"
    await world.shutdown()


async def test_the_issue_goes_after_a_day_without_failures(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    _break(monkeypatch, "person_start")
    world = await _world(hass, timers)
    for _ in range(3):
        await _person_start(world)
    assert _issue(hass) is not None
    await _later(hass, freezer, timedelta(hours=23))
    assert _issue(hass) is not None, "still within the day after the last failure"
    await _later(hass, freezer, timedelta(hours=2))
    assert _issue(hass) is None
    # The count starts again: one more failure raises nothing.
    await _person_start(world)
    assert _issue(hass) is None
    await world.shutdown()


async def test_an_issue_left_from_before_a_crash_goes_when_the_charger_starts(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    ir.async_create_issue(
        hass, DOMAIN, ISSUE, is_fixable=False, severity=ir.IssueSeverity.WARNING,
        translation_key=CORE_DECISION_FAILED_ISSUE, translation_placeholders={"charger": TITLE},
    )
    world = await _world(hass, timers)
    assert _issue(hass) is None, "nothing in memory would ever end it"
    await world.shutdown()


async def test_the_issue_goes_at_a_reload_and_survives_a_resync(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    _break(monkeypatch, "person_start")
    world = await _world(hass, timers)
    for _ in range(3):
        await _person_start(world)
    await async_sync_resolution_repairs(hass)
    assert _issue(hass) is not None, "the decision issues' sync leaves it alone"
    restarted = await world.restart()
    assert _issue(hass) is None, "the charger's unload deletes it"
    # The timer went with it: nothing fires a day later.
    await _later(hass, freezer, timedelta(hours=25))
    assert _issue(hass) is None
    await restarted.shutdown()


async def test_the_issue_goes_when_its_entry_is_removed(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break(monkeypatch, "person_start")
    world = await _world(hass, timers)
    for _ in range(3):
        await _person_start(world)
    await async_sync_resolution_repairs(hass, (ENTRY,))
    assert _issue(hass) is None
    await world.shutdown()


# ---------------------------------------------------------------------------------------------- facts unreadable


async def test_facts_that_cannot_be_read_are_a_failure_and_send_nothing(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A window start whose facts cannot be read is the core's `Keep`: no start goes out (at the window's start, nor
    at each plug-in inside it), the session is left as it was, and the window starts once the facts can be read."""
    world = await _world(hass, timers)
    real = world.controller._shadow_start_facts  # noqa: SLF001

    def unreadable() -> dict[str, Any]:
        raise RuntimeError("the start's facts cannot be read")

    monkeypatch.setattr(world.controller, "_shadow_start_facts", unreadable)
    await install_schedule(world.controller, open_window())
    await hass.async_block_till_done()
    assert world.starts == [], "nothing is started for an event the core could not decide"
    for _ in range(2):
        assert _issue(hass) is None
        await world.plug.set(False, control="off")
        await world.plug.set(True)
        await hass.async_block_till_done()
        assert world.starts == [], "nor at the plug-in"
    shadow = world.controller.ownership_shadow
    assert shadow.counts["failures"] == {"facts/window_start": 3}
    assert shadow.session.owner == "none"
    assert len(_warnings(caplog)) == 1
    assert _issue(hass) is not None

    monkeypatch.setattr(world.controller, "_shadow_start_facts", real)
    await world.plug.set(False, control="off")
    await world.plug.set(True)
    await hass.async_block_till_done()
    assert len(world.starts) == 1, "decided again at the next event"
    assert world.controller.charge_origin == "plan_window"
    await world.shutdown()


# ---------------------------------------------------------------------------------------------- the stored session


async def test_a_stored_session_from_a_later_release_is_no_failure(
    hass: HomeAssistant, timers: FakeScheduler, caplog: pytest.LogCaptureFixture
) -> None:
    """After a rollback the later release's record is expected: read from today's keys, with no warning, count or
    issue, however often it restarts."""
    world = await _world(hass, timers)
    await world.executor.async_manual_stop()
    world.controller._cancel_session_save()  # noqa: SLF001
    record = dict(await world.controller._store.async_load())  # noqa: SLF001
    record[SESSION_STORE_KEY] = {**record[SESSION_STORE_KEY], "version": STORED_VERSION + 1, "car_ended": None}
    await world.controller._store.async_save(record)  # noqa: SLF001

    restarted = await world.restart()
    shadow = restarted.controller.ownership_shadow
    assert shadow.counts.get("failures", {}) == {}
    assert _warnings(caplog) == []
    assert shadow.session.manual is not None, "the person's Stop is read from today's keys"
    assert _issue(hass) is None
    await restarted.shutdown()


async def test_a_stored_session_that_cannot_be_read_is_a_failure(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Read from today's keys instead, with one warning and a count: one failure raises no issue, and two more in
    the same run do."""
    world = await _world(hass, timers)
    await world.executor.async_manual_stop()
    world.controller._cancel_session_save()  # noqa: SLF001
    record = dict(await world.controller._store.async_load())  # noqa: SLF001
    record[SESSION_STORE_KEY] = "garbled"
    await world.controller._store.async_save(record)  # noqa: SLF001

    restarted = await world.restart()
    shadow = restarted.controller.ownership_shadow
    assert shadow.counts["failures"] == {"stored_session/restart": 1}
    (warning,) = _warnings(caplog)
    assert "stored_session" in warning.getMessage()
    assert shadow.session.manual is not None, "the person's Stop is read from today's keys"
    assert _issue(hass) is None

    _break(monkeypatch, "person_start")
    await restarted.executor.async_manual_start()
    await hass.async_block_till_done()
    assert _issue(hass) is None
    await restarted.executor.async_manual_stop()
    await restarted.executor.async_manual_start()
    await hass.async_block_till_done()
    assert _issue(hass) is not None
    await restarted.shutdown()


@pytest.mark.parametrize("kind", ["unplug", "plug_in"])
async def test_a_plug_in_or_unplug_the_core_cannot_decide_is_fed_once_and_the_connection_still_moves(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """The connection is what the charger stated, an observation: it moves in the session also when the decision of
    the plug-in or the unplug fails, so the reports after it do not find it changed again and feed it once more (the
    controller reads the connection from the session)."""
    world = await _world(hass, timers, connected=kind == "unplug")
    controller = world.controller
    shadow = controller.ownership_shadow
    fed: list[str] = []
    real = controller._connection_changed  # noqa: SLF001

    def counting(previous: Any, connected: bool, **kwargs: Any) -> None:
        fed.append("unplug" if not connected else "plug_in")
        real(previous, connected, **kwargs)

    monkeypatch.setattr(controller, "_connection_changed", counting)
    _break(monkeypatch, kind)
    await world.plug.set(kind == "plug_in")
    for stamp in range(3):
        # Reports that state the same connection again.
        current = hass.states.get(SWITCH)
        hass.states.async_set(SWITCH, current.state, {**current.attributes, "stamp": stamp})
        await hass.async_block_till_done()

    assert fed == [kind], "fed once, not at every report after it"
    expected = kind == "plug_in"
    assert controller.known_connected is expected and shadow.session.plugged is expected
    failures = shadow.counts["failures"]
    assert sum(failures.values()) == 1 and all(label.endswith(f"/{kind}") for label in failures)
    await world.shutdown()


# ---------------------------------------------------------------------------------------------- the fuse


@pytest.mark.parametrize("code", ["pause", "safety_stop"])
async def test_load_balancings_stop_goes_out_when_the_core_cannot_decide(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """The site regulator's pause and its safety stop do not wait for the core: one the core cannot decide still
    stops the charge (the fuse is never left unprotected), and the failure is told."""
    world = await _world(hass, timers, plan=open_window())
    assert world.controller.charging
    stops = len(world.stops)
    _break(monkeypatch, "balancing_pause")
    result = await world.controller._regulated_stop(code)  # noqa: SLF001 - the regulator's own stop
    await hass.async_block_till_done()
    assert len(world.stops) == stops + 1 and not world.controller.charging
    assert result.code == code
    assert world.controller.ownership_shadow.counts["failures"] == {"decide_early/balancing_pause": 1}
    await world.shutdown()
