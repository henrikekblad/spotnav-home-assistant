"""Review round 3: the correction endpoint against an open question."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.vehicles.identification import ASK_AFTER_S

from .test_vehicle_identification import _identify, World
from .world import add_car

pytestmark = pytest.mark.usefixtures("offline_relay")


@pytest.fixture
async def world(hass: HomeAssistant, freezer: Any) -> World:
    return World(hass, freezer)


async def test_a_correction_to_a_car_added_after_the_question_still_retires_it(
    world: World, hass: HomeAssistant, hass_ws_client, hass_read_only_user
) -> None:
    await world.start()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    question = world.sent()[0]
    assert question["data"].get("actions"), "the question is out"
    # A third car is detected while the question is out (all detected cars can charge here).
    world.cars["Volvo"] = add_car(hass, "Volvo")
    result = await _identify(world, hass, hass_ws_client, hass_read_only_user, world.cars["Volvo"])
    assert result["ok"] is True and world.settings.target.vehicle_id == world.cars["Volvo"]
    last = world.sent()[-1]
    assert last is not question and "actions" not in last.get("data", {}), (
        "the question with its buttons is left on every phone"
    )
