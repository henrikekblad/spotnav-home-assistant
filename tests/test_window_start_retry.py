"""A window's start that does not take is sent again, a bounded number of times, and an untaken start is not the car
ending the charge.

Before, a window's start went out once: a cloud error, an Easee that took the command and stayed `awaiting_start`, a
charge control unavailable for longer than the owed wait left the window uncharged, and a switch that read on while
the charger idled was recorded five minutes later as "the car ended the charge", which kept the window (and later
ones) from starting. Now the start is decided again at `START_RETRY_AFTER_S` after it, as a plug-in inside the window
decides it, while nothing says not to; an owed start waits for the charger as long as the window is open; and the car
can end only a charge it drew in. Each case runs with the core deciding and with the previous logic.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CHARGING_STATE, CONF_CORE_OWNERSHIP
from custom_components.spotnav.execution.controller import (
    ChargingController,
    OWED_GIVE_UP_S,
    START_RETRY_AFTER_S,
)
from custom_components.spotnav.flows.charger_detection import detect_charger

from .charger_helpers import detected_config
from .charger_shapes import register_shape, SHAPES
from .helpers import install_schedule
from .pause_world import pause_world
from .relay import FakeScheduler

pytestmark = pytest.mark.usefixtures("offline_relay")

CONTROL = "switch.charger"
STATUS = "sensor.charger_status"
EASEE_STATUS = "sensor.easee_status"

_CONTROLLERS: list[ChargingController] = []


@pytest.fixture(autouse=True)
async def _shut_down_controllers():
    yield
    while _CONTROLLERS:
        await _CONTROLLERS.pop().async_shutdown()


@pytest.fixture(params=[True, False], ids=["core", "legacy"])
def core(request: pytest.FixtureRequest) -> bool:
    return request.param


async def _after(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _walk(hass: HomeAssistant, freezer: Any, seconds: float, step: float = 30.0) -> None:
    """Time passes in steps, as it does: every timer fires at its own moment."""
    while seconds > 0:
        await _after(hass, freezer, min(step, seconds))
        seconds -= step


def _window(start_in: float = 30.0, hours: float = 2.0) -> dict[str, Any]:
    start = dt_util.utcnow() + timedelta(seconds=start_in)
    return {"start": start.isoformat(), "end": (start + timedelta(hours=hours)).isoformat(), "amps": 10}


async def _switch_charger(
    hass: HomeAssistant, core: bool, *, mode: str = "ignored", status: str = "SuspendedEVSE"
) -> tuple[ChargingController, list[Any]]:
    """A charger with a plain switch and a status sensor: `mode` says what a start does (`ignored`: the switch stays
    off, `follows`: it turns on while the status still says the charger idles, `raise`: the call fails)."""
    starts: list[Any] = []

    async def turn_on(call: ServiceCall) -> None:
        starts.append(call)
        if mode == "raise":
            raise HomeAssistantError("charger rejected")
        if mode == "follows":
            hass.states.async_set(CONTROL, "on")

    async def turn_off(call: ServiceCall) -> None:
        hass.states.async_set(CONTROL, "off")

    hass.services.async_register("switch", "turn_on", turn_on)
    hass.services.async_register("switch", "turn_off", turn_off)
    hass.states.async_set(CONTROL, "off")
    hass.states.async_set(STATUS, status)
    controller = ChargingController(
        hass,
        "entry_a",
        {
            CONF_CHARGE_CONTROL: CONTROL,
            CONF_CHARGING_STATE: {"entity_id": STATUS, "charging_values": ["Charging"]},
            CONF_CORE_OWNERSHIP: core,
        },
    )
    _CONTROLLERS.append(controller)
    await controller.async_initialize()
    return controller, starts


async def _shaped_charger(hass: HomeAssistant, platform: str, core: bool) -> ChargingController:
    ids = register_shape(hass, SHAPES[platform])
    found = detect_charger(hass, ids["device_id"])
    controller = ChargingController(hass, f"entry_{platform}", {**detected_config(found), CONF_CORE_OWNERSHIP: core})
    _CONTROLLERS.append(controller)
    await controller.async_initialize()
    return controller


def _easee_commands(hass: HomeAssistant) -> list[str]:
    commands: list[str] = []

    async def handler(call: ServiceCall) -> None:
        commands.append(call.data["action_command"])

    hass.services.async_register("easee", "action_command", handler)
    return commands


# ------------------------------------------------------------------------------------------------- sent again


async def test_a_start_the_charger_ignores_is_sent_again_three_times_and_no_more(
    hass: HomeAssistant, freezer: Any, core: bool
) -> None:
    controller, starts = await _switch_charger(hass, core)
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    assert len(starts) == 1, "the window's own start"
    for retry, after in enumerate(START_RETRY_AFTER_S, start=1):
        await _walk(hass, freezer, after - (0 if retry == 1 else START_RETRY_AFTER_S[retry - 2]))
        assert len(starts) == 1 + retry, f"retry {retry} at {after:.0f} s"
    await _walk(hass, freezer, 3600, step=300)
    assert len(starts) == 1 + len(START_RETRY_AFTER_S), "bounded"
    facts = controller.window_start_retry
    assert facts["retries"] == len(START_RETRY_AFTER_S) and facts["next_due"] is False


async def test_a_start_that_raises_is_logged_and_sent_again(
    hass: HomeAssistant, freezer: Any, core: bool, caplog: pytest.LogCaptureFixture
) -> None:
    controller, starts = await _switch_charger(hass, core, mode="raise")
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    assert len(starts) == 1
    assert "the window's start failed: HomeAssistantError" in caplog.text
    assert "Task exception was never retrieved" not in caplog.text
    await _walk(hass, freezer, START_RETRY_AFTER_S[-1] + 60)
    assert len(starts) == 1 + len(START_RETRY_AFTER_S)
    assert "the window start's retry 1 of 3 failed: HomeAssistantError" in caplog.text
    assert controller.window_start_retry["last"]["result"] == "failed"


async def test_an_easee_left_awaiting_start_is_resumed_again_until_it_charges(
    hass: HomeAssistant, freezer: Any, core: bool
) -> None:
    controller = await _shaped_charger(hass, "easee", core)
    hass.states.async_set(EASEE_STATUS, "awaiting_start", {"config_authorizationRequired": False})
    commands = _easee_commands(hass)
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    sent = len(commands)
    assert sent >= 1, "the window's start"

    await _walk(hass, freezer, START_RETRY_AFTER_S[0])
    assert len(commands) > sent, "sent again at the first retry"
    hass.states.async_set(EASEE_STATUS, "charging", {"config_authorizationRequired": False})
    await hass.async_block_till_done()
    assert controller.window_start_retry["active"] is False, "it charges: nothing more is sent"
    sent = len(commands)
    await _walk(hass, freezer, 1200, step=60)
    assert len(commands) == sent


async def test_a_toggling_button_charger_is_sent_its_start_again_once(
    hass: HomeAssistant, freezer: Any, core: bool
) -> None:
    """Webasto's start presses its stop first: a retry is a stop and a start, so there is at most one."""
    controller = await _shaped_charger(hass, "webasto_next_modbus", core)
    presses: list[str] = []

    async def press(call: ServiceCall) -> None:
        presses.append(call.data["entity_id"])

    hass.services.async_register("button", "press", press)
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    first = len(presses)
    assert first >= 1
    await _walk(hass, freezer, 3600, step=60)
    assert len(presses) == 2 * first, "one retry"
    assert controller.window_start_retry["limit"] == 1


# ------------------------------------------------------------------------------------------------- not sent again


async def test_an_easee_whose_own_scheduler_holds_the_start_is_not_sent_it_again(
    hass: HomeAssistant, freezer: Any, core: bool
) -> None:
    controller = await _shaped_charger(hass, "easee", core)
    hass.states.async_set(EASEE_STATUS, "awaiting_start", {"config_authorizationRequired": False})
    commands = _easee_commands(hass)
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    hass.states.async_set(EASEE_STATUS, "awaiting_scheduled_start", {"config_authorizationRequired": False})
    await hass.async_block_till_done()
    sent = len(commands)
    await _walk(hass, freezer, 1200, step=60)
    assert len(commands) == sent
    assert controller.window_start_retry["last"]["result"] == "held_by_charger"


async def test_a_car_that_finished_is_not_sent_the_start_again(hass: HomeAssistant, freezer: Any, core: bool) -> None:
    controller = await _shaped_charger(hass, "easee", core)
    hass.states.async_set(EASEE_STATUS, "awaiting_start", {"config_authorizationRequired": False})
    commands = _easee_commands(hass)
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    hass.states.async_set(EASEE_STATUS, "completed", {"config_authorizationRequired": False})
    await hass.async_block_till_done()
    sent = len(commands)
    await _walk(hass, freezer, 1200, step=60)
    assert len(commands) == sent
    assert controller.window_start_retry["last"]["result"] == "finished"


async def test_an_unplugged_car_ends_the_retries(hass: HomeAssistant, freezer: Any, core: bool) -> None:
    controller = await _shaped_charger(hass, "easee", core)
    hass.states.async_set(EASEE_STATUS, "awaiting_start", {"config_authorizationRequired": False})
    commands = _easee_commands(hass)
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    hass.states.async_set(EASEE_STATUS, "disconnected", {"config_authorizationRequired": False})
    await hass.async_block_till_done()
    assert controller.window_start_retry["active"] is False
    sent = len(commands)
    await _walk(hass, freezer, 1200, step=60)
    assert len(commands) == sent


async def test_no_retry_after_the_window_ended(hass: HomeAssistant, freezer: Any, core: bool) -> None:
    controller, starts = await _switch_charger(hass, core)
    await install_schedule(controller, _window(hours=1 / 60))
    await _after(hass, freezer, 31)
    assert len(starts) == 1
    await _walk(hass, freezer, 60)
    assert controller.window_start_retry["active"] is False, "the window's end ends them"
    await _walk(hass, freezer, 1200, step=60)
    assert len(starts) == 1


@pytest.mark.parametrize("owner", ["balancing", "solar"])
async def test_no_retry_while_load_balancing_or_the_sun_holds_the_charger(
    hass: HomeAssistant, freezer: Any, core: bool, owner: str
) -> None:
    controller, starts = await _switch_charger(hass, core)
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    assert len(starts) == 1
    if owner == "balancing":
        controller._paused_by_balancing = True  # noqa: SLF001 - the regulator's pause, as it records it
    else:
        controller.set_hold_guard(lambda: True)
    await _walk(hass, freezer, 1200, step=60)
    assert len(starts) == 1
    assert controller.window_start_retry["last"]["result"] == (
        "balancing_paused" if owner == "balancing" else "owned_elsewhere"
    )


async def test_no_retry_under_a_persons_stop(hass: HomeAssistant, freezer: Any, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers)
    started: list[Any] = []

    async def ignored(call: ServiceCall) -> None:
        started.append(call)

    hass.services.async_register("switch", "turn_on", ignored)
    now = dt_util.utcnow()
    await install_schedule(
        world.controller,
        {"start": (now - timedelta(minutes=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat(), "amps": 10},
    )
    await hass.async_block_till_done()
    assert len(started) == 1, "the open window's start, which the charger ignores"
    assert world.controller.window_start_retry["active"] is True
    await world.executor.async_manual_stop()
    await hass.async_block_till_done()
    await _walk(hass, freezer, 1200, step=60)
    assert len(started) == 1
    assert world.controller.window_start_retry["active"] is False
    await world.shutdown()


# ------------------------------------------------------------------------------------------------- owed


async def test_an_owed_start_is_sent_once_the_charger_is_back_after_the_owed_wait(
    hass: HomeAssistant, freezer: Any, core: bool
) -> None:
    controller, starts = await _switch_charger(hass, core, mode="follows", status="Charging")
    hass.states.async_set(STATUS, "SuspendedEVSE")
    await install_schedule(controller, _window())
    hass.states.async_set(CONTROL, "unavailable")
    await _after(hass, freezer, 40)
    assert controller.commands_owed and starts == []
    await _walk(hass, freezer, OWED_GIVE_UP_S + 60, step=60)
    assert controller.commands_owed, "still owed while the window is open"
    hass.states.async_set(CONTROL, "off")
    await hass.async_block_till_done()
    await _after(hass, freezer, 10)
    assert len(starts) == 1


async def test_an_owed_start_is_not_sent_once_its_window_is_over(hass: HomeAssistant, freezer: Any, core: bool) -> None:
    controller, starts = await _switch_charger(hass, core)
    await install_schedule(controller, _window(hours=0.25))
    hass.states.async_set(CONTROL, "unavailable")
    await _after(hass, freezer, 40)
    assert controller.commands_owed
    await _walk(hass, freezer, OWED_GIVE_UP_S + 120, step=60)
    assert controller.plan_window_active_now is False
    hass.states.async_set(CONTROL, "off")
    await hass.async_block_till_done()
    await _walk(hass, freezer, 120)
    assert starts == []


# ------------------------------------------------------------------------------------------------- the car ended


@pytest.mark.parametrize("status", ["SuspendedEVSE", "Ready", "awaiting_start"])
async def test_a_switch_on_while_the_charger_idles_is_not_the_car_ending_the_charge(
    hass: HomeAssistant, freezer: Any, core: bool, status: str
) -> None:
    controller, starts = await _switch_charger(hass, core, mode="follows", status=status)
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    for tick in range(12):
        await _after(hass, freezer, 300)
        hass.states.async_set(STATUS, status, {"tick": tick})
        await hass.async_block_till_done()
    assert len(starts) == 1, "a switch already on gets no stop-and-start kick"
    assert controller._car_ended_at is None  # noqa: SLF001
    assert controller.plan is not None and controller.plan_window_active_now
