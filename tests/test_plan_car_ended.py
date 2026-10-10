"""The car ends a plan's charge by itself inside a window: the controller.

Before, only a person's Start was watched for the car ending it (`manual_pause.py`); a plan charge the car stopped
taking (OCPP `SuspendedEV`, or a current near 0 A with the charge on) was never noticed, so a target at the car's own
limit, which SpotNav leaves to the car to end, was never ended at all: the coverage of the ownership core showed no
`car_ended`, `target_reached` or `need_met` event on the field site, ever.

Now five minutes of the car not drawing in an open window, with the plan's charge on, is the car ending it:

* full by its reading, or by the estimate from delivered energy (`target_stop.car_ended_full`): the plan ends as a
  reached target (a manual amount to the car's limit: a met need), the charge is stopped and the windows ahead are
  cleared;
* not shown full: the car-ended record (R3) skips the window open now, the charge and the plan stay;
* either way the core hears `car_ended`, and the charger's owner is told once (a fresh reading is asked for).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CHARGER_CURRENT_ENTITIES
from custom_components.spotnav.execution.controller import ChargingController, ChargingPlan
from custom_components.spotnav.execution.plan_car_ended import PLAN_CAR_IDLE_S
from custom_components.spotnav.execution.target_stop import SocReading

SWITCH = "switch.halo"
CURRENT = "sensor.halo_current"
CONFIG = {CONF_CHARGE_CONTROL: SWITCH, CONF_CHARGER_CURRENT_ENTITIES: [CURRENT]}


class Halo:
    """A switch charger with a measured current, whose switch obeys SpotNav's commands."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[str] = []
        hass.states.async_set(SWITCH, "off")
        self.amps(0.0)

        async def handle(call: ServiceCall) -> None:
            self.calls.append(call.service)
            hass.states.async_set(SWITCH, "on" if call.service == "turn_on" else "off")
            if call.service == "turn_off":
                self.amps(0.0)

        hass.services.async_register("switch", "turn_on", handle)
        hass.services.async_register("switch", "turn_off", handle)

    def amps(self, value: float) -> None:
        self.hass.states.async_set(CURRENT, str(value), {"unit_of_measurement": "A"})

    @property
    def stops(self) -> int:
        return self.calls.count("turn_off")


class Car:
    """What the car's state of charge reads as: a value, and whether it is the estimate from delivered energy."""

    def __init__(self, percent: float | None, *, estimated: bool) -> None:
        self.percent = percent
        self.estimated = estimated

    def __call__(self, vehicle_id: str | None) -> SocReading:
        return SocReading(self.percent, "vehicle", "sensor.ev6_battery", vehicle_id, 6 * 3600.0, estimated=self.estimated)


async def _controller(hass: HomeAssistant, car: Car, limit: float | None = 100.0) -> ChargingController:
    controller = ChargingController(
        hass, "halo", dict(CONFIG), soc_reader=car, vehicle_limit_reader=lambda _vehicle_id: limit
    )
    await controller.async_initialize()
    return controller


def _plan(*, target: float | None = 100.0, to_vehicle_limit: bool = False, hours: float = 2.0) -> ChargingPlan:
    now = dt_util.utcnow()
    start, end = now - timedelta(minutes=5), now + timedelta(hours=hours)
    later = end + timedelta(hours=1)
    return ChargingPlan(
        start=start.isoformat(),
        end=(later + timedelta(hours=1)).isoformat(),
        amps=16,
        periods=[
            {"start": start.isoformat(), "end": end.isoformat()},
            {"start": later.isoformat(), "end": (later + timedelta(hours=1)).isoformat()},
        ],
        target_soc_percent=target,
        vehicle_id="ev6" if target is not None else None,
        to_vehicle_limit=to_vehicle_limit,
        auto_identity="0123456789abcdef0123456789abcdef",
        auto_settings_revision=1,
        auto_price_identity="test-price-identity",
    )


async def _later(hass: HomeAssistant, freezer: Any, seconds: float, step: float = 30.0) -> None:
    left = seconds
    while left > 0:
        moved = min(step, left)
        freezer.tick(timedelta(seconds=moved))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        left -= moved


async def _charging(hass: HomeAssistant, freezer: Any, car: Car, **plan: Any) -> tuple[ChargingController, Halo, list]:
    halo = Halo(hass)
    controller = await _controller(hass, car, plan.pop("limit", 100.0))
    told: list[tuple[str | None, bool]] = []
    controller.set_car_ended_observer(lambda vehicle_id, full: told.append((vehicle_id, full)))
    await controller.async_install(_plan(**plan))
    await hass.async_block_till_done()
    assert halo.calls == ["turn_on"], "the open window starts the charge"
    halo.amps(14.8)
    await _later(hass, freezer, 120)
    return controller, halo, told


def _coverage(controller: ChargingController, kind: str) -> dict[str, Any]:
    return controller._shadow.diagnostics()["coverage"]["kinds"][kind]  # noqa: SLF001 - what the field report shows


async def test_the_night_of_10_october_the_car_full_by_the_estimate_ends_the_plan(
    hass: HomeAssistant, freezer: Any
) -> None:
    """02:35: the EV6 stops drawing at the car's own 100 % limit, 24.26 kWh after a 72 % reading the cloud never
    updated; the estimate from that energy reads 100 %."""
    car = Car(100.0, estimated=True)
    controller, halo, told = await _charging(hass, freezer, car)

    halo.amps(0.0)
    await _later(hass, freezer, PLAN_CAR_IDLE_S - 30)
    assert controller.plan is not None and halo.stops == 0, "not yet five minutes"
    await _later(hass, freezer, 60)

    assert controller.plan is None, "the windows ahead are cleared: nothing is chased"
    assert halo.stops == 1
    record = controller.target_stop_record
    assert record is not None and record["basis"] == "estimate" and record["soc_percent"] == 100.0
    assert record["car_ended"] is True
    assert controller.completion_record is not None and controller.completion_record["reason"] == "vehicle_full"
    assert controller.car_ended_at is not None
    assert told == [("ev6", True)]
    assert _coverage(controller, "car_ended")["compared"] == 1
    assert _coverage(controller, "target_reached")["compared"] == 1
    await _later(hass, freezer, 4 * 3600)
    assert halo.calls == ["turn_on", "turn_off"], "no later window starts the full car again"
    await controller.async_shutdown()


async def test_a_target_below_the_limit_the_car_ended_at_is_a_reached_target(hass: HomeAssistant, freezer: Any) -> None:
    car = Car(79.5, estimated=False)
    controller, halo, told = await _charging(hass, freezer, car, target=80.0)
    halo.amps(0.0)
    await _later(hass, freezer, PLAN_CAR_IDLE_S + 30)
    assert controller.plan is None and halo.stops == 1
    assert controller.completion_record is not None and controller.completion_record["reason"] == "target"
    assert told == [("ev6", True)]
    await controller.async_shutdown()


async def test_a_car_that_ended_the_charge_short_of_full_keeps_the_plan(hass: HomeAssistant, freezer: Any) -> None:
    """The estimate says 85 %: the car stopped for a reason of its own (a timer, a fault). Nothing is stopped or
    cleared on it; the window open now is skipped (R3) and the next one charges as planned."""
    car = Car(85.0, estimated=True)
    controller, halo, told = await _charging(hass, freezer, car)
    halo.amps(0.0)
    await _later(hass, freezer, PLAN_CAR_IDLE_S + 30)

    assert controller.plan is not None and halo.stops == 0
    assert controller.target_stop_record is None
    assert controller.car_ended_at is not None
    assert told == [("ev6", False)]
    assert _coverage(controller, "car_ended")["compared"] == 1
    assert _coverage(controller, "target_reached")["events"] == 0
    await _later(hass, freezer, 30 * 60)
    assert told == [("ev6", False)], "told once"
    await controller.async_shutdown()


async def test_a_stale_reading_from_before_the_charge_is_not_full(hass: HomeAssistant, freezer: Any) -> None:
    """No energy register: the 72 % reading is all there is. The car ended the charge, but nothing says it is full."""
    car = Car(72.0, estimated=False)
    controller, halo, told = await _charging(hass, freezer, car)
    halo.amps(0.0)
    await _later(hass, freezer, PLAN_CAR_IDLE_S + 30)
    assert controller.plan is not None and halo.stops == 0
    assert told == [("ev6", False)]
    await controller.async_shutdown()


async def test_a_manual_amount_to_the_cars_limit_the_car_ended_full_is_a_met_need(
    hass: HomeAssistant, freezer: Any
) -> None:
    car = Car(100.0, estimated=True)
    controller, halo, told = await _charging(hass, freezer, car, target=None, to_vehicle_limit=True)
    halo.amps(0.0)
    await _later(hass, freezer, PLAN_CAR_IDLE_S + 30)
    assert controller.plan is None and halo.stops == 1
    assert controller.completion_record is not None and controller.completion_record["reason"] == "vehicle_full"
    assert told == [(None, True)]
    assert _coverage(controller, "need_met")["compared"] == 1
    await controller.async_shutdown()


async def test_a_car_still_drawing_or_paused_by_load_balancing_has_not_ended_the_charge(
    hass: HomeAssistant, freezer: Any
) -> None:
    car = Car(100.0, estimated=True)
    controller, halo, told = await _charging(hass, freezer, car)
    halo.amps(3.0)
    await _later(hass, freezer, 2 * PLAN_CAR_IDLE_S)
    assert controller.plan is not None and told == []

    await controller._regulated_stop("pause")  # noqa: SLF001 - load balancing's pause
    await _later(hass, freezer, 2 * PLAN_CAR_IDLE_S)
    assert controller.plan is not None and told == [] and controller.car_ended_at is None
    await controller.async_shutdown()
