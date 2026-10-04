"""Easee: a status that blinks through `unavailable` is neither a plug-in nor an unplug, and a pause of
ours stays a pause.

The report's sequence (E1): SpotNav pauses the charger, its status reads `awaiting_start` ->
`unavailable` -> `awaiting_start`. That used to be read as a plug-in: the pause was forgotten and the
requested current sent again, and a limit above 0 resumes the charge behind a person's Stop, a target
stop or a balancing pause. And (E2): a regulator write landing while the pause command is still on its
way passed the pause check and lifted it.
"""

from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_IGNORED_WHILE_PAUSED,
    WRITE_REGULATOR,
    WRITE_RESEND,
)
from custom_components.spotnav.execution.controller import (
    REGULATED_WROTE,
    ChargingController,
)
from custom_components.spotnav.flows.charger_detection import detect_charger

from ..charger_helpers import adapter_for, Clock, detected_config
from ..charger_shapes import register_shape, SHAPES

STATUS = "sensor.easee_status"


async def _paused_easee(hass: HomeAssistant):
    ids = register_shape(hass, SHAPES["easee"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=Clock())
    limits = async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    async_mock_service(hass, "easee", "action_command")
    hass.states.async_set(STATUS, "charging")
    await adapter.async_start()
    await adapter.async_stop()
    hass.states.async_set(STATUS, "awaiting_start")
    return adapter, limits


async def test_a_blip_through_unavailable_is_not_a_plug_in_and_keeps_our_pause(hass: HomeAssistant) -> None:
    adapter, limits = await _paused_easee(hass)
    current = adapter.current

    hass.states.async_set(STATUS, "unavailable")
    assert current.needs_resend("awaiting_start", "unavailable") is False
    hass.states.async_set(STATUS, "awaiting_start")
    assert current.needs_resend("unavailable", "awaiting_start") is False, "a blip is not a plug-in"

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_IGNORED_WHILE_PAUSED
    assert await adapter.async_set_current(10, reason=WRITE_RESEND) == ASSIGN_IGNORED_WHILE_PAUSED
    assert limits == []


async def test_while_unavailable_the_pause_is_still_ours(hass: HomeAssistant) -> None:
    adapter, limits = await _paused_easee(hass)
    hass.states.async_set(STATUS, "unavailable")

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_IGNORED_WHILE_PAUSED
    assert limits == []


async def test_a_real_plug_in_after_the_blip_still_forgets_the_pause(hass: HomeAssistant) -> None:
    adapter, _ = await _paused_easee(hass)
    current = adapter.current
    hass.states.async_set(STATUS, "disconnected")
    assert current.needs_resend("awaiting_start", "disconnected") is False
    hass.states.async_set(STATUS, "unavailable")
    assert current.needs_resend("disconnected", "unavailable") is False
    hass.states.async_set(STATUS, "awaiting_start")
    assert current.needs_resend("unavailable", "awaiting_start") is True, "unplugged before the blip: a plug-in"


async def test_a_reboot_of_a_charger_we_never_paused_is_still_owed_its_limit(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=Clock())
    hass.states.async_set(STATUS, "charging")
    assert adapter.current.needs_resend("charging", "unavailable") is False
    assert adapter.current.needs_resend("unavailable", "charging") is True


async def test_a_regulator_write_during_the_pause_command_does_not_lift_it(hass: HomeAssistant) -> None:
    """The pause is on its way (Easee's cloud can take many seconds) while the status still says
    `charging`: a regulator write in that time must be held, not sent."""
    ids = register_shape(hass, SHAPES["easee"])
    found = detect_charger(hass, ids["device_id"])
    controller = ChargingController(hass, "entry_easee", detected_config(found))
    limits = async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    release = asyncio.Event()
    commands: list[str] = []

    async def slow_command(call: ServiceCall) -> None:
        commands.append(call.data["action_command"])
        if call.data["action_command"] == "pause":
            await release.wait()

    hass.services.async_register("easee", "action_command", slow_command)
    hass.states.async_set(STATUS, "charging")
    await hass.async_block_till_done()

    stop = hass.async_create_task(controller.async_stop())
    for _ in range(5):
        await asyncio.sleep(0)
    assert commands == ["pause"]

    write = await controller.async_apply_regulated_current(10, must_lower=False)

    assert write.outcome != REGULATED_WROTE
    assert limits == []
    release.set()
    await stop
    await controller.async_shutdown()
