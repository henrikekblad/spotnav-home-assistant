"""Which car was plugged in: evidence from the cars, then a question, never a car woken.

The judgement is pure (`judge`, `decide`, `ordered`, `buttons`); the runtime (`VehicleIdentifier`) runs on
a real charger entry: the plug-in edge, the 3-minute ask, the 30-minute listen, at most one automatic switch,
the person's answer that always wins, the question on the phones and its retirement, the card's and the
app's answer, and the session record of how the vehicle was decided. Identification only ever writes the
settings' target vehicle; it never starts, stops or owns a charge.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.notifications.settings import NotificationSettings
from custom_components.spotnav.planning.auto_settings import TargetSocIntent
from custom_components.spotnav.runtime import charger_data, domain_data
from custom_components.spotnav.vehicles.identification import (
    ASK_AFTER_S,
    buttons,
    Candidate,
    decide,
    Evidence,
    judge,
    LISTEN_FOR_S,
    METHOD_ANSWERED,
    METHOD_ASSUMED,
    METHOD_LOCATION,
    METHOD_MANUAL,
    METHOD_ONLY_CANDIDATE,
    METHOD_PLUG_SENSOR,
    ordered,
)

from .world import add_car, setup_charger, vehicle_entity, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

T0 = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)


def state(entity_id: str, value: str, *, changed: datetime, reported: datetime | None = None) -> State:
    return State(
        entity_id, value, last_changed=changed, last_updated=changed, last_reported=reported or changed
    )


# --------------------------------------------------------------------------------- the judgement


def test_a_plug_that_went_on_around_the_plug_in_is_strong_and_one_on_long_before_is_weak() -> None:
    fresh = Candidate("a", plug=state("binary_sensor.a", "on", changed=T0 + timedelta(seconds=40)))
    assert judge(fresh, T0, T0 + timedelta(minutes=1)) == Evidence("a", positive="strong", negative=None)
    early = Candidate("a", plug=state("binary_sensor.a", "on", changed=T0 - timedelta(minutes=4)))
    assert judge(early, T0, T0 + timedelta(minutes=1)).positive == "strong", "within five minutes before"
    old = Candidate("a", plug=state("binary_sensor.a", "on", changed=T0 - timedelta(hours=2)))
    assert judge(old, T0, T0 + timedelta(minutes=1)) == Evidence("a", positive="weak", negative=None)


def test_unplugged_is_strong_only_when_the_car_said_so_after_the_plug_in() -> None:
    reported = Candidate(
        "b", plug=state("binary_sensor.b", "off", changed=T0 - timedelta(hours=5), reported=T0 + timedelta(minutes=2))
    )
    assert judge(reported, T0, T0 + timedelta(minutes=2)) == Evidence("b", None, METHOD_PLUG_SENSOR)
    stale = Candidate("b", plug=state("binary_sensor.b", "off", changed=T0 - timedelta(hours=5)))
    assert judge(stale, T0, T0 + timedelta(minutes=20)) == Evidence("b", None, None), "an old report says nothing"
    too_soon = Candidate(
        "b", plug=state("binary_sensor.b", "off", changed=T0 - timedelta(hours=5), reported=T0 + timedelta(seconds=30))
    )
    assert judge(too_soon, T0, T0 + timedelta(minutes=1)).negative is None


def test_a_streaming_car_that_stays_unplugged_past_its_report_time_is_not_here() -> None:
    quiet = Candidate(
        "b", plug=state("binary_sensor.b", "off", changed=T0 - timedelta(hours=5)), plug_platform="teslemetry"
    )
    assert judge(quiet, T0, T0 + timedelta(seconds=30)).negative is None
    assert judge(quiet, T0, T0 + timedelta(minutes=3)).negative == METHOD_PLUG_SENSOR
    polled = replace(quiet, plug_platform="kia_uvo")
    assert judge(polled, T0, T0 + timedelta(minutes=29)).negative is None, "a cloud poll may simply be late"


def test_a_fresh_position_away_from_home_excludes_and_an_old_one_does_not() -> None:
    away = Candidate("b", location=state("device_tracker.b", "not_home", changed=T0 - timedelta(minutes=50)))
    assert judge(away, T0, T0 + timedelta(minutes=1)) == Evidence("b", None, METHOD_LOCATION)
    old = Candidate("b", location=state("device_tracker.b", "Work", changed=T0 - timedelta(hours=3)))
    assert judge(old, T0, T0 + timedelta(minutes=1)).negative is None
    home = Candidate("b", location=state("device_tracker.b", "home", changed=T0 - timedelta(hours=9)))
    assert judge(home, T0, T0 + timedelta(minutes=1)) == Evidence("b", positive="weak", negative=None)


def test_a_car_identified_at_another_charger_is_not_here() -> None:
    assert judge(Candidate("b", elsewhere=True), T0, T0).negative == METHOD_LOCATION


def test_one_strong_car_is_chosen_two_are_a_conflict_and_elimination_names_its_reason() -> None:
    a, b, c = (Evidence(v, None, None) for v in "abc")
    assert decide([replace(a, positive="strong"), b]) == ("a", METHOD_PLUG_SENSOR, False)
    assert decide([replace(a, positive="strong"), replace(b, positive="strong")]) == (None, None, True)
    assert decide([a, replace(b, negative=METHOD_LOCATION)]) == ("a", METHOD_LOCATION, False)
    assert decide([a, replace(b, negative=METHOD_PLUG_SENSOR), c]) == (None, None, False)
    assert decide([a, replace(b, negative=METHOD_PLUG_SENSOR), replace(c, negative=METHOD_LOCATION)]) == (
        "a", METHOD_LOCATION, False
    )
    assert decide([replace(a, negative=METHOD_LOCATION), replace(b, negative=METHOD_LOCATION)]) == (None, None, False)
    assert decide([replace(a, positive="strong", negative=METHOD_LOCATION), b]) == ("b", METHOD_LOCATION, False)


def test_the_choices_are_ordered_by_evidence_then_the_current_car() -> None:
    evidence = [
        Evidence("a", None, METHOD_LOCATION), Evidence("b", None, None), Evidence("c", "weak", None),
        Evidence("d", "strong", None), Evidence("e", None, None),
    ]
    assert ordered(evidence, current="e") == ["d", "c", "e", "b", "a"]


def test_android_shows_three_buttons_at_most() -> None:
    assert buttons(["a", "b"]) == (["a", "b"], False)
    assert buttons(["a", "b", "c"]) == (["a", "b", "c"], False)
    assert buttons(["a", "b", "c", "d"]) == (["a", "b"], True), "the two likeliest and Open SpotNav"


# --------------------------------------------------------------------------------- the runtime


class World:
    """One charger that reports its plug, two cars with plug sensors and trackers, and one phone."""

    def __init__(self, hass: HomeAssistant, freezer: Any) -> None:
        self.hass = hass
        self.freezer = freezer
        self.connected: bool | None = False
        self.stamp = 0
        self.t0 = dt_util.utcnow().replace(microsecond=0) + timedelta(days=1)

    async def start(self, *, mode: str = "automatic", cars: int = 2, phones: tuple[str, ...] = ("mobile_app_pixel",)):
        hass = self.hass
        self.freezer.move_to(self.t0 - timedelta(hours=1))
        self.entry = await setup_charger(hass, title="Garage")
        self.switch_on = async_mock_service(hass, "switch", "turn_on")
        self.switch_off = async_mock_service(hass, "switch", "turn_off")
        self.refreshes = async_mock_service(hass, "homeassistant", "update_entity")
        self.calls = {phone: async_mock_service(hass, "notify", phone) for phone in phones}
        self.cars: dict[str, str] = {}
        self.plugs: dict[str, str] = {}
        self.trackers: dict[str, str] = {}
        for name in ("Kia", "Tesla", "Volvo", "Zoe")[:cars]:
            vehicle = add_car(hass, name)
            self.cars[name] = vehicle
            self.plugs[name] = vehicle_entity(
                hass, device_id=vehicle, domain="binary_sensor", object_id=f"{name.lower()}_plugged_in",
                state="off", attributes={"device_class": "plug"},
            )
            self.trackers[name] = vehicle_entity(
                hass, device_id=vehicle, domain="device_tracker", object_id=f"{name.lower()}_location",
                state="home",
            )
        controller = charger_data(hass, self.entry.entry_id).controller
        controller.adapter.vehicle_connected = lambda: self.connected  # type: ignore[method-assign]
        store = domain_data(hass).auto_store
        kia = self.cars["Kia"]
        await store.async_update(
            self.entry.entry_id,
            mutate=lambda settings: replace(
                settings,
                notifications=NotificationSettings(targets=phones),
                identify_mode=mode,
                target=TargetSocIntent(vehicle_id=kia, target_percent=80.0),
                vehicle_targets=((self.cars["Tesla"], 60.0),),
            ),
        )
        await self.observe()
        self.freezer.move_to(self.t0 - timedelta(seconds=1))
        return self

    async def observe(self) -> None:
        """The charger reports again (its charge control changes an attribute): the controller's listeners run."""
        self.stamp += 1
        current = self.hass.states.get("switch.charger_a")
        self.hass.states.async_set(
            "switch.charger_a", current.state if current else "off", {"stamp": self.stamp}
        )
        await self.hass.async_block_till_done()

    async def plug_in(self) -> None:
        self.freezer.move_to(self.t0)
        self.connected = True
        await self.observe()

    async def unplug(self) -> None:
        self.connected = False
        await self.observe()

    async def later(self, seconds: float) -> None:
        self.freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(self.hass, dt_util.utcnow())
        await self.hass.async_block_till_done()

    def car_says(self, name: str, entity: str, value: str) -> None:
        self.hass.states.async_set(
            (self.plugs if entity == "plug" else self.trackers)[name], value,
            {"device_class": "plug"} if entity == "plug" else {},
        )

    def car_reports_again(self, name: str) -> None:
        """A poll that writes the same value: only `last_reported` moves."""
        current = self.hass.states.get(self.plugs[name])
        self.hass.states.async_set(self.plugs[name], current.state, current.attributes, force_update=False)

    @property
    def settings(self) -> Any:
        return domain_data(self.hass).auto_store.settings(self.entry.entry_id)

    @property
    def identifier(self) -> Any:
        return charger_data(self.hass, self.entry.entry_id).identifier

    def sent(self, phone: str = "mobile_app_pixel") -> list[dict[str, Any]]:
        return [call.data for call in self.calls[phone]]

    async def tap(self, payload: dict[str, Any], index: int) -> None:
        action = payload["data"]["actions"][index]["action"]
        self.hass.bus.async_fire("mobile_app_notification_action", {"action": action})
        await self.hass.async_block_till_done()


@pytest.fixture
async def world(hass: HomeAssistant, freezer: Any) -> World:
    return World(hass, freezer)


async def test_the_cars_own_plug_sensor_picks_it_and_its_own_target_without_asking(world: World) -> None:
    await world.start()
    await world.plug_in()
    await world.later(40)
    world.car_says("Tesla", "plug", "on")
    await world.hass.async_block_till_done()
    assert world.settings.target == TargetSocIntent(vehicle_id=world.cars["Tesla"], target_percent=60.0)
    assert world.identifier.method == METHOD_PLUG_SENSOR
    await world.later(ASK_AFTER_S + 60)
    assert world.sent() == [], "nothing to ask"
    assert not world.switch_on and not world.switch_off, "identification never starts or stops a charge"


async def test_with_nothing_to_go_on_it_asks_after_three_minutes_and_the_first_answer_wins(
    world: World, hass: HomeAssistant
) -> None:
    await world.start(phones=("mobile_app_pixel", "mobile_app_iphone"))
    await world.plug_in()
    assert world.refreshes, "the cars' own entities are re-read, never a brand's wake-up"
    assert all(call.domain == "homeassistant" for call in world.refreshes)
    await world.later(ASK_AFTER_S - 10)
    assert world.sent() == []
    await world.later(20)
    question = world.sent()[0]
    assert world.sent("mobile_app_iphone")[0] == question
    assert question["message"] == "Which car is plugged in?", "no plate, place or person in the text"
    assert question["data"]["tag"] == f"spotnav_{world.entry.entry_id}_identify"
    actions = question["data"]["actions"]
    assert [action["title"] for action in actions] == ["Kia", "Tesla"], "the current car first"
    nonce = actions[0]["action"].removeprefix("SPOTNAV_ID_").rsplit("_", 1)[0]
    assert len(nonce) >= 24, "at least 96 bits"
    assert world.identifier.dashboard()["state"] == "asking"

    hass.bus.async_fire("mobile_app_notification_action", {"action": f"SPOTNAV_ID_{'0' * 32}_1"})
    await hass.async_block_till_done()
    assert world.settings.target.vehicle_id == world.cars["Kia"], "a guessed nonce does nothing"

    await world.tap(question, 1)
    assert world.settings.target == TargetSocIntent(vehicle_id=world.cars["Tesla"], target_percent=60.0)
    assert world.identifier.method == METHOD_ANSWERED
    for phone in ("mobile_app_pixel", "mobile_app_iphone"):
        retired = world.sent(phone)[-1]
        assert retired["message"] == "Tesla chosen."
        assert retired["data"]["tag"] == question["data"]["tag"] and "actions" not in retired["data"]

    await world.tap(question, 0)
    assert world.settings.target.vehicle_id == world.cars["Tesla"], "only the first answer counts"
    world.car_says("Kia", "plug", "on")
    await world.later(60)
    assert world.settings.target.vehicle_id == world.cars["Tesla"], "a person's answer always wins"


async def test_decisive_evidence_after_the_question_switches_once_and_retires_it(world: World) -> None:
    await world.start(cars=3)
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    question = world.sent()[0]
    assert len(question["data"]["actions"]) == 3
    world.car_says("Tesla", "plug", "on")
    await world.later(5)
    assert world.settings.target.vehicle_id == world.cars["Tesla"]
    assert world.identifier.method == METHOD_PLUG_SENSOR
    assert world.sent()[-1]["message"] == "Recognised as Tesla."
    world.car_says("Tesla", "plug", "off")
    world.car_says("Volvo", "plug", "on")
    await world.later(60)
    assert world.settings.target.vehicle_id == world.cars["Tesla"], "at most one automatic switch"


async def test_a_car_away_from_home_leaves_the_other(world: World) -> None:
    await world.start()
    world.car_says("Kia", "location", "not_home")
    await world.plug_in()
    await world.hass.async_block_till_done()
    assert world.settings.target.vehicle_id == world.cars["Tesla"]
    assert world.identifier.method == METHOD_LOCATION


async def test_listening_ends_after_thirty_minutes_and_the_current_car_is_kept(world: World) -> None:
    await world.start()
    await world.plug_in()
    await world.later(LISTEN_FOR_S + 60)
    world.car_says("Tesla", "plug", "on")
    await world.later(60)
    assert world.settings.target.vehicle_id == world.cars["Kia"]
    assert world.identifier.method == METHOD_ASSUMED
    assert world.identifier.dashboard()["state"] == "asking", "the question can still be answered"


async def test_always_ask_asks_at_once_orders_by_evidence_and_never_switches(world: World) -> None:
    await world.start(mode="ask")
    world.car_says("Tesla", "plug", "on")
    await world.plug_in()
    question = world.sent()[0]
    assert [action["title"] for action in question["data"]["actions"]] == ["Tesla", "Kia"]
    await world.later(LISTEN_FOR_S)
    assert world.settings.target.vehicle_id == world.cars["Kia"]
    assert world.identifier.dashboard()["candidates"][0] == {
        "vehicle_id": world.cars["Tesla"], "name": "Tesla", "likely": True
    }


async def test_off_and_a_single_vehicle_identify_nothing(world: World, hass: HomeAssistant) -> None:
    await world.start(mode="off")
    await world.plug_in()
    await world.later(ASK_AFTER_S + 60)
    assert world.sent() == [] and world.identifier.dashboard() is None
    assert world.identifier.method == METHOD_MANUAL
    await world.unplug()
    store = domain_data(hass).auto_store
    await store.async_update(
        world.entry.entry_id,
        mutate=lambda settings: replace(settings, identify_mode="automatic", vehicle_ids=(world.cars["Kia"],)),
    )
    await world.plug_in()
    await world.later(ASK_AFTER_S + 60)
    assert world.sent() == [] and world.identifier.method == METHOD_ONLY_CANDIDATE


async def test_a_dismissed_question_keeps_the_current_car(world: World, hass: HomeAssistant) -> None:
    await world.start(phones=("mobile_app_pixel", "mobile_app_iphone"))
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    tag = world.sent()[0]["data"]["tag"]
    hass.bus.async_fire("mobile_app_notification_cleared", {"tag": tag})
    await hass.async_block_till_done()
    assert world.settings.target.vehicle_id == world.cars["Kia"]
    assert world.identifier.method == METHOD_ASSUMED
    assert world.identifier.dashboard()["state"] == "decided"
    assert world.sent("mobile_app_iphone")[-1]["message"] == "Kia kept."


async def test_unplugging_before_an_answer_clears_the_question(world: World) -> None:
    await world.start()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    tag = world.sent()[0]["data"]["tag"]
    await world.unplug()
    assert world.sent()[-1] == {"message": "clear_notification", "data": {"tag": tag}}
    assert world.identifier.dashboard() is None


async def test_choosing_the_car_in_the_settings_meanwhile_is_the_answer(world: World) -> None:
    await world.start()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    preview = charger_data(world.hass, world.entry.entry_id).preview
    tesla = world.cars["Tesla"]
    await preview.async_apply_settings(
        mutate=lambda settings: replace(settings, target=TargetSocIntent(vehicle_id=tesla, target_percent=60.0))
    )
    await world.hass.async_block_till_done()
    assert world.identifier.method == METHOD_MANUAL
    assert world.sent()[-1]["message"] == "Tesla chosen."
    world.car_says("Kia", "plug", "on")
    await world.later(60)
    assert world.settings.target.vehicle_id == tesla


async def test_the_card_shows_the_question_and_answers_it(
    world: World, hass: HomeAssistant, hass_ws_client, hass_admin_user
) -> None:
    from pytest_homeassistant_custom_component.common import CLIENT_ID

    from custom_components.spotnav.api import dashboard as dashboard_api

    await world.start()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    # Signed in now: a token issued before the clock moved on has expired.
    refresh = await hass.auth.async_create_refresh_token(hass_admin_user, CLIENT_ID)
    socket = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh))
    payload = dashboard_api.serialize_dashboard(
        dashboard_api.capture_dashboard(hass, world.entry), can_act=True
    )
    block = payload["identification"]
    assert block["state"] == "asking" and block["vehicle_id"] == world.cars["Kia"]
    assert [item["name"] for item in block["candidates"]] == ["Kia", "Tesla"]
    refused = await ws_call(
        socket,
        {"type": "spotnav/identify_vehicle", "api_version": 1, "charger_id": world.entry.entry_id,
         "vehicle_id": "not-a-car"},
    )
    assert refused["result"]["ok"] is False and refused["result"]["error"] == "spotnav_invalid_value"
    answer = await ws_call(
        socket,
        {"type": "spotnav/identify_vehicle", "api_version": 1, "charger_id": world.entry.entry_id,
         "vehicle_id": world.cars["Tesla"]},
    )
    assert answer["result"]["ok"] is True
    assert world.settings.target.vehicle_id == world.cars["Tesla"]
    assert world.identifier.method == METHOD_ANSWERED
    assert world.sent()[-1]["message"] == "Tesla chosen."


async def test_the_app_answers_through_the_webhook(world: World, hass: HomeAssistant, hass_client_no_auth) -> None:
    await world.start()
    client = await hass_client_no_auth()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    response = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "identify_vehicle", "vehicle_id": world.cars["Tesla"]},
    )
    assert response.status == 200 and (await response.json())["ok"] is True
    assert world.settings.target.vehicle_id == world.cars["Tesla"]
    late = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "identify_vehicle", "vehicle_id": "nope"},
    )
    assert late.status == 400


async def test_the_session_records_how_the_car_was_decided(world: World, hass: HomeAssistant) -> None:
    from custom_components.spotnav.sessions.inputs import session_facts

    await world.start()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    controller = charger_data(hass, world.entry.entry_id).controller
    assert session_facts(hass, controller).vehicle_decided_by == METHOD_ASSUMED
    await world.tap(world.sent()[0], 1)
    facts = session_facts(hass, controller)
    assert facts.vehicle_id == world.cars["Tesla"] and facts.vehicle_decided_by == METHOD_ANSWERED


async def test_a_person_chooses_a_cars_sources_over_both_transports(
    world: World, hass: HomeAssistant, hass_ws_client, hass_admin_user, hass_client_no_auth
) -> None:
    from pytest_homeassistant_custom_component.common import CLIENT_ID

    await world.start()
    refresh = await hass.auth.async_create_refresh_token(hass_admin_user, CLIENT_ID)
    socket = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh))
    kia = world.cars["Kia"]
    answer = await ws_call(
        socket,
        {"type": "spotnav/choose_vehicle_identification", "api_version": 1, "charger_id": world.entry.entry_id,
         "vehicle_id": kia, "source": "location", "entity_id": "none"},
    )
    result = answer["result"]
    assert result["ok"] is True
    assert result["identification"]["location"] == {
        "entity_id": None, "name": None, "chosen": True,
        "candidates": [{"entity_id": world.trackers["Kia"], "name": "kia location"}],
    }
    refused = await ws_call(
        socket,
        {"type": "spotnav/choose_vehicle_identification", "api_version": 1, "charger_id": world.entry.entry_id,
         "vehicle_id": kia, "source": "plug", "entity_id": "binary_sensor.someone_elses"},
    )
    assert refused["result"] == {"api_version": 1, "ok": False, "error": "spotnav_invalid_value"}
    client = await hass_client_no_auth()
    response = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "choose_vehicle_identification", "vehicle_id": kia, "source": "location",
              "entity_id": None},
    )
    body = await response.json()
    assert response.status == 200 and body["identification"]["location"]["entity_id"] == world.trackers["Kia"]


async def test_the_diagnostics_say_how_and_when_but_name_nothing(world: World) -> None:
    await world.start()
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    diagnostics = world.identifier.diagnostics()
    assert diagnostics["method"] == METHOD_ASSUMED and diagnostics["state"] == "asking"
    assert diagnostics["candidates"] == 2 and diagnostics["asked_phones"] == 1
    text = repr(diagnostics)
    for name, vehicle in world.cars.items():
        assert name not in text and vehicle not in text


async def test_the_app_is_woken_for_the_question(world: World) -> None:
    await world.start()
    push = charger_data(world.hass, world.entry.entry_id).push
    events: list[str] = []
    push.async_event = lambda event, now: events.append(event)  # type: ignore[method-assign]
    await world.plug_in()
    await world.later(ASK_AFTER_S + 5)
    assert events.count("vehicle_identify") == 1


def test_a_session_keeps_how_its_car_was_decided() -> None:
    from custom_components.spotnav.sessions.model import ChargeSession

    session = ChargeSession(
        id="s", charger_id="c", start=T0, end=T0 + timedelta(hours=1), energy_kwh=5.0, energy_source="register",
        priced_kwh=0.0, cost_minor=None, reference_cost_minor=None, currency=None, major_unit=None,
        minor_unit=None, started_by="plan_window", strategy="cheapest", vehicle_id="a", vehicle_name="Kia",
        solar_kwh=0.0, solar_known_kwh=0.0, last_register_kwh=None, last_sample_at=None,
        vehicle_decided_by=METHOD_ANSWERED,
    )
    stored = session.as_dict()
    assert stored["vehicle_decided_by"] == "answered"
    assert ChargeSession.from_dict(stored).vehicle_decided_by == "answered"
    assert session.public()["vehicle_decided_by"] == "answered"
    plain = replace(session, vehicle_decided_by=None).as_dict()
    assert "vehicle_decided_by" not in plain and ChargeSession.from_dict(plain).vehicle_decided_by is None
    assert ChargeSession.from_dict({**stored, "vehicle_decided_by": "camera"}) is None
