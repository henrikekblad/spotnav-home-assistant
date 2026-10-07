"""Adversarial review: each test states what must hold and fails on the branch."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import TargetSocIntent
from custom_components.spotnav.runtime import charger_data, domain_data
from custom_components.spotnav.vehicles import vehicle_properties
from custom_components.spotnav.vehicles.identification import ASK_AFTER_S

from .test_vehicle_identification import World
from .world import setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")


@pytest.fixture
async def world(hass: HomeAssistant, freezer: Any) -> World:
    return World(hass, freezer)


async def _second_charger(world: World) -> tuple[Any, dict[str, bool | None]]:
    """A second charger B that can charge the same cars, reporting its own plug."""
    hass = world.hass
    world.freezer.move_to(world.t0 - timedelta(minutes=50))
    entry_b = await setup_charger(hass, entry_id="entry_b", webhook_id="webhook-b",
                                  charge_control="switch.charger_b", title="Carport")
    state = {"connected": False}
    controller = charger_data(hass, "entry_b").controller
    controller.adapter.vehicle_connected = lambda: state["connected"]  # type: ignore[method-assign]
    store = domain_data(hass).auto_store
    kia = world.cars["Kia"]
    await store.async_update(
        "entry_b",
        mutate=lambda s: replace(s, target=TargetSocIntent(vehicle_id=kia, target_percent=80.0)),
    )
    await hass.async_block_till_done()
    await _observe_b(hass, 0)
    world.freezer.move_to(world.t0 - timedelta(seconds=1))
    return entry_b, state


def _real_disk(hass: HomeAssistant) -> None:
    """The test storage saves without yielding; Home Assistant's own writes in an executor and yields."""
    import asyncio

    inner = domain_data(hass).auto_store._store
    original = inner.async_save

    async def save(data: Any) -> None:
        await asyncio.sleep(0)
        await original(data)

    inner.async_save = save


async def _observe_b(hass: HomeAssistant, stamp: int) -> None:
    hass.states.async_set("switch.charger_b", "off", {"stamp": stamp})
    await hass.async_block_till_done()


async def test_two_chargers_never_both_take_the_one_car_whose_plug_went_on(world: World) -> None:
    """A has Tesla, B has Kia, both plugged in within a minute; Tesla's own plug sensor goes on.

    A decides Tesla. While A's switch is being saved, A still "claims" its old car (Kia), so B excludes
    Kia, sees Tesla strong and takes Tesla as well: Kia charges at B as Tesla.
    """
    hass = world.hass
    await world.start()
    _, b = await _second_charger(world)
    await world.plug_in()
    world.freezer.tick(timedelta(seconds=30))
    b["connected"] = True
    await _observe_b(hass, 1)
    world.freezer.tick(timedelta(seconds=30))
    _real_disk(hass)
    world.car_says("Tesla", "plug", "on")
    await hass.async_block_till_done()
    store = domain_data(hass).auto_store
    a_car = store.settings(world.entry.entry_id).target.vehicle_id
    b_car = store.settings("entry_b").target.vehicle_id
    assert not (a_car == b_car == world.cars["Tesla"]), "one car is planned at both chargers"


async def test_a_persons_answer_survives_reseating_the_cable(world: World) -> None:
    """The person answers Tesla, reseats the plug a minute later; the Kia's plug sensor then reports on.

    A choice in the settings within ten minutes is kept as the answer at the next plug-in; an answer to
    the question is not, so the new plug-in switches away from what the person said automatically.
    """
    hass = world.hass
    await world.start()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    assert await world.identifier.async_answer(world.cars["Tesla"])
    await hass.async_block_till_done()
    assert world.settings.target.vehicle_id == world.cars["Tesla"]
    await world.later(60)
    await world.unplug()
    world.freezer.tick(timedelta(seconds=20))
    world.connected = True
    await world.observe()
    world.car_says("Kia", "plug", "on")
    await hass.async_block_till_done()
    assert world.settings.target.vehicle_id == world.cars["Tesla"], "the person's answer was overridden"


async def test_a_person_answering_while_an_automatic_switch_is_saving_wins(world: World) -> None:
    """The person taps Kia in the same instant Tesla's plug sensor decides Tesla.

    `_settle` compares the answer with the stored car (still Kia, the switch is not saved yet), so the
    answer switches nothing, then the automatic switch lands: Tesla planned, method "answered".
    """
    hass = world.hass
    await world.start()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    _real_disk(hass)
    world.car_says("Tesla", "plug", "on")  # A decides Tesla; its write is in flight
    assert await world.identifier.async_answer(world.cars["Kia"])
    await hass.async_block_till_done()
    assert world.identifier.method == "answered"
    assert world.settings.target.vehicle_id == world.cars["Kia"], "the person's answer lost to the evidence"


async def test_a_car_that_just_drove_home_is_not_excluded_by_its_last_away_position(world: World) -> None:
    """The Kia (a cloud poll every 30 min) last reported "not_home" 20 minutes ago, on the way home, and is now
    plugged in; the Tesla is parked at home. The 2-hour freshness rule excludes the Kia and switches to the
    Tesla at once (by "location"): the Kia charges with the Tesla's target.
    """
    hass = world.hass
    await world.start()
    world.freezer.move_to(world.t0 - timedelta(minutes=20))
    world.car_says("Kia", "location", "not_home")
    await hass.async_block_till_done()
    await world.plug_in()
    await hass.async_block_till_done()
    assert world.settings.target.vehicle_id == world.cars["Kia"], "switched away from the car that arrived"


async def test_two_quick_target_edits_at_two_chargers_leave_one_target_per_car(world: World) -> None:
    """Both chargers plan for the Kia (80). A person sets 70 at A, then 90 at B. Every charger planning for
    the Kia and the Kia itself must end on the same target."""
    hass = world.hass
    await world.start()
    await _second_charger(world)
    store = domain_data(hass).auto_store
    kia = world.cars["Kia"]
    await vehicle_properties.async_update_vehicle_properties(
        hass, domain_data(hass).decision_store, kia, {vehicle_properties.KEY_TARGET: 80.0}
    )
    await hass.async_block_till_done()
    await store.async_update(
        world.entry.entry_id, mutate=lambda s: replace(s, target=TargetSocIntent(kia, 70.0)), confirm=True
    )
    await store.async_update("entry_b", mutate=lambda s: replace(s, target=TargetSocIntent(kia, 90.0)), confirm=True)
    await hass.async_block_till_done()
    a = store.settings(world.entry.entry_id).target.target_percent
    b = store.settings("entry_b").target.target_percent
    car = vehicle_properties.stored_properties(hass, kia).target_percent
    assert a == b == car, f"A={a} B={b} car={car}"
    assert car == 90.0, "the latest edit wins"


async def test_migrating_two_chargers_that_planned_one_car_at_different_targets_agrees(world: World) -> None:
    """Before this release A planned the Kia to 80 % and B the Kia to 60 %. After set-up the car's own target
    (what the card now shows for B) and B's planned target must be the same."""
    from custom_components.spotnav.vehicles.vehicle_target import async_adopt

    hass = world.hass
    await world.start()
    await _second_charger(world)
    store = domain_data(hass).auto_store
    kia = world.cars["Kia"]
    listeners, store._write_listeners = store._write_listeners, []  # a record written by the old release
    await vehicle_properties.async_update_vehicle_properties(
        hass, domain_data(hass).decision_store, kia, {vehicle_properties.KEY_TARGET: None}
    )
    await store.async_update("entry_b", mutate=lambda s: replace(s, target=TargetSocIntent(kia, 60.0)))
    store._write_listeners = listeners
    await async_adopt(hass, world.entry.entry_id)
    await async_adopt(hass, "entry_b")
    await hass.async_block_till_done()
    car = vehicle_properties.stored_properties(hass, kia).target_percent
    b = store.settings("entry_b").target.target_percent
    assert b == car, f"B plans {b} % while the Kia's own target (shown in B's card) is {car} %"


async def test_after_migration_an_unrelated_edit_at_one_charger_does_not_move_the_other_chargers_target(
    world: World,
) -> None:
    """As above (A: Kia 80 %, B: Kia 60 % from the old release). Someone then changes only B's departure
    time; A's target, which nobody touched, must stay 80 %."""
    from datetime import time

    from custom_components.spotnav.vehicles.vehicle_target import async_adopt

    hass = world.hass
    await world.start()
    await _second_charger(world)
    store = domain_data(hass).auto_store
    kia = world.cars["Kia"]
    listeners, store._write_listeners = store._write_listeners, []
    await vehicle_properties.async_update_vehicle_properties(
        hass, domain_data(hass).decision_store, kia, {vehicle_properties.KEY_TARGET: None}
    )
    await store.async_update("entry_b", mutate=lambda s: replace(s, target=TargetSocIntent(kia, 60.0)))
    store._write_listeners = listeners
    await async_adopt(hass, world.entry.entry_id)
    await async_adopt(hass, "entry_b")
    await hass.async_block_till_done()
    await store.async_update("entry_b", mutate=lambda s: replace(s, departure=time(6, 30)), confirm=True)
    await hass.async_block_till_done()
    a = store.settings(world.entry.entry_id).target.target_percent
    assert a == 80.0, f"A's target moved to {a} % by a departure edit at B"
