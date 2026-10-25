"""A command made before the charger is ready is not counted as done.

The field case (an OCPP HALO, twice on 2026-10-17): Home Assistant restarts, SpotNav loads before the OCPP
integration, and its re-arm decides to stop a charge before the plan's next window. The charge control does not
exist yet, so nothing was sent, but the stop was recorded as done (`unobserved` in the core's `CommandResult`).
When the OCPP entities appeared some 40 s later, the charger charged on its own at 16 A until the window opened.

Now such a command is owed: nothing of the charge is forgotten, the charger is looked at until it is ready, and
then the decision is taken again on what holds at that moment (a window that opened meanwhile starts, it is not
stopped), or given up on, logged once, after `OWED_GIVE_UP_S`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    CURRENT_CONTROL_NUMBER,
)
from custom_components.spotnav.execution.chargers.easee import EaseeCommandPath
from custom_components.spotnav.execution.chargers.generic import ButtonPath, NumberCurrent, SwitchPath
from custom_components.spotnav.execution.chargers.ocpp import OcppAssignedCurrent
from custom_components.spotnav.execution.charger_profiles import WritePolicy
from custom_components.spotnav.execution.controller import OWED_GIVE_UP_S, ChargingController
from custom_components.spotnav.vehicles.ocpp_identity import OcppConnectorTarget

from .helpers import install_schedule

pytestmark = pytest.mark.usefixtures("offline_relay")

CONTROL = "switch.a"
OCPP_CONFIG: dict[str, Any] = {
    CONF_CHARGE_CONTROL: CONTROL,
    CONF_CURRENT_LIMIT: "number.charger_connector_1_session_current_limit",
    CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
    CONF_OCPP_CHARGE_POINT_ID: "charger",
    CONF_OCPP_CONNECTOR_ID: 1,
}

_CONTROLLERS: list[ChargingController] = []


@pytest.fixture(autouse=True)
async def _shut_down_controllers():
    yield
    while _CONTROLLERS:
        await _CONTROLLERS.pop().async_shutdown()


class Charger:
    """An OCPP charger whose integration loads when told: its charge control (a switch that follows the
    commands) and its `get_configuration`/`configure` services."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.starts: list[Any] = []
        self.stops: list[Any] = []
        self.configured: list[str] = []
        self.assigned = "1.16"

        def follow(calls: list[Any], state: str):
            async def handle(call: Any) -> None:
                calls.append(call)
                if hass.states.get(CONTROL) is not None:
                    hass.states.async_set(CONTROL, state)

            return handle

        # The switch domain is Home Assistant's own: its services exist from the start.
        hass.services.async_register("switch", "turn_on", follow(self.starts, "on"))
        hass.services.async_register("switch", "turn_off", follow(self.stops, "off"))

    def load(self, control: str) -> None:
        """The OCPP integration has loaded: the charge control reads `control`, its services exist."""
        self.hass.states.async_set(CONTROL, control)

        async def get_configuration(call: Any) -> dict[str, Any]:
            return {"value": self.assigned}

        async def configure(call: Any) -> None:
            self.configured.append(call.data["value"])
            self.assigned = call.data["value"]

        from homeassistant.core import SupportsResponse

        self.hass.services.async_register(
            "ocpp", "get_configuration", get_configuration, supports_response=SupportsResponse.ONLY
        )
        self.hass.services.async_register("ocpp", "configure", configure)

    def unload(self) -> None:
        """Home Assistant restarts: nothing of the OCPP integration exists yet."""
        self.hass.states.async_remove(CONTROL)
        for service in ("get_configuration", "configure"):
            self.hass.services.async_remove("ocpp", service)


async def _controller(hass: HomeAssistant, config: dict[str, Any] = OCPP_CONFIG) -> ChargingController:
    controller = ChargingController(hass, "entry_a", config)
    _CONTROLLERS.append(controller)
    await controller.async_initialize()
    return controller


def _window(start_in: timedelta, length: timedelta = timedelta(hours=1), amps: int = 10) -> dict[str, Any]:
    start = dt_util.utcnow() + start_in
    return {"start": start.isoformat(), "end": (start + length).isoformat(), "amps": amps}


async def _restart_before_the_charger(
    hass: HomeAssistant, charger: Charger, plan: dict[str, Any], config: dict[str, Any] = OCPP_CONFIG
) -> ChargingController:
    """A plan installed while the charger was there, then a restart in which SpotNav loads first."""
    charger.load("off")
    before = await _controller(hass, config)
    await install_schedule(before, plan)
    await hass.async_block_till_done()
    _CONTROLLERS.remove(before)
    await before.async_shutdown()
    charger.starts.clear()
    charger.stops.clear()
    charger.configured.clear()
    charger.unload()
    controller = await _controller(hass, config)
    await hass.async_block_till_done()
    return controller


def _core_agrees(controller: ChargingController) -> None:
    """The core, in shadow or driving, came to what today's code did: no disagreement and no drift."""
    shadow = controller._shadow.diagnostics()  # noqa: SLF001 - the comparison is what is tested
    assert shadow["disagreements"] == [], shadow["disagreements"]
    assert shadow["drift"] == [], shadow["drift"]


async def _after(hass: HomeAssistant, freezer, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# ------------------------------------------------------------------------------------------ the field case


async def test_a_stop_before_the_window_goes_out_once_the_charger_is_ready(
    hass: HomeAssistant, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    charger = Charger(hass)
    controller = await _restart_before_the_charger(hass, charger, _window(timedelta(hours=2)))

    assert charger.stops == [], "nothing could be sent: the charge control does not exist yet"
    assert controller.commands_owed, "the re-arm's stop is owed, not done"
    assert "not ready yet" in caplog.text

    await _after(hass, freezer, 40)  # the OCPP integration loads: the charger charges by itself at 16 A
    charger.load("on")
    await _after(hass, freezer, 6)

    assert len(charger.stops) == 1, "the stop went out once the charger was ready"
    assert hass.states.get(CONTROL).state == "off", "the charger does not keep charging until the window"
    assert not controller.commands_owed

    await _after(hass, freezer, 60)
    assert len(charger.stops) == 1, "sent once"
    _core_agrees(controller)


async def test_a_window_start_goes_out_once_the_charger_is_ready_at_the_plan_current(
    hass: HomeAssistant, freezer
) -> None:
    charger = Charger(hass)
    controller = await _restart_before_the_charger(hass, charger, _window(-timedelta(minutes=10), amps=10))

    assert charger.starts == [] and charger.configured == []
    assert controller.commands_owed, "the window's start (and its current) is owed"

    await _after(hass, freezer, 40)
    charger.load("off")
    await _after(hass, freezer, 6)

    assert len(charger.starts) == 1, "the window's charge starts once the charger is ready"
    assert charger.configured == ["1.10"], "at the plan's current, not the charger's 16 A"
    assert charger.stops == []
    assert controller.charge_origin == "plan_window"
    assert not controller.commands_owed
    _core_agrees(controller)


async def test_a_decision_that_changed_while_waiting_is_taken_as_it_is_now(hass: HomeAssistant, freezer) -> None:
    """The stop was owed before the window; the window opened while the charger was still loading: once it is
    ready the charge is started, never the stale stop sent."""
    charger = Charger(hass)
    controller = await _restart_before_the_charger(hass, charger, _window(timedelta(seconds=20), amps=10))
    assert controller.commands_owed

    await _after(hass, freezer, 25)  # the window opens; the charger is still not there
    assert charger.starts == [] and charger.stops == []

    await _after(hass, freezer, 15)
    charger.load("off")
    await _after(hass, freezer, 6)

    assert charger.stops == [], "the stop owed from before the window is not replayed"
    assert len(charger.starts) == 1
    assert charger.configured == ["1.10"]
    _core_agrees(controller)


async def test_a_charger_that_never_becomes_ready_is_given_up_on_once(
    hass: HomeAssistant, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    charger = Charger(hass)
    controller = await _restart_before_the_charger(hass, charger, _window(timedelta(hours=2)))
    assert controller.commands_owed

    for _ in range(int(OWED_GIVE_UP_S // 60) + 2):
        await _after(hass, freezer, 60)

    assert not controller.commands_owed
    assert caplog.text.count("is given up") == 1

    charger.load("on")
    await _after(hass, freezer, 60)
    assert charger.stops == [], "nothing is sent after the give-up"


async def test_a_current_write_whose_entity_is_not_there_yet_is_written_once_it_is(
    hass: HomeAssistant, freezer
) -> None:
    """A charger whose switch loads before its current number: the start goes out, the current is owed and
    written, as the request stands then, once the number exists."""
    hass.states.async_set(CONTROL, "off")
    charger = Charger(hass)
    writes = async_mock_service(hass, "number", "set_value")
    config = {CONF_CHARGE_CONTROL: CONTROL, CONF_CURRENT_LIMIT: "number.limit", CONF_CURRENT_CONTROL: CURRENT_CONTROL_NUMBER}
    controller = await _controller(hass, config)
    await install_schedule(controller, _window(-timedelta(minutes=10), amps=10))
    await hass.async_block_till_done()

    assert len(charger.starts) == 1
    assert writes == [] and controller.commands_owed

    await _after(hass, freezer, 40)
    hass.states.async_set("number.limit", "16", {"min": 6, "max": 16, "step": 1, "unit_of_measurement": "A"})
    await _after(hass, freezer, 6)

    assert [call.data["value"] for call in writes] == [10]
    assert not controller.commands_owed


async def test_a_ready_charger_known_off_needs_no_stop_and_owes_nothing(hass: HomeAssistant) -> None:
    charger = Charger(hass)
    charger.load("off")
    controller = await _controller(hass)
    await install_schedule(controller, _window(timedelta(hours=2)))
    await hass.async_block_till_done()

    assert charger.stops == []
    assert not controller.commands_owed


# ------------------------------------------------------------------------------------------ readiness


async def test_paths_are_not_ready_before_their_entities_and_services_exist(hass: HomeAssistant) -> None:
    switch = SwitchPath(hass, "switch.x")
    buttons = ButtonPath(hass, "button.start", "button.stop")
    easee = EaseeCommandPath(hass, "device")
    ocpp = OcppAssignedCurrent(hass, lambda: OcppConnectorTarget("charger", 1))
    number = NumberCurrent(hass, "number.x", WritePolicy(), enabled=lambda: True)
    assert not any(path.ready() for path in (switch, buttons, easee, ocpp, number))

    for state in ("unavailable", "unknown"):
        hass.states.async_set("switch.x", state)
        assert not switch.ready(), state
    hass.states.async_set("switch.x", "off")
    assert switch.ready()

    hass.states.async_set("button.start", "unknown")  # a button never pressed
    hass.states.async_set("button.stop", "unavailable")
    assert not buttons.ready()
    hass.states.async_set("button.stop", "unknown")
    assert buttons.ready()

    async_mock_service(hass, "easee", "action_command")
    assert easee.ready()

    async_mock_service(hass, "ocpp", "get_configuration")
    assert not ocpp.ready()
    async_mock_service(hass, "ocpp", "configure")
    assert ocpp.ready()

    hass.states.async_set("number.x", "16", {"unit_of_measurement": "A"})
    assert number.ready()
