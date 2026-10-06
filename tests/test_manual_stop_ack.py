"""A person's Stop awaits the charger's report as a Start does.

The Stop itself is sent at once, exactly as before: nothing here delays or blocks it. What changes is
what is *offered* afterwards. Until the charger reports it has stopped (charging false), or one bounded
appointment passes, the immediate axis offers nothing with `action_pending` (and the automatic axis,
under the person's own pause, likewise), so a card or app keeps saying "Stopping…" instead of offering
Start beside a charger that still reports the charge. On the bound, or a Stop that failed, the axes
answer from the charger's state again: Stop once more while it still charges.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution import controller as controller_module
from custom_components.spotnav.execution.auto_execution import (
    CONTROL_ACTION_PENDING,
    EXECUTION_PAUSE_STOP_FAILED,
    MANUAL_START_ACK_TIMEOUT,
    AutoControlCommitted,
    AutoControlRefused,
)
from tests.harness import Session
from tests.relay import serve
from tests.test_manual_start_ack import RecordedStateListeners


@pytest.fixture
def state_reports(monkeypatch: pytest.MonkeyPatch) -> RecordedStateListeners:
    recorder = RecordedStateListeners()
    monkeypatch.setattr(controller_module, "async_track_state_change_event", recorder)
    return recorder


@pytest.fixture(autouse=True)
def frozen_real_clock(monkeypatch: pytest.MonkeyPatch, clock: Any) -> None:
    now = clock()
    monkeypatch.setattr(dt_util, "utcnow", lambda *_a, **_k: now)


def executor_of(session: Session) -> Any:
    assert session.executor is not None
    return session.executor


def preview_of(session: Session) -> Any:
    assert session.preview is not None
    return session.preview


async def charging_in_auto(session: Session) -> None:
    """One charger in Auto whose charge control reports charging; turn_off/turn_on change no state."""
    serve(session.transport)
    await session.set_auto()
    async_mock_service(session.hass, "switch", "turn_off")
    async_mock_service(session.hass, "switch", "turn_on")
    session.hass.states.async_set(session.charge_control, "on")
    await session.hass.async_block_till_done()
    assert session.controller.charging is True, "the prerequisite: the charger reports charging"
    assert executor_of(session).immediate_decision().action == "stop"


async def test_an_accepted_stop_is_pending_until_the_charger_reports_it_stopped(
    session: Session, state_reports: RecordedStateListeners, pause_appointments: Any
) -> None:
    await charging_in_auto(session)
    executor = executor_of(session)
    turn_off = async_mock_service(session.hass, "switch", "turn_off")

    decision = await preview_of(session).async_manual_action("stop")

    assert decision.action == "stop"
    assert len(turn_off) == 1, "the Stop went out at once"
    assert executor.manual_stop_pending is True
    assert executor.manual_start_pending is False
    assert state_reports.watching == [session.charge_control]
    assert pause_appointments.when == executor._now() + MANUAL_START_ACK_TIMEOUT
    axes = executor.control_axes()
    assert (axes.immediate.action, axes.immediate.reason) == ("none", CONTROL_ACTION_PENDING)
    assert (axes.automatic.action, axes.automatic.reason) == ("none", CONTROL_ACTION_PENDING)
    assert axes.automatic.choices == ()

    # A non-answer (still charging) changes nothing.
    state_reports.report(session.hass, session.charge_control, "on")
    await session.hass.async_block_till_done()
    assert executor.manual_stop_pending is True

    state_reports.report(session.hass, session.charge_control, "off")
    await session.hass.async_block_till_done()

    assert executor.manual_stop_pending is False
    assert state_reports.armed == [] and pause_appointments.armed == [], "nothing is left watching"
    axes = executor.control_axes()
    assert (axes.immediate.action, axes.immediate.reason) == ("start", None)
    assert axes.automatic.action == "resume", "the person's own pause, as before"
    assert executor.last_error is None


async def test_a_second_stop_while_one_is_pending_is_still_sent(
    session: Session, state_reports: RecordedStateListeners, pause_appointments: Any
) -> None:
    """Only what is offered changes: a person's Stop is never refused for a Stop already on its way."""
    await charging_in_auto(session)
    turn_off = async_mock_service(session.hass, "switch", "turn_off")
    await preview_of(session).async_manual_action("stop")

    again = await preview_of(session).async_manual_action("stop")

    assert again.action == "stop"
    assert len(turn_off) == 2
    assert executor_of(session).manual_stop_pending is True
    assert len(pause_appointments.armed) == 1, "one bound, re-armed rather than doubled"
    with pytest.raises(AutoControlRefused):
        await preview_of(session).async_manual_action("start")


async def test_the_bound_offers_stop_again_while_the_charger_still_charges(
    session: Session, state_reports: RecordedStateListeners, pause_appointments: Any
) -> None:
    await charging_in_auto(session)
    executor = executor_of(session)
    await preview_of(session).async_manual_action("stop")

    await pause_appointments.fire(pause_appointments.when)

    assert executor.manual_stop_pending is False
    assert state_reports.armed == [], "the watch ends with the bound"
    axes = executor.control_axes()
    assert (axes.immediate.action, axes.immediate.reason) == ("stop", None), "Stop is offered again"
    assert axes.automatic.action == "resume"
    turn_off = async_mock_service(session.hass, "switch", "turn_off")
    again = await preview_of(session).async_manual_action("stop")
    assert again.action == "stop" and len(turn_off) == 1


async def test_a_failed_stop_leaves_nothing_pending_and_offers_stop_again(
    session: Session, state_reports: RecordedStateListeners, pause_appointments: Any
) -> None:
    await charging_in_auto(session)
    executor = executor_of(session)

    async def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the switch did not answer")

    session.controller.async_stop = refuse  # type: ignore[method-assign]
    with pytest.raises(AutoControlCommitted):  # the person's pause is stored all the same
        await preview_of(session).async_manual_action("stop")

    assert executor.manual_stop_pending is False
    assert state_reports.armed == [] and pause_appointments.armed == []
    assert executor.last_error == EXECUTION_PAUSE_STOP_FAILED
    assert executor.immediate_decision().action == "stop", "Stop again is the retry"


async def test_a_stop_to_a_charger_already_off_is_not_pending(
    session: Session, state_reports: RecordedStateListeners, pause_appointments: Any
) -> None:
    """The charger has already answered: nothing to wait for."""
    await charging_in_auto(session)
    executor = executor_of(session)

    async def stop_and_report(*_args: Any, **_kwargs: Any) -> None:
        session.hass.states.async_set(session.charge_control, "off")

    session.controller.async_stop = stop_and_report  # type: ignore[method-assign]
    await preview_of(session).async_manual_action("stop")

    assert executor.manual_stop_pending is False
    assert state_reports.armed == [] and pause_appointments.armed == []
    assert executor.immediate_decision().action == "start"


async def test_a_pending_start_is_unchanged_and_a_stop_replaces_it(
    session: Session, state_reports: RecordedStateListeners, pause_appointments: Any
) -> None:
    serve(session.transport)
    await session.set_auto()
    async_mock_service(session.hass, "switch", "turn_on")
    turn_off = async_mock_service(session.hass, "switch", "turn_off")
    executor = executor_of(session)

    await preview_of(session).async_manual_action("start")

    assert executor.manual_start_pending is True and executor.manual_stop_pending is False
    axes = executor.control_axes()
    assert (axes.immediate.action, axes.immediate.reason) == ("none", CONTROL_ACTION_PENDING)
    assert (axes.automatic.action, axes.automatic.reason) == ("none", CONTROL_ACTION_PENDING)

    # The charger answers the Start, then the person stops: the Stop is the outstanding command now.
    state_reports.report(session.hass, session.charge_control, "on")
    await session.hass.async_block_till_done()
    assert executor.manual_start_pending is False
    await preview_of(session).async_manual_action("stop")
    assert len(turn_off) == 1
    assert executor.manual_stop_pending is True and executor.manual_start_pending is False


async def test_shutdown_drops_a_pending_stop(
    session: Session, state_reports: RecordedStateListeners, pause_appointments: Any
) -> None:
    await charging_in_auto(session)
    executor = executor_of(session)
    await preview_of(session).async_manual_action("stop")
    assert executor.manual_stop_pending is True

    await executor.async_shutdown()

    assert executor.manual_stop_pending is False
    assert state_reports.armed == [] and pause_appointments.armed == []


async def test_a_stop_while_a_start_awaits_its_report_ends_that_wait(
    session: Session, state_reports: RecordedStateListeners, pause_appointments: Any
) -> None:
    """A Stop is never held back by a Start on its way; the Start's wait ends with it."""
    serve(session.transport)
    await session.set_auto()
    async_mock_service(session.hass, "switch", "turn_on")
    async_mock_service(session.hass, "switch", "turn_off")
    executor = executor_of(session)
    await preview_of(session).async_manual_action("start")
    assert executor.manual_start_pending is True

    await executor.async_manual_stop()

    assert executor.pause_intent.action == "stop", "the person's Stop took effect"
    assert executor.manual_start_pending is False and executor.manual_stop_pending is False
    assert state_reports.armed == [] and pause_appointments.armed == [], "no watch is left behind"
    assert executor.immediate_decision().action == "start", "the charger reads off: the truthful offer"
