"""Instant notifications for the paired app: the `push_register` webhook action, the wake-ups a charger's
events send to the relay (mocked), what a 404 does, the diagnostics, and the hidden test service."""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.diagnostics import entry_diagnostics
from custom_components.spotnav.notifications import push as push_module
from custom_components.spotnav.notifications.push import (
    HOURLY_LIMIT,
    parse_push_register,
    PushRegisterError,
    PushRegistration,
    REPEAT_S,
    WAKE_TIMEOUT_S,
)
from custom_components.spotnav.notifications.settings import DEFAULT_EVENTS, NOTIFICATION_EVENTS
from custom_components.spotnav.runtime import charger_data

from .test_notifications import _choose
from .world import setup_charger

REF = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKiss-_"
WAKE_URL = "https://spotnav.sensnology.se/v1/push/wake"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "webhook"
WRITE = os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1"


class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type: Any = "application/json") -> Any:
        if self._body is None:
            raise ValueError("not JSON")
        return self._body

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeRelay:
    """The relay's wake-up endpoint: records every request and answers what a test sets."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.status = 200
        self.body: Any = {"v": 1, "sent": True}
        self.error: BaseException | None = None

    def post(self, url: str, *, json: dict[str, Any]) -> _Response:  # noqa: A002 - aiohttp's name
        self.requests.append((url, json))
        if self.error is not None:
            raise self.error
        return _Response(self.status, self.body)


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> FakeRelay:
    fake = FakeRelay()
    monkeypatch.setattr(push_module, "async_get_clientsession", lambda _hass: fake)
    return fake


async def _post(client: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = await client.post("/api/webhook/webhook-a", json={"version": 1, **payload})
    return response.status, await response.json()


async def _charger(hass: HomeAssistant) -> Any:
    entry = await setup_charger(hass, title="Garage")
    # After set-up: the core switch platform would replace a mock registered before it.
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    return entry


async def _register(hass: HomeAssistant, entry_id: str, events: tuple[str, ...] = DEFAULT_EVENTS) -> None:
    await charger_data(hass, entry_id).push.async_register(PushRegistration(push_ref=REF, events=events))


async def _later(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def _toggle(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.charger_a", "off")
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()


def _ours(caplog: pytest.LogCaptureFixture) -> str:
    """What the integration logged (the test harness's own storage mock logs what it writes)."""
    return "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("custom_components"))


def _pinned(name: str, payload: Any) -> None:
    path = FIXTURE_DIR / name
    if WRITE:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    assert json.loads(path.read_text(encoding="utf-8")) == payload, f"{name} drifted from the backend"


# ------------------------------------------------------------------ the body


def test_a_body_names_a_ref_and_the_events_or_clears_with_null() -> None:
    assert parse_push_register({"push_ref": REF}) == PushRegistration(REF, DEFAULT_EVENTS)
    assert parse_push_register({"push_ref": REF, "events": ["unplugged", "plan_stopped"]}) == PushRegistration(
        REF, ("plan_stopped", "unplugged")
    )
    assert parse_push_register({"push_ref": REF + "=="}).push_ref == REF + "=="
    assert parse_push_register({"push_ref": None}) is None
    assert parse_push_register({"push_ref": None, "events": "ignored"}) is None
    for bad in (
        {},
        {"push_ref": ""},
        {"push_ref": 42},
        {"push_ref": "short"},
        {"push_ref": "x" * 513},
        {"push_ref": REF + "/+"},
        {"push_ref": REF + " "},
        {"push_ref": REF, "events": "plan_stopped"},
        {"push_ref": REF, "events": ["stopped"]},
        {"push_ref": REF, "events": ["plan_stopped", "plan_stopped"]},
    ):
        with pytest.raises(PushRegisterError):
            parse_push_register(bad)
    assert parse_push_register({"push_ref": "x" * 512}).push_ref == "x" * 512


# ------------------------------------------------------------------ the webhook action


async def test_the_app_registers_and_clears_over_the_webhook_and_the_ref_is_kept_per_charger(
    hass: HomeAssistant, hass_client_no_auth, hass_storage: dict[str, Any]
) -> None:
    entry = await _charger(hass)
    client = await hass_client_no_auth()

    status, answer = await _post(client, {"action": "push_register", "push_ref": REF, "events": ["charge_complete"]})
    assert status == 200 and answer == {"ok": True, "action": "push_register"}
    _pinned("push_register_success.json", answer)
    assert charger_data(hass, entry.entry_id).push.registration == PushRegistration(REF, ("charge_complete",))
    assert hass_storage[f"spotnav.push.{entry.entry_id}"]["data"] == {
        "push_ref": REF,
        "events": ["charge_complete"],
    }

    # A reload restores it from the charger's store.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert charger_data(hass, entry.entry_id).push.registration == PushRegistration(REF, ("charge_complete",))

    status, answer = await _post(client, {"action": "push_register", "push_ref": None})
    assert status == 200 and answer["ok"] is True
    assert charger_data(hass, entry.entry_id).push.registration is None
    assert f"spotnav.push.{entry.entry_id}" not in hass_storage


async def test_a_bad_body_is_refused_with_a_stable_code_and_nothing_changes(
    hass: HomeAssistant, hass_client_no_auth, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    entry = await _charger(hass)
    await _register(hass, entry.entry_id)
    client = await hass_client_no_auth()
    for body in ({}, {"push_ref": "not base64url!"}, {"push_ref": REF + "x", "events": ["fire"]}):
        status, answer = await _post(client, {"action": "push_register", **body})
        assert status == 400 and answer == {"ok": False, "error": "invalid_push_register", "action": "push_register"}
    _pinned("push_register_invalid.json", answer)
    assert charger_data(hass, entry.entry_id).push.registration.push_ref == REF
    assert REF not in _ours(caplog)


async def test_a_removed_charger_forgets_its_registration(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = await _charger(hass)
    await _register(hass, entry.entry_id)
    assert f"spotnav.push.{entry.entry_id}" in hass_storage
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"spotnav.push.{entry.entry_id}" not in hass_storage


# ------------------------------------------------------------------ waking the app


async def test_a_chosen_event_wakes_the_app_once_without_any_companion_phone(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    entry = await _charger(hass)
    await _register(hass, entry.entry_id, events=("charge_started",))
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    assert relay.requests == [(WAKE_URL, {"v": 1, "push_ref": REF})], "only the ref: no kind, no content"

    await _toggle(hass)
    assert len(relay.requests) == 1, "not again within fifteen minutes"
    await _later(hass, freezer, REPEAT_S + 60)
    await _toggle(hass)
    assert len(relay.requests) == 2
    diagnostics = charger_data(hass, entry.entry_id).push.diagnostics()
    assert diagnostics["registered"] is True and diagnostics["last_wake_result"] == "sent"
    assert diagnostics["last_wake_at"] is not None


async def test_an_event_the_app_did_not_choose_wakes_nothing_and_nor_does_no_registration(
    hass: HomeAssistant, relay: FakeRelay
) -> None:
    entry = await _charger(hass)
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    await _register(hass, entry.entry_id)  # the defaults: charge_started is not among them
    await _toggle(hass)
    assert relay.requests == []


async def test_the_wake_up_and_the_companion_phone_are_independent(
    hass: HomeAssistant, relay: FakeRelay
) -> None:
    entry = await _charger(hass)
    calls = async_mock_service(hass, "notify", "mobile_app_pixel")
    await _choose(hass, entry.entry_id, targets=("mobile_app_pixel",), events=("plugged_in",))
    await _register(hass, entry.entry_id, events=("charge_started",))
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    assert calls == [] and len(relay.requests) == 1


async def test_the_hourly_limit_holds_wake_ups_back(hass: HomeAssistant) -> None:
    entry = await _charger(hass)
    push = charger_data(hass, entry.entry_id).push
    sent: list[str] = []

    async def _fake_send(ref: str, kind: str) -> str:
        sent.append(kind)
        return "sent"

    push._async_send = _fake_send  # type: ignore[method-assign]
    await _register(hass, entry.entry_id, events=NOTIFICATION_EVENTS)
    now = dt_util.utcnow()
    for round_ in range(2):
        for event in NOTIFICATION_EVENTS:
            push.async_event(event, now + timedelta(minutes=16 * round_))
            await hass.async_block_till_done()
    assert 2 * len(NOTIFICATION_EVENTS) > HOURLY_LIMIT
    assert len(sent) == HOURLY_LIMIT


async def test_the_relay_forgetting_the_token_drops_the_ref_and_other_failures_keep_it(
    hass: HomeAssistant,
    freezer: Any,
    relay: FakeRelay,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    entry = await _charger(hass)
    push = charger_data(hass, entry.entry_id).push
    await _register(hass, entry.entry_id, events=("charge_started",))

    for status, body, result in (
        (404, None, "http_404"),  # a proxy's 404 is not the relay's answer
        (429, {"error": "rate_limited"}, "rate_limited"),
        (503, {"error": "push_disabled"}, "push_disabled"),
        (502, {"error": "fcm_failed"}, "http_502"),
        (400, {"error": "invalid_request"}, "http_400"),  # only invalid_ref drops it
    ):
        relay.status, relay.body = status, body
        await _toggle(hass)
        assert push.diagnostics()["last_wake_result"] == result
        assert push.registration is not None
        await _later(hass, freezer, REPEAT_S + 60)

    relay.error = ClientError("down")
    await _toggle(hass)
    assert push.diagnostics()["last_wake_result"] == "network"
    await _later(hass, freezer, REPEAT_S + 60)

    relay.error = None
    relay.status, relay.body = 404, {"error": "unknown_ref"}
    await _toggle(hass)
    assert push.registration is None
    assert f"spotnav.push.{entry.entry_id}" not in hass_storage
    assert push.diagnostics() == {
        "registered": False,
        "events": None,
        "last_wake_result": "unknown_ref",
        "last_wake_at": push.diagnostics()["last_wake_at"],
    }
    assert REF not in _ours(caplog), "the ref is never logged"

    # A ref the relay can no longer open (its push key rotated) is dropped as well.
    await _register(hass, entry.entry_id, events=("charge_started",))
    await _later(hass, freezer, REPEAT_S + 60)
    relay.status, relay.body = 400, {"error": "invalid_ref"}
    await _toggle(hass)
    assert push.registration is None


async def test_a_wake_up_is_bounded_in_time(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = await _charger(hass)
    push = charger_data(hass, entry.entry_id).push

    class _Slow:
        def post(self, url: str, *, json: dict[str, Any]) -> Any:  # noqa: A002
            raise TimeoutError

    monkeypatch.setattr(push_module, "async_get_clientsession", lambda _hass: _Slow())
    assert WAKE_TIMEOUT_S == 10.0
    assert await push._async_post(REF, "wake") == "timeout"


async def test_diagnostics_say_registered_but_never_carry_the_ref(hass: HomeAssistant) -> None:
    entry = await _charger(hass)
    assert entry_diagnostics(hass, entry)["push"] == {
        "registered": False,
        "events": None,
        "last_wake_result": None,
        "last_wake_at": None,
    }
    await _register(hass, entry.entry_id)
    diagnostics = entry_diagnostics(hass, entry)
    assert diagnostics["push"]["registered"] is True
    assert diagnostics["push"]["events"] == list(DEFAULT_EVENTS)
    assert REF not in json.dumps(diagnostics, default=str)


# ------------------------------------------------------------------ the hidden test service


async def test_the_test_service_pushes_kind_test_and_tells_the_chosen_phones(
    hass: HomeAssistant, relay: FakeRelay, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    hass.config.language = "sv"
    entry = await _charger(hass)
    calls = async_mock_service(hass, "notify", "mobile_app_pixel")
    await _choose(hass, entry.entry_id, targets=("mobile_app_pixel",))
    await _register(hass, entry.entry_id)

    answer = await hass.services.async_call(
        "spotnav", "send_test_notification", {}, blocking=True, return_response=True
    )
    assert relay.requests == [(WAKE_URL, {"v": 1, "push_ref": REF, "kind": "test"})]
    assert answer == {"chargers": {entry.entry_id: {"name": "Garage", "push": "sent", "notified": ["mobile_app_pixel"]}}}
    assert [call.data["message"] for call in calls] == ["Testnotis från SpotNav: notiserna når den här telefonen."]
    assert calls[0].data["data"]["tag"] == f"spotnav_{entry.entry_id}_test"
    assert REF not in _ours(caplog)

    # One charger by one of its entities.
    entity = next(e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))
    await hass.services.async_call("spotnav", "send_test_notification", {"charger": entity}, blocking=True)
    assert len(relay.requests) == 2
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("spotnav", "send_test_notification", {"charger": "nope"}, blocking=True)


async def test_the_test_service_refuses_when_no_app_is_registered_after_telling_the_phones(
    hass: HomeAssistant, relay: FakeRelay
) -> None:
    entry = await _charger(hass)
    calls = async_mock_service(hass, "notify", "mobile_app_pixel")
    await _choose(hass, entry.entry_id, targets=("mobile_app_pixel",))
    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call("spotnav", "send_test_notification", {}, blocking=True)
    assert refused.value.translation_key == "no_push_registered"
    assert relay.requests == [] and len(calls) == 1
