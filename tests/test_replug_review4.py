"""Regressions from the fourth review: a charge load balancing holds back keeps what it was (and only that
charge), a reading nobody saw is counted once the register is watched again, a count wavering at the
request does not stop and start the charge, and the charge clock runs whenever charging cannot be ruled
out.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from tests.relay import serve as serve_prices

from .test_dashboard_api import NOW
from .test_replug import (
    _car,
    _delivered,
    _meter,
    _record_charger_commands,
    _switch_controller,
    _turn_offs,
    _turn_ons,
)

pytestmark = pytest.mark.usefixtures("offline_relay")


# ------------------------------------------- F1, F2: what balancing holds back, and only that


async def test_a_window_start_refused_below_the_floor_resumes_as_the_plans(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0)
        c = car.controller
        await car.unplug()
        c.set_start_cap(lambda: 2.0)  # no headroom at the replug: the window's start is refused
        await car.replug()
        assert c.paused_by_balancing and hass.states.get("switch.wallbox").state == "off"
        c.set_start_cap(None)
        assert await c.async_battery_probe_start(8)  # the regulator resumes it
        await hass.async_block_till_done()
        assert c.charge_origin == "plan_window" and c._plan_charge  # noqa: SLF001

        await _delivered(hass, frozen, 1003.1, minutes=15)

        assert c.plan is None and hass.states.get("switch.wallbox").state == "off", "the need-met stop applies"


async def test_a_persons_stop_forgets_what_balancing_paused(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0)
        c = car.controller
        await c._regulated_stop("pause")  # noqa: SLF001 - balancing pauses the plan's charge
        await hass.async_block_till_done()
        await car.executor.async_manual_stop()  # the person stops it meanwhile
        await hass.async_block_till_done()
        assert not c.paused_by_balancing
        c.set_start_cap(lambda: 2.0)
        with pytest.raises(Exception):  # noqa: B017 - Charge now, refused for want of headroom
            await car.executor.async_manual_start()
        await hass.async_block_till_done()
        assert c.paused_by_balancing
        c.set_start_cap(None)

        assert await c.async_battery_probe_start(8)

        assert c.charge_origin == "manual" and not c._plan_charge, "the person's Charge now stays theirs"  # noqa: SLF001


async def test_a_manual_charge_the_balancing_resume_could_not_start_waits_as_the_persons(hass: HomeAssistant) -> None:
    controller, plug, _, _ = await _switch_controller(hass, None)
    await controller.async_start(manual=True)
    await plug.set(True, control="on")
    await controller._regulated_stop("pause")  # noqa: SLF001
    await plug.set(True, control="off")
    controller.set_start_cap(lambda: 2.0)
    assert await controller.async_battery_probe_start(8, capped=True) is False
    controller.set_start_cap(None)

    assert await controller.async_battery_probe_start(8)

    assert controller.charge_origin == "manual"
    await controller.async_shutdown()


# ------------------------------------------------- F3: a reading nobody saw is counted once watched


async def test_a_reading_the_watcher_did_not_see_is_counted_when_it_watches_again(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=10.0, departure=time(20, 0), departure_enabled=True)
        frozen.tick(timedelta(minutes=20))
        car.preview._drop_energy_watch()  # noqa: SLF001 - stand-in for Home Assistant restarting
        _meter(hass, 1004.0)
        await hass.async_block_till_done()

        await car.preview.async_recalculate()  # watches again; the register does not move again

        assert car.preview.snapshot().remaining_kwh == pytest.approx(6.0)


async def test_a_rejected_reading_read_again_on_rearming_is_still_not_believed(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=10.0, departure=time(20, 0), departure_enabled=True)
        await _delivered(hass, frozen, 1000.5, minutes=12)
        _meter(hass, 1100.0)  # one bogus reading, rejected
        await hass.async_block_till_done()
        car.preview._drop_energy_watch()  # noqa: SLF001

        await car.preview.async_recalculate()

        assert car.preview.snapshot().remaining_kwh == pytest.approx(9.5)
        assert car.controller.plan is not None


# ------------------------------------------------------- F4: a count wavering at the request


async def test_a_register_wavering_at_the_request_stops_the_charge_once(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0, departure=time(20, 0), departure_enabled=True)
        await _delivered(hass, frozen, 1002.9, minutes=12)
        for _ in range(3):
            await _delivered(hass, frozen, 1003.0, minutes=0.5)
            await _delivered(hass, frozen, 1002.9, minutes=0.5)
        frozen.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        await car.preview.async_recalculate()

        assert _turn_offs(calls) == 1 and _turn_ons(calls) == 1
        assert car.controller.plan is None and car.preview.snapshot().state == "nothing_to_charge"


# ------------------------------------------------------- F5: when charging cannot be ruled out


async def test_the_charge_clock_runs_while_the_control_cannot_be_read(hass: HomeAssistant) -> None:
    with freeze_time(dt_util.utcnow()) as frozen:
        controller, plug, _, _ = await _switch_controller(hass, None)
        assert controller.charging_seconds() == 0.0
        frozen.tick(timedelta(minutes=10))
        assert controller.charging_seconds() == 0.0, "a switch that says off: not charging"

        hass.states.async_set("switch.a", "unavailable")
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=10))

        assert controller.charging_seconds() == pytest.approx(600.0)
        await controller.async_shutdown()


async def test_the_charge_clock_runs_for_a_charger_that_cannot_say_whether_it_charges(hass: HomeAssistant) -> None:
    with freeze_time(dt_util.utcnow()) as frozen:
        controller, plug, _, _ = await _switch_controller(hass, None)
        # A button pair with no status sensor: its control says nothing either way.
        controller.adapter.enabled_state = lambda: None  # type: ignore[method-assign]
        controller.adapter.charging_state = lambda: False  # type: ignore[method-assign]
        await plug.set(None)
        frozen.tick(timedelta(minutes=10))

        assert controller.charging_seconds() == pytest.approx(600.0)
        await controller.async_shutdown()
