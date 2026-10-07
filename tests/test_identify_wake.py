"""The paired app's wake-up for the question which car is plugged in: every new question wakes it, and so does
the question's end however it ends (answered anywhere, decided by the cars, swiped, unanswered, unplugged), so the
app can take its own notification down. The wake-up is the same empty one as for any event."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.notifications import push as push_module
from custom_components.spotnav.notifications.push import PushRegistration, REPEAT_S
from custom_components.spotnav.runtime import charger_data, domain_data
from custom_components.spotnav.vehicles.identification import ASK_AFTER_S, QUESTION_FOR_S, UNPLUG_DEBOUNCE_S

from .test_push import FakeRelay, REF, WAKE_URL
from .test_vehicle_identification import World

pytestmark = pytest.mark.usefixtures("offline_relay")

WAKE = (WAKE_URL, {"v": 1, "push_ref": REF})


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> FakeRelay:
    fake = FakeRelay()
    monkeypatch.setattr(push_module, "async_get_clientsession", lambda _hass: fake)
    return fake


@pytest.fixture
async def world(hass: HomeAssistant, freezer: Any) -> World:
    return World(hass, freezer)


async def _asked(world: World, relay: FakeRelay, **start: Any) -> dict[str, Any]:
    await world.start(**start)
    await charger_data(world.hass, world.entry.entry_id).push.async_register(PushRegistration(push_ref=REF))
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    assert relay.requests == [WAKE], "the question wakes the app"
    return world.sent()[0]


async def test_an_answer_on_a_phone_wakes_the_app_to_take_its_question_down(world: World, relay: FakeRelay) -> None:
    question = await _asked(world, relay)
    await world.tap(question, 1)
    assert relay.requests == [WAKE, WAKE], "not swallowed by the fifteen-minute repeat rule"
    assert all(body == WAKE[1] for _, body in relay.requests), "no event name, no content"


async def test_a_question_the_cars_decide_wakes_the_app(world: World, relay: FakeRelay) -> None:
    await _asked(world, relay)
    world.car_says("Tesla", "plug", "on")
    await world.later(5)
    assert len(relay.requests) == 2


async def test_a_swiped_question_wakes_the_app(world: World, relay: FakeRelay) -> None:
    question = await _asked(world, relay)
    world.hass.bus.async_fire("mobile_app_notification_cleared", {"tag": question["data"]["tag"]})
    await world.hass.async_block_till_done()
    assert len(relay.requests) == 2


async def test_an_unanswered_question_wakes_the_app_when_it_expires(world: World, relay: FakeRelay) -> None:
    await _asked(world, relay, mode="ask")
    await world.later(QUESTION_FOR_S)
    assert len(relay.requests) == 2


async def test_unplugging_wakes_the_app_once_the_plug_in_is_over(world: World, relay: FakeRelay) -> None:
    await _asked(world, relay)
    await world.unplug()
    await world.later(UNPLUG_DEBOUNCE_S - 30)
    assert len(relay.requests) == 1, "a flapping connector is the same plug-in"
    await world.later(60)
    assert len(relay.requests) == 2


async def test_an_answer_in_the_app_or_the_card_wakes_the_app(world: World, relay: FakeRelay) -> None:
    await _asked(world, relay)
    assert await world.identifier.async_answer(world.cars["Tesla"]) is True
    await world.hass.async_block_till_done()
    assert len(relay.requests) == 2


async def test_a_correction_while_the_question_is_out_wakes_the_app_even_with_no_phone_asked(
    world: World, relay: FakeRelay
) -> None:
    await world.start(cars=3, phones=())
    store = domain_data(world.hass).auto_store
    kia, tesla, volvo = (world.cars[name] for name in ("Kia", "Tesla", "Volvo"))
    await store.async_update(world.entry.entry_id, mutate=lambda s: replace(s, vehicle_ids=(kia, tesla)))
    await charger_data(world.hass, world.entry.entry_id).push.async_register(PushRegistration(push_ref=REF))
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    assert len(relay.requests) == 1 and world.identifier.dashboard()["state"] == "asking"
    # A car added after the question: the answer is a correction outside the question's cars.
    await store.async_update(world.entry.entry_id, mutate=lambda s: replace(s, vehicle_ids=(kia, tesla, volvo)))
    assert await world.identifier.async_answer(volvo) is True
    await world.hass.async_block_till_done()
    assert len(relay.requests) == 2


async def test_a_new_plug_in_soon_after_wakes_for_its_own_question_and_its_end(world: World, relay: FakeRelay) -> None:
    question = await _asked(world, relay)
    await world.tap(question, 1)
    await world.unplug()
    await world.later(UNPLUG_DEBOUNCE_S + 1)
    assert len(relay.requests) == 2, "a decided question is not woken for again at the unplug"
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    assert (dt_util.utcnow() - world.t0).total_seconds() < REPEAT_S
    assert len(relay.requests) == 3, "a new plug-in's question always wakes"
    await world.tap(world.sent()[-1], 0)
    assert len(relay.requests) == 4


async def test_no_question_no_wake(world: World, relay: FakeRelay) -> None:
    await world.start()
    await charger_data(world.hass, world.entry.entry_id).push.async_register(PushRegistration(push_ref=REF))
    await world.plug_in()
    world.car_says("Tesla", "plug", "on")
    await world.later(5)
    await world.unplug()
    await world.later(UNPLUG_DEBOUNCE_S + 1)
    assert relay.requests == []
