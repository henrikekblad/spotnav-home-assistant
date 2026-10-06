"""Adversarial re-review of the identification fixes: each test states what must hold."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import TargetSocIntent
from custom_components.spotnav.runtime import charger_data, domain_data
from custom_components.spotnav.vehicles.identification import ASK_AFTER_S

from .test_review_vehicle_identification import _real_disk
from .test_vehicle_identification import World

pytestmark = pytest.mark.usefixtures("offline_relay")


@pytest.fixture
async def world(hass: HomeAssistant, freezer: Any) -> World:
    return World(hass, freezer)


async def test_moving_the_cable_to_the_other_car_within_two_minutes_identifies_the_other_car(world: World) -> None:
    """The Kia is identified by its plug sensor and charges. Done, the cable is moved to the Tesla parked beside
    it (40 s). The Kia now says unplugged and the Tesla says plugged in: the Tesla must be planned, or at least
    asked about; the debounce keeps the Kia's decision and ignores both cars' reports."""
    hass = world.hass
    await world.start()
    await world.plug_in()
    world.car_says("Kia", "plug", "on")
    await world.later(30)
    assert world.settings.target.vehicle_id == world.cars["Kia"]
    await world.later(3600)
    await world.unplug()
    world.car_says("Kia", "plug", "off")
    world.freezer.tick(timedelta(seconds=40))
    world.connected = True
    await world.observe()
    world.car_says("Tesla", "plug", "on")
    await world.later(ASK_AFTER_S + 10)
    asked = any(call.get("data", {}).get("actions") for call in world.sent())
    assert world.settings.target.vehicle_id == world.cars["Tesla"] or asked, "the Tesla charges as the Kia"


async def test_an_answer_retried_on_a_conflict_does_not_overwrite_a_newer_choice_in_the_settings(
    world: World,
) -> None:
    """The person taps "Tesla" on the phone; at the same moment another person picks the Volvo in the card's
    settings. The settings write lands first, so the answer meets a revision conflict and is retried; the
    retry must not undo the newer choice of the Volvo silently (it is either kept, or the method says manual)."""
    hass = world.hass
    await world.start(cars=3)
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    _real_disk(hass)
    preview = charger_data(hass, world.entry.entry_id).preview
    volvo = world.cars["Volvo"]
    card = hass.async_create_task(
        preview.async_apply_settings(
            mutate=lambda s: replace(s, target=TargetSocIntent(vehicle_id=volvo, target_percent=70.0)),
            expected_revision=world.settings.revision,
        ),
        eager_start=True,
    )
    answer = hass.async_create_task(world.identifier.async_answer(world.cars["Tesla"]), eager_start=True)
    await asyncio.gather(card, answer)
    await hass.async_block_till_done()
    final = world.settings.target.vehicle_id
    method = world.identifier.method
    assert not (final == world.cars["Tesla"] and method == "answered"), (
        "the card's newer choice of the Volvo was overwritten by the retried answer and nobody was told"
    )


async def test_no_question_is_pushed_for_a_car_that_was_just_unplugged(world: World) -> None:
    """Plugged in, nothing decisive; the car is unplugged at 2 min 50 s and stays unplugged. The ask deadline at
    3 min must not push "Which car is plugged in?" to the phones for a charger with no car."""
    await world.start()
    await world.plug_in()
    await world.later(170)
    await world.unplug()
    await world.later(15)
    assert not any(call.get("data", {}).get("actions") for call in world.sent()), "asked about an empty charger"


async def test_a_reseated_cable_with_nothing_new_keeps_the_decided_car_and_its_one_switch(world: World) -> None:
    await world.start()
    await world.plug_in()
    world.car_says("Tesla", "plug", "on")
    await world.later(30)
    assert world.settings.target.vehicle_id == world.cars["Tesla"]
    await world.unplug()
    await world.later(20)
    world.connected = True
    await world.observe()
    await world.later(ASK_AFTER_S + 10)
    assert world.settings.target.vehicle_id == world.cars["Tesla"]
    assert world.identifier.method == "plug_sensor"
    assert not any(call.get("data", {}).get("actions") for call in world.sent()), "nothing new, nothing asked"


async def test_a_car_replugged_after_the_ask_deadline_passed_while_unplugged_is_asked_about(world: World) -> None:
    await world.start()
    await world.plug_in()
    await world.later(170)
    await world.unplug()
    await world.later(30)
    world.connected = True
    await world.observe()
    await world.hass.async_block_till_done()
    assert any(call.get("data", {}).get("actions") for call in world.sent()), "the question was never asked"


async def _restart(world: World, monkeypatch: Any) -> None:
    """Home Assistant restarts while the car stays plugged in: the reloaded charger sees it connected, no edge."""
    hass = world.hass
    adapter_class = type(charger_data(hass, world.entry.entry_id).controller.adapter)
    monkeypatch.setattr(adapter_class, "vehicle_connected", lambda self: True)
    assert await hass.config_entries.async_reload(world.entry.entry_id)
    await hass.async_block_till_done()


async def test_a_restart_with_a_car_plugged_in_and_nothing_decided_takes_identification_up(
    world: World, monkeypatch: Any
) -> None:
    await world.start()
    await world.plug_in()
    await world.later(60)
    await _restart(world, monkeypatch)
    assert world.identifier.dashboard()["state"] == "waiting"
    await world.later(ASK_AFTER_S + 5)
    asked = [call for call in world.sent() if call.get("data", {}).get("actions")]
    assert asked, "the question is asked after the restart"


async def test_a_restart_after_a_person_answered_keeps_the_answer(world: World, monkeypatch: Any) -> None:
    await world.start()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    assert await world.identifier.async_answer(world.cars["Tesla"])
    sent = len(world.sent())
    await _restart(world, monkeypatch)
    await world.later(ASK_AFTER_S + 5)
    assert world.identifier.method == "answered"
    assert world.identifier.dashboard() is None or world.identifier.dashboard()["state"] == "decided"
    assert len(world.sent()) == sent, "nothing asked again"


async def test_a_button_from_a_question_that_is_gone_clears_it(world: World, monkeypatch: Any) -> None:
    await world.start()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    old = world.sent()[-1]
    await world.unplug()
    await world.later(130)
    clear = {"message": "clear_notification", "data": {"tag": old["data"]["tag"]}}
    before = world.sent().count(clear)
    await world.tap(old, 0)
    assert world.settings.target.vehicle_id == world.cars["Kia"]
    assert world.sent().count(clear) == before + 1, "a stale button takes the question off the phones"
