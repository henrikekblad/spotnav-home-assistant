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
from custom_components.spotnav.notifications.notifier import SETTLE_S
from custom_components.spotnav.notifications.messages import compose, Money, money_text, NAMESPACE
from custom_components.spotnav.texts import language_of, languages, read_files
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
from custom_components.spotnav.runtime import charger_data, domain_data, executor_for

from .world import controller_of, setup_charger

# ------------------------------------------------------------------ settings


def test_the_defaults_are_no_phone_and_the_events_that_need_attention() -> None:
    assert NotificationSettings() == NotificationSettings(targets=(), events=DEFAULT_EVENTS, url=None)
    assert DEFAULT_EVENTS == ("plan_stopped", "plan_at_risk", "charge_complete", "vehicle_identify")
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
        "event_set": 2,
    }
    assert AutoSettings.from_stored(stored).notifications == chosen.notifications
    stored["notifications"] = {"targets": "nope", "events": []}
    with pytest.raises(AutoSettingsError) as refused:
        AutoSettings.from_stored(stored)
    assert refused.value.code == "invalid_notifications"


def test_a_choice_stored_before_the_identification_question_has_it_on_and_a_later_one_keeps_it_off() -> None:
    before = replace(
        AutoSettings(), notifications=NotificationSettings(targets=("mobile_app_a",), events=("plan_stopped",))
    ).validated().as_dict()
    del before["notifications"]["event_set"]
    assert AutoSettings.from_stored(before).notifications.events == ("plan_stopped", "vehicle_identify")
    off = replace(
        AutoSettings(), notifications=NotificationSettings(targets=("mobile_app_a",), events=("plan_stopped",))
    ).validated().as_dict()
    assert AutoSettings.from_stored(off).notifications.events == ("plan_stopped",)


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
    files = read_files()
    assert set(languages()) == {"en", "sv", "da", "nb", "fi", "de", "nl", "fr", "es"}
    for language in languages():
        assert set(files[language][NAMESPACE]) == set(files["en"][NAMESPACE]), language


def test_home_assistants_language_picks_one_of_spotnavs() -> None:
    codes = ("sv", "sv-SE", "nb", "no", "nn", "da", "fi", "de-DE", "nl", "fr-BE", "es", "it", None)
    assert [language_of(code) for code in codes] == [
        "sv", "sv", "nb", "nb", "nb", "da", "fi", "de", "nl", "fr", "es", "en", "en",
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
    assert money_text("de", Money(450, "EUR")) == "4,50 €"
    assert money_text("fr", Money(450, "EUR")) == "4,50 €"
    assert money_text("nl", Money(450, "EUR")) == "€ 4,50"
    assert compose("plan_at_risk", "G", {"time": "07:00"}, "sv")[1] == (
        "Laddningen hinner inte bli klar till avresan 07:00."
    )
    for language in languages():
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
    await executor_for(hass, entry.entry_id).async_manual_stop()
    hass.states.async_set("switch.charger_a", "off")
    await hass.async_block_till_done()
    await _later(hass, freezer, GRACE_S + 60)
    assert calls == []


async def test_a_persons_start_and_its_end_are_no_surprise_either(hass: HomeAssistant, freezer: Any) -> None:
    """A person's Start pauses Auto too: nothing the plan expected is missing while it runs or after the
    person stops it again."""
    entry, calls = await _charger(hass)
    controller = controller_of(hass, entry.entry_id)
    await controller.async_install(_window_now())
    await hass.async_block_till_done()
    executor = executor_for(hass, entry.entry_id)
    await executor.async_manual_start()
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    await _later(hass, freezer, GRACE_S + 60)
    await executor.async_manual_stop()
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


# ------------------------------------------------------------------ a plan a person's own settings write caused


async def _install(hass: HomeAssistant, freezer: Any, controller: Any, plan: ChargingPlan) -> None:
    """Install a plan and let it settle: a new plan is told once no other follows within `SETTLE_S`."""
    await controller.async_install(plan)
    await hass.async_block_till_done()
    await _later(hass, freezer, SETTLE_S + 1)


def _installs(calls: list[Any]) -> list[Any]:
    return [call for call in calls if call.data["message"].startswith("New plan")]


async def _quiet_charger(hass: HomeAssistant, freezer: Any) -> tuple[Any, list[Any], Any]:
    hass.config.language = "en"
    entry, calls = await _charger(
        hass, events=("plan_installed", "plan_at_risk", "plan_stopped")
    )
    await _later(hass, freezer, 120)  # the set-up's own writes are long past
    return entry, calls, controller_of(hass, entry.entry_id)


async def _write_amps(hass: HomeAssistant, entry_id: str, amps: int = 12) -> None:
    store = domain_data(hass).auto_store
    await store.async_update(entry_id, mutate=lambda settings: replace(settings, amps=amps))


async def test_a_plan_after_a_settings_write_is_not_announced(hass: HomeAssistant, freezer: Any) -> None:
    entry, calls, controller = await _quiet_charger(hass, freezer)
    await _write_amps(hass, entry.entry_id)
    await _later(hass, freezer, 5)
    await _install(hass, freezer, controller, _window_now(amps=12))
    assert _installs(calls) == []


async def test_a_plan_from_new_prices_is_announced(hass: HomeAssistant, freezer: Any) -> None:
    entry, calls, controller = await _quiet_charger(hass, freezer)
    await _install(hass, freezer, controller, _window_now(amps=12))
    assert len(_installs(calls)) == 1


async def test_a_write_a_minute_ago_does_not_silence_the_next_plan(hass: HomeAssistant, freezer: Any) -> None:
    entry, calls, controller = await _quiet_charger(hass, freezer)
    await _write_amps(hass, entry.entry_id)
    await _later(hass, freezer, 61)
    await _install(hass, freezer, controller, _window_now(amps=12))
    assert len(_installs(calls)) == 1


async def test_a_problem_right_after_a_write_is_still_told(hass: HomeAssistant, freezer: Any) -> None:
    entry, calls, controller = await _quiet_charger(hass, freezer)
    await _write_amps(hass, entry.entry_id)
    notifier = charger_data(hass, entry.entry_id).notifier
    assert notifier is not None
    notifier._maybe_send("plan_at_risk", {"departure_time": "07:00", "requested_kwh": 40.0}, dt_util.utcnow())
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_an_entity_write_quiets_the_plan_too(hass: HomeAssistant, freezer: Any) -> None:
    from .world import call, entity_id

    entry, calls, controller = await _quiet_charger(hass, freezer)
    # The notification choice alone does not move the entities' displayed revision; a write that does, first.
    await charger_data(hass, entry.entry_id).preview.async_apply_settings(
        mutate=lambda settings: replace(settings, amps=11), expected_revision=None
    )
    await _later(hass, freezer, 120)
    amps = entity_id(hass, entry.entry_id, "charging_current", "number")
    await call(hass, "number", "set_value", {"entity_id": amps, "value": 12})
    await _install(hass, freezer, controller, _window_now(amps=12))
    assert _installs(calls) == []


async def test_a_webhook_write_quiets_the_plan_too(
    hass: HomeAssistant, freezer: Any, hass_client_no_auth
) -> None:
    entry, calls, controller = await _quiet_charger(hass, freezer)
    client = await hass_client_no_auth()
    _, dashboard = await _post(client, {"action": "dashboard", "api_version": 1})
    settings = dashboard["settings"]
    body = {key: value for key, value in settings.items() if key != "revision"}
    status, answer = await _post(
        client, {"action": "settings", "expected_revision": settings["revision"], "settings": {**body, "amps": 12}}
    )
    assert status == 200 and answer["ok"] is True
    await _install(hass, freezer, controller, _window_now(amps=12))
    assert _installs(calls) == []


async def test_a_websocket_write_quiets_the_plan_too(hass: HomeAssistant, freezer: Any, hass_ws_client) -> None:
    from .messages import update_settings_message
    from .world import admin, ws_call

    entry, calls, controller = await _quiet_charger(hass, freezer)
    client = await admin(hass, hass_ws_client)
    current = domain_data(hass).auto_store.settings(entry.entry_id)
    body = {
        key: value
        for key, value in encode_settings(current).items()
        if key not in ("revision", "fiscal_included")
    }
    frame = await ws_call(client, update_settings_message(entry.entry_id, current.revision, {**body, "amps": 12}))
    assert frame["result"]["ok"] is True, frame
    await _install(hass, freezer, controller, _window_now(amps=12))
    assert _installs(calls) == []


# ------------------------------------------------------------------ the same plan is not told again


def _plan_at(hour: int = 1, amps: int = 10, **changes: Any) -> ChargingPlan:
    # A fixed day a while ahead, so two calls minutes apart still describe the same plan.
    base = datetime(2100, 1, 1, tzinfo=timezone.utc) + timedelta(hours=hour)
    start, end = base, base + timedelta(hours=2)
    return ChargingPlan(
        start=start.isoformat(),
        end=end.isoformat(),
        amps=amps,
        energy_kwh=changes.pop("energy_kwh", 12.0),
        periods=[{"start": start.isoformat(), "end": end.isoformat()}],
        **changes,
    )


async def _restart(hass: HomeAssistant, freezer: Any, entry: MockConfigEntry) -> Any:
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _later(hass, freezer, 120)
    return controller_of(hass, entry.entry_id)


async def test_a_restart_with_the_same_plan_is_not_a_new_plan(hass: HomeAssistant, freezer: Any) -> None:
    entry, calls, controller = await _quiet_charger(hass, freezer)
    await _install(hass, freezer, controller, _plan_at())
    assert len(_installs(calls)) == 1
    controller = await _restart(hass, freezer, entry)
    # Calculated again after the restart: another amperage and identity, the same periods and energy.
    await _install(hass, freezer, controller, _plan_at(amps=16, auto_identity="a" * 32))
    assert len(_installs(calls)) == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"hour": 3},
        {"energy_kwh": 20.0},
        {"vehicle_id": "other_car"},
    ],
)
async def test_a_restart_with_a_changed_plan_is_a_new_plan(
    hass: HomeAssistant, freezer: Any, changed: dict[str, Any]
) -> None:
    entry, calls, controller = await _quiet_charger(hass, freezer)
    await _install(hass, freezer, controller, _plan_at())
    controller = await _restart(hass, freezer, entry)
    await _install(hass, freezer, controller, _plan_at(amps=16, **changed))
    assert len(_installs(calls)) == 2


async def test_the_told_plan_is_kept_across_a_reload(hass: HomeAssistant, freezer: Any, hass_storage: dict) -> None:
    entry, calls, controller = await _quiet_charger(hass, freezer)
    key = f"spotnav.notified_plan.{entry.entry_id}"
    assert key not in hass_storage
    await _install(hass, freezer, controller, _plan_at())
    fingerprint = hass_storage[key]["data"]["fingerprint"]
    assert fingerprint
    await _restart(hass, freezer, entry)
    assert entry.runtime_data.notifier._notified_plan == fingerprint


async def test_a_plan_quiet_after_a_persons_own_change_is_remembered_and_not_told_after_a_restart(
    hass: HomeAssistant, freezer: Any, hass_storage: dict
) -> None:
    entry, calls, controller = await _quiet_charger(hass, freezer)
    await _write_amps(hass, entry.entry_id)
    await _later(hass, freezer, 5)
    await _install(hass, freezer, controller, _plan_at())
    assert _installs(calls) == []
    assert hass_storage[f"spotnav.notified_plan.{entry.entry_id}"]["data"]["fingerprint"]
    controller = await _restart(hass, freezer, entry)
    await _install(hass, freezer, controller, _plan_at(amps=16, auto_identity="a" * 32))
    assert _installs(calls) == []
