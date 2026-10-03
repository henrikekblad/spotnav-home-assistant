"""Notifications through the Companion app's notify service: the settings, the judgement of a plan that
stopped unexpectedly, the words, and the notifier a real charger gets (notify services mocked)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
    MockConfigEntry,
)

from custom_components.spotnav.api.settings import decode_settings, encode_settings
from custom_components.spotnav.api.webhook import _for_app, APP_UNREAD_SETTINGS
from custom_components.spotnav.execution.controller import ChargingPlan
from custom_components.spotnav.notifications.messages import (
    _TEXT,
    compose,
    language_of,
    LANGUAGES,
    Money,
)
from custom_components.spotnav.notifications.settings import (
    DEFAULT_EVENTS,
    NOTIFICATION_EVENTS,
    NotificationSettings,
    NotificationSettingsError,
)
from custom_components.spotnav.notifications.targets import available_targets
from custom_components.spotnav.notifications.unexpected_stop import (
    ExpectationFacts,
    GRACE_S,
    UnexpectedStopDetector,
)
from custom_components.spotnav.planning.auto_settings import AutoSettings, AutoSettingsError
from custom_components.spotnav.runtime import domain_data

from .world import controller_of, setup_charger

# ------------------------------------------------------------------ settings


def test_the_defaults_are_no_phone_and_the_three_events_that_need_attention() -> None:
    assert NotificationSettings() == NotificationSettings(targets=(), events=DEFAULT_EVENTS, url=None)
    assert DEFAULT_EVENTS == ("plan_stopped", "plan_at_risk", "charge_complete")
    assert not NotificationSettings().wants("plan_stopped"), "nothing is sent without a phone"
    assert NotificationSettings(targets=("mobile_app_a",)).wants("plan_stopped")
    assert not NotificationSettings(targets=("mobile_app_a",)).wants("plugged_in")


def test_a_body_is_read_exactly_and_events_come_back_in_their_own_order() -> None:
    read = NotificationSettings.from_dict(
        {"targets": ["mobile_app_a"], "events": ["unplugged", "plan_stopped"], "available": [{"x": 1}]}
    )
    assert read == NotificationSettings(targets=("mobile_app_a",), events=("plan_stopped", "unplugged"))
    for bad in (
        {"targets": ["mobile_app_a"]},
        {"targets": "mobile_app_a", "events": []},
        {"targets": ["Not A Service"], "events": []},
        {"targets": ["a", "a"], "events": []},
        {"targets": [], "events": ["fire"]},
        {"targets": [], "events": [], "url": "https://elsewhere.example"},
        {"targets": [], "events": [], "url": "//elsewhere.example"},
        {"targets": [], "events": [], "sound": "loud"},
        {"targets": [f"mobile_app_{n}" for n in range(11)], "events": []},
    ):
        with pytest.raises(NotificationSettingsError):
            NotificationSettings.from_dict(bad)


def test_the_stored_record_carries_notifications_only_once_chosen() -> None:
    assert "notifications" not in AutoSettings().as_dict()
    chosen = replace(
        AutoSettings(), notifications=NotificationSettings(targets=("mobile_app_a",), url="/lovelace/ev")
    ).validated()
    stored = chosen.as_dict()
    assert stored["notifications"] == {
        "targets": ["mobile_app_a"],
        "events": list(DEFAULT_EVENTS),
        "url": "/lovelace/ev",
    }
    assert AutoSettings.from_stored(stored).notifications == chosen.notifications
    stored["notifications"] = {"targets": "nope", "events": []}
    with pytest.raises(AutoSettingsError) as refused:
        AutoSettings.from_stored(stored)
    assert refused.value.code == "invalid_notifications"


def _body(**changes: Any) -> dict[str, Any]:
    body = encode_settings(AutoSettings(area_id="SE4", amps=10))
    del body["revision"]
    body.update(changes)
    return body


def test_the_wire_record_lists_the_phones_and_a_body_may_leave_notifications_out() -> None:
    encoded = encode_settings(AutoSettings(), available=(("mobile_app_pixel", "Pixel"),))
    assert encoded["notifications"] == {
        "targets": [],
        "events": list(DEFAULT_EVENTS),
        "url": None,
        "available": [{"service": "mobile_app_pixel", "name": "Pixel"}],
    }
    legacy = _body()
    del legacy["notifications"]
    assert decode_settings(legacy).notifications == NotificationSettings()
    echoed = decode_settings(_body(notifications={**encoded["notifications"], "targets": ["mobile_app_pixel"]}))
    assert echoed.notifications.targets == ("mobile_app_pixel",)
    with pytest.raises(AutoSettingsError) as refused:
        decode_settings(_body(notifications={"targets": [], "events": ["fire"]}))
    assert refused.value.code == "invalid_notifications"


def test_the_paired_app_reads_notifications_only_when_it_asks() -> None:
    assert "notifications" in APP_UNREAD_SETTINGS
    body = {"settings": {"amps": 10, "notifications": {"targets": []}}}
    assert _for_app(body, {}) == {"settings": {"amps": 10}}
    assert _for_app(body, {"reads": ["notifications"]}) == body


# ------------------------------------------------------------------ the unexpected stop

T0 = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)


def at(seconds: float, **facts: Any) -> ExpectationFacts:
    values: dict[str, Any] = {"expected": True, "window": "w1", "charging": False}
    values.update(facts)
    return ExpectationFacts(now=T0 + timedelta(seconds=seconds), **values)


def test_a_window_that_does_not_start_is_told_once_after_the_grace_period() -> None:
    detector = UnexpectedStopDetector()
    assert detector.observe(at(0)) is None
    assert detector.due_at == T0 + timedelta(seconds=GRACE_S)
    assert detector.observe(at(GRACE_S - 1)) is None
    assert detector.observe(at(GRACE_S)) == "not_started"
    assert detector.observe(at(GRACE_S + 600)) is None, "once while it lasts"


def test_a_charge_that_stops_inside_its_window_is_told_and_told_again_after_it_recovers() -> None:
    detector = UnexpectedStopDetector()
    detector.observe(at(0, charging=True))
    assert detector.observe(at(10)) is None
    assert detector.observe(at(10 + GRACE_S)) == "stopped"
    detector.observe(at(400, charging=True))
    detector.observe(at(500))
    assert detector.observe(at(500 + GRACE_S)) == "stopped"


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        ({"available": False}, "charger_unavailable"),
        ({"charger_disabled": True}, "charger_disabled"),
        ({"held_by_charger": True}, "held_by_charger"),
        ({"charging": True, "vehicle_not_requesting": True}, "vehicle_not_requesting"),
    ],
)
def test_the_reason_names_what_is_wrong(facts: dict[str, Any], reason: str) -> None:
    detector = UnexpectedStopDetector()
    detector.observe(at(0, **facts))
    assert detector.observe(at(GRACE_S, **facts)) == reason


def test_nothing_is_told_when_nothing_was_expected_or_a_start_is_still_unanswered() -> None:
    # A window's planned end, a person's Stop, a pause, load balancing's pause and an unplugged car all
    # make the controller's `plan_expects_charge` false.
    detector = UnexpectedStopDetector()
    for second in range(0, 1000, 60):
        assert detector.observe(at(second, expected=False)) is None
    assert detector.due_at is None
    pending = UnexpectedStopDetector()
    for second in range(0, 1000, 60):
        assert pending.observe(at(second, start_pending=True)) is None


def test_a_new_window_starts_afresh() -> None:
    detector = UnexpectedStopDetector()
    detector.observe(at(0))
    assert detector.observe(at(GRACE_S)) == "not_started"
    detector.observe(at(4000, window="w2"))
    assert detector.observe(at(4000 + GRACE_S, window="w2")) == "not_started"


# ------------------------------------------------------------------ the words


def test_every_language_has_every_text() -> None:
    assert set(_TEXT) == set(LANGUAGES) == {"en", "sv", "da", "nb", "fi"}
    for language in LANGUAGES:
        assert set(_TEXT[language]) == set(_TEXT["en"]), language


def test_home_assistants_language_picks_one_of_the_five() -> None:
    assert [language_of(code) for code in ("sv", "sv-SE", "nb", "no", "nn", "da", "fi", "de", None)] == [
        "sv", "sv", "nb", "nb", "nb", "da", "fi", "en", "en",
    ]


def test_messages_are_short_and_carry_the_charger_energy_and_money() -> None:
    title, message = compose(
        "charge_complete",
        "Garage",
        {"reason": "target", "target_percent": 80, "kwh": 12.43, "cost": Money(1830, "SEK")},
        "sv",
    )
    assert title == "SpotNav · Garage"
    assert message == "Laddningen är klar: målet 80 % är nått. 12,4 kWh laddat, 18,30 kr."
    assert compose("plan_stopped", "Garage", {"reason": "stopped"}, "en")[1] == (
        "Charging stopped during a planned window."
    )
    assert compose("plan_installed", "G", {"time": "01:00", "kwh": 20.0, "cost": Money(450, "EUR")}, "en")[1] == (
        "New plan: charging from 01:00. 20.0 kWh planned, about €4.50."
    )
    assert compose("plan_at_risk", "G", {"time": "07:00"}, "sv")[1] == (
        "Laddningen hinner inte bli klar till avresan 07:00."
    )
    for language in LANGUAGES:
        for event in NOTIFICATION_EVENTS:
            assert len(compose(event, "G", {"reason": "not_started", "time": "07:00"}, language)[1]) < 120


# ------------------------------------------------------------------ the notifier on a real charger


async def _choose(hass: HomeAssistant, entry_id: str, **notifications: Any) -> None:
    store = domain_data(hass).auto_store
    chosen = NotificationSettings(**notifications)
    await store.async_update(entry_id, mutate=lambda settings: replace(settings, notifications=chosen))


async def _charger(hass: HomeAssistant, **notifications: Any) -> tuple[Any, list[Any]]:
    entry = await setup_charger(hass, title="Garage")
    # After set-up: the core switch platform would replace a mock registered before it.
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    calls = async_mock_service(hass, "notify", "mobile_app_pixel")
    notifications.setdefault("targets", ("mobile_app_pixel",))
    await _choose(hass, entry.entry_id, **notifications)
    return entry, calls


async def _later(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_a_started_charge_is_sent_once_to_the_chosen_phone_with_a_tag_and_a_url(
    hass: HomeAssistant, freezer: Any
) -> None:
    entry, calls = await _charger(hass, events=("charge_started",), url="/lovelace/ev")
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    assert len(calls) == 1
    data = calls[0].data
    assert data["title"] == "SpotNav · Garage"
    assert data["message"] == "Charging started."
    assert data["data"]["url"] == data["data"]["clickAction"] == "/lovelace/ev"
    assert data["data"]["tag"] == f"spotnav_{entry.entry_id}_charge_started"

    hass.states.async_set("switch.charger_a", "off")
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    assert len(calls) == 1, "not again within fifteen minutes"
    await _later(hass, freezer, 16 * 60)
    hass.states.async_set("switch.charger_a", "off")
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    assert len(calls) == 2


async def test_nothing_is_sent_for_an_event_that_is_off_or_to_a_phone_that_is_gone(hass: HomeAssistant) -> None:
    _entry, calls = await _charger(hass, targets=("mobile_app_pixel", "mobile_app_gone"))
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    assert calls == [], "charge_started is off by default"


def _window_now(amps: int = 10, **changes: Any) -> ChargingPlan:
    now = dt_util.utcnow()
    return ChargingPlan(
        start=(now - timedelta(minutes=5)).isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        amps=amps,
        **changes,
    )


async def test_a_window_whose_charge_does_not_start_is_told_after_the_grace_period(
    hass: HomeAssistant, freezer: Any
) -> None:
    hass.config.language = "sv"
    entry, calls = await _charger(hass)
    controller = controller_of(hass, entry.entry_id)
    await controller.async_install(_window_now())
    await hass.async_block_till_done()
    assert calls == []
    await _later(hass, freezer, GRACE_S + 5)
    assert [call.data["message"] for call in calls] == ["Laddningen startade inte enligt planen."]
    await _later(hass, freezer, 600)
    assert len(calls) == 1


async def test_a_persons_stop_inside_the_window_is_not_a_surprise(hass: HomeAssistant, freezer: Any) -> None:
    entry, calls = await _charger(hass)
    controller = controller_of(hass, entry.entry_id)
    hass.states.async_set("switch.charger_a", "on")
    await controller.async_install(_window_now())
    await hass.async_block_till_done()
    await controller.async_stop(person=True)
    hass.states.async_set("switch.charger_a", "off")
    await hass.async_block_till_done()
    await _later(hass, freezer, GRACE_S + 60)
    assert calls == []


async def test_a_charge_stopped_by_something_else_inside_the_window_is_told(hass: HomeAssistant, freezer: Any) -> None:
    entry, calls = await _charger(hass)
    controller = controller_of(hass, entry.entry_id)
    hass.states.async_set("switch.charger_a", "on")
    await controller.async_install(_window_now())
    await hass.async_block_till_done()
    hass.states.async_set("switch.charger_a", "off")
    await hass.async_block_till_done()
    await _later(hass, freezer, GRACE_S + 5)
    assert [call.data["message"] for call in calls] == ["Charging stopped during a planned window."]


async def test_load_balancing_pausing_the_charge_is_not_a_surprise(hass: HomeAssistant) -> None:
    entry, _calls = await _charger(hass)
    controller = controller_of(hass, entry.entry_id)
    await controller.async_install(_window_now())
    assert controller.plan_expects_charge
    controller._paused_by_balancing = True
    assert not controller.plan_expects_charge


async def test_a_need_met_while_charging_is_a_complete_charge(hass: HomeAssistant) -> None:
    entry, calls = await _charger(hass)
    controller = controller_of(hass, entry.entry_id)
    hass.states.async_set("switch.charger_a", "on")
    await controller.async_install(_window_now())
    await hass.async_block_till_done()
    assert await controller.async_end_plan_need_met()
    await hass.async_block_till_done()
    assert controller.completion_record["reason"] == "energy"
    assert [call.data["message"] for call in calls] == [
        "Charging complete: the requested energy was delivered."
    ]
    assert controller.plan is None


async def test_the_companion_apps_services_are_offered_by_their_phones_names(hass: HomeAssistant) -> None:
    MockConfigEntry(domain="mobile_app", data={"device_name": "Pixel 8 Pro"}).add_to_hass(hass)
    async_mock_service(hass, "notify", "mobile_app_pixel_8_pro")
    async_mock_service(hass, "notify", "mobile_app_ipad")
    async_mock_service(hass, "notify", "persistent_notification")
    assert available_targets(hass) == (
        ("mobile_app_ipad", "ipad"),
        ("mobile_app_pixel_8_pro", "Pixel 8 Pro"),
    )


# ------------------------------------------------------------------ the webhook, for the paired app


async def _post(client: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = await client.post("/api/webhook/webhook-a", json={"version": 1, **payload})
    return response.status, await response.json()


async def test_the_app_keeps_notifications_it_does_not_read_and_edits_them_when_it_asks(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await setup_charger(hass)
    async_mock_service(hass, "notify", "mobile_app_pixel")
    await _choose(hass, entry.entry_id, targets=("mobile_app_pixel",))
    client = await hass_client_no_auth()

    status, dashboard = await _post(client, {"action": "dashboard", "api_version": 1})
    assert status == 200 and "notifications" not in dashboard["settings"]
    status, dashboard = await _post(client, {"action": "dashboard", "api_version": 1, "reads": ["notifications"]})
    assert dashboard["settings"]["notifications"]["targets"] == ["mobile_app_pixel"]
    assert dashboard["settings"]["notifications"]["available"] == [{"service": "mobile_app_pixel", "name": "pixel"}]

    # An app that does not know the field replaces everything else: the phones stay chosen.
    older = {key: value for key, value in dashboard["settings"].items() if key not in ("revision", "notifications")}
    status, answer = await _post(
        client,
        {"action": "settings", "expected_revision": dashboard["settings"]["revision"], "settings": {**older, "amps": 12}},
    )
    assert status == 200 and answer["ok"] is True and "notifications" not in answer["settings"]
    stored = domain_data(hass).auto_store.settings(entry.entry_id)
    assert stored.amps == 12 and stored.notifications.targets == ("mobile_app_pixel",)

    # One that reads it may change it.
    status, answer = await _post(
        client,
        {
            "action": "settings",
            "reads": ["notifications"],
            "expected_revision": stored.revision,
            "settings": {**older, "amps": 12, "notifications": {"targets": [], "events": ["plugged_in"]}},
        },
    )
    assert status == 200 and answer["settings"]["notifications"]["events"] == ["plugged_in"]
    assert domain_data(hass).auto_store.settings(entry.entry_id).notifications == NotificationSettings(
        events=("plugged_in",)
    )
