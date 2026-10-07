"""A state that Home Assistant writes at its start, or when an integration re-creates or restores an entity, is
no report from the car: its `last_changed` and `last_reported` move, but the car said nothing.

Field case: a restart re-created a Kia's plug sensor with "on" (unchanged); an emulated plug-in four and a half
minutes later took that for the car being plugged in (`plug_sensor`). Only a change SpotNav saw happen counts.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.runtime import charger_data
from custom_components.spotnav.vehicles.identification import (
    ASK_AFTER_S,
    METHOD_LOCATION,
    METHOD_PLUG_SENSOR,
)

from .test_vehicle_identification import World

pytestmark = pytest.mark.usefixtures("offline_relay")


@pytest.fixture
async def world(hass: HomeAssistant, freezer: Any) -> World:
    return World(hass, freezer)


async def _restart(world: World, *, plug: str | None = None, tracker: str | None = None, before: bool = True) -> None:
    """Home Assistant restarts with nothing at the charger: the Tesla's entities are created again with these
    values, before SpotNav loads (`before`) or after it."""
    hass = world.hass

    def recreate() -> None:
        if plug is not None:
            hass.states.async_remove(world.plugs["Tesla"])
            hass.states.async_set(world.plugs["Tesla"], plug, {"device_class": "plug"})
        if tracker is not None:
            hass.states.async_remove(world.trackers["Tesla"])
            hass.states.async_set(world.trackers["Tesla"], tracker, {})

    if before:
        recreate()
    adapter_class = type(charger_data(hass, world.entry.entry_id).controller.adapter)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(adapter_class, "vehicle_connected", lambda _self: world.connected)
        assert await hass.config_entries.async_reload(world.entry.entry_id)
        await hass.async_block_till_done()
    charger_data(hass, world.entry.entry_id).controller.adapter.vehicle_connected = lambda: world.connected
    if not before:
        recreate()
    await hass.async_block_till_done()


@pytest.mark.parametrize("before", [True, False], ids=["integration-first", "spotnav-first"])
async def test_a_plug_sensor_created_again_at_a_restart_does_not_decide_a_plug_in_four_and_a_half_minutes_later(
    world: World, before: bool
) -> None:
    await world.start()
    world.car_says("Tesla", "plug", "on")
    await world.later(3600)
    await _restart(world, plug="on", before=before)
    await world.later(270)
    await world.plug_in()
    await world.later(30)
    assert world.identifier.method != METHOD_PLUG_SENSOR, "no real change: the restart is no plug-in"
    assert world.settings.target.vehicle_id == world.cars["Kia"]
    await world.later(ASK_AFTER_S)
    assert any(call.get("data", {}).get("actions") for call in world.sent()), "the question is asked"


async def test_a_plug_sensor_re_created_by_its_integration_without_a_restart_is_no_change(world: World) -> None:
    hass = world.hass
    await world.start()
    world.car_says("Tesla", "plug", "on")
    await world.later(3600)
    hass.states.async_remove(world.plugs["Tesla"])
    await hass.async_block_till_done()
    world.car_says("Tesla", "plug", "on")
    await world.later(270)
    await world.plug_in()
    await world.later(30)
    assert world.identifier.method != METHOD_PLUG_SENSOR


async def test_a_plug_sensor_back_from_unavailable_with_the_same_value_is_no_change(world: World) -> None:
    await world.start()
    world.car_says("Tesla", "plug", "on")
    await world.later(3600)
    world.car_says("Tesla", "plug", "unavailable")
    await world.later(60)
    world.car_says("Tesla", "plug", "on")
    await world.later(200)
    await world.plug_in()
    await world.later(30)
    assert world.identifier.method != METHOD_PLUG_SENSOR


async def test_a_plug_sensor_back_from_unavailable_with_another_value_is_a_change(world: World) -> None:
    await world.start()
    world.car_says("Tesla", "plug", "unavailable")
    await world.later(60)
    world.car_says("Tesla", "plug", "on")
    await world.later(60)
    await world.plug_in()
    await world.later(30)
    assert world.identifier.method == METHOD_PLUG_SENSOR
    assert world.settings.target.vehicle_id == world.cars["Tesla"]


@pytest.mark.parametrize("before", [True, False], ids=["integration-first", "spotnav-first"])
async def test_a_real_plug_in_shortly_after_a_restart_still_decides(world: World, before: bool) -> None:
    await world.start()
    await world.later(3600)
    await _restart(world, plug="off", before=before)
    await world.later(30)
    world.car_says("Tesla", "plug", "on")
    await world.later(120)
    await world.plug_in()
    await world.later(30)
    assert world.identifier.method == METHOD_PLUG_SENSOR
    assert world.settings.target.vehicle_id == world.cars["Tesla"]


async def test_a_real_change_after_the_plug_in_that_follows_a_restart_still_decides(world: World) -> None:
    await world.start()
    world.car_says("Tesla", "plug", "on")
    await world.later(3600)
    await _restart(world, plug="off")
    await world.later(60)
    await world.plug_in()
    await world.later(40)
    world.car_says("Tesla", "plug", "on")
    await world.hass.async_block_till_done()
    assert world.identifier.method == METHOD_PLUG_SENSOR
    assert world.settings.target.vehicle_id == world.cars["Tesla"]


async def test_an_unplugged_plug_sensor_created_again_at_a_restart_excludes_nothing(world: World) -> None:
    await world.start()
    await world.later(3600)
    await _restart(world, plug="off", before=False)
    await world.later(30)
    await world.plug_in()
    await world.later(30)
    assert world.identifier.method != METHOD_PLUG_SENSOR, "the Kia is not decided by the Tesla's re-created state"
    assert world.identifier.method != METHOD_LOCATION


async def test_a_position_created_again_at_a_restart_excludes_nothing(world: World) -> None:
    await world.start()
    world.car_says("Tesla", "location", "not_home")
    await world.later(3600)
    await _restart(world, tracker="not_home", before=False)
    await world.later(30)
    await world.plug_in()
    await world.later(30)
    assert world.identifier.method != METHOD_LOCATION, "the Tesla's restored position is no report"


async def test_a_position_reported_after_a_restart_still_excludes(world: World) -> None:
    await world.start()
    world.car_says("Tesla", "location", "not_home")
    await world.later(3600)
    await _restart(world, tracker="not_home")
    await world.later(600)
    await world.plug_in()
    await world.later(150)
    world.hass.states.async_set(world.trackers["Tesla"], "not_home", {"lat": 1})
    await world.hass.async_block_till_done()
    await world.later(60)
    assert world.identifier.method == METHOD_LOCATION
    assert world.settings.target.vehicle_id == world.cars["Kia"]
