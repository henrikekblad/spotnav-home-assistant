"""Regressions from the review of planning on plug-in, unplug and departure: a register that glitches or
spikes ends nothing, a target plan ends only on the target stop's own evidence, a charger's fault is not a
plug-in, a charger that cannot report a plug-in still counts a new need, and the starts and stops around a
plug-in keep every rule a start or a stop has.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time
from homeassistant.core import callback, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
)
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.execution.target_stop import SocReading
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.planning.auto_settings import DRIVER_MANUAL_KWH, TargetSocIntent
from tests.relay import serve as serve_prices

from .charger_helpers import detected_config
from .charger_shapes import register_shape, SHAPES
from .helpers import install_schedule
from .test_dashboard_api import NOW
from .test_replug import (
    _car,
    _delivered,
    _meter,
    _obedient_switch,
    _open_window,
    _record_charger_commands,
    _switch_controller,
    _turn_offs,
    _turn_ons,
    Car,
)
from .world import charger_and_car

pytestmark = pytest.mark.usefixtures("offline_relay")


# ------------------------------------------------------------------- H1, H2: the register's word


async def test_a_lifetime_register_that_reads_zero_during_a_reboot_ends_nothing(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=10.0, departure=time(20, 0), departure_enabled=True)
        assert car.controller.plan is not None and _turn_ons(calls) == 1
        await _delivered(hass, frozen, 0.0, minutes=1)  # the charger reboots
        await car.preview.async_recalculate()
        await _delivered(hass, frozen, 1000.3, minutes=1)  # its true lifetime value again
        await car.preview.async_recalculate()

        assert car.controller.plan is not None and _turn_offs(calls) == 0
        assert car.preview.snapshot().remaining_kwh == pytest.approx(9.7)
        assert car.baseline().register_kwh == pytest.approx(1000.0) and car.baseline().carried_kwh == 0.0


async def test_one_false_high_reading_neither_ends_the_plan_nor_is_carried(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=10.0, departure=time(20, 0), departure_enabled=True)
        await _delivered(hass, frozen, 1000.2, minutes=1)
        await car.preview.async_recalculate()
        await _delivered(hass, frozen, 1100.0, minutes=1)  # one bogus reading
        assert car.controller.plan is not None and _turn_offs(calls) == 0
        await _delivered(hass, frozen, 1000.4, minutes=1)  # the true value again
        await car.preview.async_recalculate()

        assert car.preview.snapshot().state == "proposal_ready"
        assert car.preview.snapshot().remaining_kwh == pytest.approx(9.6)


# --------------------------------------------------------------------- M1: a target plan's word


async def test_a_target_plan_is_not_cleared_on_an_estimate_within_its_margin(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.a", "off")
    _obedient_switch(hass)
    estimate = SocReading(soc_percent=80.5, source="test", entity_id=None, vehicle_id="car", age_s=600.0, estimated=True)
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"}, soc_reader=lambda _v: estimate)
    await controller.async_initialize()
    start = dt_util.utcnow() + timedelta(hours=2)
    await install_schedule(controller, {
        "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "amps": 10,
        "target_soc_percent": 80, "vehicle_id": "car",
    })
    assert controller.plan is not None

    assert await controller.async_end_plan_need_met() is False
    assert controller.plan is not None, "an estimate needs its margin, as the target stop does"
    await controller.async_shutdown()


async def test_a_target_plan_at_a_measured_target_is_ended_by_the_target_stop(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.a", "off")
    _obedient_switch(hass)
    reading = SocReading(soc_percent=80.0, source="test", entity_id=None, vehicle_id="car", age_s=10.0)
    soc = {"reading": SocReading(soc_percent=50.0, source="test", entity_id=None, vehicle_id="car", age_s=10.0)}
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"}, soc_reader=lambda _v: soc["reading"])
    await controller.async_initialize()
    start = dt_util.utcnow() + timedelta(hours=2)
    await install_schedule(controller, {
        "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "amps": 10,
        "target_soc_percent": 80, "vehicle_id": "car",
    })
    soc["reading"] = reading

    assert await controller.async_end_plan_need_met() is True
    assert controller.plan is None and controller.target_stop_record is not None
    await controller.async_shutdown()


# ------------------------------------------------------ M2: a fault is not a vehicle, nor its absence


def _status_controller(hass: HomeAssistant, shape: str) -> tuple[ChargingController, str]:
    ids = register_shape(hass, SHAPES[shape])
    config = detected_config(detect_charger(hass, ids["device_id"]))
    controller = ChargingController(hass, f"entry_{shape}", config)
    status = controller.adapter.status_entity_id
    assert status
    return controller, status


@pytest.mark.parametrize(
    ("shape", "value", "connected"),
    [
        ("easee", "disconnected", False),
        ("easee", "awaiting_start", True),
        ("easee", "charging", True),
        ("easee", "error", None),
        ("easee", "offline", None),
        ("wallbox", "Disconnected", False),
        ("wallbox", "Charging", True),
        ("wallbox", "Locked, car connected", True),
        ("wallbox", "Error", None),
        ("wallbox", "Updating", None),
        ("wallbox", "Ready", None),
        ("wallbox", "Locked", None),
        ("zaptec", "disconnected", False),
        ("zaptec", "connected_charging", True),
        ("zaptec", "unknown_state", None),
        ("openevse", "not_connected", False),
        ("openevse", "charging", True),
        ("openevse", "error", None),
    ],
)
async def test_only_a_status_that_proves_a_vehicle_is_connected(
    hass: HomeAssistant, shape: str, value: str, connected: bool | None
) -> None:
    controller, status = _status_controller(hass, shape)
    hass.states.async_set(status, value)
    assert controller.adapter.vehicle_connected() is connected


@pytest.mark.parametrize(
    ("value", "connected"),
    [
        ("Available", False),
        ("Preparing", True),
        ("Charging", True),
        ("SuspendedEV", True),
        ("Finishing", True),
        ("Faulted", None),
        ("Unavailable", None),
        ("Reserved", None),
    ],
)
async def test_an_ocpp_connector_fault_says_nothing_about_the_vehicle(
    hass: HomeAssistant, value: str, connected: bool | None
) -> None:
    status = "sensor.charger_connector_1_status_connector"
    hass.states.async_set("switch.a", "off")
    controller = ChargingController(hass, "entry_a", {
        CONF_CHARGE_CONTROL: "switch.a",
        CONF_CURRENT_LIMIT: "number.charger_connector_1_session_current_limit",
        CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
        CONF_OCPP_CHARGE_POINT_ID: "charger",
        CONF_OCPP_CONNECTOR_ID: 1,
    })
    hass.states.async_set(status, value)
    assert controller.adapter.vehicle_connected() is connected


async def test_an_easee_that_goes_offline_and_back_is_not_a_plug_in(hass: HomeAssistant) -> None:
    controller, status = _status_controller(hass, "easee")
    hass.states.async_set(status, "charging")
    async_mock_service(hass, "easee", "action_command")
    await controller.async_initialize()
    heard: list[str] = []
    controller.set_connection_handler(lambda event: heard.append(event) or True)

    for value in ("error", "offline", "charging"):
        hass.states.async_set(status, value)
        await hass.async_block_till_done()

    assert heard == []
    await controller.async_shutdown()


# ---------------------------------- M3: a charger that cannot report a plug-in still counts a new need


async def test_without_plug_in_reports_a_new_charge_after_a_met_need_counts_afresh(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        charger, _, _ = await charger_and_car(
            hass, driver=DRIVER_MANUAL_KWH, requested_kwh=3.0, target=TargetSocIntent(), amps=16, phases=3
        )
        car = Car(hass, frozen, charger)  # its plug says nothing: `vehicle_connected` stays None
        assert car.baseline().departure_key == "no_deadline" and _turn_ons(calls) == 1
        await _delivered(hass, frozen, 1003.0)
        await _delivered(hass, frozen, 1003.1, minutes=1)
        assert car.controller.plan is None and car.baseline().met_at is not None
        await car.preview.async_recalculate()
        assert car.preview.snapshot().state == "nothing_to_charge"
        for _ in range(3):  # the charge is over, and the session with it
            frozen.tick(timedelta(minutes=1))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()

        hass.states.async_set("switch.wallbox", "on")  # a new charge begins (the car came back)
        await hass.async_block_till_done()
        await car.settle()

        assert car.baseline().departure_key.startswith("charge:"), "a new count from the new charge"
        assert car.preview.snapshot().remaining_kwh == pytest.approx(3.0)
        assert car.controller.plan is not None


# ---------------------------------------------- L1, L2: one start per plug-in, and balancing decides


def _record_only(hass: HomeAssistant) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    @callback
    def on_call(event: Any) -> None:
        data = event.data
        if data["service"] in ("turn_on", "turn_off"):
            calls.append((data["service"], str(data.get("service_data", {}).get("entity_id"))))

    hass.bus.async_listen("call_service", on_call)
    return calls


async def test_one_start_when_the_plug_in_replan_installs_a_plan_with_its_window_open(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0)
        await _delivered(hass, frozen, 1003.0)
        await _delivered(hass, frozen, 1003.1, minutes=1)
        assert car.controller.plan is None
        await car.unplug()
        slow = _record_only(hass)
        # A charger whose control does not say "on" until the car answers.
        car.controller.adapter.enabled_state = lambda: None  # type: ignore[method-assign]
        car.controller.adapter.charging_state = lambda: False  # type: ignore[method-assign]
        frozen.move_to(dt_util.parse_datetime("2026-09-22T06:44:54+00:00"))
        await car.plug.set(True)
        await car.settle()

        assert car.controller.plan is not None
        assert len([c for c in slow if c[0] == "turn_on"]) == 1, "one start for one plug-in"


async def test_a_charge_balancing_paused_is_resumed_by_the_regulator_not_the_plug_in(hass: HomeAssistant) -> None:
    allowance = {"a": 3.0}
    hass.states.async_set("switch.a", "off")
    starts, _ = _obedient_switch(hass)
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    controller.set_start_cap(lambda: allowance["a"])
    await controller.async_initialize()
    from .test_replug import Plug

    plug = Plug(hass, controller, "switch.a")
    await plug.set(True)
    await install_schedule(controller, _open_window())
    assert starts == [] and controller.paused_by_balancing
    allowance["a"] = 6.5  # headroom, but within the regulator's resume margin
    await plug.set(False)

    await plug.set(True)

    assert starts == [] and controller.paused_by_balancing, "the regulator's resume decides"
    await controller.async_shutdown()


# ------------------------------------------------------ L3: a met need stops only the plan's charge


@pytest.mark.parametrize("owner", ["hold_guard", "end_window_guard"])
async def test_a_met_need_does_not_stop_a_charge_the_plan_does_not_own(hass: HomeAssistant, owner: str) -> None:
    controller, plug, starts, stops = await _switch_controller(hass, None)
    await install_schedule(controller, _open_window())
    await plug.set(True, control="on")
    assert controller.charge_origin == "plan_window"
    if owner == "hold_guard":
        controller.set_hold_guard(lambda: True)
    else:
        controller.set_end_window_guard(lambda: True)
    stops.clear()

    assert await controller.async_end_plan_need_met() is True

    assert controller.plan is None and stops == []
    await controller.async_shutdown()


# ---------------------------------------------------------- L5: the energy stop needs no planner


async def test_the_energy_stop_ends_the_plan_without_a_planner_pass_and_asks_it_once(
    hass: HomeAssistant, transport: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0)
        recalculate = AsyncMock()
        # A planner that gets nowhere (prices missing, say) and is only counted.
        monkeypatch.setattr(car.preview, "async_recalculate", recalculate)

        await _delivered(hass, frozen, 1003.0)
        await _delivered(hass, frozen, 1003.1, minutes=1)

        assert car.controller.plan is None and _turn_offs(calls) == 1
        for kwh in (1003.2, 1003.3, 1003.4):
            await _delivered(hass, frozen, kwh, minutes=1)
        assert recalculate.await_count == 1, "asked once, not on every reading"


# ---------------------------------------------------------- L6: where the fallbacks count from


async def test_a_plug_ins_count_begins_at_the_plug_in(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen)
        await car.unplug()
        await car.replug()
        assert car.baseline().started_at == car.controller.plugged_in_at, "not the first calculation after it"


async def test_a_kept_count_follows_a_changed_request(hass: HomeAssistant, transport: Any) -> None:
    from dataclasses import replace

    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, departure=time(20, 0), departure_enabled=True)
        await _delivered(hass, frozen, 1003.5, minutes=20)
        await car.preview.async_recalculate()
        _meter(hass, None)
        await hass.async_block_till_done()

        await car.preview.async_apply_settings(mutate=lambda settings: replace(settings, requested_kwh=12.0))
        assert car.preview.snapshot().energy_basis == "kept"
        assert car.preview.snapshot().remaining_kwh == pytest.approx(8.5), "twelve less the 3.5 delivered"
        await car.preview.async_apply_settings(mutate=lambda settings: replace(settings, requested_kwh=5.0))
        assert car.preview.snapshot().remaining_kwh == pytest.approx(1.5)

