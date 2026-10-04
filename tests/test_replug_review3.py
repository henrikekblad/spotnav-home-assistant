"""Regressions from the third review: the energy count moves only by what the charger can have delivered
while charging, and only the register watcher moves it; a charge load balancing paused comes back as what
it was; a raised request counts on from what was delivered; a start that raises leaves nothing claimed;
and the plan-stopped notification stays quiet when no car may be there or the car is at its own limit.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.notifications.unexpected_stop import (
    ExpectationFacts,
    GRACE_S,
    UnexpectedStopDetector,
)
from custom_components.spotnav.runtime import domain_data
from tests.relay import serve as serve_prices

from .test_dashboard_api import NOW
from .test_replug import _car, _delivered, _meter, _record_charger_commands, _switch_controller, _turn_offs
from .test_replug import _later_window
from .helpers import install_schedule

pytestmark = pytest.mark.usefixtures("offline_relay")


# ------------------------------------------------------- N2: a false reading read again stays false


async def test_a_spike_still_showing_at_the_next_calculation_is_not_believed(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=10.0, departure=time(20, 0), departure_enabled=True)
        await _delivered(hass, frozen, 1000.5, minutes=12)
        _meter(hass, 1100.0)  # one bogus reading
        await hass.async_block_till_done()
        assert car.controller.plan is not None, "rejected by the watcher"

        await car.preview.async_recalculate()  # any calculation while the state still shows it
        await hass.async_block_till_done()

        assert car.controller.plan is not None and _turn_offs(calls) == 0
        assert car.preview.snapshot().remaining_kwh == pytest.approx(9.5)


# --------------------------------------------- N3: the allowance is charging time, not wall time


async def test_a_glitch_after_hours_of_an_idle_register_is_not_believed(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, flat=True)  # the window is ahead
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=10.0, departure=time(20, 0), departure_enabled=True)
        plan = car.controller.plan
        assert plan is not None
        frozen.move_to(plan.windows[0][0] + timedelta(minutes=1))  # hours idle, then the window starts
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert hass.states.get("switch.wallbox").state == "on"

        _meter(hass, 1050.0)  # within 43 kW x the idle hours, not within one minute of charging
        await hass.async_block_till_done()

        assert car.controller.plan is not None and _turn_offs(calls) == 0


async def test_a_need_met_on_a_count_that_proves_false_is_planned_again(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0, departure=time(20, 0), departure_enabled=True)
        await _delivered(hass, frozen, 1001.0, minutes=10)
        await _delivered(hass, frozen, 1003.1, minutes=15)  # believable, and it meets the need
        assert car.controller.plan is None and car.baseline().met_at is not None

        await _delivered(hass, frozen, 1001.1, minutes=1)  # back where it was: that rise was false

        assert car.baseline().met_at is None
        assert car.preview.snapshot().remaining_kwh == pytest.approx(1.9)
        assert car.controller.plan is not None, "the open need is planned again"


# ---------------------------------------------- N1: a charge balancing paused comes back as what it was


async def test_a_plan_charge_resumed_after_a_balancing_pause_is_still_the_plans(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0)
        c = car.controller
        assert c._plan_charge and c.charge_origin == "plan_window"  # noqa: SLF001
        await c._regulated_stop("pause")  # noqa: SLF001 - load balancing pauses the plan's charge
        await hass.async_block_till_done()
        assert c.paused_by_balancing
        assert await c.async_battery_probe_start(8)  # headroom back: the regulator resumes it
        await hass.async_block_till_done()
        assert c.charge_origin == "plan_window" and c._plan_charge  # noqa: SLF001

        await _delivered(hass, frozen, 1003.1, minutes=15)

        assert c.plan is None and hass.states.get("switch.wallbox").state == "off", "the need-met stop applies"
        assert _turn_offs(calls) >= 2


async def test_a_persons_charge_resumed_after_a_balancing_pause_is_still_the_persons(hass: HomeAssistant) -> None:
    controller, plug, starts, _ = await _switch_controller(hass, None)
    await controller.async_start(manual=True)
    await plug.set(True, control="on")
    await controller._regulated_stop("pause")  # noqa: SLF001
    await plug.set(True, control="off")

    assert await controller.async_battery_probe_start(8)

    assert controller.charge_origin == "manual"
    await controller.async_shutdown()


# ------------------------------- N4: a raised request counts on; a plan's own window is not a new need


async def test_raising_the_request_after_a_met_need_buys_only_the_rest(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0)  # a charger that never reported a plug-in
        assert car.controller.plugged_in_at is None
        await _delivered(hass, frozen, 1003.1, minutes=12)
        assert car.controller.plan is None
        frozen.tick(timedelta(minutes=20))
        store = domain_data(hass).auto_store
        await store.async_update(
            car.charger.entry_id, mutate=lambda s: replace(s, requested_kwh=5.0, revision=s.revision + 1)
        )
        await car.preview.async_recalculate()
        await hass.async_block_till_done()
        assert car.baseline().met_at is None
        for _ in range(4):  # the plan for the rest starts its window, and its session opens
            frozen.tick(timedelta(minutes=5))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
            await car.settle()

        assert car.baseline().departure_key == "no_deadline", "the plan's own charge is not a new need"
        assert car.preview.snapshot().remaining_kwh == pytest.approx(1.9, abs=0.05)


# ---------------------------------------------------- N5: a start that raises leaves nothing claimed


async def test_a_start_that_raises_restores_what_the_charge_was(hass: HomeAssistant) -> None:
    controller, plug, _, _ = await _switch_controller(hass, None)

    async def broken(_amps: Any = None) -> bool:
        raise RuntimeError("the charger said no")

    controller.adapter.async_start = broken  # type: ignore[method-assign]
    await install_schedule(controller, _later_window())
    with pytest.raises(RuntimeError):
        await controller.async_start(cause="plan_window")

    assert controller.charge_origin is None
    assert controller._plan_charge is False and controller._start_cause is None  # noqa: SLF001
    assert controller._hold.owned is False  # noqa: SLF001
    await controller.async_shutdown()


# ------------------------------------------------------------- the plan-stopped notification

T0 = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)


def _at(seconds: float, **facts: Any) -> ExpectationFacts:
    values: dict[str, Any] = {"expected": True, "window": "w1", "charging": False}
    values.update(facts)
    return ExpectationFacts(now=T0 + timedelta(seconds=seconds), **values)


def test_no_car_maybe_is_not_a_start_that_failed() -> None:
    detector = UnexpectedStopDetector()
    assert detector.observe(_at(0, vehicle_unknown=True)) is None
    assert detector.observe(_at(GRACE_S + 60, vehicle_unknown=True)) is None
    # Once it charged in the window, a stop is still a stop.
    assert detector.observe(_at(GRACE_S + 120, charging=True)) is None
    assert detector.observe(_at(GRACE_S + 130, vehicle_unknown=True)) is None
    assert detector.observe(_at(2 * GRACE_S + 140, vehicle_unknown=True)) == "stopped"


def test_a_car_at_its_own_limit_is_not_a_car_that_takes_no_current() -> None:
    detector = UnexpectedStopDetector()
    facts = {"charging": True, "vehicle_not_requesting": True, "at_vehicle_limit": True}
    assert detector.observe(_at(0, **facts)) is None
    assert detector.observe(_at(GRACE_S + 10, **facts)) is None
    other = UnexpectedStopDetector()
    assert other.observe(_at(0, charging=True, vehicle_not_requesting=True)) is None
    assert other.observe(_at(GRACE_S, charging=True, vehicle_not_requesting=True)) == "vehicle_not_requesting"
