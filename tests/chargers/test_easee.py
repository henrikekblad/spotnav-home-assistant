"""Easee: its own start and stop service, the dynamic limit with its read-back, the 7 A start floor, the
pause that must not be lifted, plug-in resend and the waiting statuses.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_IGNORED_WHILE_PAUSED,
    ASSIGN_RATE_LIMITED,
    ASSIGN_UNCHANGED,
    WRITE_REGULATOR,
    WRITE_RESEND,
    WRITE_SESSION_START,
)
from custom_components.spotnav.flows.charger_detection import detect_charger

from ..charger_helpers import adapter_for, Clock, enable_easee_limit_sensor, set_easee_limit
from ..charger_shapes import register_shape, SHAPES


async def test_easee_starts_and_stops_through_its_own_service_and_never_writes_a_max_limit(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    found = detect_charger(hass, ids["device_id"])
    adapter = adapter_for(hass, found)
    commands = async_mock_service(hass, "easee", "action_command")
    flash = [
        async_mock_service(hass, "easee", name)
        for name in ("set_charger_max_limit", "set_circuit_max_limit", "set_charger_offline_limit")
    ]
    async_mock_service(hass, "easee", "set_charger_dynamic_limit")

    await adapter.async_start()
    await adapter.async_stop()
    await adapter.async_set_current(12, reason=WRITE_SESSION_START)

    assert [dict(call.data) for call in commands] == [
        {"device_id": ids["device_id"], "action_command": "resume"},
        {"device_id": ids["device_id"], "action_command": "pause"},
    ]
    assert all(calls == [] for calls in flash)


async def _easee(hass: HomeAssistant, clock: Clock):
    ids = register_shape(hass, SHAPES["easee"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=clock)
    limits = async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    return ids, adapter, limits


async def test_easee_sets_the_dynamic_limit_with_no_expiry(hass: HomeAssistant) -> None:
    ids, adapter, limits = await _easee(hass, Clock())

    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED

    assert [dict(call.data) for call in limits] == [
        {"device_id": ids["device_id"], "current": 12, "time_to_live": 0}
    ]
    assert adapter.policy.max_writes_per_minute == 20 and adapter.policy.resend_after_plug_in
    assert adapter.capabilities.regulated_current is True


async def test_easee_does_not_trust_its_own_memory_of_a_write_after_a_plug_in(hass: HomeAssistant) -> None:
    """With no read-back the last value sent may have been cleared by the charger since."""
    clock = Clock()
    ids, adapter, limits = await _easee(hass, clock)
    hass.states.async_set("sensor.easee_status", "charging")
    assert await adapter.async_set_current(12, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    clock.advance(1)

    assert await adapter.async_set_current(12, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED

    assert [call.data["current"] for call in limits] == [12, 12]


async def test_easee_skips_a_value_the_charger_already_reports(hass: HomeAssistant) -> None:
    ids, adapter, limits = await _easee(hass, Clock())
    enable_easee_limit_sensor(hass, "12")

    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_UNCHANGED
    assert limits == []


async def test_easee_never_exceeds_twenty_settings_changes_a_minute(hass: HomeAssistant) -> None:
    clock = Clock()
    ids, adapter, limits = await _easee(hass, clock)
    enable_easee_limit_sensor(hass, "99")

    outcomes = []
    for index in range(25):
        outcomes.append(await adapter.async_set_current(6 + (index % 2) * 2, reason=WRITE_REGULATOR))
        clock.advance(1)

    assert outcomes.count(ASSIGN_ASSIGNED) == 20
    assert outcomes[20:] == [ASSIGN_RATE_LIMITED] * 5
    assert len(limits) == 20
    clock.advance(60)
    assert await adapter.async_set_current(9, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED


async def test_easee_commands_count_against_the_budget_but_are_never_refused(hass: HomeAssistant) -> None:
    clock = Clock()
    ids, adapter, limits = await _easee(hass, clock)
    commands = async_mock_service(hass, "easee", "action_command")
    enable_easee_limit_sensor(hass, "99")

    for _ in range(30):
        await adapter.async_stop()

    assert len(commands) == 30
    # The status still says `charging` only because the pauses have not been reported yet: held.
    assert await adapter.async_set_current(9, reason=WRITE_REGULATOR) == ASSIGN_IGNORED_WHILE_PAUSED
    await adapter.async_start()
    assert len(commands) == 31
    # Resumed, and the commands have used up the minute's budget.
    assert await adapter.async_set_current(9, reason=WRITE_REGULATOR) == ASSIGN_RATE_LIMITED
    assert limits == []


async def test_easee_sends_the_limit_again_after_a_plug_in_and_a_reboot(hass: HomeAssistant) -> None:
    ids, adapter, limits = await _easee(hass, Clock())
    current = adapter.current

    assert current.needs_resend("disconnected", "awaiting_start") is True
    assert current.needs_resend("unavailable", "charging") is True  # a reboot
    assert current.needs_resend("awaiting_start", "charging") is False
    assert current.needs_resend("charging", "disconnected") is False


async def test_a_resend_reaches_the_charger_even_though_the_service_skips_an_unchanged_value(
    hass: HomeAssistant,
) -> None:
    """The service short-circuits on its cached value, so a resend of the same number would do
    nothing: it is preceded by one amp lower, the safe direction, and then the wanted value.
    """
    clock = Clock()
    ids, adapter, limits = await _easee(hass, clock)
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    limits.clear()
    clock.advance(5)

    from custom_components.spotnav.execution.chargers.base import WRITE_RESEND

    assert await adapter.async_set_current(12, reason=WRITE_RESEND) == ASSIGN_ASSIGNED

    assert [call.data["current"] for call in limits] == [11, 12]


async def test_an_easee_write_the_charger_never_reports_marks_the_cache_suspect(hass: HomeAssistant) -> None:
    clock = Clock()
    ids, adapter, limits = await _easee(hass, clock)
    enable_easee_limit_sensor(hass, "16")
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    limits.clear()

    # Well after the write the charger still reports 16: the service did nothing.
    clock.advance(60)
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert [call.data["current"] for call in limits] == [9, 10]


async def test_an_easee_limit_is_read_back_when_asked(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.spotnav.execution.chargers import easee

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(easee.asyncio, "sleep", no_wait)
    ids, adapter, limits = await _easee(hass, Clock())
    enable_easee_limit_sensor(hass, "16")

    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR, verify=True) == "unconfirmed"
    set_easee_limit(hass, "10")
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR, verify=True) == ASSIGN_UNCHANGED


async def _easee_commands(hass: HomeAssistant, clock: Clock | None = None):
    ids, adapter, limits = await _easee(hass, clock or Clock())
    commands = async_mock_service(hass, "easee", "action_command")
    return adapter, commands, limits


def _names(commands) -> list[str]:
    return [call.data["action_command"] for call in commands]


async def test_easee_start_from_awaiting_start_authorizes_then_resumes(hass: HomeAssistant) -> None:
    adapter, commands, _ = await _easee_commands(hass)
    hass.states.async_set("sensor.easee_status", "awaiting_start")

    await adapter.async_start()

    assert _names(commands) == ["start", "resume"]


async def test_easee_start_from_awaiting_authorization_authorizes_then_resumes(hass: HomeAssistant) -> None:
    adapter, commands, _ = await _easee_commands(hass)
    hass.states.async_set("sensor.easee_status", "awaiting_authorization")

    await adapter.async_start()

    assert _names(commands) == ["start", "resume"]


async def test_easee_stop_pauses_and_never_deauthorizes(hass: HomeAssistant) -> None:
    adapter, commands, _ = await _easee_commands(hass)
    hass.states.async_set("sensor.easee_status", "charging")

    await adapter.async_stop()

    assert _names(commands) == ["pause"]


async def test_easee_writes_no_dynamic_limit_while_paused_and_sends_it_after_the_resume(
    hass: HomeAssistant,
) -> None:
    adapter, commands, limits = await _easee_commands(hass)
    hass.states.async_set("sensor.easee_status", "charging")
    await adapter.async_stop()
    hass.states.async_set("sensor.easee_status", "awaiting_start")

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_IGNORED_WHILE_PAUSED
    assert limits == []

    await adapter.async_start()
    hass.states.async_set("sensor.easee_status", "charging")
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert [call.data["current"] for call in limits] == [10]


async def test_easee_does_not_hold_back_a_limit_when_it_never_paused(hass: HomeAssistant) -> None:
    adapter, _, limits = await _easee_commands(hass)
    hass.states.async_set("sensor.easee_status", "awaiting_start")

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert len(limits) == 1


async def test_easee_resends_the_limit_after_a_plug_in_that_followed_a_pause(hass: HomeAssistant) -> None:
    adapter, _, limits = await _easee_commands(hass)
    hass.states.async_set("sensor.easee_status", "charging")
    await adapter.async_stop()
    hass.states.async_set("sensor.easee_status", "disconnected")
    hass.states.async_set("sensor.easee_status", "awaiting_start")
    assert adapter.current.needs_resend("disconnected", "awaiting_start") is True

    assert await adapter.async_set_current(8, reason=WRITE_RESEND) == ASSIGN_ASSIGNED
    assert limits[-1].data["current"] == 8


async def test_a_charging_status_that_lags_behind_our_pause_does_not_let_a_limit_lift_it(
    hass: HomeAssistant,
) -> None:
    """The cloud reports the pause seconds after it was sent: a `charging` status meanwhile is the old
    one, and a limit above 0 written now would resume the charge."""
    clock = Clock()
    adapter, commands, limits = await _easee_commands(hass, clock)
    hass.states.async_set("sensor.easee_status", "charging")
    await adapter.async_stop()
    clock.advance(5)

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_IGNORED_WHILE_PAUSED
    clock.advance(60)
    assert await adapter.async_set_current(11, reason=WRITE_REGULATOR) == ASSIGN_IGNORED_WHILE_PAUSED
    assert limits == []


async def test_a_resume_in_the_easee_app_after_the_pause_has_shown_is_still_recognised(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    adapter, _, limits = await _easee_commands(hass, clock)
    hass.states.async_set("sensor.easee_status", "charging")
    await adapter.async_stop()
    clock.advance(5)
    hass.states.async_set("sensor.easee_status", "awaiting_start")
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_IGNORED_WHILE_PAUSED
    clock.advance(5)
    hass.states.async_set("sensor.easee_status", "charging")

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert [call.data["current"] for call in limits] == [10]


async def test_a_paused_status_seen_only_as_a_state_change_still_ends_the_lag(hass: HomeAssistant) -> None:
    """The pause can land and be resumed between two regulator passes: the controller hands every status
    change to `needs_resend`, which is how the adapter learns of it."""
    clock = Clock()
    adapter, _, limits = await _easee_commands(hass, clock)
    hass.states.async_set("sensor.easee_status", "charging")
    await adapter.async_stop()
    clock.advance(5)
    adapter.current.needs_resend("charging", "awaiting_start")
    adapter.current.needs_resend("awaiting_start", "charging")

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert len(limits) == 1


async def test_an_unreadable_status_does_not_end_the_lag(hass: HomeAssistant) -> None:
    clock = Clock()
    adapter, _, limits = await _easee_commands(hass, clock)
    hass.states.async_set("sensor.easee_status", "charging")
    await adapter.async_stop()
    adapter.current.needs_resend("charging", "unavailable")
    adapter.current.needs_resend("unavailable", "charging")

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_IGNORED_WHILE_PAUSED
    assert limits == []


async def test_a_charging_status_after_the_lag_window_is_a_resume(hass: HomeAssistant) -> None:
    """A pause the cloud never reported (or a resume that came before the status could show the pause):
    after the window the charger's own `charging` is the truth again."""
    clock = Clock()
    adapter, _, limits = await _easee_commands(hass, clock)
    hass.states.async_set("sensor.easee_status", "charging")
    await adapter.async_stop()
    clock.advance(89)
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_IGNORED_WHILE_PAUSED
    clock.advance(1)

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert len(limits) == 1
