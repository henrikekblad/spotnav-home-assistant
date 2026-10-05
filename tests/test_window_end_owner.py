"""A window's end stops only the plan's own charge.

When a plan window ends (another follows) or the last one ends, SpotNav stops the charge the plan owns:
the window's charge, or one the charger began by itself inside the window that nobody claimed (as
before). A charge a person started (a Start, or Charge now on a charger without Auto, which is a start
with no cause) goes on, and so does one load balancing holds back for that person. A charge the car
finishes past the last window is the top-off's, and a hybrid hand-off to the sun is untouched; both are
pinned in `test_top_off.py` and `test_hybrid_wiring.py`.

These drive a bare `ChargingController`: no Auto, so no manual pause stands between a person's Start and
the window's end, which is the case the rule is for.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.const import CONF_CHARGE_CONTROL
from custom_components.spotnav.execution.controller import ChargingController

from .helpers import install_schedule
from .test_replug import _obedient_switch

pytestmark = pytest.mark.usefixtures("offline_relay")

SWITCH = "switch.a"

_CONTROLLERS: list[ChargingController] = []


@pytest.fixture(autouse=True)
async def _shut_down_controllers():
    yield
    while _CONTROLLERS:
        await _CONTROLLERS.pop().async_shutdown()


def _periods(*windows: tuple[int, int]) -> dict[str, Any]:
    """A plan whose windows run from/to these minutes after now (negative: already open)."""
    now = dt_util.utcnow()
    periods = [
        {"start": (now + timedelta(minutes=start)).isoformat(), "end": (now + timedelta(minutes=end)).isoformat()}
        for start, end in windows
    ]
    return {"periods": periods, "amps": 10}


async def _controller(hass: HomeAssistant, *windows: tuple[int, int]) -> tuple[ChargingController, list[Any]]:
    """A plan with its first window open now, whose start turned the charger on."""
    hass.states.async_set(SWITCH, "off")
    _starts, stops = _obedient_switch(hass)
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: SWITCH})
    _CONTROLLERS.append(controller)
    await controller.async_initialize()
    await install_schedule(controller, _periods(*windows))
    await hass.async_block_till_done()
    assert controller.charging and controller.charge_origin == "plan_window"
    return controller, stops


async def _to(hass: HomeAssistant, freezer: Any, when: datetime) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


def _minutes(minutes: int) -> datetime:
    return dt_util.utcnow() + timedelta(minutes=minutes)


TWO_WINDOWS = ((-10, 10), (60, 120))
ONE_WINDOW = ((-10, 10),)


async def test_a_plan_charge_stops_at_a_window_end_and_the_plan_stays(hass: HomeAssistant, freezer: Any) -> None:
    controller, stops = await _controller(hass, *TWO_WINDOWS)
    plan = controller.plan

    await _to(hass, freezer, _minutes(11))

    assert len(stops) == 1 and not controller.charging, "the plan's own charge stops with its window"
    assert controller.plan is plan


async def test_a_plan_charge_stops_at_the_last_window_end_and_the_plan_ends(hass: HomeAssistant, freezer: Any) -> None:
    controller, stops = await _controller(hass, *ONE_WINDOW)

    await _to(hass, freezer, _minutes(11))

    assert len(stops) == 1 and not controller.charging
    assert controller.plan is None


async def test_a_persons_start_goes_on_past_a_window_end(hass: HomeAssistant, freezer: Any) -> None:
    """The report's sequence: a window runs, the person presses Start inside it, the window ends."""
    controller, stops = await _controller(hass, *TWO_WINDOWS)
    plan = controller.plan
    assert await controller.async_start(manual=True)
    assert controller.charge_origin == "manual"

    await _to(hass, freezer, _minutes(11))

    assert stops == [] and controller.charging, "a person's charge is not the window's to stop"
    assert controller.charge_origin == "manual"
    assert controller.plan is plan, "the plan's later windows stay"


async def test_a_persons_start_goes_on_past_the_last_window_end(hass: HomeAssistant, freezer: Any) -> None:
    controller, stops = await _controller(hass, *ONE_WINDOW)
    assert await controller.async_start(manual=True)

    await _to(hass, freezer, _minutes(11))

    assert stops == [] and controller.charging
    assert controller.charge_origin == "manual"
    assert controller.plan is None, "the plan is over with its last window, the charge is not"
    assert controller.top_off_until is None


async def test_charge_now_without_auto_goes_on_past_a_window_end(hass: HomeAssistant, freezer: Any) -> None:
    """Charge now on a charger without Auto: the button's start with no cause (origin `other`)."""
    controller, stops = await _controller(hass, *TWO_WINDOWS)
    assert await controller.async_start()
    assert controller.charge_origin == "other"

    await _to(hass, freezer, _minutes(11))

    assert stops == [] and controller.charging
    assert controller.plan is not None


async def test_a_solar_start_inside_a_window_goes_on_past_its_end(hass: HomeAssistant, freezer: Any) -> None:
    controller, stops = await _controller(hass, *TWO_WINDOWS)
    assert await controller.async_start(cause="solar")
    assert controller.charge_origin == "solar"

    await _to(hass, freezer, _minutes(11))

    assert stops == [] and controller.charging


async def test_a_persons_charge_load_balancing_holds_back_is_kept_past_a_window_end(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The regulator paused the person's charge for want of headroom: the window's end does not forget
    it, so the regulator still gives it back when there is room."""
    controller, stops = await _controller(hass, *TWO_WINDOWS)
    assert await controller.async_start(manual=True)
    await controller._regulated_stop("pause")  # noqa: SLF001 - load balancing pauses the charge
    await hass.async_block_till_done()
    assert controller.paused_by_balancing and controller.paused_charge_origin == "manual"
    assert len(stops) == 1

    await _to(hass, freezer, _minutes(11))

    assert len(stops) == 1
    assert controller.paused_by_balancing and controller.paused_charge_origin == "manual"


async def test_a_plan_charge_load_balancing_holds_back_is_forgotten_at_a_window_end(
    hass: HomeAssistant, freezer: Any
) -> None:
    controller, _stops = await _controller(hass, *TWO_WINDOWS)
    await controller._regulated_stop("pause")  # noqa: SLF001 - load balancing pauses the charge
    await hass.async_block_till_done()
    assert controller.paused_by_balancing

    await _to(hass, freezer, _minutes(11))

    assert not controller.paused_by_balancing, "the plan's paused charge ends with its window, as before"


async def test_a_charge_the_charger_began_by_itself_unclaimed_stops_at_the_window_end_as_before(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Pinned as it was: a charge the charger began by itself inside the window that nothing claimed (here
    a guard said something else owned the charger at the time) is stopped by the window's end. Past it,
    the hold decides a charge that begins by itself in the gap exactly as before: stopped once."""
    controller, stops = await _controller(hass, *TWO_WINDOWS)
    # The charge ends by itself; then the charger begins again while the claim is held back.
    hass.states.async_set(SWITCH, "off")
    await hass.async_block_till_done()
    controller._notify()  # noqa: SLF001 - a report that moves the status forgets the ended charge's origin
    assert controller.charge_origin is None
    blocked = True
    controller.set_hold_guard(lambda: blocked)
    hass.states.async_set(SWITCH, "on")
    await hass.async_block_till_done()
    assert controller.charging and controller.charge_origin is None
    assert controller.self_started_charge()
    blocked = False

    await _to(hass, freezer, _minutes(11))

    assert len(stops) == 1 and not controller.charging, "the window's end stops it, as before"

    # In the gap the charger begins by itself again: the hold has not held this session yet, so it is
    # stopped once; a second begin after that is a person's override and is respected.
    hass.states.async_set(SWITCH, "on")
    await hass.async_block_till_done()
    assert len(stops) == 2 and not controller.charging
    hass.states.async_set(SWITCH, "on")
    await hass.async_block_till_done()
    assert len(stops) == 2 and controller.charging and controller.hold_overridden
