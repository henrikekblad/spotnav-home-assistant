"""A guest car: a visitor's car planned by kWh with no vehicle, only once a person said so.

The question offers "Guest car" (also at a one-car charger, there only when its car is ruled out), the camera that
sees none of the cars puts it first, and nothing switches to a guest by itself. A guest plan is the `manual_kwh`
driver with the remembered guest amount and no vehicle: nothing of a car is read or learned. Once the amount is
delivered the charger is held until the unplug (a person's Start still charges); the unplug brings the household
car and its settings back, a restart while plugged in keeps the guest. The dashboard and the webhook carry the guest
only for clients that read it. The last test is the whole evening end to end, and its core events replayed.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.webhook import _for_app
from custom_components.spotnav.const import GUEST_VEHICLE_ID
from custom_components.spotnav.core.replay import replay_bundle
from custom_components.spotnav.notifications.messages import compose
from custom_components.spotnav.notifications.settings import NotificationSettings
from custom_components.spotnav.planning.auto_controller import live_vehicle_facts
from custom_components.spotnav.planning.auto_settings import (
    AutoSettingsError,
    DRIVER_MANUAL_KWH,
    DRIVER_TARGET_SOC,
    TargetSocIntent,
)
from custom_components.spotnav.runtime import charger_data, domain_data
from custom_components.spotnav.sessions.inputs import session_facts
from custom_components.spotnav.vehicles import vehicle_properties
from custom_components.spotnav.vehicles.camera_rule import ANSWER_OTHER
from custom_components.spotnav.vehicles.camera_identification import CameraAnswer
from custom_components.spotnav.vehicles.guest import (
    DEFAULT_GUEST_KWH,
    guest_amount_kwh,
    GuestPrevious,
    is_guest,
    restored,
    with_guest,
)
from custom_components.spotnav.vehicles.identification import (
    ANSWERED_BY_CHOICE,
    ANSWERED_BY_NOTIFICATION,
    ASK_AFTER_S,
    LISTEN_FOR_S,
    METHOD_ANSWERED,
    RECENT_CHOICE_S,
    METHOD_ONLY_CANDIDATE,
    UNPLUG_DEBOUNCE_S,
)
from custom_components.spotnav.vehicles.vehicle_discovery import resolve_target_vehicle
from tests.relay import serve as serve_prices

from .test_core_replay import _bundle
from .test_dashboard_api import NOW
from .test_replug import _meter, _record_charger_commands, Car
from .test_vehicle_identification import World
from .world import charger_and_car, vehicle_entity, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")


@pytest.fixture
async def world(hass: HomeAssistant, freezer: Any) -> World:
    return World(hass, freezer)


def titles(payload: dict[str, Any]) -> list[str]:
    return [action["title"] for action in payload["data"]["actions"]]


# --------------------------------------------------------------------------------- the settings (pure)


def test_a_guest_plan_is_the_manual_driver_with_no_vehicle_and_the_settings_it_found_come_back() -> None:
    from custom_components.spotnav.planning.auto_settings import AutoSettings

    household = AutoSettings(
        driver=DRIVER_TARGET_SOC, requested_kwh=20.0, fill_to_limit=True,
        target=TargetSocIntent(vehicle_id="kia", target_percent=80.0),
    )
    guest = with_guest(household, 10.0)
    assert is_guest(guest) and not is_guest(household)
    assert (guest.driver, guest.requested_kwh, guest.fill_to_limit) == (DRIVER_MANUAL_KWH, 10.0, False)
    assert guest.target == TargetSocIntent(vehicle_id=GUEST_VEHICLE_ID, target_percent=None)
    assert "vehicle" not in guest.missing_for_auto() and "target_percent" not in guest.missing_for_auto()
    previous = GuestPrevious.of(household)
    assert GuestPrevious.from_stored(previous.as_dict()) == previous
    assert restored(guest, previous) == household
    assert GuestPrevious.from_stored({"driver": "nope"}) is None
    lost = restored(guest, None)
    assert lost.target.vehicle_id is None and lost.driver == DRIVER_MANUAL_KWH, "only the vehicle goes"


def test_the_new_plan_notification_says_it_is_the_guests() -> None:
    _, message = compose("plan_installed", "Garage", {"time": "02:00", "guest": True}, "sv")
    assert message.startswith("Ny plan för gästbilen: laddar från 02:00.")
    _, message = compose("plan_installed", "Garage", {"time": "02:00"}, "en")
    assert message.startswith("New plan: charging from 02:00.")


# --------------------------------------------------------------------------------- the question


async def test_a_one_car_charger_asks_only_when_its_car_is_ruled_out(world: World) -> None:
    await world.start(cars=1)
    await world.plug_in()
    await world.later(ASK_AFTER_S + 60)
    assert world.sent() == [], "a car that says nothing is no question"
    assert world.identifier.dashboard() is None or world.identifier.dashboard()["state"] == "decided"
    await world.later(LISTEN_FOR_S)
    assert world.sent() == [] and world.identifier.method == METHOD_ONLY_CANDIDATE
    entry = world.identifier.diagnostics()["history"][-1]
    assert entry["method"] == METHOD_ONLY_CANDIDATE and entry["skipped"] is None and entry["question"] is None

    await world.unplug()
    await world.later(UNPLUG_DEBOUNCE_S + 1)
    await world.plug_in()
    await world.later(40)
    world.car_says("Kia", "plug", "on")
    world.car_says("Kia", "plug", "off")  # the Kia says it went unplugged after this plug-in: not it
    await world.later(10)
    assert world.sent() == [], "the same three minutes' wait"
    await world.later(ASK_AFTER_S)
    question = world.sent()[0]
    assert question["message"] == "Which car is plugged in?"
    assert titles(question) == ["Kia", "Guest car"], "<car> or Guest car, the car first (owner 2026-11-03)"
    assert world.identifier.dashboard()["state"] == "asking"
    assert world.settings.target.vehicle_id == world.cars["Kia"], "nothing switches to a guest by itself"


async def test_two_cars_ask_with_guest_car_and_all_ruled_out_puts_it_first(world: World) -> None:
    await world.start()
    # Both cars report a position away just before the plug-in: fresh, so neither is here.
    world.car_says("Kia", "location", "not_home")
    world.car_says("Tesla", "location", "not_home")
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    assert titles(world.sent()[0]) == ["Guest car", "Kia", "Tesla"]
    block = dashboard_api.capture_guest(world.hass, world.entry.entry_id, world.settings)
    assert block == {"active": False, "amount_kwh": DEFAULT_GUEST_KWH, "first": False}
    await world.later(LISTEN_FOR_S + 60)
    assert world.settings.target.vehicle_id == world.cars["Kia"], "unanswered: the current car is kept"
    assert not is_guest(world.settings)


async def test_three_cars_get_the_likeliest_car_guest_car_and_open_spotnav(world: World) -> None:
    await world.start(cars=3)
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    question = world.sent()[0]
    assert titles(question) == ["Kia", "Guest car", "Open SpotNav"]
    assert question["data"]["actions"][2]["action"] == "URI"


async def test_always_ask_always_offers_guest_car_and_off_asks_nothing(world: World) -> None:
    await world.start(mode="ask")
    await world.plug_in()
    assert "Guest car" in titles(world.sent()[0])
    await world.unplug()
    await world.later(UNPLUG_DEBOUNCE_S + 1)
    store = domain_data(world.hass).auto_store
    await store.async_update(world.entry.entry_id, mutate=lambda settings: replace(settings, identify_mode="off"))
    sent = len(world.sent())
    await world.plug_in()
    await world.later(ASK_AFTER_S + 60)
    assert len(world.sent()) == sent, "off: no question"
    assert world.identifier.answer_refusal(GUEST_VEHICLE_ID) is None, "the card's and the app's choice still offer it"


async def test_a_swiped_question_is_no_guest(world: World, hass: HomeAssistant) -> None:
    await world.start(cars=1)
    await world.plug_in()
    await world.later(40)
    world.car_says("Kia", "plug", "on")
    world.car_says("Kia", "plug", "off")
    await world.later(ASK_AFTER_S)
    hass.bus.async_fire("mobile_app_notification_cleared", {"tag": world.sent()[0]["data"]["tag"]})
    await hass.async_block_till_done()
    assert not is_guest(world.settings) and world.settings.target.vehicle_id == world.cars["Kia"]


async def test_the_guest_answer_plans_by_kwh_with_no_vehicle_and_the_unplug_brings_the_car_back(
    world: World, hass: HomeAssistant
) -> None:
    await world.start()
    before = world.settings
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    question = world.sent()[0]
    await world.tap(question, titles(question).index("Guest car"))
    await hass.async_block_till_done()
    settings = world.settings
    assert is_guest(settings)
    assert (settings.driver, settings.requested_kwh, settings.fill_to_limit) == (DRIVER_MANUAL_KWH, 10.0, False)
    assert world.sent()[-1]["message"] == "Guest car selected."
    assert world.identifier.method == METHOD_ANSWERED
    entry = world.identifier.diagnostics()["history"][-1]
    assert entry["vehicle_id"] == GUEST_VEHICLE_ID and entry["answered_by"] == ANSWERED_BY_NOTIFICATION
    # Nothing of a car: no vehicle resolves, no level is read, the floor and the wake have no car.
    assert resolve_target_vehicle(hass, settings.target.vehicle_id, settings.vehicle_ids)[0] is None
    assert vehicle_properties.resolved_vehicle_id(hass, world.entry.entry_id) is None
    reader = charger_data(hass, world.entry.entry_id).soc_reader
    assert live_vehicle_facts(hass, reader, GUEST_VEHICLE_ID, None) is None
    assert not world.identifier.claims(GUEST_VEHICLE_ID)
    from custom_components.spotnav.runtime import preview_for

    assert preview_for(hass, world.entry.entry_id)._learned_power(settings).kw is None, "nothing learned is used"
    await vehicle_properties.async_update_vehicle_properties(
        hass, domain_data(hass).decision_store, world.cars["Kia"], {vehicle_properties.KEY_MIN: 50}
    )
    assert dashboard_api._min_soc_percent(hass, world.entry.entry_id) is None, "no car's minimum level"
    controller = charger_data(hass, world.entry.entry_id).controller
    facts = session_facts(hass, controller)
    assert (facts.vehicle_id, facts.vehicle_name) == (GUEST_VEHICLE_ID, "Guest car"), "the charge history's car"

    await world.unplug()
    await world.later(UNPLUG_DEBOUNCE_S - 30)
    assert is_guest(world.settings), "a flapping connector is the same plug-in"
    await world.later(60)
    assert world.settings.target == before.target
    assert (world.settings.driver, world.settings.requested_kwh) == (before.driver, before.requested_kwh)


async def test_a_household_car_chosen_in_the_card_ends_the_guest_at_once(world: World, hass: HomeAssistant) -> None:
    await world.start()
    before = world.settings
    await world.plug_in()
    assert await world.identifier.async_answer(GUEST_VEHICLE_ID)
    await hass.async_block_till_done()
    assert is_guest(world.settings)
    assert world.identifier.diagnostics()["history"][-1]["answered_by"] == ANSWERED_BY_CHOICE
    assert await world.identifier.async_answer(world.cars["Tesla"])
    await hass.async_block_till_done()
    settings = world.settings
    assert settings.target == TargetSocIntent(vehicle_id=world.cars["Tesla"], target_percent=60.0)
    assert (settings.driver, settings.requested_kwh) == (before.driver, before.requested_kwh)


async def test_a_household_car_chosen_in_the_settings_ends_the_guest_with_that_car(
    world: World, hass: HomeAssistant
) -> None:
    await world.start()
    before = world.settings
    await world.plug_in()
    assert await world.identifier.async_answer(GUEST_VEHICLE_ID)
    await hass.async_block_till_done()
    store = domain_data(hass).auto_store
    tesla = world.cars["Tesla"]
    await store.async_update(
        world.entry.entry_id, mutate=lambda s: replace(s, target=TargetSocIntent(vehicle_id=tesla, target_percent=70.0))
    )
    await hass.async_block_till_done()
    settings = world.settings
    assert settings.target == TargetSocIntent(vehicle_id=tesla, target_percent=70.0)
    assert settings.driver == before.driver


async def test_a_restart_while_the_guest_is_plugged_in_keeps_it_and_the_unplug_after_brings_the_car_back(
    world: World, hass: HomeAssistant
) -> None:
    await world.start()
    before = world.settings
    await world.plug_in()
    assert await world.identifier.async_answer(GUEST_VEHICLE_ID)
    await hass.async_block_till_done()
    await world.later(5)

    assert await hass.config_entries.async_reload(world.entry.entry_id)
    await hass.async_block_till_done()
    controller = charger_data(hass, world.entry.entry_id).controller
    controller.adapter.vehicle_connected = lambda: world.connected  # type: ignore[method-assign]
    await world.observe()
    assert is_guest(world.settings), "still plugged in: still the guest"
    assert world.identifier.guest and world.identifier.method == METHOD_ANSWERED

    await world.unplug()
    await world.later(UNPLUG_DEBOUNCE_S + 1)
    assert not is_guest(world.settings)
    assert world.settings.target == before.target and world.settings.driver == before.driver


async def test_the_guest_amount_is_remembered_from_the_plan_dialog_and_the_guest_tab(
    world: World, hass: HomeAssistant, hass_ws_client: Any, hass_admin_user: Any
) -> None:
    from pytest_homeassistant_custom_component.common import CLIENT_ID

    await world.start()
    await world.plug_in()
    assert await world.identifier.async_answer(GUEST_VEHICLE_ID)
    await hass.async_block_till_done()
    store = domain_data(hass).auto_store
    await store.async_update(world.entry.entry_id, mutate=lambda s: replace(s, requested_kwh=14.0))
    await hass.async_block_till_done()
    assert guest_amount_kwh(hass) == 14.0, "a change during a guest plan is the next guest's"

    # Signed in now: a token issued before the clock moved on has expired.
    refresh = await hass.auth.async_create_refresh_token(hass_admin_user, CLIENT_ID)
    client = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh))
    answer = await ws_call(
        client, {"type": "spotnav/update_guest", "api_version": 1, "charger_id": world.entry.entry_id, "amount_kwh": 18}
    )
    assert answer["result"]["ok"] and answer["result"]["guest"] == {"active": True, "amount_kwh": 18.0, "first": False}
    refused = await ws_call(
        client, {"type": "spotnav/update_guest", "api_version": 1, "charger_id": world.entry.entry_id, "amount_kwh": 0}
    )
    assert refused["result"] == {"api_version": 1, "ok": False, "error": "spotnav_invalid_value"}
    assert world.settings.requested_kwh == 14.0, "the guest plugged in now keeps its own plan"

    await world.unplug()
    await world.later(UNPLUG_DEBOUNCE_S + 1)
    await world.plug_in()
    assert await world.identifier.async_answer(GUEST_VEHICLE_ID)
    await hass.async_block_till_done()
    assert world.settings.requested_kwh == 18.0, "the last change wins"


async def test_a_settings_write_never_names_a_guest_and_one_that_names_no_vehicle_keeps_it(world: World) -> None:
    from custom_components.spotnav.api.settings import replacement_mutator

    await world.start()
    current = world.settings
    with pytest.raises(AutoSettingsError):
        replacement_mutator(with_guest(current, 10.0))(current)
    guest = with_guest(current, 10.0)
    kept = replacement_mutator(replace(guest, target=TargetSocIntent(), requested_kwh=12.0))(guest)
    assert is_guest(kept) and kept.requested_kwh == 12.0, "an app that knows no guest writes no vehicle"


# --------------------------------------------------------------------------------- the camera


class _Camera:
    """A camera whose model answers `answer` at `confidence`, with a reference picture of every car."""

    def __init__(self, answer: str | None, confidence: str = "high") -> None:
        self.answer, self.confidence = answer, confidence

    def settings(self) -> Any:
        return SimpleNamespace(camera_entity_id="camera.spot")

    def ai_task(self) -> tuple[str | None, str | None]:
        return "ai_task.local", "model"

    def ready_for(self, vehicles: Any) -> bool:
        return True

    def reference_signatures(self, vehicles: Any) -> dict[str, list[Any]]:
        return {car: [None] for car in vehicles}

    async def async_ask(self, candidates: Any) -> CameraAnswer:
        return CameraAnswer(self.answer, self.confidence, None)

    def diagnostics(self) -> dict[str, Any]:
        return {}


async def test_the_camera_that_sees_none_of_the_cars_makes_a_one_car_charger_ask_and_never_decides(
    world: World, hass: HomeAssistant
) -> None:
    await world.start(cars=1)
    world.identifier._camera = _Camera(ANSWER_OTHER)
    await world.plug_in()
    await hass.async_block_till_done()
    await world.later(ASK_AFTER_S + 5)
    question = world.sent()[0]
    assert titles(question) == ["Kia", "Guest car"]
    assert not is_guest(world.settings), "a person confirms a guest"
    camera = world.identifier.diagnostics()["history"][-1]["camera"]
    assert camera["answer"] == ANSWER_OTHER and camera["camera_reason"] == "none_of_these" and camera["used"]


async def test_the_camera_that_sees_none_of_two_cars_puts_guest_car_first(world: World, hass: HomeAssistant) -> None:
    await world.start()
    world.identifier._camera = _Camera(ANSWER_OTHER, "medium")
    await world.plug_in()
    await hass.async_block_till_done()
    await world.later(ASK_AFTER_S + 5)
    assert titles(world.sent()[0]) == ["Guest car", "Kia", "Tesla"]
    assert world.settings.target.vehicle_id == world.cars["Kia"]


async def test_a_one_car_camera_that_names_the_car_asks_nothing(world: World, hass: HomeAssistant) -> None:
    await world.start(cars=1)
    world.identifier._camera = _Camera("unused")
    world.identifier._camera.answer = world.cars["Kia"]
    await world.plug_in()
    await hass.async_block_till_done()
    await world.later(ASK_AFTER_S + 5)
    assert world.sent() == []


# --------------------------------------------------------------------------------- the dashboard and the webhook


async def test_the_webhook_carries_the_guest_only_for_an_app_that_reads_it(world: World, hass: HomeAssistant) -> None:
    await world.start(cars=1)
    await world.plug_in()
    await world.later(40)
    world.car_says("Kia", "plug", "on")
    world.car_says("Kia", "plug", "off")
    await world.later(ASK_AFTER_S)
    body = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, world.entry), can_act=True)
    assert body["guest"] == {"active": False, "amount_kwh": 10.0, "first": False}
    assert body["identification"]["state"] == "asking" and len(body["identification"]["candidates"]) == 1
    old = _for_app(body, {})
    assert "guest" not in old and old["identification"] is None, "an older app never meets a one-car question"
    new = _for_app(body, {"reads": ["guest"]})
    assert new["guest"] == body["guest"] and new["identification"] == body["identification"]

    assert await world.identifier.async_answer(GUEST_VEHICLE_ID)
    await hass.async_block_till_done()
    body = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, world.entry), can_act=True)
    assert body["guest"]["active"] is True
    assert body["settings"]["target"]["vehicle_id"] == GUEST_VEHICLE_ID
    old = _for_app(body, {})
    assert old["settings"]["target"]["vehicle_id"] is None, "no car an older app does not know"
    assert not [line for line in old["status"]["lines"] if line["code"].startswith("guest_")]
    new = _for_app(body, {"reads": ["guest"]})
    assert new["settings"]["target"]["vehicle_id"] == GUEST_VEHICLE_ID


# --------------------------------------------------------------------------------- the evening, end to end


async def test_a_guest_evening_end_to_end_and_its_core_events_replay(hass: HomeAssistant, transport: Any) -> None:
    """Plug-in, the question, the answer "Guest car", its plan, 10 kWh delivered, held until the unplug (a charge the
    charger begins by itself is stopped, a person's Start still charges), the unplug, and the household car back."""
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    phone = async_mock_service(hass, "notify", "mobile_app_pixel")
    with freeze_time(NOW) as frozen:
        charger, ev6, _ = await charger_and_car(hass, amps=16, phases=3)
        store = domain_data(hass).auto_store
        await store.async_update(
            charger.entry_id,
            mutate=lambda s: replace(s, notifications=NotificationSettings(targets=("mobile_app_pixel",))),
        )
        plug = vehicle_entity(
            hass, device_id=ev6, domain="binary_sensor", object_id="ev6_plugged_in", state="on",
            attributes={"device_class": "plug"},
        )
        car = Car(hass, frozen, charger)
        before = store.settings(charger.entry_id)
        assert before.driver == DRIVER_TARGET_SOC
        await car.plug.set(False)
        # Long enough after the car was chosen in the settings that the plug-in is not answered by that choice.
        await car.settle(RECENT_CHOICE_S + 60)

        # Plugged in: the EV6, the charger's one car, says it is not plugged in. A guest, perhaps: ask.
        await car.plug.set(True)
        await car.settle(40)
        hass.states.async_set(plug, "off", {"device_class": "plug"})
        await car.settle(ASK_AFTER_S)
        question = phone[-1].data
        assert titles(question) == ["EV6", "Guest car"]
        hass.bus.async_fire("mobile_app_notification_action", {"action": question["data"]["actions"][0]["action"]})
        await car.settle()
        await car.preview.async_recalculate()
        await car.settle()
        settings = store.settings(charger.entry_id)
        assert is_guest(settings) and settings.requested_kwh == 10.0

        # Its plan: the window open now charges it.
        assert car.controller.charging or calls[-1][0] == "turn_on"
        payload = car.dashboard()
        assert payload["guest"]["active"] is True
        assert payload["status"]["lines"][0] == {"code": "guest_planned", "params": {"kwh": 10.0, "departure": None}}
        start = car.baseline().register_kwh
        assert start is not None

        # Delivered: the charge stops, and the charger is held until the unplug.
        offs = len([call for call in calls if call[0] == "turn_off"])
        frozen.tick(timedelta(minutes=55))
        _meter(hass, start + 10.2)
        await car.settle()
        assert len([call for call in calls if call[0] == "turn_off"]) == offs + 1
        assert car.controller.guest_met
        assert car.controller.plan is None
        payload = car.dashboard()
        assert payload["status"]["lines"][0] == {"code": "guest_done", "params": {}}

        # The charger begins by itself: stopped again (the guest must not charge on at any price).
        offs += 1
        hass.states.async_set("switch.wallbox", "on", {"plug": True, "stamp": 9000})
        await car.settle()
        assert len([call for call in calls if call[0] == "turn_off"]) == offs + 1
        hass.states.async_set("switch.wallbox", "off", {"plug": True, "stamp": 9001})
        await car.settle()

        # A person's Start still charges.
        ons = len([call for call in calls if call[0] == "turn_on"])
        await car.executor.async_manual_start()
        await car.settle()
        assert len([call for call in calls if call[0] == "turn_on"]) == ons + 1

        # The unplug: the household car and its settings come back.
        await car.unplug()
        await car.settle(UNPLUG_DEBOUNCE_S + 1)
        settings = store.settings(charger.entry_id)
        assert not is_guest(settings)
        assert settings.target == before.target and settings.driver == DRIVER_TARGET_SOC
        assert not car.controller.guest_met
        assert car.dashboard()["guest"]["active"] is False

        # The core's own record of the evening replays as it was decided.
        diagnostics = car.controller.session_runner.diagnostics()
        assert "guest_met" in [record["event"]["kind"] for record in diagnostics["events"]]
        bundle = json.loads(json.dumps(_bundle(diagnostics["events"]), default=str))
        report = next(iter(replay_bundle(bundle).values()))
        assert report.mismatches == ()
