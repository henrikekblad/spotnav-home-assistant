"""An estimate that reaches the target asks the car before it stops anything.

The field night of 12 October: the Mazda's charge was stopped on an estimate of 89.98 % against an 85 % target; the
car read 87 % afterwards. An estimate carries the error of the pack size, the charging loss and a register that books
late, so now, when only an estimate says the target is reached:

* the car is asked once for a fresh reading (its integration's own re-read, never a wake-up) and the charge goes on;
* a reading at or above the target stops it, as a reading;
* a reading below the target goes on charging, and the car is asked again only once the estimate moved a percent
  further: no refresh on every register step;
* no answer within `ESTIMATE_CONFIRM_WAIT_S`, or a car that offers no refresh: the estimate decides alone, at the
  target plus `ESTIMATE_STOP_MARGIN_PERCENT`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.execution.target_stop import (
    confirm_estimate,
    ESTIMATE_CHECK_STALE_S,
    ESTIMATE_CONFIRM_WAIT_S,
    ESTIMATE_REASK_STEP_PERCENT,
    ESTIMATE_STOP_MARGIN_PERCENT,
    EstimateCheck,
    SocReading,
)

from tests.test_plan_car_ended import _later, _plan, CONFIG, Halo

T0 = datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)


def _estimate(percent: float) -> SocReading:
    return SocReading(percent, "vehicle", "sensor.mx_30_charge_level", "mx30", 3600.0, estimated=True)


# ------------------------------------------------------------------------------------------------ the rule


def test_the_first_estimate_at_the_target_asks() -> None:
    assert confirm_estimate(target_soc_percent=85.0, reading=_estimate(85.0), check=None, now=T0) == "ask"
    other = EstimateCheck("another car", T0, 85.0, True)
    assert confirm_estimate(target_soc_percent=85.0, reading=_estimate(85.0), check=other, now=T0) == "ask"


def test_an_unanswered_ask_is_waited_for_then_the_estimate_decides() -> None:
    check = EstimateCheck("mx30", T0, 85.2, True)
    wait = T0 + timedelta(seconds=ESTIMATE_CONFIRM_WAIT_S - 1)
    over = T0 + timedelta(seconds=ESTIMATE_CONFIRM_WAIT_S)
    assert confirm_estimate(target_soc_percent=85.0, reading=_estimate(89.0), check=check, now=wait) == "wait"
    assert confirm_estimate(target_soc_percent=85.0, reading=_estimate(86.0), check=check, now=over) == "estimate"
    stale = T0 + timedelta(seconds=ESTIMATE_CHECK_STALE_S)
    assert confirm_estimate(target_soc_percent=85.0, reading=_estimate(86.0), check=check, now=stale) == "ask"


def test_a_car_that_offers_no_refresh_leaves_the_estimate_to_decide_at_once() -> None:
    check = EstimateCheck("mx30", T0, 85.2, False)
    assert confirm_estimate(target_soc_percent=85.0, reading=_estimate(85.5), check=check, now=T0) == "estimate"


def test_after_a_reading_below_the_target_the_car_is_asked_again_only_a_percent_further() -> None:
    check = EstimateCheck("mx30", T0, 85.2, True, answered=True)
    now = T0 + timedelta(minutes=5)
    step = 85.2 + ESTIMATE_REASK_STEP_PERCENT
    assert confirm_estimate(target_soc_percent=85.0, reading=_estimate(step - 0.1), check=check, now=now) == "wait"
    assert confirm_estimate(target_soc_percent=85.0, reading=_estimate(step), check=check, now=now) == "ask"


# ------------------------------------------------------------------------------------------ the controller


@dataclass
class Mazda:
    """The level the controller reads: a value, whether it is an estimate, and when the car read it."""

    percent: float
    estimated: bool = True
    read_at: datetime | None = None

    def __call__(self, vehicle_id: str | None) -> SocReading:
        now = datetime.now(timezone.utc)
        age = 3600.0 if self.read_at is None else (now - self.read_at).total_seconds()
        return SocReading(self.percent, "vehicle", "sensor.mx_30_charge_level", vehicle_id, age, estimated=self.estimated)

    def reads(self, percent: float) -> None:
        """The car answers with a fresh reading."""
        self.percent, self.estimated, self.read_at = percent, False, datetime.now(timezone.utc)

    def estimates(self, percent: float) -> None:
        self.percent, self.estimated = percent, True


async def _charge(
    hass: HomeAssistant, freezer: Any, car: Mazda, *, refreshable: bool | None = True
) -> tuple[ChargingController, Halo, list[str | None]]:
    halo = Halo(hass)
    controller = ChargingController(
        hass, "halo", dict(CONFIG), soc_reader=car, vehicle_limit_reader=lambda _vehicle_id: 100.0
    )
    await controller.async_initialize()
    asked: list[str | None] = []
    if refreshable is not None:
        controller.set_reading_asker(lambda vehicle_id: asked.append(vehicle_id) or refreshable)
    await controller.async_install(_plan(target=85.0, hours=3.0))
    await hass.async_block_till_done()
    assert halo.calls == ["turn_on"]
    halo.amps(14.8)
    await _later(hass, freezer, 60)
    return controller, halo, asked


async def _look(hass: HomeAssistant, controller: ChargingController) -> None:
    """A reading or a register step arrived: the target is decided again."""
    await controller._async_enforce_target()  # noqa: SLF001 - what the reading's listener runs
    await hass.async_block_till_done()


async def test_an_estimate_at_the_target_asks_the_car_and_a_reading_at_the_target_stops(
    hass: HomeAssistant, freezer: Any
) -> None:
    car = Mazda(84.0)
    controller, halo, asked = await _charge(hass, freezer, car)
    car.estimates(85.3)
    await _look(hass, controller)
    assert asked == ["ev6"] and halo.stops == 0 and controller.plan is not None, "asked, and charging on"
    car.estimates(87.5)
    await _look(hass, controller)
    assert asked == ["ev6"] and halo.stops == 0, "one ask: the estimate past its margin waits for the answer"
    await _later(hass, freezer, 120)
    car.reads(86.0)
    await _look(hass, controller)
    assert halo.stops == 1 and controller.plan is None
    completion = controller.completion_record
    assert completion is not None and completion["basis"] == "reading" and completion["soc_percent"] == 86.0
    assert controller.target_stop_record is not None and controller.target_stop_record["basis"] == "reading"
    await controller.async_shutdown()


async def test_a_reading_below_the_target_charges_on_and_asks_again_only_further_on(
    hass: HomeAssistant, freezer: Any
) -> None:
    car = Mazda(84.0)
    controller, halo, asked = await _charge(hass, freezer, car)
    car.estimates(85.2)
    await _look(hass, controller)
    assert asked == ["ev6"]
    await _later(hass, freezer, 90)
    car.reads(83.0)
    await _look(hass, controller)
    assert halo.stops == 0 and controller.plan is not None, "83 % read: below the target, charging on"
    # The estimate carries the 83 % reading forward; it reaches the target again.
    await _later(hass, freezer, 300)
    car.estimates(85.0)
    await _look(hass, controller)
    car.estimates(86.0)
    await _look(hass, controller)
    assert asked == ["ev6"], "not again until the estimate moved a percent past the last ask"
    # Long after the wait: an answered ask is never the estimate's licence to stop.
    await _later(hass, freezer, ESTIMATE_CONFIRM_WAIT_S + 60)
    assert halo.stops == 0
    car.estimates(86.2)
    await _look(hass, controller)
    assert asked == ["ev6", "ev6"] and halo.stops == 0
    await controller.async_shutdown()


async def test_no_answer_within_the_wait_leaves_it_to_the_estimate_with_its_margin(
    hass: HomeAssistant, freezer: Any
) -> None:
    car = Mazda(84.0)
    controller, halo, asked = await _charge(hass, freezer, car)
    car.estimates(85.4)
    await _look(hass, controller)
    assert asked == ["ev6"]
    await _later(hass, freezer, ESTIMATE_CONFIRM_WAIT_S + 60)
    assert halo.stops == 0, "the wait is over, but 85.4 % is below the target plus the margin"
    car.estimates(85.0 + ESTIMATE_STOP_MARGIN_PERCENT - 0.1)
    await _look(hass, controller)
    assert halo.stops == 0
    car.estimates(85.0 + ESTIMATE_STOP_MARGIN_PERCENT)
    await _look(hass, controller)
    assert halo.stops == 1 and controller.plan is None
    completion = controller.completion_record
    assert completion is not None and completion["basis"] == "estimate"
    await controller.async_shutdown()


async def test_the_end_of_the_wait_decides_by_itself(hass: HomeAssistant, freezer: Any) -> None:
    """A register that steps once an hour gives no other moment to decide at: the wait's end looks again."""
    car = Mazda(84.0)
    controller, halo, asked = await _charge(hass, freezer, car)
    car.estimates(85.0 + ESTIMATE_STOP_MARGIN_PERCENT + 0.5)
    await _look(hass, controller)
    assert asked == ["ev6"] and halo.stops == 0, "past the margin at once, still the car is asked first"
    await _later(hass, freezer, ESTIMATE_CONFIRM_WAIT_S - 60)
    assert halo.stops == 0
    await _later(hass, freezer, 120)
    assert halo.stops == 1 and controller.plan is None
    await controller.async_shutdown()


@pytest.mark.parametrize("refreshable", [False, None], ids=["nothing-to-re-read", "no-asker"])
async def test_a_car_that_offers_no_refresh_stops_on_the_estimate_at_its_margin(
    hass: HomeAssistant, freezer: Any, refreshable: bool | None
) -> None:
    car = Mazda(84.0)
    controller, halo, _ = await _charge(hass, freezer, car, refreshable=refreshable)
    car.estimates(86.0)
    await _look(hass, controller)
    assert halo.stops == 0, "the target plus one is not the target plus the margin"
    car.estimates(85.0 + ESTIMATE_STOP_MARGIN_PERCENT)
    await _look(hass, controller)
    assert halo.stops == 1 and controller.plan is None
    await controller.async_shutdown()


async def test_a_new_plan_keeps_the_ask_so_the_car_is_not_asked_on_every_calculation(
    hass: HomeAssistant, freezer: Any
) -> None:
    car = Mazda(84.0)
    controller, halo, asked = await _charge(hass, freezer, car)
    car.estimates(85.3)
    await _look(hass, controller)
    await controller.async_install(_plan(target=85.0, hours=3.0))
    await hass.async_block_till_done()
    await _look(hass, controller)
    assert asked == ["ev6"] and halo.stops == 0
    await controller.async_shutdown()
