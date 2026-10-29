"""A window's start ends the plug-in session's hold (`WindowHold.end_session`): the core is fed that start where
today's code ends the hold, also when the target ends the plan instead, so a replay from the first recorded session
with no line-up finds no difference in the hold."""

from __future__ import annotations

from datetime import timedelta
import json
from typing import Any

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import CONF_CHARGE_CONTROL
from custom_components.spotnav.core.replay import replay, ReplayReport
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.execution.target_stop import SocReading

from .helpers import install_schedule


def _plan(**extra: Any) -> dict[str, Any]:
    start = dt_util.utcnow() + timedelta(hours=2)
    return {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "amps": 10, **extra}


async def _held(hass: HomeAssistant, soc: dict[str, float], **plan: Any) -> tuple[ChargingController, dict, list]:
    """A charger that began by itself between windows, which the hold stopped: the plug-in session is held."""
    hass.states.async_set("switch.a", "off")
    controller = ChargingController(
        hass,
        "entry_a",
        {CONF_CHARGE_CONTROL: "switch.a"},
        soc_reader=lambda _vehicle: SocReading(
            soc_percent=soc["percent"], source="vehicle", entity_id=None, vehicle_id="car", age_s=0.0
        ),
    )
    await controller.async_initialize()
    payload = _plan(**plan)
    await install_schedule(controller, payload)
    async_mock_service(hass, "switch", "turn_off")
    starts = async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set("switch.a", "on")
    await hass.async_block_till_done()
    hass.states.async_set("switch.a", "off")
    await hass.async_block_till_done()
    assert controller.hold_until is not None, "held until the window"
    return controller, payload, starts


def _replayed(controller: ChargingController) -> tuple[ReplayReport, list[dict[str, Any]]]:
    events = json.loads(json.dumps(controller.ownership_shadow.diagnostics()["events"], default=str))
    return replay(events, line_up=False), events


def _hold_differences(report: ReplayReport) -> list[Any]:
    return [item for item in report.real if item.field in ("held", "overridden")]


async def test_the_windows_start_is_fed_as_it_ends_the_hold(hass: HomeAssistant) -> None:
    controller, plan, starts = await _held(hass, {"percent": 50.0})

    with freeze_time(dt_util.parse_datetime(plan["start"]) + timedelta(seconds=1)):
        await controller._async_start_window()  # noqa: SLF001 - the timer's own entry

    assert len(starts) == 1
    report, events = _replayed(controller)
    (start,) = [record for record in events if record["event"]["kind"] == "window_start"]
    assert start["pre"].get("held") is True, "lined up before the hold ended: the core ends it"
    assert _hold_differences(report) == [], report.real
    await controller.async_shutdown()


async def test_a_window_start_the_target_ends_is_fed_too(hass: HomeAssistant) -> None:
    soc = {"percent": 50.0}
    controller, plan, starts = await _held(hass, soc, target_soc_percent=80.0, vehicle_id="car")
    soc["percent"] = 85.0  # charged elsewhere meanwhile

    with freeze_time(dt_util.parse_datetime(plan["start"]) + timedelta(seconds=1)):
        await controller._async_start_window()  # noqa: SLF001 - the timer's own entry

    assert starts == [] and controller.plan is None
    report, events = _replayed(controller)
    kinds = [record["event"]["kind"] for record in events]
    assert "window_start" in kinds and kinds.index("window_start") < kinds.index("target_reached"), kinds
    (start,) = [record for record in events if record["event"]["kind"] == "window_start"]
    assert start["event"]["target_reached"] is True and start["core"] == []
    assert _hold_differences(report) == [], report.real
    await controller.async_shutdown()


async def test_a_window_start_while_the_targets_stop_is_on_its_way_is_fed_too(hass: HomeAssistant) -> None:
    controller, plan, starts = await _held(hass, {"percent": 50.0})
    controller._target_stopping = True  # noqa: SLF001 - the stop's await, held open

    with freeze_time(dt_util.parse_datetime(plan["start"]) + timedelta(seconds=1)):
        await controller._async_start_window()  # noqa: SLF001 - the timer's own entry

    controller._target_stopping = False  # noqa: SLF001
    assert starts == []
    report, events = _replayed(controller)
    (start,) = [record for record in events if record["event"]["kind"] == "window_start"]
    assert start["event"]["target_stopping"] is True and start["core"] == []
    assert _hold_differences(report) == [], report.real
    assert controller.ownership_shadow.session.held is False
    await controller.async_shutdown()
