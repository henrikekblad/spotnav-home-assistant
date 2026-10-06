"""One off decision, one stop: a stop already on its way is not sent again by another path.

The field trace of 2026-10-06 (an OCPP Charge Amps HALO): at 02:30:00 the last window's end stopped the charge and
the re-arm of the plan installed a moment later stopped it again 0.25 s after; at 03:30:14 two balancing pauses
did the same. The charger had not answered the first stop yet, so its control still read on; the second stop met
no transaction and the charger rejected it ("Stop transaction failed with response Rejected").
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import CONF_CHARGE_CONTROL
from custom_components.spotnav.execution.controller import ChargingController, STOP_SETTLE_S

from .helpers import install_schedule

pytestmark = pytest.mark.usefixtures("offline_relay")

_SWITCH = "switch.a"


def _lagging_switch(hass: HomeAssistant) -> tuple[list[Any], list[Any]]:
    """`switch.turn_on`/`turn_off`, recorded; the switch moves only when the charger reports (`_report`)."""
    starts: list[Any] = []
    stops: list[Any] = []

    async def start(call: Any) -> None:
        starts.append(call)

    async def stop(call: Any) -> None:
        stops.append(call)

    hass.services.async_register("switch", "turn_on", start)
    hass.services.async_register("switch", "turn_off", stop)
    return starts, stops


async def _report(hass: HomeAssistant, state: str) -> None:
    hass.states.async_set(_SWITCH, state)
    await hass.async_block_till_done()


async def _charging(hass: HomeAssistant, plan: dict[str, Any] | None = None) -> tuple[ChargingController, list, list]:
    hass.states.async_set(_SWITCH, "off")
    starts, stops = _lagging_switch(hass)
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: _SWITCH})
    await controller.async_initialize()
    if plan is not None:
        await install_schedule(controller, plan)
    else:
        await controller.async_start(10, manual=True)
    await _report(hass, "on")
    assert controller.charging
    return controller, starts, stops


def _window(minutes_left: float) -> dict[str, Any]:
    now = dt_util.utcnow()
    return {
        "start": (now - timedelta(minutes=10)).isoformat(),
        "end": (now + timedelta(minutes=minutes_left)).isoformat(),
        "amps": 10,
    }


def _later_window() -> dict[str, Any]:
    start = dt_util.utcnow() + timedelta(hours=2)
    return {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "amps": 10}


async def test_0330_two_balancing_pauses_send_one_stop(hass: HomeAssistant) -> None:
    controller, _starts, stops = await _charging(hass)
    await controller._regulated_stop("pause")  # noqa: SLF001
    await controller._regulated_stop("pause")  # noqa: SLF001 - the next pass, the charger not answered yet
    assert len(stops) == 1
    await _report(hass, "off")
    assert controller.paused_by_balancing, "the second pause did not forget the charge the first holds back"
    await controller.async_shutdown()


async def test_0230_the_last_window_end_and_the_rearm_after_it_send_one_stop(hass: HomeAssistant) -> None:
    controller, _starts, stops = await _charging(hass, _window(30))
    assert controller.charge_origin == "plan_window"
    controller._async_final_end_callback(dt_util.utcnow())  # noqa: SLF001
    await hass.async_block_till_done()
    assert len(stops) == 1
    # Auto installs the next plan a moment later; the charger still reads on.
    await install_schedule(controller, _later_window())
    await hass.async_block_till_done()
    assert len(stops) == 1
    await controller.async_shutdown()


async def test_a_stop_after_the_charger_reported_charging_again_is_sent(hass: HomeAssistant) -> None:
    controller, _starts, stops = await _charging(hass)
    await controller._regulated_stop("pause")  # noqa: SLF001
    await _report(hass, "off")
    await _report(hass, "on")  # it charges again (a person at the charger, say)
    await controller._regulated_stop("pause")  # noqa: SLF001
    assert len(stops) == 2
    await controller.async_shutdown()


async def test_a_stop_the_charger_has_not_answered_is_sent_again_after_the_settle_time(
    hass: HomeAssistant, freezer
) -> None:
    controller, _starts, stops = await _charging(hass)
    await controller._regulated_stop("pause")  # noqa: SLF001
    freezer.tick(timedelta(seconds=STOP_SETTLE_S + 1))
    await controller._regulated_stop("pause")  # noqa: SLF001
    assert len(stops) == 2
    await controller.async_shutdown()
