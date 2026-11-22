"""A window's start that does not take is sent again, a bounded number of times, and an untaken start is not the car
ending the charge.

Before, a window's start went out once: a cloud error, an Easee that took the command and stayed `awaiting_start`, a
charge control unavailable for longer than the owed wait left the window uncharged, and a switch that read on while
the charger idled was recorded five minutes later as "the car ended the charge", which kept the window (and later
ones) from starting. Now the start is decided again at `START_RETRY_AFTER_S` after it, as a plug-in inside the window
decides it, while nothing says not to; an owed start waits for the charger as long as the window is open; and the car
can end only a charge it drew in.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CHARGER_CURRENT_ENTITIES, CONF_CHARGING_STATE
from custom_components.spotnav.execution.controller import (
    ChargingController,
    OWED_GIVE_UP_S,
    START_RETRY_AFTER_S,
)
from custom_components.spotnav.core.ownership import SOC_DROP_PCT
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
    hass: HomeAssistant, *, mode: str = "ignored", status: str = "SuspendedEVSE"
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
        },
    )
    _CONTROLLERS.append(controller)
    await controller.async_initialize()
    return controller, starts


async def _shaped_charger(hass: HomeAssistant, platform: str) -> ChargingController:
    ids = register_shape(hass, SHAPES[platform])
    found = detect_charger(hass, ids["device_id"])
    controller = ChargingController(hass, f"entry_{platform}", detected_config(found))
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
    hass: HomeAssistant, freezer: Any
) -> None:
    controller, starts = await _switch_charger(hass)
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
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    controller, starts = await _switch_charger(hass, mode="raise")
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
    hass: HomeAssistant, freezer: Any
) -> None:
    controller = await _shaped_charger(hass, "easee")
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
    hass: HomeAssistant, freezer: Any
) -> None:
    """Webasto's start presses its stop first: a retry is a stop and a start, so there is at most one."""
    controller = await _shaped_charger(hass, "webasto_next_modbus")
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
    hass: HomeAssistant, freezer: Any
) -> None:
    controller = await _shaped_charger(hass, "easee")
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


async def test_a_car_that_finished_is_not_sent_the_start_again(hass: HomeAssistant, freezer: Any) -> None:
    controller = await _shaped_charger(hass, "easee")
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


def _windows(*spans: tuple[float, float]) -> dict[str, Any]:
    """Periods as (start, end) in minutes from now (a window opens 30 s in)."""
    origin = dt_util.utcnow() + timedelta(seconds=30)
    periods = [
        {"start": (origin + timedelta(minutes=a)).isoformat(), "end": (origin + timedelta(minutes=b)).isoformat()}
        for a, b in spans
    ]
    return {"start": periods[0]["start"], "end": periods[-1]["end"], "amps": 10, "periods": periods}


def _easee(hass: HomeAssistant, status: str) -> None:
    hass.states.async_set(EASEE_STATUS, status, {"config_authorizationRequired": False})


async def test_a_car_that_finished_by_itself_is_not_resumed_at_the_next_windows(
    hass: HomeAssistant, freezer: Any
) -> None:
    """SN-7PME: an Easee charged the car full and said `completed`; each later window sent `resume` again."""
    controller = await _shaped_charger(hass, "easee")
    _easee(hass, "awaiting_start")
    commands = _easee_commands(hass)
    await install_schedule(controller, _windows((0, 30), (60, 90), (120, 150)))
    await _after(hass, freezer, 31)
    assert commands[-1] == "resume"
    _easee(hass, "charging")
    await _walk(hass, freezer, 600)
    _easee(hass, "completed")  # the car is full
    await hass.async_block_till_done()
    await _walk(hass, freezer, 9000, step=60)
    assert commands.count("resume") == 2, f"the window's start and one try for the plan, no more: {commands}"


def _soc(level: list[float]) -> Any:
    from custom_components.spotnav.execution.target_stop import SocReading

    return lambda _vehicle: SocReading(level[0], "vehicle", "sensor.car_soc", None, 10.0)


async def _finished_after_a_try(hass: HomeAssistant, freezer: Any, level: list[float], plan: dict[str, Any]):
    """An Easee that charged the car until `completed`, and the second window's one try already spent."""
    controller = await _shaped_charger(hass, "easee")
    controller._soc_reader = _soc(level)  # noqa: SLF001 - the car's level, as the entry wires it
    _easee(hass, "awaiting_start")
    commands = _easee_commands(hass)
    await install_schedule(controller, plan)
    await _after(hass, freezer, 31)
    _easee(hass, "charging")
    await _walk(hass, freezer, 600)
    _easee(hass, "completed")
    await hass.async_block_till_done()
    await _walk(hass, freezer, 65 * 60, step=60)  # into the second window: its one try
    assert commands.count("resume") == 2, commands
    return controller, commands


async def test_a_raised_target_after_the_car_finished_is_tried_again(hass: HomeAssistant, freezer: Any) -> None:
    level = [75.0]  # the car stopped below the target (its own limit)
    plan = {**_windows((0, 30), (60, 90), (120, 150)), "target_soc_percent": 80}
    controller, commands = await _finished_after_a_try(hass, freezer, level, plan)
    # A new plan for a raised target, made while the car still says `completed`.
    raised = {**_windows((0, 20), (40, 60)), "target_soc_percent": 95}
    await install_schedule(controller, raised)
    await _walk(hass, freezer, 70 * 60, step=60)
    assert commands.count("resume") == 3, commands


async def test_a_car_level_dropped_since_the_try_is_tried_again(hass: HomeAssistant, freezer: Any) -> None:
    level = [100.0]
    controller, commands = await _finished_after_a_try(hass, freezer, level, _windows((0, 30), (60, 90), (120, 150)))
    level[0] = 100.0 - SOC_DROP_PCT
    await _walk(hass, freezer, 60 * 60, step=60)  # the third window
    assert commands.count("resume") == 3, commands


async def test_a_full_car_is_tried_once_per_plan(hass: HomeAssistant, freezer: Any) -> None:
    level = [100.0]
    controller, commands = await _finished_after_a_try(hass, freezer, level, _windows((0, 30), (60, 90), (120, 150)))
    await _walk(hass, freezer, 60 * 60, step=60)  # the third window: nothing new
    assert commands.count("resume") == 2, commands


async def test_a_charger_that_reads_finished_after_our_stop_is_started_at_the_next_window(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A charger whose stop reads as finished (Zaptec's `connected_finished`): that is SpotNav's stop, not the car."""
    controller = await _shaped_charger(hass, "easee")
    _easee(hass, "awaiting_start")
    commands = _easee_commands(hass)
    await install_schedule(controller, _windows((0, 30), (60, 90)))
    await _after(hass, freezer, 31)
    _easee(hass, "charging")
    await _walk(hass, freezer, 30 * 60 + 30)
    assert commands[-1] == "pause", "the first window's end"
    await _after(hass, freezer, 20)
    _easee(hass, "completed")
    await hass.async_block_till_done()
    await _walk(hass, freezer, 31 * 60)
    assert commands.count("resume") == 2, commands


def _followed_starts(hass: HomeAssistant) -> list[str]:
    """Switch and select calls that the entity follows, as the charger's integration would: `on`/`off` and options."""
    calls: list[str] = []

    async def switch(call: ServiceCall) -> None:
        on = call.service == "turn_on"
        calls.append("start" if on else "stop")
        for entity_id in call.data["entity_id"] if isinstance(call.data["entity_id"], list) else [call.data["entity_id"]]:
            hass.states.async_set(entity_id, "on" if on else "off")

    async def select(call: ServiceCall) -> None:
        option = call.data["option"]
        calls.append("start" if option.lower() == "charge" else "stop")
        for entity_id in call.data["entity_id"] if isinstance(call.data["entity_id"], list) else [call.data["entity_id"]]:
            hass.states.async_set(entity_id, option)

    hass.services.async_register("switch", "turn_on", switch)
    hass.services.async_register("switch", "turn_off", switch)
    hass.services.async_register("select", "select_option", select)
    return calls


async def _shaped_with_config(hass: HomeAssistant, platform: str) -> tuple[ChargingController, dict[str, Any]]:
    ids = register_shape(hass, SHAPES[platform])
    config = detected_config(detect_charger(hass, ids["device_id"]))
    controller = ChargingController(hass, f"entry_{platform}", config)
    _CONTROLLERS.append(controller)
    await controller.async_initialize()
    return controller, config


def _set_currents(hass: HomeAssistant, config: dict[str, Any], amps: float) -> None:
    for entity_id in config.get(CONF_CHARGER_CURRENT_ENTITIES) or []:
        hass.states.async_set(entity_id, str(amps), {"unit_of_measurement": "A"})


async def test_our_stop_read_as_finished_after_a_restart_is_not_the_car(hass: HomeAssistant, freezer: Any) -> None:
    """A window's end stopped a Zaptec; Home Assistant restarted before it read `connected_finished`. That is
    SpotNav's own stop (kept across the restart), so the next window starts the charge."""
    controller, config = await _shaped_with_config(hass, "zaptec")
    status = controller.adapter.status_entity_id
    calls = _followed_starts(hass)
    hass.states.async_set(status, "connected_requesting")
    _set_currents(hass, config, 0)
    await install_schedule(controller, _windows((0, 30), (60, 90)))
    await _after(hass, freezer, 31)
    hass.states.async_set(status, "connected_charging")
    _set_currents(hass, config, 10)
    await _walk(hass, freezer, 30 * 60 + 30)
    assert calls[-1] == "stop", "the first window's end"
    hass.states.async_set(status, "connected_requesting")
    _set_currents(hass, config, 0)

    await controller.async_shutdown()
    _CONTROLLERS.remove(controller)
    restarted = ChargingController(hass, "entry_zaptec", config)
    _CONTROLLERS.append(restarted)
    await restarted.async_initialize()
    assert restarted.plan is not None
    await _after(hass, freezer, 400)  # read long after any stop of the restart's own
    hass.states.async_set(status, "connected_finished")
    await hass.async_block_till_done()
    starts = calls.count("start")
    await _walk(hass, freezer, 25 * 60, step=60)
    assert calls.count("start") == starts + 1, calls


async def test_our_stop_landing_late_as_finished_is_not_the_car(hass: HomeAssistant, freezer: Any) -> None:
    """A go-e reads `Complete` 400 s after SpotNav's window-end stop (a slow cloud): the next window starts it."""
    controller, config = await _shaped_with_config(hass, "goecharger_api2")
    status = controller.adapter.status_entity_id
    calls = _followed_starts(hass)
    hass.states.async_set(status, "Wait car")
    _set_currents(hass, config, 0)
    await install_schedule(controller, _windows((0, 30), (60, 90)))
    await _after(hass, freezer, 31)
    hass.states.async_set(status, "Charging")
    _set_currents(hass, config, 10)
    await _walk(hass, freezer, 30 * 60 + 30)
    assert calls[-1] == "stop", "the first window's end"
    hass.states.async_set(status, "Wait car")
    _set_currents(hass, config, 0)
    await _after(hass, freezer, 400)
    hass.states.async_set(status, "Complete")
    await hass.async_block_till_done()
    starts = calls.count("start")
    await _walk(hass, freezer, 31 * 60, step=60)
    assert calls.count("start") == starts + 1, calls


async def test_a_new_plan_does_not_pause_an_empty_charger(hass: HomeAssistant, freezer: Any) -> None:
    """SN-7PME 12:04: a price update's new plan paused a `disconnected` Easee."""
    controller = await _shaped_charger(hass, "easee")
    _easee(hass, "disconnected")
    await hass.async_block_till_done()
    commands = _easee_commands(hass)
    await install_schedule(controller, _windows((60, 90)))
    await _after(hass, freezer, 5)
    assert commands == []


async def _completed_after_charging(hass: HomeAssistant, freezer: Any, plan: dict[str, Any]):
    controller = await _shaped_charger(hass, "easee")
    _easee(hass, "awaiting_start")
    commands = _easee_commands(hass)
    await install_schedule(controller, plan)
    await _after(hass, freezer, 31)
    _easee(hass, "charging")
    await _walk(hass, freezer, 600)
    _easee(hass, "completed")
    await hass.async_block_till_done()
    return controller, commands


async def test_a_moment_out_of_completed_after_the_try_does_not_give_another(hass: HomeAssistant, freezer: Any) -> None:
    """After a try's `resume` an Easee shows `awaiting_start` for half a minute, then `completed` again: still one
    try for the plan, not one per window."""
    _controller, commands = await _completed_after_charging(
        hass, freezer, _windows((0, 30), (60, 90), (120, 150), (180, 210))
    )
    seen = len(commands)
    for _minute in range(200):
        await _after(hass, freezer, 60)
        if len(commands) > seen and commands[-1] == "resume":
            _easee(hass, "awaiting_start")
            await _after(hass, freezer, 30)
            _easee(hass, "completed")
            await hass.async_block_till_done()
        seen = len(commands)
    assert commands.count("resume") == 2, f"the first window's start and one try: {commands}"


async def test_a_stale_car_cloud_unplugged_does_not_spare_the_stop(hass: HomeAssistant, freezer: Any) -> None:
    """Only the charger's own "no vehicle" spares a new plan's stop: the car's plug sensor standing in for a charger
    that says nothing may be a stale cloud reading."""
    controller = await _shaped_charger(hass, "easee")
    _easee(hass, "error")  # says nothing about the car
    controller.adapter.connection_fallback = lambda: False  # the car's cloud: unplugged, long ago
    await hass.async_block_till_done()
    assert controller.adapter.vehicle_connected() is False
    assert controller.adapter.charger_vehicle_connected() is None
    commands = _easee_commands(hass)
    await install_schedule(controller, _windows((60, 90)))
    await _after(hass, freezer, 5)
    assert commands == ["pause"]


async def test_an_unplugged_car_ends_the_retries(hass: HomeAssistant, freezer: Any) -> None:
    controller = await _shaped_charger(hass, "easee")
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


async def test_no_retry_after_the_window_ended(hass: HomeAssistant, freezer: Any) -> None:
    controller, starts = await _switch_charger(hass)
    await install_schedule(controller, _window(hours=1 / 60))
    await _after(hass, freezer, 31)
    assert len(starts) == 1
    await _walk(hass, freezer, 60)
    assert controller.window_start_retry["active"] is False, "the window's end ends them"
    await _walk(hass, freezer, 1200, step=60)
    assert len(starts) == 1


@pytest.mark.parametrize("owner", ["balancing", "solar"])
async def test_no_retry_while_load_balancing_or_the_sun_holds_the_charger(
    hass: HomeAssistant, freezer: Any, owner: str
) -> None:
    controller, starts = await _switch_charger(hass)
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    assert len(starts) == 1
    if owner == "balancing":
        # Load balancing holds the window's charge back below the floor (no room on the site now): its pause.
        controller.set_start_cap(lambda: 0.0)
        assert await controller.async_start(16, cause="plan_window") is False
        assert controller.paused_by_balancing
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
    hass: HomeAssistant, freezer: Any
) -> None:
    controller, starts = await _switch_charger(hass, mode="follows", status="Charging")
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


async def test_an_owed_start_is_not_sent_once_its_window_is_over(hass: HomeAssistant, freezer: Any) -> None:
    controller, starts = await _switch_charger(hass)
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
    hass: HomeAssistant, freezer: Any, status: str
) -> None:
    controller, starts = await _switch_charger(hass, mode="follows", status=status)
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    for tick in range(12):
        await _after(hass, freezer, 300)
        hass.states.async_set(STATUS, status, {"tick": tick})
        await hass.async_block_till_done()
    assert len(starts) == 1, "a switch already on gets no stop-and-start kick"
    assert controller.car_ended_at is None
    assert controller.plan is not None and controller.plan_window_active_now


async def test_a_full_car_refusing_at_once_is_the_car_ending_the_charge(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The charger took the start (its switch is on) and the car says `SuspendedEV` from the first moment: a full car,
    recognised after `PLAN_CAR_IDLE_S`, and the start is not sent again."""
    controller, starts = await _switch_charger(hass, mode="follows", status="SuspendedEV")
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    assert len(starts) == 1
    for tick in range(14):
        await _after(hass, freezer, 30)
        hass.states.async_set(STATUS, "SuspendedEV", {"tick": tick})
        await hass.async_block_till_done()
    assert controller.car_ended_at is not None
    assert len(starts) == 1


async def test_a_start_the_car_refuses_is_not_sent_again(hass: HomeAssistant, freezer: Any) -> None:
    controller, starts = await _switch_charger(hass, status="SuspendedEV")
    await install_schedule(controller, _window())
    await _after(hass, freezer, 31)
    assert len(starts) == 1
    await _walk(hass, freezer, 1200, step=60)
    assert len(starts) == 1
    assert controller.window_start_retry["last"]["result"] == "car_refuses"
