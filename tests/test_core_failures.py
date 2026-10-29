"""A charging decision the core could not take is surfaced, never silent (`OwnershipShadow.failed`): the core raised,
its facts could not be read, or its stored session could not be read. The previous logic decides that event, as
ever. While the core drives: a warning once per (charger, where, kind) per run, then only a count (diagnostics and
the debug bundle); a Repairs issue only when three fall within a day on one charger, gone after a day without one
and at the charger's unload. While it only shadows (the previous logic set): a debug line and a count, no issue."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_fire_time_changed, MockConfigEntry

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.execution.controller import SESSION_STORE_KEY
from custom_components.spotnav.repairs import (
    async_sync_resolution_repairs,
    CORE_DECISION_FAILED_ISSUE,
    core_decision_issue_id,
)

from .pause_world import ENTRY, open_window, pause_world, World
from .helpers import install_schedule
from .relay import FakeScheduler

ISSUE = core_decision_issue_id(ENTRY)
TITLE = "Garage charger"

#: Every test here makes the core fail on purpose: the shadow's errors are what is tested, not a regression.
pytestmark = pytest.mark.shadow_disagreement_expected


@pytest.fixture(params=[True, False], ids=["drives", "shadows"])
def drives(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    """The core drives (the default), or the charger keeps the previous logic (`SPOTNAV_CORE_OWNERSHIP=0`)."""
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", request.param)
    return request.param


@pytest.fixture
def driving(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", True)


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
    assert len(world.starts) == before + 1, "the previous logic's start is still sent"
    await world.executor.async_manual_stop()
    await world.hass.async_block_till_done()


async def _later(hass: HomeAssistant, freezer: Any, delta: timedelta) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------------------------- the core raises


async def test_a_failing_decision_warns_once_and_raises_the_issue_at_the_third(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    drives: bool,
) -> None:
    _break(monkeypatch, "person_start")
    world = await _world(hass, timers)
    shadow = world.controller.ownership_shadow
    assert shadow.drives is drives

    await _person_start(world)
    await _person_start(world)
    assert _issue(hass) is None, "one or two failures are a log line and a count only"
    await _person_start(world)

    # Driving, the core decides a person's Start before anything acts; shadowing, after today's code did.
    where = "decide_early" if drives else "decide"
    assert shadow.counts["failures"] == {f"{where}/person_start": 3}
    assert shadow.counts["last_failure"] is not None
    assert shadow.diagnostics()["counts"]["failures"] == {f"{where}/person_start": 3}
    if drives:
        (warning,) = _warnings(caplog)
        assert ENTRY in warning.getMessage() and warning.exc_info is not None
        issue = _issue(hass)
        assert issue is not None
        assert issue.translation_key == CORE_DECISION_FAILED_ISSUE
        assert issue.translation_placeholders == {"charger": TITLE}
        assert issue.severity == ir.IssueSeverity.WARNING and not issue.is_fixable
    else:
        assert _warnings(caplog) == [], "the previous logic decides: the core's failure is a debug line"
        assert _issue(hass) is None, "no Repairs for a core that only shadows"
    await world.shutdown()


@pytest.mark.usefixtures("driving")
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


@pytest.mark.usefixtures("driving")
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


@pytest.mark.usefixtures("driving")
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


@pytest.mark.usefixtures("driving")
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


@pytest.mark.usefixtures("driving")
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


async def test_facts_that_cannot_be_read_are_a_failure(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    drives: bool,
) -> None:
    """A window start whose facts cannot be read is decided by the previous logic: the start goes out (once at the
    window's start, then at each plug-in inside it)."""
    world = await _world(hass, timers)

    def unreadable() -> dict[str, Any]:
        raise RuntimeError("the start's facts cannot be read")

    monkeypatch.setattr(world.controller, "_shadow_start_facts", unreadable)
    await install_schedule(world.controller, open_window())
    await hass.async_block_till_done()
    assert len(world.starts) == 1, "the previous logic starts the window"
    for round_ in range(2):
        assert _issue(hass) is None
        await world.plug.set(False, control="off")
        await world.plug.set(True)
        await hass.async_block_till_done()
        assert len(world.starts) == round_ + 2, "the previous logic starts the window at the plug-in"

    shadow = world.controller.ownership_shadow
    assert shadow.counts["failures"] == {"facts/window_start": 3}
    assert (len(_warnings(caplog)) == 1) is drives
    assert (_issue(hass) is not None) is drives
    await world.shutdown()


# ---------------------------------------------------------------------------------------------- the stored session


@pytest.mark.usefixtures("driving")
async def test_a_stored_session_from_a_later_release_is_no_failure(
    hass: HomeAssistant, timers: FakeScheduler, caplog: pytest.LogCaptureFixture
) -> None:
    """After a rollback the later release's record is expected: read from today's keys, with no warning, count or
    issue, however often it restarts."""
    world = await _world(hass, timers)
    await world.executor.async_manual_stop()
    world.controller._cancel_session_save()  # noqa: SLF001
    record = dict(await world.controller._store.async_load())  # noqa: SLF001
    record[SESSION_STORE_KEY] = {**record[SESSION_STORE_KEY], "version": 2, "car_ended": None}
    await world.controller._store.async_save(record)  # noqa: SLF001

    restarted = await world.restart()
    shadow = restarted.controller.ownership_shadow
    assert shadow.counts.get("failures", {}) == {}
    assert _warnings(caplog) == []
    assert shadow.session.manual is not None, "the person's Stop is read from today's keys"
    assert _issue(hass) is None
    await restarted.shutdown()


@pytest.mark.usefixtures("driving")
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
