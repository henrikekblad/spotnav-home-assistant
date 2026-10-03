"""The first fix round after the charger audit: Easee read-back, floor and waiting statuses, and the start
minimum the solar executor reads.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution.chargers import easee
from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_IGNORED_WHILE_PAUSED,
    ASSIGN_RATE_LIMITED,
    ASSIGN_UNCHANGED,
    ASSIGN_UNCONFIRMED,
    WRITE_REGULATOR,
    WRITE_RESEND,
    WRITE_RESTORE,
    WRITE_SESSION_START,
)
from custom_components.spotnav.execution.chargers.easee import EASEE_START_HOLD_S
from custom_components.spotnav.execution.controller import ChargingController, RESTORE_RESTORED
from custom_components.spotnav.flows.charger_detection import detect_charger

from ..charger_helpers import adapter_for, Clock, detected_config, enable_easee_limit_sensor, set_easee_limit
from ..charger_shapes import register_shape, SHAPES


async def _easee(hass: HomeAssistant, clock: Clock | None = None):
    ids = register_shape(hass, SHAPES["easee"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=clock or Clock())
    limits = async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    commands = async_mock_service(hass, "easee", "action_command")
    return ids, adapter, limits, commands


def _sent(limits) -> list[int]:
    return [call.data["current"] for call in limits]


async def test_the_status_sensor_is_never_a_read_back_source(hass: HomeAssistant) -> None:
    """easee_hass's status sensor has no dynamic limit attribute; one planted there is not read."""
    ids, adapter, limits, _ = await _easee(hass)
    hass.states.async_set("sensor.easee_status", "charging", {"state_dynamicChargerCurrent": 12})

    assert adapter.current.read_back_a() is None
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert _sent(limits) == [12]


async def test_the_limit_is_read_from_the_state_of_the_enabled_limit_sensor(hass: HomeAssistant) -> None:
    ids, adapter, limits, _ = await _easee(hass)
    assert adapter.current.limit_entity_id() is None, "disabled by default: nothing to read"

    enable_easee_limit_sensor(hass, "12")

    assert adapter.current.limit_entity_id() == "sensor.easee_dynamic_charger_limit"
    assert adapter.current.read_back_a() == 12
    set_easee_limit(hass, "unavailable")
    assert adapter.current.read_back_a() is None


async def test_a_write_nobody_can_read_back_is_unverifiable_and_not_failed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(easee.asyncio, "sleep", record)
    ids, adapter, limits, _ = await _easee(hass)

    assert await adapter.async_set_current(12, reason=WRITE_RESTORE, verify=True) == ASSIGN_ASSIGNED
    assert slept == [], "there is nothing to wait for"

    enable_easee_limit_sensor(hass, "unknown")
    assert await adapter.async_set_current(12, reason=WRITE_RESTORE, verify=True) == ASSIGN_ASSIGNED
    set_easee_limit(hass, "9")
    assert await adapter.async_set_current(12, reason=WRITE_RESTORE, verify=True) == ASSIGN_UNCONFIRMED


async def test_a_restore_the_charger_cannot_confirm_is_not_reported_as_failed(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    found = detect_charger(hass, ids["device_id"])
    async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    async_mock_service(hass, "easee", "action_command")
    controller = ChargingController(hass, "entry_easee", detected_config(found))
    await controller.async_initialize()
    await controller.async_set_requested_current(16)
    await controller.async_apply_regulated_current(10, must_lower=False)

    restore = await controller.async_restore_current(lowered_by_balancing=True)

    assert (restore.outcome, restore.to_a) == (RESTORE_RESTORED, 16)
    await controller.async_shutdown()


async def test_the_regulator_does_not_resend_a_value_it_cannot_read_back(hass: HomeAssistant) -> None:
    ids, adapter, limits, _ = await _easee(hass, Clock())

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    for _ in range(5):
        assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_UNCHANGED
    assert _sent(limits) == [10]
    # A start, a restore and a plug-in resend still send it: the charger may have forgotten it.
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert _sent(limits) == [10, 10]


async def test_a_start_and_a_resend_are_never_below_seven_amps(hass: HomeAssistant) -> None:
    clock = Clock()
    ids, adapter, limits, _ = await _easee(hass, clock)

    assert await adapter.async_set_current(6, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    clock.advance(1)
    assert await adapter.async_set_current(6, reason=WRITE_RESEND) == ASSIGN_ASSIGNED
    clock.advance(1)
    assert await adapter.async_set_current(7, reason=WRITE_RESEND) == ASSIGN_ASSIGNED

    assert _sent(limits) == [7, 7, 7], "no 6 A write first, not even the nudge one amp lower"
    assert adapter.min_start_current_a == 7.0


async def test_a_resend_above_the_floor_still_nudges_one_amp_lower(hass: HomeAssistant) -> None:
    ids, adapter, limits, _ = await _easee(hass, Clock())

    assert await adapter.async_set_current(8, reason=WRITE_RESEND) == ASSIGN_ASSIGNED

    assert _sent(limits) == [7, 8]


async def test_the_regulator_may_go_to_six_but_not_straight_after_a_start(hass: HomeAssistant) -> None:
    clock = Clock()
    ids, adapter, limits, _ = await _easee(hass, clock)
    await adapter.async_set_current(6, reason=WRITE_SESSION_START)
    clock.advance(EASEE_START_HOLD_S / 2)

    assert await adapter.async_set_current(6, reason=WRITE_REGULATOR) == ASSIGN_RATE_LIMITED
    assert await adapter.async_set_current(8, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED, "above is free"

    clock.advance(EASEE_START_HOLD_S)
    assert await adapter.async_set_current(6, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert _sent(limits) == [7, 8, 6]


async def test_the_planner_reasons_with_the_profile_minimum(hass: HomeAssistant) -> None:
    ids, easee, _, _ = await _easee(hass)
    wallbox_ids = register_shape(hass, SHAPES["wallbox"])
    wallbox = adapter_for(hass, detect_charger(hass, wallbox_ids["device_id"]), clock=Clock())

    assert easee.min_start_current_a == 7.0
    assert wallbox.min_start_current_a == 6.0


async def test_after_a_restart_a_limit_that_reads_zero_is_a_pause(hass: HomeAssistant) -> None:
    ids, adapter, limits, commands = await _easee(hass, Clock())
    hass.states.async_set("sensor.easee_status", "awaiting_start")
    enable_easee_limit_sensor(hass, "0")
    assert adapter.current._paused() is True  # noqa: SLF001

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_IGNORED_WHILE_PAUSED
    assert limits == []

    await adapter.async_start()
    set_easee_limit(hass, "16")
    # After a restart the pause is not known to be ours, so the start authorizes first.
    assert [call.data["action_command"] for call in commands] == ["start", "resume"]
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED


async def test_a_limit_that_reads_zero_is_ignored_when_the_sensor_is_not_enabled(hass: HomeAssistant) -> None:
    ids, adapter, limits, _ = await _easee(hass, Clock())
    hass.states.async_set("sensor.easee_status", "awaiting_start")

    assert adapter.current._paused() is False  # noqa: SLF001
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED


async def test_a_plug_in_is_owed_its_limit_even_when_the_old_limit_still_reads_zero(hass: HomeAssistant) -> None:
    ids, adapter, limits, _ = await _easee(hass, Clock())
    enable_easee_limit_sensor(hass, "0")
    hass.states.async_set("sensor.easee_status", "disconnected")
    assert adapter.current._paused() is False  # noqa: SLF001
    hass.states.async_set("sensor.easee_status", "awaiting_start")
    assert adapter.current.needs_resend("disconnected", "awaiting_start") is True

    assert await adapter.async_set_current(10, reason=WRITE_RESEND) == ASSIGN_ASSIGNED


@pytest.mark.parametrize(
    "status",
    ["awaiting_scheduled_start", "awaiting_smart_start", "awaiting_load_balancing", "paused_due_to_equalizer"],
)
async def test_the_chargers_own_scheduler_explains_itself(hass: HomeAssistant, status: str) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    found = detect_charger(hass, ids["device_id"])
    async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    async_mock_service(hass, "easee", "action_command")
    controller = ChargingController(hass, "entry_easee", detected_config(found))
    await controller.async_initialize()
    hass.states.async_set("sensor.easee_status", "awaiting_start")

    await controller.async_start()
    assert controller.start_pending is True, "an unanswered Start is pending"
    hass.states.async_set("sensor.easee_status", status)

    assert controller.held_by_charger is True
    assert controller.start_pending is False, "the charger answered: its own scheduler holds it"
    await controller.async_shutdown()


async def test_other_statuses_are_not_a_held_charge(hass: HomeAssistant) -> None:
    ids, adapter, _, _ = await _easee(hass)
    for status in ("awaiting_start", "charging", "ready_to_charge", "completed", "disconnected"):
        hass.states.async_set("sensor.easee_status", status)
        assert adapter.held_by_charger() is False


async def test_a_required_authorization_is_a_second_signal_for_start_before_resume(hass: HomeAssistant) -> None:
    ids, adapter, _, commands = await _easee(hass)
    hass.states.async_set("sensor.easee_status", "awaiting_start", {"config_authorizationRequired": True})

    await adapter.async_start()

    assert [call.data["action_command"] for call in commands] == ["start", "resume"]


async def test_a_start_authorizes_unless_the_pause_is_ours(
    hass: HomeAssistant,
) -> None:
    ids, adapter, _, commands = await _easee(hass)
    # A charger an earlier version deauthorized reads `awaiting_start` whatever its setting says.
    hass.states.async_set("sensor.easee_status", "awaiting_start", {"config_authorizationRequired": False})
    await adapter.async_start()
    assert [call.data["action_command"] for call in commands] == ["start", "resume"]

    commands.clear()
    hass.states.async_set("sensor.easee_status", "charging", {"config_authorizationRequired": True})
    await adapter.async_stop()
    hass.states.async_set("sensor.easee_status", "awaiting_start", {"config_authorizationRequired": True})
    await adapter.async_start()
    assert [call.data["action_command"] for call in commands] == ["pause", "resume"]


async def test_solar_starts_at_the_chargers_start_minimum_and_runs_down_to_the_floor(
    hass: HomeAssistant,
) -> None:
    from types import SimpleNamespace

    from custom_components.spotnav.execution.solar_execution import SolarExecutionCoordinator

    site = SimpleNamespace(config={})

    def solar_for(platform: str):
        ids = register_shape(hass, SHAPES[platform], suffix=platform)
        adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=Clock())
        fake = SimpleNamespace(
            _charger_entry_id="entry",
            _controller=SimpleNamespace(adapter=adapter, charging=False),
        )
        return SolarExecutionCoordinator._solar_config(fake, site)  # noqa: SLF001

    easee = solar_for("easee")
    assert (easee.start_a, easee.stop_a, easee.min_current_a) == (7.0, 6.0, 6.0)
    other = solar_for("nrgkick")
    assert (other.start_a, other.stop_a, other.min_current_a) == (6.0, 5.0, 6.0)
