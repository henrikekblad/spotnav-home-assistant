"""A person's choice of the car counts as "just before the plug-in" from the moment it was written, never from
the moment identification next happened to look (field report: the choice 15 minutes before the plug-in was
noticed only at the plug-in, and the plug-in was taken as chosen by hand, with nothing identified)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.settings import async_update_settings, encode_settings
from custom_components.spotnav.vehicles.identification import METHOD_ASSUMED, METHOD_MANUAL, RECENT_CHOICE_S

from .test_vehicle_identification import World
from .test_vehicle_identification import world as world  # noqa: F401 - the fixture

pytestmark = pytest.mark.usefixtures("offline_relay")


async def choose_in_the_card(world: World, hass: HomeAssistant, car: str) -> None:
    """A person picks the car in the card's settings while the charger is empty: the settings replacement the
    card's and the app's commands make (`async_update_settings`)."""
    current = world.settings
    body = {key: value for key, value in encode_settings(current).items() if key != "revision"}
    body["target"] = {**body["target"], "vehicle_id": world.cars[car]}
    await async_update_settings(hass, world.entry.entry_id, expected_revision=current.revision, replacement=body)
    await hass.async_block_till_done()


async def test_a_choice_long_before_the_plug_in_does_not_stop_identification( world: World, hass: HomeAssistant) -> None:
    await world.start()
    world.freezer.move_to(world.t0 - timedelta(minutes=15))
    await choose_in_the_card(world, hass, "Tesla")
    assert world.settings.target.vehicle_id == world.cars["Tesla"]
    await world.plug_in()
    block = world.identifier.dashboard()
    assert block["state"] == "waiting", "identified as any plug-in"
    assert world.identifier.method == METHOD_ASSUMED


async def test_a_choice_just_before_the_plug_in_is_the_answer(world: World, hass: HomeAssistant) -> None:
    await world.start()
    world.freezer.move_to(world.t0 - timedelta(seconds=RECENT_CHOICE_S / 2))
    await choose_in_the_card(world, hass, "Tesla")
    await world.plug_in()
    assert world.identifier.method == METHOD_MANUAL


async def test_a_restart_then_a_choice_long_before_the_plug_in(world: World, hass: HomeAssistant) -> None:
    await world.start()
    await hass.config_entries.async_reload(world.entry.entry_id)
    await hass.async_block_till_done()
    from custom_components.spotnav.runtime import charger_data

    controller = charger_data(hass, world.entry.entry_id).controller
    controller.adapter.vehicle_connected = lambda: world.connected  # type: ignore[method-assign]
    await world.observe()
    world.freezer.move_to(world.t0 - timedelta(minutes=15))
    await choose_in_the_card(world, hass, "Tesla")
    await world.plug_in()
    assert world.identifier.dashboard()["state"] == "waiting"
    assert world.identifier.method == METHOD_ASSUMED


async def test_a_choice_noticed_only_at_the_plug_in_still_counts_from_when_it_was_written(
    world: World, hass: HomeAssistant
) -> None:
    """The settings changed 15 minutes before the plug-in by a write no snapshot listener reported (nothing to
    plan with the charger empty): identification first looks at the plug-in, and must not take that as the
    moment the person chose."""
    from custom_components.spotnav.runtime import domain_data

    await world.start()
    world.freezer.move_to(world.t0 - timedelta(minutes=15))
    tesla = world.cars["Tesla"]
    await domain_data(hass).auto_store.async_update(
        world.entry.entry_id, mutate=lambda current: current.with_target_vehicle(tesla, None)
    )
    assert world.settings.target.vehicle_id == tesla
    await world.plug_in()
    assert world.identifier.dashboard()["state"] == "waiting"
    assert world.identifier.method == METHOD_ASSUMED
    diagnostics = world.identifier.diagnostics()
    assert diagnostics["chosen_at"] == (world.t0 - timedelta(minutes=15)).isoformat()


async def test_identifications_own_switch_is_no_persons_choice(world: World, hass: HomeAssistant) -> None:
    await world.start()
    chosen = world.identifier.diagnostics()["chosen_at"]  # the test's own set-up wrote the first car
    await world.plug_in()
    world.car_says("Tesla", "plug", "on")
    await hass.async_block_till_done()
    assert world.settings.target.vehicle_id == world.cars["Tesla"]
    assert world.identifier.diagnostics()["chosen_at"] == chosen
