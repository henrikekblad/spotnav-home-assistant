"""A charge the car ends itself: the battery's room caps a manual amount, and a charge to the car's own
limit is never stopped by an estimate, a reading or the delivered energy of SpotNav's.

The field case: an EV6 at 96 % with its own limit at 100 %, 77.4 kWh, efficiency 0.9, asked for
43.5 kWh by hand. The battery has room for 77.4 x 4 % / 0.9 = 3.44 kWh, and that is what is planned.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant, callback
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.execution.controller import ChargingPlan
from custom_components.spotnav.execution.target_stop import (
    charge_ceiling_percent,
    charges_to_vehicle_limit,
    decide_target_stop,
    SocReading,
)
from custom_components.spotnav.planning.auto_controller import _EnergyResolution, LiveVehicleFacts
from custom_components.spotnav.planning.auto_settings import DRIVER_MANUAL_KWH, TargetSocIntent
from custom_components.spotnav.runtime import charger_data, preview_for
from custom_components.spotnav.vehicles.soc_estimate import battery_room_kwh
from tests.relay import serve as serve_prices
from tests.test_dashboard_api import NOW

from .world import REGISTER, charger_and_car, controller_of, future_window

pytestmark = pytest.mark.usefixtures("offline_relay")

FIELD_CAPACITY = 77.4


def _reading(value: float, *, estimated: bool = False) -> SocReading:
    return SocReading(
        soc_percent=value, source="vehicle", entity_id="sensor.car", vehicle_id="car", age_s=10.0,
        estimated=estimated,
    )


# ---------------------------------------------------------------------------- the pure rules


def test_the_room_is_capacity_times_the_headroom_over_the_efficiency() -> None:
    room = battery_room_kwh(soc_percent=96.0, capacity_kwh=FIELD_CAPACITY, vehicle_max_percent=100.0)
    assert room == pytest.approx(FIELD_CAPACITY * 0.04 / 0.9)
    assert round(room, 2) == 3.44
    # The car's own limit is the ceiling; nothing above it is room.
    assert battery_room_kwh(soc_percent=60.0, capacity_kwh=FIELD_CAPACITY, vehicle_max_percent=80.0) == (
        pytest.approx(FIELD_CAPACITY * 0.20 / 0.9)
    )
    assert battery_room_kwh(soc_percent=85.0, capacity_kwh=FIELD_CAPACITY, vehicle_max_percent=80.0) == 0.0
    # Without a level or a battery size there is no room to tell.
    assert battery_room_kwh(soc_percent=None, capacity_kwh=FIELD_CAPACITY) is None
    assert battery_room_kwh(soc_percent=96.0, capacity_kwh=None) is None


def test_a_target_at_or_above_the_cars_own_limit_charges_to_it() -> None:
    assert charge_ceiling_percent(None) == 100.0 and charge_ceiling_percent(80.6) == 80.0
    assert charges_to_vehicle_limit(100.0, None)
    assert charges_to_vehicle_limit(80.0, 80.0) and charges_to_vehicle_limit(90.0, 80.0)
    assert not charges_to_vehicle_limit(79.0, 80.0) and not charges_to_vehicle_limit(90.0, None)
    assert not charges_to_vehicle_limit(None, 80.0)


def test_a_charge_to_the_cars_limit_is_never_stopped_by_a_reading_or_an_estimate() -> None:
    for reading in (_reading(100.0), _reading(100.0, estimated=True), _reading(80.0)):
        decision = decide_target_stop(target_soc_percent=80.0, reading=reading, to_vehicle_limit=True)
        assert decision.stop is False and decision.reason == "vehicle_limit"
    # A target below the car's limit still stops at the target.
    assert decide_target_stop(target_soc_percent=80.0, reading=_reading(80.0)).stop is True
    assert decide_target_stop(target_soc_percent=80.0, reading=_reading(81.0, estimated=True)).stop is True


# ------------------------------------------------------------------ the cap on a manual need


def _cap(hass: HomeAssistant, entry_id: str, facts: LiveVehicleFacts | None, kwh: float) -> _EnergyResolution:
    preview = preview_for(hass, entry_id)
    preview._vehicle_reader = lambda _vehicle_id: facts  # noqa: SLF001 - the live car, made up
    settings = preview._store.settings(entry_id)  # noqa: SLF001
    return preview._capped_by_room(  # noqa: SLF001
        settings, _EnergyResolution(kwh=kwh, delivered_energy_trustworthy=True, basis="register")
    )


@freeze_time(NOW)
async def test_a_manual_need_is_capped_by_the_room_only_when_the_level_and_the_size_are_known(
    hass: HomeAssistant,
) -> None:
    charger, car_id, _ = await charger_and_car(
        hass, driver=DRIVER_MANUAL_KWH, requested_kwh=43.5, target=TargetSocIntent(), amps=16, phases=3
    )
    entry_id = charger.entry_id

    def facts(soc: float | None, capacity: float | None, limit: float | None = None) -> LiveVehicleFacts:
        return LiveVehicleFacts(vehicle_id=car_id, soc_percent=soc, reported_capacity_kwh=capacity, max_percent=limit)

    capped = _cap(hass, entry_id, facts(96.0, FIELD_CAPACITY, 100.0), 43.5)
    assert capped.room_limited and capped.kwh == pytest.approx(3.44, abs=0.01)
    assert capped.uncapped_kwh == 43.5 and capped.room_kwh == capped.kwh

    # The car's own limit is the ceiling: 60 % to 80 % of 77.4 kWh.
    limited = _cap(hass, entry_id, facts(60.0, FIELD_CAPACITY, 80.0), 43.5)
    assert limited.room_limited and limited.kwh == pytest.approx(FIELD_CAPACITY * 0.2 / 0.9)

    # A need below the room stands as counted, and says the room.
    small = _cap(hass, entry_id, facts(40.0, FIELD_CAPACITY), 10.0)
    assert not small.room_limited and small.kwh == 10.0 and small.room_kwh == pytest.approx(51.6)

    # Without a level, a battery size or a vehicle nothing changes.
    for unknown in (facts(None, FIELD_CAPACITY), facts(96.0, None), None):
        same = _cap(hass, entry_id, unknown, 43.5)
        assert same.kwh == 43.5 and not same.room_limited and same.room_kwh is None


def _record_charger_commands(hass: HomeAssistant) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    @callback
    def on_call(event: Any) -> None:
        data = event.data
        service, service_data = data["service"], data.get("service_data", {})
        if service in ("turn_on", "turn_off"):
            targets = service_data.get("entity_id", [])
            for entity_id in [targets] if isinstance(targets, str) else targets:
                calls.append((service, entity_id))
                hass.states.async_set(entity_id, "on" if service == "turn_on" else "off")

    hass.bus.async_listen("call_service", on_call)
    return calls


def _soc(hass: HomeAssistant, entity_id: str, percent: float) -> None:
    hass.states.async_set(entity_id, str(percent), {"device_class": "battery", "unit_of_measurement": "%"})


async def test_the_field_case_plans_the_room_and_the_car_ends_the_charge(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW):
        charger, _, soc_entity = await charger_and_car(
            hass, capacity=FIELD_CAPACITY, soc_percent="96", driver=DRIVER_MANUAL_KWH, requested_kwh=43.5,
            target=TargetSocIntent(), amps=16, phases=3,
        )
        controller = controller_of(hass, charger.entry_id)
        preview = preview_for(hass, charger.entry_id)
        await hass.async_block_till_done()

        snapshot = preview.snapshot()
        assert snapshot.proposal is not None
        assert snapshot.proposal.requested_kwh == pytest.approx(3.44, abs=0.01), "the room, not 43.5 kWh"
        assert snapshot.room_limited and snapshot.remaining_kwh == pytest.approx(43.5)
        assert controller.plan is not None and controller.plan.to_vehicle_limit
        assert controller.charges_to_vehicle_limit()

        payload = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, charger), can_act=True)
        assert payload["soc"]["room_kwh"] == pytest.approx(3.44, abs=0.01)
        codes = {line["code"]: line["params"] for line in payload["status"]["lines"]}
        assert codes["need_limited_by_room"] == {"kwh": 3.4}
        assert ("turn_on", "switch.wallbox") in calls
        assert codes["charging_to_vehicle_limit"] == {"percent": 100}
        # A car that stops taking current now is full, not a charge that failed: nothing is notified.
        notifier = charger_data(hass, charger.entry_id).notifier
        assert notifier is not None and notifier._at_vehicle_limit()  # noqa: SLF001

        # The car reads full: nothing is left to plan, and still nothing of ours ends the charge.
        _soc(hass, soc_entity, 100.0)
        await hass.async_block_till_done()
        await preview.async_recalculate()
        await hass.async_block_till_done()
        assert preview.snapshot().state == "nothing_to_charge"
        assert controller.plan is not None, "the plan stays: the car ends the charge"
        assert ("turn_off", "switch.wallbox") not in calls

        # Nor does the energy stop (the register watcher's way of ending a delivered need).
        assert await charger_data(hass, charger.entry_id).executor.async_end_plan_need_met() is False
        assert controller.plan is not None and ("turn_off", "switch.wallbox") not in calls


@freeze_time(NOW)
async def test_a_need_below_the_room_still_ends_on_the_energy(hass: HomeAssistant) -> None:
    charger, _, _ = await charger_and_car(
        hass, driver=DRIVER_MANUAL_KWH, requested_kwh=10.0, target=TargetSocIntent(), amps=16, phases=3
    )
    controller = controller_of(hass, charger.entry_id)
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    start, end = future_window()
    await controller.async_install(ChargingPlan(start=start, end=end, amps=16, phases=3, energy_kwh=10.0))
    assert not controller.charges_to_vehicle_limit()
    assert await controller.async_end_plan_need_met() is True
    assert controller.plan is None


# --------------------------------------------------------------- a target at the car's limit


def _meter(hass: HomeAssistant, kwh: float) -> None:
    hass.states.async_set(
        REGISTER, str(1000.0 + kwh),
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    )


@pytest.mark.parametrize(("target", "limit"), [(100.0, None), (80.0, 80.0), (90.0, 80.0)])
async def test_a_target_at_the_cars_limit_is_never_stopped_on_an_estimate(
    hass: HomeAssistant, target: float, limit: float | None
) -> None:
    with freeze_time(NOW) as frozen:
        charger, device_id, _ = await charger_and_car(hass)
        controller = controller_of(hass, charger.entry_id)
        controller._vehicle_limit_reader = lambda _vehicle_id: limit  # noqa: SLF001 - the car's own limit
        turn_off = async_mock_service(hass, "homeassistant", "turn_off")
        async_mock_service(hass, "homeassistant", "turn_on")
        start, end = future_window()
        await controller.async_install(
            ChargingPlan(start=start, end=end, amps=16, phases=3, target_soc_percent=target, vehicle_id=device_id)
        )
        assert controller.charges_to_vehicle_limit()
        frozen.tick(timedelta(minutes=30))
        # 40 % plus 60 points of 77 kWh at 0.9: the estimate says 100 %.
        _meter(hass, 52.0)
        await hass.async_block_till_done()
        assert controller.plan is not None and not turn_off, "the car ends it, not the estimate"
        assert controller.target_stop_record is None
        assert await controller.async_end_plan_need_met() is False
        assert controller.plan is not None and not turn_off


async def test_a_target_below_the_cars_limit_still_stops_on_the_estimate(hass: HomeAssistant) -> None:
    with freeze_time(NOW) as frozen:
        charger, device_id, _ = await charger_and_car(hass)
        controller = controller_of(hass, charger.entry_id)
        controller._vehicle_limit_reader = lambda _vehicle_id: 90.0  # noqa: SLF001
        async_mock_service(hass, "homeassistant", "turn_off")
        async_mock_service(hass, "homeassistant", "turn_on")
        start, end = future_window()
        await controller.async_install(
            ChargingPlan(start=start, end=end, amps=16, phases=3, target_soc_percent=80.0, vehicle_id=device_id)
        )
        assert not controller.charges_to_vehicle_limit()
        frozen.tick(timedelta(minutes=30))
        _meter(hass, 35.2)  # 81 % estimated: the target plus its margin
        await hass.async_block_till_done()
        assert controller.plan is None
        assert controller.target_stop_record is not None and controller.target_stop_record["basis"] == "estimate"
