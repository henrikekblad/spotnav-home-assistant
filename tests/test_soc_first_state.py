"""A car whose state of charge arrives after the planner's first calculation is planned for at once.

The owner's start-up order: the planner calculated 12 ms before the car's integration gave its battery
sensor a first state, said `target_soc_unknown`, and never calculated again. Nothing watched the sensor:
an entity without a state does not resolve, so there was nothing to watch.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.runtime import preview_for
from tests.relay import serve as serve_prices

from .test_dashboard_api import NOW
from .world import charger_and_car

pytestmark = pytest.mark.usefixtures("offline_relay")


def _car_device(hass: HomeAssistant) -> str:
    """A car device with its battery sensor in the registry (no state yet), and another device's battery sensor."""
    MockConfigEntry(domain="test", entry_id="car_entry").add_to_hass(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    car = devices.async_get_or_create(config_entry_id="car_entry", identifiers={("test", "ev6")}, name="EV6")
    entities.async_get_or_create("sensor", "test", "ev6_battery", device_id=car.id, suggested_object_id="ev6_battery")
    phone = devices.async_get_or_create(config_entry_id="car_entry", identifiers={("test", "phone")}, name="Phone")
    entities.async_get_or_create(
        "sensor", "test", "phone_battery", device_id=phone.id, suggested_object_id="phone_battery"
    )
    return car.id


def _reader(hass: HomeAssistant, entity: str) -> Any:
    from custom_components.spotnav.execution.target_stop import resolve_soc_reading
    from custom_components.spotnav.vehicles.soc_estimate import SocReader

    return SocReader(
        hass, "entry-first", raw_reader=lambda _v: resolve_soc_reading(hass, vehicle_id="car", entity_id=entity),
        register_entity_id=lambda: None, charge_control=lambda: None, remembered_capacity=lambda _v: 77.0,
    )

_BATTERY = {"device_class": "battery", "unit_of_measurement": "%"}


async def _restarted_before_the_car(hass: HomeAssistant, transport: Any, frozen: Any) -> tuple[Any, str]:
    serve_prices(transport)
    charger, _car, soc_entity = await charger_and_car(hass, soc_percent="40")
    # Home Assistant starts again: SpotNav is up before the car's integration has given its sensor a state.
    hass.states.async_remove(soc_entity)
    frozen.tick(timedelta(minutes=1))
    assert await hass.config_entries.async_reload(charger.entry_id)
    await hass.async_block_till_done()
    preview = preview_for(hass, charger.entry_id)
    first = await preview.async_recalculate()
    assert (first.state, first.reason) == ("incomplete_settings", "target_soc_unknown")
    return charger, soc_entity


async def test_the_first_state_of_the_cars_sensor_after_the_planner_ran_plans_at_once(
    hass: HomeAssistant, transport: Any
) -> None:
    with freeze_time(NOW) as frozen:
        charger, soc_entity = await _restarted_before_the_car(hass, transport, frozen)
        frozen.tick(timedelta(seconds=1))
        hass.states.async_set(soc_entity, "40", _BATTERY)
        await hass.async_block_till_done()
        snapshot = preview_for(hass, charger.entry_id).snapshot()
        assert snapshot.reason != "target_soc_unknown", "the reading that arrived was never planned on"
        assert snapshot.proposal is not None


async def test_a_level_after_a_calculation_without_one_is_told_even_at_the_value_last_heard(
    hass: HomeAssistant,
) -> None:
    """The reader's own rule: a reading that moved less than two points is not worth a new calculation,
    except the first one after a calculation that had none."""
    from datetime import datetime, timezone

    from custom_components.spotnav.execution.target_stop import resolve_soc_reading
    from custom_components.spotnav.vehicles.soc_estimate import SocReader

    car = _car_device(hass)
    entity = "sensor.ev6_battery"
    told: list[int] = []
    with freeze_time(datetime(2026, 10, 6, 14, 25, 55, tzinfo=timezone.utc)):
        reader = SocReader(
            hass, "entry-first", raw_reader=lambda _v: resolve_soc_reading(hass, vehicle_id="car", entity_id=entity),
            register_entity_id=lambda: None, charge_control=lambda: None, remembered_capacity=lambda _v: 77.0,
        )
        await reader.async_load()
        reader.set_on_reading(lambda: told.append(1))
        reader.await_reading(car)  # a calculation found no level: the sensor has no state yet
        hass.states.async_set(entity, "89", _BATTERY)
        await hass.async_block_till_done()
        assert told == [1], "the first level, heard through the start-up listener"
        reader._last_notified = 89.0  # as after that calculation
        hass.states.async_set(entity, "unavailable", _BATTERY)
        await hass.async_block_till_done()
        reader.await_reading(car)
        hass.states.async_set(entity, "89", _BATTERY)
        await hass.async_block_till_done()
        assert told == [1, 1], "the same value, after a calculation that lacked one, is told"
        reader.async_shutdown()


async def test_only_the_awaited_cars_sensors_are_listened_to(hass: HomeAssistant) -> None:
    """Not every battery sensor in the house: a phone's battery reporting is no reason to resolve the car again."""
    car = _car_device(hass)
    reader = _reader(hass, "sensor.ev6_battery")
    await reader.async_load()
    told: list[int] = []
    reader.set_on_reading(lambda: told.append(1))
    reader.await_reading(car)
    assert reader._arrival_watched == ("sensor.ev6_battery",)  # noqa: SLF001

    hass.states.async_set("sensor.phone_battery", "55", _BATTERY)
    await hass.async_block_till_done()
    assert told == []
    hass.states.async_set("sensor.ev6_battery", "70", _BATTERY)
    await hass.async_block_till_done()
    assert told == [1]
    assert reader._arrival_cancel is None, "heard: the listener is gone"  # noqa: SLF001
    reader.async_shutdown()


async def test_with_no_vehicle_to_wait_for_nothing_is_listened_to(hass: HomeAssistant) -> None:
    _car_device(hass)
    reader = _reader(hass, "sensor.ev6_battery")
    await reader.async_load()
    reader.await_reading(None)

    assert reader._arrival_cancel is None and reader._arrival_watched == ()  # noqa: SLF001
    reader.async_shutdown()
