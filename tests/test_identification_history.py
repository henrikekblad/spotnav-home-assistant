"""What a field report shows of vehicle identification: the status line while a car is identified, and the history.

* While a plug-in's car is being identified the dashboard's status leads with `identifying_vehicle`, and with
  `asking_vehicle` while the question is open; once decided the line is gone.
* The diagnostics keep the last ten sessions (in memory and with the identification state, so a restart keeps them):
  plug-in time, candidates, each car's evidence, the camera (answer, confidence, used, latency, error, AI task and
  model, and why it did or did not decide alone), a session skipped and why, the question (sent, phones), the
  answer, the car and how it was decided (a swiped question says so), and later corrections. No picture, no place.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.debug_bundle import async_build_debug_bundle
from custom_components.spotnav.vehicles.camera_rule import QUERY_TIMEOUT_S
from custom_components.spotnav.vehicles.identification import ASK_AFTER_S, HISTORY_SIZE, RECENT_CHOICE_S

from .test_camera_identification import BLUE, DARK_BLUE, DARK_GREY, Garage
from .test_camera_identification import garage as garage  # noqa: F401 - the fixture
from .test_identification_choice_time import choose_in_the_card
from .test_review2_vehicle_identification import _restart
from .test_vehicle_identification import World
from .test_vehicle_identification import world as world  # noqa: F401 - the fixture

pytestmark = pytest.mark.usefixtures("offline_relay")


def identifying(world: World) -> str | None:
    """The status facts' identification state, from which the dashboard's status is composed (the test charger has no
    price area, so its own status is the blocking "finish the setup", which the line never joins:
    `test_status_compose.py` holds the line ahead of a configured charger's status)."""
    return dashboard_api.status_facts(dashboard_api.capture_dashboard(world.hass, world.entry)).identification


def history(world: World) -> list[dict[str, Any]]:
    return world.identifier.diagnostics()["history"]


# --------------------------------------------------------------------------------- the status line


async def test_the_status_says_the_car_is_identified_until_it_is_decided(world: World) -> None:
    await world.start()
    assert identifying(world) is None, "nothing is identified before a plug-in"
    await world.plug_in()
    assert identifying(world) == "waiting"
    await world.later(ASK_AFTER_S + 5)
    assert identifying(world) == "asking"
    assert await world.identifier.async_answer(world.cars["Tesla"])
    await world.hass.async_block_till_done()
    assert identifying(world) == "decided"


# --------------------------------------------------------------------------------- the history


async def test_an_asked_and_answered_session_is_kept_with_its_question_and_answer(world: World) -> None:
    await world.start(phones=("mobile_app_pixel", "mobile_app_iphone"))
    await world.plug_in()
    plugged_in = world.t0
    await world.later(ASK_AFTER_S + 5)
    question = world.sent()[0]
    await world.later(20)
    await world.tap(question, 1)
    [entry] = history(world)
    assert entry["plugged_in_at"] == plugged_in.isoformat()
    assert entry["trigger"] == "plug_in"
    assert entry["mode"] == "automatic"
    assert entry["candidates"] == [world.cars["Kia"], world.cars["Tesla"]]
    assert {item["vehicle_id"] for item in entry["evidence"]} == set(world.cars.values())
    assert entry["camera"] is None and entry["skipped"] is None
    assert entry["question"] == {"sent_at": (plugged_in + timedelta(seconds=ASK_AFTER_S + 5)).isoformat(), "phones": 2}
    assert entry["answered_at"] == (plugged_in + timedelta(seconds=ASK_AFTER_S + 25)).isoformat()
    assert entry["vehicle_id"] == world.cars["Tesla"] and entry["method"] == "answered"
    assert entry["decided_at"] == entry["answered_at"]
    assert entry["assumed"] is None and entry["corrections"] == []
    assert "Kia" not in repr(entry) and "Tesla" not in repr(entry), "ids only, never a name"


async def test_a_swiped_question_says_it_was_swiped(world: World, hass: HomeAssistant) -> None:
    await world.start()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    hass.bus.async_fire("mobile_app_notification_cleared", {"tag": world.sent()[0]["data"]["tag"]})
    await hass.async_block_till_done()
    [entry] = history(world)
    assert entry["method"] == "assumed" and entry["assumed"] == "swiped"
    assert entry["vehicle_id"] == world.cars["Kia"] and entry["answered_at"] is None


async def test_unplugging_before_a_decision_closes_the_session_as_unplugged(world: World) -> None:
    await world.start()
    await world.plug_in()
    await world.later(60)
    await world.unplug()
    await world.later(130)
    [entry] = history(world)
    assert entry["method"] == "assumed" and entry["assumed"] == "unplugged"


async def test_a_later_correction_is_kept_after_the_decision(world: World) -> None:
    await world.start()
    await world.plug_in()
    world.car_says("Tesla", "plug", "on")
    await world.later(5)
    assert await world.identifier.async_answer(world.cars["Kia"])
    [entry] = history(world)
    assert entry["vehicle_id"] == world.cars["Tesla"] and entry["method"] == "plug_sensor"
    assert entry["corrections"] == [
        {"at": entry["corrections"][0]["at"], "vehicle_id": world.cars["Kia"], "method": "answered"}
    ]


async def test_a_session_that_did_not_run_says_why(world: World, hass: HomeAssistant) -> None:
    await world.start()
    world.freezer.move_to(world.t0 - timedelta(seconds=RECENT_CHOICE_S / 2))
    await choose_in_the_card(world, hass, "Tesla")
    chosen_at = world.identifier.diagnostics()["chosen_at"]
    await world.plug_in()
    [entry] = history(world)
    assert entry["skipped"] == {"reason": "recent_choice", "chosen_at": chosen_at}
    assert entry["method"] == "manual" and entry["vehicle_id"] == world.cars["Tesla"]
    assert entry["question"] is None and entry["evidence"] == []


async def test_identification_off_is_a_skipped_session(world: World) -> None:
    await world.start(mode="off")
    await world.plug_in()
    [entry] = history(world)
    assert entry["skipped"] == {"reason": "off", "chosen_at": None}


async def test_the_last_ten_are_kept_and_survive_a_restart(world: World, monkeypatch: Any) -> None:
    await world.start()
    for _ in range(HISTORY_SIZE + 2):
        await world.plug_in()
        world.car_says("Tesla", "plug", "on")
        await world.later(5)
        world.car_says("Tesla", "plug", "off")
        await world.unplug()
        await world.later(300)
    kept = history(world)
    assert len(kept) == HISTORY_SIZE
    assert all(entry["method"] == "plug_sensor" for entry in kept)
    await world.plug_in()
    await world.later(5)
    before = history(world)
    assert before[-1]["decided_at"] is None
    await _restart(world, monkeypatch)
    after = history(world)
    assert len(after) == HISTORY_SIZE, "the history is kept with the identification state"
    assert [item["plugged_in_at"] for item in after[:-1]] == [item["plugged_in_at"] for item in before[1:]]
    assert [item["method"] for item in after[:-1]] == [item["method"] for item in before[1:]]
    assert after[-1]["trigger"] == "restart", "a plug-in still undecided at the restart is identified again"


async def test_the_debug_bundle_carries_the_history(world: World, hass: HomeAssistant) -> None:
    await world.start()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    bundle = await async_build_debug_bundle(hass)
    [charger] = [section for section in bundle["chargers"] if section["entry_id"] == world.entry.entry_id]
    identification = charger["diagnostics"]["vehicle_identification"]
    assert identification["history"][0]["question"]["phones"] == 1


# --------------------------------------------------------------------------------- the camera in the history


def camera_entry(garage: Garage) -> dict[str, Any]:
    return history(garage.world)[-1]["camera"]


async def test_a_camera_that_decided_alone_says_so_with_its_latency_and_ai_task(garage: Garage) -> None:
    await garage.start()
    garage.car_parks(BLUE)
    await garage.world.plug_in()
    await garage.settle()
    camera = camera_entry(garage)
    assert camera["entity_id"] == "camera.norr" and camera["ai_task_entity_id"] == "ai_task.local"
    assert camera["model"] is None, "the fake AI Task names no model"
    assert camera["answer"] == garage.world.cars["Tesla"] and camera["confidence"] == "high" and camera["used"] is True
    assert camera["camera_reason"] == "decided" and camera["colour_distance"] is None
    assert camera["error"] is None and camera["attempts"] == 1
    assert isinstance(camera["latency_s"], float) and camera["latency_s"] >= 0
    entry = history(garage.world)[-1]
    assert entry["method"] == "camera" and entry["vehicle_id"] == garage.world.cars["Tesla"]
    assert "data" not in repr(camera), "never a picture"


async def test_between_similar_cars_the_reason_is_the_colour_with_its_distance(garage: Garage) -> None:
    await garage.start(colours=(DARK_BLUE, DARK_GREY))
    garage.car_parks(DARK_GREY)
    await garage.world.plug_in()
    await garage.settle()
    camera = camera_entry(garage)
    assert camera["camera_reason"] == "similar_colour"
    assert 0 < camera["colour_distance"] < 0.25


async def test_a_less_sure_answer_says_why(garage: Garage) -> None:
    await garage.start()
    garage.model.confidence = "medium"
    garage.car_parks(BLUE)
    await garage.world.plug_in()
    await garage.settle()
    assert camera_entry(garage)["camera_reason"] == "confidence_low"


async def test_a_slow_model_is_a_timeout(garage: Garage) -> None:
    await garage.start()
    garage.model.hold = asyncio.Event()
    garage.car_parks(BLUE)
    await garage.world.plug_in()
    await garage.model.asked.wait()
    await garage.world.later(QUERY_TIMEOUT_S + 1)
    await garage.settle()
    camera = camera_entry(garage)
    assert camera["error"] == "timeout" and camera["answer"] is None and camera["camera_reason"] is None
    assert camera["latency_s"] >= QUERY_TIMEOUT_S - 1
    garage.model.hold.set()


async def test_a_camera_without_a_picture_and_a_failing_model_name_their_error(garage: Garage) -> None:
    await garage.start()
    garage.camera.fail = True
    await garage.world.plug_in()
    await garage.settle()
    camera = camera_entry(garage)
    assert camera["error"] == "no_snapshot" and camera["attempts"] == 2

    garage.camera.fail = False
    await garage.world.unplug()
    await garage.world.later(300)
    garage.model.errors = [HomeAssistantError("down"), HomeAssistantError("still down")]
    garage.car_parks(BLUE)
    await garage.world.plug_in()
    await garage.settle()
    await garage.world.later(60)
    await garage.settle()
    assert camera_entry(garage)["error"] == "ai_task_error"


# --------------------------------------------------------------------------------- the paired app


def test_the_webhook_withholds_the_identification_lines_from_an_app_that_does_not_ask_for_them() -> None:
    """An app released before these lines words a code it does not know as "see Home Assistant", in place of a few
    minutes of real status: the webhook leaves them out unless the request reads `identification_status`."""
    from custom_components.spotnav.api.webhook import _for_app

    body = {
        "ok": True,
        "status": {
            "tone": "normal",
            "lines": [
                {"code": "asking_vehicle", "params": {}},
                {"code": "auto_installed", "params": {"start": "2026-10-07T22:00:00+00:00"}},
            ],
        },
    }
    older = _for_app(body, {"action": "dashboard"})
    assert [line["code"] for line in older["status"]["lines"]] == ["auto_installed"]
    assert older["status"]["tone"] == "normal"
    newer = _for_app(body, {"action": "dashboard", "reads": ["identification_status"]})
    assert [line["code"] for line in newer["status"]["lines"]] == ["asking_vehicle", "auto_installed"]
    assert [line["code"] for line in body["status"]["lines"]] == ["asking_vehicle", "auto_installed"], "a copy"
