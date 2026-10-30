"""Hardware quirks other projects found, replayed against SpotNav: what the controller and the site make of them.

* ABB Terra AC: a firmware self-reset puts the current limit back at 32 A (JernejHren/ABB-Terra-AC #7), so a charge
  SpotNav pauses at 0 A can start again by itself.
* GARO: with Always off and a car connected, the status reads UNAVAILABLE instead of CHARGING_PAUSED
  (sockless-coding/garo_wallbox #61).
* Shelly Pro 3EM and EM mini Gen4: the device reconnects every 2-3 minutes and its sensors read unavailable for
  seconds (home-assistant/core #170327).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.planning.status_compose import compose_status
from custom_components.spotnav.site.measurement_problem import UNHEALTHY_STATES

from .helpers import install_schedule
from .test_charger_controller_paths import _controller
from .test_lb_start_credit import _halo, _site_a

pytestmark = pytest.mark.usefixtures("offline_relay")

#: The status lines that name the site's measurement as unusable.
METER_LINES = ("site_meter_unavailable", "site_measurement_problem")

ABB_LIMIT = "number.abb_terra_ac_current_limit"
ABB_STATUS = "sensor.abb_terra_ac_charging_state"
ABB_ATTRS = {"unit_of_measurement": "A", "min": 0, "max": 32, "step": 1}


def _later_window() -> dict[str, Any]:
    start = dt_util.utcnow() + timedelta(hours=2)
    return {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "amps": 10}


def _obedient_abb(hass: HomeAssistant) -> list[float]:
    """`number.set_value` on the ABB's limit, recorded, with the number following it as the charger does."""
    written: list[float] = []

    async def handle(call: ServiceCall) -> None:
        written.append(call.data["value"])
        hass.states.async_set(call.data["entity_id"], str(call.data["value"]), ABB_ATTRS)

    hass.services.async_register("number", "set_value", handle)
    hass.services.async_register("button", "press", lambda call: None)
    return written


async def _abb_reset(hass: HomeAssistant) -> None:
    """The firmware resets: the limit is 32 A again and the car charges."""
    hass.states.async_set(ABB_LIMIT, "32", ABB_ATTRS)
    hass.states.async_set(ABB_STATUS, "State C2 - Charging")
    await hass.async_block_till_done()


async def test_an_abb_that_resets_while_paused_outside_the_windows_is_paused_again_once(hass: HomeAssistant) -> None:
    """The start button reads `unknown` until it is first pressed (most ABBs need no authorization), so the charge is
    read on the limit: paused at 0 A is off, and the reset's 32 A a charge that started by itself, held once. A second
    one in the same plug-in cannot be told from a person raising the limit, and is left to run as their start."""
    controller = await _controller(hass, "abb_terra_ac")
    written = _obedient_abb(hass)
    hass.states.async_set(ABB_LIMIT, "0", ABB_ATTRS)
    hass.states.async_set(ABB_STATUS, "State B2 - EV Plug in, charging complete")
    await hass.async_block_till_done()
    await install_schedule(controller, _later_window())
    assert hass.states.get("button.abb_terra_ac_start_charging").state == "unknown"
    assert controller.commands_owed is False

    await _abb_reset(hass)

    assert written == [0], "paused again at once"
    hass.states.async_set(ABB_STATUS, "State B2 - EV Plug in, charging complete")
    await hass.async_block_till_done()

    await _abb_reset(hass)

    assert written == [0], "a second start in the plug-in is a person's"
    await controller.async_shutdown()


GARO_MODE = "select.garo_wallbox_sensor"
GARO_STATUS = "sensor.garo_wallbox_status"


def _obedient_garo(hass: HomeAssistant) -> list[str]:
    """`select.select_option` on the GARO's mode, recorded, with the select following it."""
    chosen: list[str] = []

    async def handle(call: ServiceCall) -> None:
        chosen.append(call.data["option"])
        current = hass.states.get(call.data["entity_id"])
        hass.states.async_set(call.data["entity_id"], call.data["option"], None if current is None else current.attributes)

    hass.services.async_register("select", "select_option", handle)
    return chosen


@pytest.mark.parametrize("status", ["UNAVAILABLE", "unavailable"])
async def test_a_garo_whose_status_reads_unavailable_once_stopped_keeps_its_car_and_is_stopped_once(
    hass: HomeAssistant, freezer, status: str
) -> None:
    """Stopped with Always off and a car connected, the status reads UNAVAILABLE (whether the text or Home Assistant's
    own state): it says nothing, so the car is not taken as gone, the stop is not repeated, the card names no
    connection, and the next window starts the charge as on any stopped charger."""
    controller = await _controller(hass, "garo_wallbox")
    chosen = _obedient_garo(hass)
    heard: list[str] = []
    controller.set_connection_handler(lambda event: heard.append(event) or True)
    hass.states.async_set(GARO_STATUS, "CHARGING")
    await hass.async_block_till_done()
    assert controller.adapter.vehicle_connected() is True

    await controller.async_stop()
    hass.states.async_set(GARO_STATUS, status)
    await hass.async_block_till_done()
    for _ in range(10):
        freezer.tick(timedelta(seconds=60))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert chosen == ["ALWAYS_OFF"]
    assert controller.charging is False and controller.commands_owed is False
    assert controller.adapter.vehicle_connected() is None
    assert heard == []
    assert controller.connection() == ("unknown", GARO_STATUS)

    now = dt_util.utcnow()
    await install_schedule(
        controller, {"start": (now - timedelta(minutes=5)).isoformat(), "end": (now + timedelta(hours=1)).isoformat(), "amps": 10}
    )
    await hass.async_block_till_done()

    assert chosen == ["ALWAYS_OFF", "ALWAYS_ON"]
    await controller.async_shutdown()


async def test_a_meter_that_drops_out_for_five_seconds_neither_stops_nor_changes_a_balanced_charge(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Every sensor of the meter reads unavailable for about 5 s every 2-3 minutes: the site's measurement is missing
    for that long, and the charge goes on at what it was set to, with nothing written, until the meter reads again.
    The card's status says nothing of it: a dropout that short is nothing the person can act on."""
    halo = await _halo(hass, monkeypatch)
    await halo.start()
    halo.switch("on")
    halo.reads(13.0)
    _site_a(hass, halo.site, 14.63, 13.92, 19.36)
    await halo.tick()
    halo.advance(30.0)
    await halo.tick()
    assert halo.controller.result.state not in UNHEALTHY_STATES
    halo.configure.clear()

    _meter_gone(hass, halo)
    await halo.tick()
    halo.advance(5.0)
    await halo.tick()

    assert halo.controller.result.state == "missing_measurements"
    assert halo.turn_off == [] and halo.written() == []
    assert halo.cc.charging
    assert _meter_lines(hass, halo) == []

    _meter_back(hass, halo)
    await halo.tick()

    assert halo.controller.result.state not in UNHEALTHY_STATES
    assert halo.turn_off == [] and halo.written() == []
    assert _meter_lines(hass, halo) == []


async def test_a_meter_lost_for_over_a_minute_is_named_on_the_card_until_it_reads_again(
    hass: HomeAssistant, monkeypatch
) -> None:
    """The same meter gone for 70 s: past the minute load balancing waits before it holds the charger at its
    minimum, the card's status names the unavailable meter; the line goes as soon as the meter reads again."""
    halo = await _halo(hass, monkeypatch)
    await halo.start()
    halo.switch("on")
    halo.reads(13.0)
    _site_a(hass, halo.site, 14.63, 13.92, 19.36)
    await halo.tick()
    halo.advance(30.0)
    await halo.tick()

    _meter_gone(hass, halo)
    await halo.tick()
    halo.advance(59.0)
    await halo.tick()
    assert halo.controller.result.state == "missing_measurements"
    assert _meter_lines(hass, halo) == []

    halo.advance(11.0)
    await halo.tick()
    assert _meter_lines(hass, halo) == ["site_meter_unavailable"]

    _meter_back(hass, halo)
    await halo.tick()
    assert halo.controller.result.state not in UNHEALTHY_STATES
    assert _meter_lines(hass, halo) == []

    # A new dropout starts its own minute.
    _meter_gone(hass, halo)
    await halo.tick()
    halo.advance(5.0)
    await halo.tick()
    assert _meter_lines(hass, halo) == []


def _meter_gone(hass: HomeAssistant, halo: Any) -> None:
    """The Shelly reconnects: every sensor of the meter reads unavailable."""
    for phase in ("l1", "l2", "l3"):
        for kind in ("power", "reactive", "voltage"):
            hass.states.async_set(f"sensor.{halo.site}_{kind}_{phase}", "unavailable")


def _meter_back(hass: HomeAssistant, halo: Any) -> None:
    for phase in ("l1", "l2", "l3"):
        hass.states.async_set(f"sensor.{halo.site}_reactive_{phase}", "0", {"unit_of_measurement": "var"})
        hass.states.async_set(f"sensor.{halo.site}_voltage_{phase}", "230", {"unit_of_measurement": "V"})
    _site_a(hass, halo.site, 14.63, 13.92, 19.36)


def _meter_lines(hass: HomeAssistant, halo: Any) -> list[str]:
    """The card's status lines about the site's measurement for the charger."""
    capture = dashboard_api.capture_dashboard(hass, halo.charger)
    facts = replace(dashboard_api.status_facts(capture), planning=None, has_settings=False)
    return [line["code"] for line in compose_status(facts)["lines"] if line["code"] in METER_LINES]
