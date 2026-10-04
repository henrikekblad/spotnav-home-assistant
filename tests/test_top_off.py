"""A charge to the car's own limit is finished past the plan's last window: the top-off.

A car near its limit tapers the current over the last half hour, so the plan's last window can end while
it still draws. Then the charge stays on until the car stops drawing by itself (two minutes in a row), at
most an hour past the window and never past the departure. A plan below the car's limit ends with its
window as it always did.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CHARGER_CURRENT_ENTITIES
from custom_components.spotnav.execution import top_off
from custom_components.spotnav.execution.controller import ChargingController, ChargingPlan
from custom_components.spotnav.notifications.messages import compose

SWITCH = "switch.a"
CURRENT = "sensor.a_current"
CONFIG = {CONF_CHARGE_CONTROL: SWITCH, CONF_CHARGER_CURRENT_ENTITIES: [CURRENT]}
CAR_LIMIT = 80.0


# ---------------------------------------------------------------------------- the pure rules


def test_drawing_is_read_from_the_current_then_the_status_and_never_from_a_control_that_is_on() -> None:
    assert top_off.car_drawing(connector_status=None, current_a=6.0) is True
    assert top_off.car_drawing(connector_status="Charging", current_a=0.4) is False, "the current wins"
    assert top_off.car_drawing(connector_status="Charging", current_a=None) is True
    assert top_off.car_drawing(connector_status="SuspendedEV", current_a=None) is False
    assert top_off.car_drawing(connector_status=None, current_a=None) is None
    assert top_off.car_drawing(connector_status=None, current_a=None, power_mode=True, power_w=1500.0) is True
    assert top_off.car_drawing(connector_status=None, current_a=None, power_mode=True, power_w=40.0) is False
    assert top_off.car_drawing(connector_status=None, current_a=None, power_mode=True, power_w=None) is None


def test_the_deadline_is_an_hour_past_the_window_and_never_past_the_departure() -> None:
    end = datetime(2026, 10, 4, 5, 0, tzinfo=dt_util.UTC)
    assert top_off.deadline(end, None) == end + timedelta(minutes=60)
    assert top_off.deadline(end, end + timedelta(minutes=20)) == end + timedelta(minutes=20)
    assert top_off.deadline(end, end + timedelta(hours=3)) == end + timedelta(minutes=60)


def test_a_top_off_the_car_ended_is_a_complete_charge_in_every_language() -> None:
    _, message = compose("charge_complete", "Garage", {"reason": "vehicle_full"}, "en")
    assert message == "Charging complete: the car is full."
    _, message = compose("charge_complete", "Garage", {"reason": "vehicle_full"}, "sv")
    assert message == "Laddningen är klar: bilen är full."
    for language in ("da", "nb", "fi"):
        _, message = compose("charge_complete", "Garage", {"reason": "vehicle_full"}, language)
        assert message and message != compose("charge_complete", "Garage", {"reason": "plan_done"}, language)[1]


# ---------------------------------------------------------------------------- the controller


class Charger:
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


async def _controller(hass: HomeAssistant, limit: float | None = CAR_LIMIT) -> ChargingController:
    controller = ChargingController(hass, "entry_a", dict(CONFIG), vehicle_limit_reader=lambda _vehicle_id: limit)
    await controller.async_initialize()
    return controller


def _plan(end: datetime, *, target: float | None = None, to_vehicle_limit: bool = False, departure: datetime | None = None) -> ChargingPlan:
    start = end - timedelta(hours=1)
    return ChargingPlan(
        start=start.isoformat(),
        end=end.isoformat(),
        amps=16,
        periods=[{"start": start.isoformat(), "end": end.isoformat()}],
        target_soc_percent=target,
        vehicle_id="car" if target is not None else None,
        to_vehicle_limit=to_vehicle_limit,
        departure=None if departure is None else departure.isoformat(),
        auto_identity="0123456789abcdef0123456789abcdef",
        auto_settings_revision=1,
        auto_price_identity="test-price-identity",
    )


async def _later(hass: HomeAssistant, freezer: Any, seconds: float, step: float = 30.0) -> None:
    """Let `seconds` pass, firing every timer due on the way (the top-off looks every 30 s)."""
    left = seconds
    while left > 0:
        moved = min(step, left)
        freezer.tick(timedelta(seconds=moved))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        left -= moved


async def _charging_at_the_window_end(
    hass: HomeAssistant, freezer: Any, amps: float = 10.0, **plan: Any
) -> tuple[ChargingController, Charger, datetime]:
    """A plan whose window is open, charging, then its end arriving with the car drawing `amps`."""
    charger = Charger(hass)
    controller = await _controller(hass, plan.pop("limit", CAR_LIMIT))
    end = dt_util.utcnow() + timedelta(minutes=10)
    await controller.async_install(_plan(end, **plan))
    await hass.async_block_till_done()
    assert charger.calls == ["turn_on"], "the open window starts the charge"
    charger.amps(amps)
    await hass.async_block_till_done()
    await _later(hass, freezer, 600)
    return controller, charger, end


@pytest.mark.parametrize("plan", [{"to_vehicle_limit": True}, {"target": 80.0}, {"target": 100.0, "limit": None}])
async def test_a_tapering_car_finishes_within_the_extension(
    hass: HomeAssistant, freezer: Any, plan: dict[str, Any]
) -> None:
    controller, charger, end = await _charging_at_the_window_end(hass, freezer, **plan)

    assert controller.top_off_until == end + timedelta(minutes=60)
    assert controller.plan is not None and charger.stops == 0, "the car still draws: it finishes"
    assert controller.plan_window_active_now and controller.charging_to_vehicle_limit
    saved = await controller._store.async_load()  # noqa: SLF001 - the deadline survives a restart
    assert saved is not None and saved["top_off_until"] == (end + timedelta(minutes=60)).isoformat()

    # The car tapers: still drawing is still charging.
    charger.amps(2.5)
    await _later(hass, freezer, 15 * 60)
    assert controller.top_off_until is not None and charger.stops == 0

    # It stops by itself; one minute of nothing is not yet full.
    charger.amps(0.0)
    await _later(hass, freezer, 60)
    assert controller.top_off_until is not None and charger.stops == 0
    await _later(hass, freezer, 90)
    assert controller.plan is None and controller.top_off_until is None
    assert charger.stops == 1
    assert controller.completion_record is not None and controller.completion_record["reason"] == "vehicle_full"
    saved = await controller._store.async_load()  # noqa: SLF001
    assert saved is not None and saved["top_off_until"] is None
    await controller.async_shutdown()


async def test_a_car_that_draws_again_is_not_full(hass: HomeAssistant, freezer: Any) -> None:
    controller, charger, _ = await _charging_at_the_window_end(hass, freezer, to_vehicle_limit=True)
    charger.amps(0.0)
    await _later(hass, freezer, 90)
    charger.amps(4.0)  # balancing in the car, a cell top-up: drawing again
    await _later(hass, freezer, 60)
    charger.amps(0.0)
    await _later(hass, freezer, 90)
    assert controller.top_off_until is not None and charger.stops == 0, "the idle clock starts again"
    await _later(hass, freezer, 60)
    assert controller.plan is None and charger.stops == 1
    await controller.async_shutdown()


async def test_the_top_off_ends_an_hour_past_the_window_as_a_normal_end(hass: HomeAssistant, freezer: Any) -> None:
    controller, charger, end = await _charging_at_the_window_end(hass, freezer, to_vehicle_limit=True)

    await _later(hass, freezer, 59 * 60)
    assert controller.top_off_until is not None and charger.stops == 0
    await _later(hass, freezer, 90)

    assert controller.plan is None and controller.top_off_until is None and charger.stops == 1
    assert controller.completion_record is not None and controller.completion_record["reason"] == "plan_done"
    # Nothing is expected of the charger any more: no "stopped unexpectedly" can follow.
    assert not controller.plan_expects_charge
    await controller.async_shutdown()


async def test_the_departure_caps_the_top_off(hass: HomeAssistant, freezer: Any) -> None:
    end_in = dt_util.utcnow() + timedelta(minutes=10)
    controller, charger, end = await _charging_at_the_window_end(
        hass, freezer, to_vehicle_limit=True, departure=end_in + timedelta(minutes=20)
    )
    assert end == end_in
    assert controller.top_off_until == end + timedelta(minutes=20)
    await _later(hass, freezer, 21 * 60)
    assert controller.plan is None and charger.stops == 1
    assert controller.completion_record is not None and controller.completion_record["reason"] == "plan_done"
    await controller.async_shutdown()


async def test_a_departure_at_the_window_end_leaves_no_top_off(hass: HomeAssistant, freezer: Any) -> None:
    end_in = dt_util.utcnow() + timedelta(minutes=10)
    controller, charger, _ = await _charging_at_the_window_end(
        hass, freezer, to_vehicle_limit=True, departure=end_in
    )
    assert controller.top_off_until is None and controller.plan is None and charger.stops == 1
    await controller.async_shutdown()


async def test_a_persons_stop_ends_the_top_off(hass: HomeAssistant, freezer: Any) -> None:
    controller, charger, _ = await _charging_at_the_window_end(hass, freezer, to_vehicle_limit=True)
    assert controller.top_off_until is not None

    await controller.async_stop()
    await hass.async_block_till_done()

    assert controller.plan is None and controller.top_off_until is None and charger.stops == 1
    assert controller.completion_record is None, "a person's Stop is not a completed charge"
    await _later(hass, freezer, 3600)
    assert charger.calls == ["turn_on", "turn_off"]
    await controller.async_shutdown()


async def test_a_balancing_pause_keeps_the_top_off_and_is_not_the_car_being_full(
    hass: HomeAssistant, freezer: Any
) -> None:
    controller, charger, _ = await _charging_at_the_window_end(hass, freezer, to_vehicle_limit=True)

    await controller._regulated_stop("pause")  # noqa: SLF001 - load balancing's pause
    await _later(hass, freezer, 5 * 60)

    assert controller.top_off_until is not None and controller.plan is not None
    assert controller.paused_by_balancing and charger.stops == 1
    assert controller.completion_record is None
    await controller.async_shutdown()


async def test_a_car_already_full_at_the_window_end_is_not_extended(hass: HomeAssistant, freezer: Any) -> None:
    controller, charger, _ = await _charging_at_the_window_end(hass, freezer, amps=0.0, to_vehicle_limit=True)

    assert controller.top_off_until is None and controller.plan is None and charger.stops == 1
    assert controller.completion_record is not None and controller.completion_record["reason"] == "plan_done"
    await controller.async_shutdown()


@pytest.mark.parametrize("plan", [{"target": 70.0}, {}])
async def test_a_plan_below_the_cars_limit_ends_with_its_window(
    hass: HomeAssistant, freezer: Any, plan: dict[str, Any]
) -> None:
    """A target below the car's own limit, or an ordinary manual amount: the window's end stops it."""
    controller, charger, _ = await _charging_at_the_window_end(hass, freezer, **plan)

    assert controller.top_off_until is None and controller.plan is None and charger.stops == 1
    await controller.async_shutdown()


async def test_the_sun_taking_over_at_the_window_end_is_no_top_off(hass: HomeAssistant, freezer: Any) -> None:
    charger = Charger(hass)
    controller = await _controller(hass)
    controller.set_end_window_guard(lambda: True)
    end = dt_util.utcnow() + timedelta(minutes=10)
    await controller.async_install(_plan(end, to_vehicle_limit=True))
    charger.amps(10.0)
    await _later(hass, freezer, 600)
    assert controller.top_off_until is None and charger.stops == 0, "solar owns the charger, as before"
    await controller.async_shutdown()


async def test_a_restart_mid_top_off_goes_on_and_ends_it_by_the_same_rules(hass: HomeAssistant, freezer: Any) -> None:
    controller, charger, end = await _charging_at_the_window_end(hass, freezer, to_vehicle_limit=True)
    await _later(hass, freezer, 10 * 60)
    await controller.async_shutdown()

    restarted = await _controller(hass)
    await hass.async_block_till_done()
    assert restarted.top_off_until == end + timedelta(minutes=60)
    assert restarted.plan is not None and charger.stops == 0, "the restart does not stop a car still drawing"

    charger.amps(0.0)
    await _later(hass, freezer, 150)
    assert restarted.plan is None and restarted.top_off_until is None and charger.stops == 1
    assert restarted.completion_record is not None and restarted.completion_record["reason"] == "vehicle_full"
    await restarted.async_shutdown()


async def test_a_restart_after_the_deadline_stops_the_charge(hass: HomeAssistant, freezer: Any) -> None:
    controller, charger, _ = await _charging_at_the_window_end(hass, freezer, to_vehicle_limit=True)
    await controller.async_shutdown()
    freezer.tick(timedelta(minutes=90))

    restarted = await _controller(hass)
    await hass.async_block_till_done()

    assert restarted.top_off_until is None and charger.stops == 1, "a charge past every deadline is stopped"
    await restarted.async_shutdown()


# ---------------------------------------------------------------------------- Auto's side


@pytest.mark.usefixtures("offline_relay")
async def test_auto_stamps_the_departure_on_the_plan_and_a_top_off_counts_as_its_window(
    hass: HomeAssistant, transport: Any
) -> None:
    from freezegun import freeze_time

    from custom_components.spotnav.planning.auto_settings import DRIVER_MANUAL_KWH, TargetSocIntent
    from custom_components.spotnav.runtime import charger_data, preview_for
    from tests.relay import serve as serve_prices
    from tests.test_dashboard_api import NOW

    from .world import charger_and_car, controller_of

    serve_prices(transport, rising=True)
    with freeze_time(NOW):
        charger, _, _ = await charger_and_car(
            hass, capacity=77.4, soc_percent="96", driver=DRIVER_MANUAL_KWH, requested_kwh=43.5,
            target=TargetSocIntent(), amps=16, phases=3, departure_enabled=True,
        )
        await hass.async_block_till_done()
        controller = controller_of(hass, charger.entry_id)
        snapshot = preview_for(hass, charger.entry_id).snapshot()
        assert snapshot.departure_at is not None
        assert controller.plan is not None and controller.plan.to_vehicle_limit
        assert controller.plan.departure == snapshot.departure_at.isoformat()

        executor = charger_data(hass, charger.entry_id).executor
        controller._top_off_until = dt_util.utcnow() + timedelta(minutes=30)  # noqa: SLF001 - a top-off running
        assert executor.window_charging_now(), "a newer plan waits for the top-off's end"
        assert executor.execution_state() == "active"
        controller._top_off_until = None  # noqa: SLF001
