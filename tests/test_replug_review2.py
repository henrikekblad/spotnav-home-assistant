"""Regressions from the second review of planning on plug-in, unplug and departure: a charge the charger
starts by itself inside a window is the plan's, a person's or the sun's charge is never a new need nor
stopped by the plan it led to, a register that restarts keeps what it counted, a faster charger is still
believed, and the energy stop acts on the first believed reading.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.planning.auto_controller import advance_register, CEILING_A
from custom_components.spotnav.planning.auto_settings import EnergyBaseline
from custom_components.spotnav.planning.planner import power_kw
from tests.relay import serve as serve_prices

from .helpers import install_schedule
from .test_dashboard_api import NOW
from .test_replug import (
    _car,
    _delivered,
    _later_window,
    _meter,
    _record_charger_commands,
    _switch_controller,
    _turn_offs,
    _turn_ons,
)

pytestmark = pytest.mark.usefixtures("offline_relay")

T0 = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)


# ------------------------------------------------------- R1: a charge that began by itself in a window


async def test_a_self_started_charge_in_its_window_is_stopped_when_the_need_is_met(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0)
        await car.unplug()
        await car.plug.set(True, control="on")  # the charger starts by itself at plug-in, in the window
        await car.settle()
        assert car.controller.charge_origin == "plan_window", "the plan's charge"
        offs = _turn_offs(calls)

        await _delivered(hass, frozen, 1003.1, minutes=12)

        assert car.controller.plan is None
        assert _turn_offs(calls) == offs + 1
        assert hass.states.get("switch.wallbox").state == "off"


async def test_a_self_started_charge_does_not_run_on_past_its_window(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0)
        await car.unplug()
        await car.plug.set(True, control="on")
        await car.settle()
        await _delivered(hass, frozen, 1003.1, minutes=12)

        frozen.move_to(dt_util.parse_datetime("2026-09-22T09:00:00+00:00"))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert hass.states.get("switch.wallbox").state == "off" and _turn_offs(calls) >= 1


async def test_a_persons_start_the_charger_has_not_answered_yet_stays_the_persons(hass: HomeAssistant) -> None:
    controller, plug, starts, stops = await _switch_controller(hass, None)
    await install_schedule(controller, {**_later_window(), "start": (dt_util.utcnow() - timedelta(minutes=5)).isoformat()})
    await plug.set(True, control="off")
    await controller.async_stop()
    stops.clear()
    controller.adapter.enabled_state = lambda: False  # type: ignore[method-assign] - a slow charger
    await controller.async_start(manual=True)
    assert controller.charge_origin == "manual", "kept while the charger has not answered"
    controller.adapter.enabled_state = lambda: True  # type: ignore[method-assign]
    await plug.set(True, control="on")
    assert controller.charge_origin == "manual", "never claimed as the plan's"
    await controller.async_shutdown()


# ------------------------------------------- R2: a person's or the sun's charge is not a new need


async def _met_without_plug_in_reports(hass: HomeAssistant, transport: Any, frozen: Any):
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    car = await _car(hass, frozen, kwh=3.0)  # the plug never reported a change: no plug-in seen
    assert car.controller.plugged_in_at is None
    await _delivered(hass, frozen, 1003.1, minutes=12)
    assert car.controller.plan is None and car.baseline().met_at is not None
    for _ in range(3):  # the charge and its session are over
        frozen.tick(timedelta(minutes=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    return car, calls


async def test_charge_now_after_a_met_need_is_neither_a_new_need_nor_stopped(
    hass: HomeAssistant, transport: Any
) -> None:
    with freeze_time(NOW) as frozen:
        car, calls = await _met_without_plug_in_reports(hass, transport, frozen)
        key = car.baseline().departure_key
        offs = _turn_offs(calls)

        await car.executor.async_manual_start()  # a person's Charge now
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        await car.settle()
        await car.settle()

        assert car.baseline().departure_key == key, "a person's charge is not a new need"
        assert _turn_offs(calls) == offs, "the person's Charge now goes on"
        assert hass.states.get("switch.wallbox").state == "on"


async def test_a_solar_charge_after_a_met_need_is_neither_a_new_need_nor_stopped(
    hass: HomeAssistant, transport: Any
) -> None:
    with freeze_time(NOW) as frozen:
        car, calls = await _met_without_plug_in_reports(hass, transport, frozen)
        key = car.baseline().departure_key
        offs = _turn_offs(calls)

        await car.controller.async_start(10, cause="solar")
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        await car.settle()

        assert car.baseline().departure_key == key
        assert _turn_offs(calls) == offs


@pytest.mark.parametrize(("origin", "stopped"), [("manual", False), ("solar", False), ("self", True)])
async def test_a_new_plan_outside_its_windows_stops_only_a_charge_that_is_nobodys(
    hass: HomeAssistant, origin: str, stopped: bool
) -> None:
    controller, plug, starts, stops = await _switch_controller(hass, None)
    if origin == "manual":
        await controller.async_start(manual=True)
    elif origin == "solar":
        await controller.async_start(10, cause="solar")
    await plug.set(True, control="on")
    stops.clear()

    await install_schedule(controller, _later_window())

    assert bool(stops) is stopped
    await controller.async_shutdown()


async def test_a_new_plan_outside_its_windows_spares_a_charge_something_else_owns(hass: HomeAssistant) -> None:
    controller, plug, _, stops = await _switch_controller(hass, None)
    await plug.set(True, control="on")
    controller.set_hold_guard(lambda: True)
    stops.clear()

    await install_schedule(controller, _later_window())

    assert stops == []
    await controller.async_shutdown()


# -------------------------------------------- R3, R4: a restart keeps its count, a fast charger counts


def test_a_session_register_that_restarted_unseen_keeps_what_it_counted() -> None:
    baseline = EnergyBaseline(
        register_kwh=0.0, departure_key="2026-09-23T07:00:00+02:00", started_at=T0,
        last_register_kwh=8.0, last_register_at=T0 + timedelta(hours=1), previous_register_kwh=7.8,
        charge_mark_s=3600.0,
    )
    # Charging again for ten minutes after the restart nobody saw.
    first = advance_register(baseline, 0.9, T0 + timedelta(hours=5), max_kw=22.0, plugged_in_at=None, charged_s=4200.0)
    assert first.delivered_kwh == pytest.approx(8.0), "the 8 kWh of this departure are not forgotten"
    held = advance_register(
        first.baseline, 1.4, T0 + timedelta(hours=5, minutes=3), max_kw=22.0, plugged_in_at=None, charged_s=4380.0
    )
    assert held.accepted and held.delivered_kwh == pytest.approx(9.4), "and what it counted while the drop held"


def test_a_43_kw_charger_with_the_default_32_a_range_is_still_counted() -> None:
    # The planner judges a rise against at least 63 A whatever range a charger states (`CEILING_A`).
    max_kw = power_kw(max(32, CEILING_A), 3)
    baseline = EnergyBaseline(register_kwh=1000.0, departure_key="k", started_at=T0, last_register_kwh=1000.0,
                              last_register_at=T0)
    for minute in range(3, 61, 3):  # a reading every 3 minutes at 43 kW
        step = advance_register(baseline, 1000.0 + 43.0 * minute / 60, T0 + timedelta(minutes=minute),
                                max_kw=max_kw, plugged_in_at=None, charged_s=minute * 60.0)
        baseline = step.baseline
    assert step.accepted and step.delivered_kwh == pytest.approx(43.0)


async def test_the_planner_judges_a_rise_against_at_least_63_a(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=30.0, departure=time(20, 0), departure_enabled=True)
        await _delivered(hass, frozen, 1000.0 + 43.0 * 10 / 60, minutes=10)  # 43 kW, a 32 A range
        await car.preview.async_recalculate()
        assert car.preview.snapshot().remaining_kwh == pytest.approx(30.0 - 43.0 * 10 / 60, abs=0.01)


# ------------------------------------------------------- R5: the first believed reading is enough


async def test_the_energy_stop_acts_on_the_first_reading_that_shows_the_energy_delivered(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0)
        frozen.tick(timedelta(minutes=12))
        _meter(hass, 1003.0)  # a register that reports, say, once an hour
        await hass.async_block_till_done()

        assert car.controller.plan is None and _turn_offs(calls) == 1
        assert _turn_ons(calls) == 1
