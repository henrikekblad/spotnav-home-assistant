"""The app as a full Home Assistant client: the webhook `dashboard` read and the bounded
writes.

Every case goes through Home Assistant's real `/api/webhook/<id>` route. The dashboard is asserted
identical to the WebSocket answer for the same moment (bar the documented writability), each write
is asserted to answer its WebSocket twin's own envelope, and the answers are pinned as fixtures under
`tests/fixtures/webhook/` written by the handlers (`SPOTNAV_WRITE_FIXTURES=1` rewrites them).
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from tests.helpers import as_app_sees
from custom_components.spotnav.api import site_settings as site_settings_module
from custom_components.spotnav.planning.auto_settings import STRATEGY_CHEAPEST
from custom_components.spotnav.const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_CHARGER_PRIORITY,
    CONF_PHASE_SWITCHING,
    CONF_PHASE_WIRING,
    CONF_PRICE_ENTRY_ID,
    CONF_SOLAR_FORECAST_ENTRIES,
    CONF_SOLAR_PRIORITY,
    SOLAR_PRIORITY_BATTERY_FIRST,
    SOLAR_PRIORITY_CAR_FIRST,
)
from custom_components.spotnav.vehicles.discovery_decisions import DECISION_DOMAIN_VEHICLE_PROPERTIES
from tests.test_dashboard_api import NOW
from tests.world import go_auto
from tests.world import admin, non_admin, ws_call
from tests.world import forecast_entry, setup_charger, setup_site_with_charger
from tests.world import add_car
from custom_components.spotnav.runtime import controller_for, domain_data
from custom_components.spotnav.pricing.price_store import price_duplicates
from tests.relay import SE4
from tests.test_price_card_api import dashboard as price_dashboard, pointer, two_chargers, two_prices
from tests.test_price_device import add_price, NOW as PRICE_NOW
from tests.test_solar_phase_execution import _world as _phase_world
from tests.world import setup_site

pytestmark = pytest.mark.usefixtures("offline_relay")

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "webhook"
WRITE = os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1"


@pytest.fixture(autouse=True)
def _forecast_capable_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(_hass: HomeAssistant) -> frozenset[str]:
        return frozenset({"roof"})

    monkeypatch.setattr(site_settings_module, "async_forecast_capable_domains", _fake)
    monkeypatch.setattr(dashboard_api, "async_forecast_capable_domains", _fake)


async def post(client, webhook_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = await client.post(f"/api/webhook/{webhook_id}", json={"version": 1, **payload})
    return response.status, await response.json()


def _pin(payload: Any, ids: dict[str, str] | None = None) -> Any:
    text = json.dumps(payload)
    for real, stable in (ids or {}).items():
        text = text.replace(real, stable)
    return json.loads(re.sub(r'"[0-9a-f]{32}"', '"<id>"', text))


def _pinned(name: str, payload: Any) -> None:
    path = FIXTURE_DIR / name
    if WRITE:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    assert json.loads(path.read_text(encoding="utf-8")) == payload, f"{name} drifted from the backend"


def _without_route_keys(body: dict[str, Any]) -> dict[str, Any]:
    body = dict(body)
    body.pop("action")
    return body


# ------------------------------------------------------------------ dashboard


async def test_the_webhook_dashboard_is_the_websocket_dashboard_bar_writability(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client, offline_relay: None
) -> None:
    with freeze_time(NOW):
        charger, _site = await setup_site_with_charger(hass, charger_entry_id="entry_dash")
        await go_auto(hass, charger.entry_id, strategy=STRATEGY_CHEAPEST)
    client = await hass_client_no_auth()
    socket = await admin(hass, hass_ws_client)
    request = {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": charger.entry_id}

    # Authenticated first: a frozen clock would expire the access token the socket already holds.
    with freeze_time(NOW):
        status, body = await post(client, "webhook-entry_dash", {"action": "dashboard", "api_version": 1})
        admin_frame = (await ws_call(socket, request))["result"]

    assert status == 200 and body["ok"] is True and body["action"] == "dashboard"
    payload = {k: v for k, v in body.items() if k not in ("ok", "action")}
    assert payload["control"]["can_act"] is True and payload["site"]["writable"] is True
    assert payload["site"]["active_control"]["writable"] is False
    # One shape over both carriers: an admin's socket may switch active control, a reader's may not.
    assert admin_frame["site"]["active_control"]["writable"] is True
    expected = copy.deepcopy(admin_frame)
    expected["site"]["active_control"]["writable"] = False
    # The one other documented difference: the webhook leaves out what the app cannot read yet.
    assert "departure_date" in expected["settings"]
    expected["settings"] = as_app_sees(expected["settings"])
    # And `prices.provider`, read only with `reads: ["price_provider"]`.
    assert "provider" in expected["prices"]
    del expected["prices"]["provider"]
    assert payload == expected
    _pinned("dashboard.json", _pin(body))


def test_no_webhook_fixture_carries_a_field_the_app_cannot_read() -> None:
    from custom_components.spotnav.api.webhook import APP_UNREAD_SETTINGS

    def keys(node: Any) -> set[str]:
        if isinstance(node, dict):
            return set(node) | {key for child in node.values() for key in keys(child)}
        if isinstance(node, list):
            return {key for child in node for key in keys(child)}
        return set()

    fixtures = sorted(FIXTURE_DIR.glob("*.json"))
    assert fixtures
    for path in fixtures:
        found = keys(json.loads(path.read_text(encoding="utf-8"))) & set(APP_UNREAD_SETTINGS)
        assert not found, f"{path.name} carries {sorted(found)}"


async def test_a_read_only_socket_sees_no_writability(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, offline_relay: None
) -> None:
    charger, _site = await setup_site_with_charger(hass, charger_entry_id="entry_ro")
    reader = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    frame = await ws_call(
        reader, {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": charger.entry_id}
    )
    site = frame["result"]["site"]
    assert site["writable"] is False and site["active_control"]["writable"] is False


async def test_the_webhook_dashboard_refuses_other_versions_like_the_socket(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client
) -> None:
    charger, _site = await setup_site_with_charger(hass, charger_entry_id="entry_ver")
    client = await hass_client_no_auth()
    socket = await admin(hass, hass_ws_client)
    for version in (None, 2, 7, True, "1"):
        payload = {"action": "dashboard"}
        message = {"type": "spotnav/get_dashboard", "charger_id": charger.entry_id}
        if version is not None:
            payload["api_version"] = version
            message["api_version"] = version
        status, body = await post(client, "webhook-entry_ver", payload)
        frame = await ws_call(socket, message)
        assert (status, body["ok"], body["error"]) == (400, False, "spotnav_unsupported_api_version")
        assert frame["error"]["code"] == body["error"]
    _pinned("dashboard_unsupported_version.json", body)


# --------------------------------------------------------------- update_vehicle


async def test_update_vehicle_over_the_webhook_shares_the_sockets_core_and_envelope(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client
) -> None:
    charger, _site = await setup_site_with_charger(hass, charger_entry_id="entry_car")
    other, _ = await setup_site_with_charger(
        hass, charger_entry_id="entry_other", site_entry_id="site_other"
    )
    car = add_car(hass, "Niro")
    client = await hass_client_no_auth()
    socket = await admin(hass, hass_ws_client)
    ids = {car: "vehicle_niro"}

    # Refused: nothing is written, the vehicle row travels back, and no entity config is exposed.
    status, refused = await post(
        client,
        "webhook-entry_car",
        {"action": "update_vehicle", "reads": ["min_soc", "wake_for_reading"], "vehicle_id": car, "changes": {"capacity_kwh": 900, "consumption_kwh_per_10km": 0}},
    )
    assert status == 400 and refused["error"] == "spotnav_invalid_value" and refused["config"] is None
    assert {e["field"] for e in refused["field_errors"]} == {"capacity_kwh", "consumption_kwh_per_10km"}
    assert domain_data(hass).decision_store.confirmed_payload(DECISION_DOMAIN_VEHICLE_PROPERTIES, car) is None

    # A `charger_id` naming another charger is never read.
    status, success = await post(
        client,
        "webhook-entry_car",
        {
            "action": "update_vehicle",
            "reads": ["min_soc", "wake_for_reading"],
            "charger_id": other.entry_id,
            "vehicle_id": car,
            "changes": {"capacity_kwh": 64.8, "consumption_kwh_per_10km": 1.7},
            "expected": {"capacity_kwh": None, "consumption_kwh_per_10km": 2.0},
        },
    )
    assert status == 200 and success["ok"] is True and success["config"] is None
    assert success["vehicle"]["capacity_kwh"] == 64.8
    assert domain_data(hass).decision_store.confirmed_payload(DECISION_DOMAIN_VEHICLE_PROPERTIES, car) == {
        "capacity_kwh": 64.8,
        "consumption_kwh_per_10km": 1.7,
    }

    status, conflict = await post(
        client,
        "webhook-entry_car",
        {"action": "update_vehicle", "reads": ["min_soc", "wake_for_reading"], "vehicle_id": car, "changes": {"capacity_kwh": 70}, "expected": {"capacity_kwh": 50}},
    )
    assert status == 409 and conflict["error"] == "spotnav_conflict" and conflict["vehicle"]["capacity_kwh"] == 64.8

    status, unknown = await post(
        client, "webhook-entry_car", {"action": "update_vehicle", "reads": ["min_soc", "wake_for_reading"], "vehicle_id": "nope", "changes": {"capacity_kwh": 70}}
    )
    assert status == 400 and unknown["field_errors"] == [{"field": "vehicle_id", "code": "unknown_vehicle"}]

    status, bad_version = await post(
        client, "webhook-entry_car",
        {"action": "update_vehicle", "reads": ["min_soc", "wake_for_reading"], "api_version": 2, "vehicle_id": car, "changes": {"capacity_kwh": 70}},
    )
    assert status == 400 and bad_version["error"] == "spotnav_unsupported_api_version"

    # The socket's answer to the same request is the same envelope, bar the config it alone carries.
    frame = (
        await ws_call(
            socket,
            {
                "type": "spotnav/update_vehicle",
                "api_version": 1,
                "charger_id": charger.entry_id,
                "vehicle_id": car,
                "changes": {"capacity_kwh": 70},
                "expected": {"capacity_kwh": 50},
            },
        )
    )["result"]
    assert {**frame, "config": None} == _without_route_keys(conflict)

    _pinned("update_vehicle_success.json", _pin(success, ids))
    _pinned("update_vehicle_refused.json", _pin(refused, ids))
    _pinned("update_vehicle_conflict.json", _pin(conflict, ids))
    _pinned("update_vehicle_unknown_vehicle.json", _pin(unknown, ids))


# ------------------------------------------------------------ update_site_settings


async def test_site_settings_over_the_webhook_write_solar_only(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client
) -> None:
    roof = forecast_entry(hass, entry_id="roof_wh", title="Roof")
    charger, site = await setup_site_with_charger(hass, charger_entry_id="entry_site", site_entry_id="site_wh")
    other, other_site = await setup_site_with_charger(
        hass, charger_entry_id="entry_site_b", site_entry_id="site_wh_b"
    )
    client = await hass_client_no_auth()
    socket = await admin(hass, hass_ws_client)

    status, success = await post(
        client,
        "webhook-entry_site",
        {
            "action": "update_site_settings",
            # Another charger's id in the body is never read.
            "charger_id": other.entry_id,
            "expected": {"solar_priority": SOLAR_PRIORITY_CAR_FIRST, "solar_forecast": []},
            "changes": {"solar_priority": SOLAR_PRIORITY_BATTERY_FIRST, "solar_forecast": [roof.entry_id]},
        },
    )
    assert status == 200 and success["ok"] is True and success["api_version"] == 1
    assert success["site"]["solar_priority"] == SOLAR_PRIORITY_BATTERY_FIRST
    assert success["site"]["solar_forecast"]["selected"] == [roof.entry_id]
    assert success["site"]["active_control"]["writable"] is False and success["site"]["writable"] is True
    assert site.data[CONF_SOLAR_PRIORITY] == SOLAR_PRIORITY_BATTERY_FIRST
    assert other_site.data.get(CONF_SOLAR_PRIORITY) != SOLAR_PRIORITY_BATTERY_FIRST
    assert other_site.data.get(CONF_SOLAR_FORECAST_ENTRIES, []) == []

    status, conflict = await post(
        client,
        "webhook-entry_site",
        {"action": "update_site_settings", "expected": {"solar_priority": SOLAR_PRIORITY_CAR_FIRST},
         "changes": {"solar_priority": SOLAR_PRIORITY_CAR_FIRST}},
    )
    assert status == 409 and conflict["error"] == "spotnav_conflict"

    status, invalid = await post(
        client,
        "webhook-entry_site",
        {"action": "update_site_settings", "expected": {}, "changes": {"solar_forecast": ["not-a-source"]}},
    )
    assert status == 400 and invalid["error"] == "spotnav_invalid_value"

    # Load balancing is not the webhook's to touch, in `changes` or `expected`, and nothing
    # is written.
    active_before = site.data.get(CONF_ACTIVE_CONTROL_ENABLED)
    forbidden = []
    for body in (
        {"expected": {}, "changes": {"active_control_enabled": False}},
        {"expected": {}, "changes": {"active_control_enabled": True, "solar_priority": SOLAR_PRIORITY_CAR_FIRST}},
        {"expected": {"active_control_enabled": True}, "changes": {"solar_priority": SOLAR_PRIORITY_CAR_FIRST}},
    ):
        status, refused = await post(client, "webhook-entry_site", {"action": "update_site_settings", **body})
        assert status == 403 and refused["error"] == "spotnav_not_permitted_over_webhook"
        assert refused["site"]["active_control"]["writable"] is False
        forbidden.append(refused)
    assert site.data.get(CONF_ACTIVE_CONTROL_ENABLED) == active_before
    assert site.data[CONF_SOLAR_PRIORITY] == SOLAR_PRIORITY_BATTERY_FIRST
    status, other_version = await post(
        client, "webhook-entry_site",
        {"action": "update_site_settings", "api_version": 2, "expected": {}, "changes": {"solar_priority": SOLAR_PRIORITY_CAR_FIRST}},
    )
    assert status == 400 and other_version["error"] == "spotnav_unsupported_api_version"

    # The socket's own answer to the same conflict differs only in the carrier's writability.
    frame = (
        await ws_call(
            socket,
            {
                "type": "spotnav/update_site_settings",
                "api_version": 1,
                "charger_id": charger.entry_id,
                "expected": {"solar_priority": SOLAR_PRIORITY_CAR_FIRST},
                "changes": {"solar_priority": SOLAR_PRIORITY_CAR_FIRST},
            },
        )
    )["result"]
    frame["site"]["active_control"]["writable"] = False
    assert frame == _without_route_keys(conflict)

    _pinned("update_site_settings_success.json", _pin(success))
    _pinned("update_site_settings_conflict.json", _pin(conflict))
    _pinned("update_site_settings_invalid_value.json", _pin(invalid))
    _pinned("update_site_settings_not_permitted.json", _pin(forbidden[0]))


# --------------------------------------------------------- update_charger_priority


async def test_charger_priority_over_the_webhook_shares_the_sockets_core(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client
) -> None:
    """The webhook writes its own charger's priority with the value it last saw, through the entity
    config core: a stale `expected` is a conflict, an unknown value is refused field by field, a
    charger on no site is refused, and the answer is the dashboard's `charger_priority` block."""
    charger, _site = await setup_site_with_charger(hass, charger_entry_id="entry_prio", site_entry_id="site_prio")
    lone = await setup_charger(hass, entry_id="entry_lone", webhook_id="webhook-entry_lone", charge_control="switch.lone")
    client = await hass_client_no_auth()
    socket = await admin(hass, hass_ws_client)
    controller_before = charger.runtime_data.controller

    status, success = await post(
        client,
        "webhook-entry_prio",
        {"action": "update_charger_priority", "api_version": 1, "expected": "normal", "priority": "first"},
    )
    assert status == 200 and success["ok"] is True and success["error"] is None
    assert success["charger_priority"] == {"value": "first", "choices": ["first", "normal", "last"], "writable": True}
    assert charger.data[CONF_CHARGER_PRIORITY] == "first"
    # A priority alone is read live by the site: the charger is not reloaded for it.
    assert charger.runtime_data.controller is controller_before

    status, dashboard = await post(client, "webhook-entry_prio", {"action": "dashboard", "api_version": 1})
    assert status == 200 and dashboard["charger_priority"]["value"] == "first"

    status, conflict = await post(
        client, "webhook-entry_prio", {"action": "update_charger_priority", "expected": "normal", "priority": "last"}
    )
    assert status == 409 and conflict["error"] == "spotnav_conflict"
    assert conflict["charger_priority"]["value"] == "first" and charger.data[CONF_CHARGER_PRIORITY] == "first"

    status, invalid = await post(
        client, "webhook-entry_prio", {"action": "update_charger_priority", "expected": "first", "priority": "top"}
    )
    assert status == 400 and invalid["error"] == "spotnav_invalid_value"
    assert invalid["field_errors"] == [{"field": "charger_priority", "code": "invalid_value"}]

    for body in ({"priority": "last"}, {"expected": "middle", "priority": "last"}, {"expected": None, "priority": "last"}):
        status, refused = await post(client, "webhook-entry_prio", {"action": "update_charger_priority", **body})
        assert status == 400 and refused["error"] == "spotnav_invalid_value"
        assert refused["field_errors"] == [{"field": "expected", "code": "invalid_value"}]
    status, missing = await post(
        client, "webhook-entry_prio", {"action": "update_charger_priority", "expected": "first"}
    )
    assert status == 400 and missing["field_errors"] == [{"field": "charger_priority", "code": "invalid_value"}]

    status, other_version = await post(
        client, "webhook-entry_prio",
        {"action": "update_charger_priority", "api_version": 2, "expected": "first", "priority": "last"},
    )
    assert status == 400 and other_version["error"] == "spotnav_unsupported_api_version"

    status, no_site = await post(
        client, "webhook-entry_lone", {"action": "update_charger_priority", "expected": "normal", "priority": "first"}
    )
    assert status == 400 and no_site["error"] == "spotnav_no_site" and no_site["charger_priority"] is None
    assert CONF_CHARGER_PRIORITY not in lone.data

    # Back to the default: stored as nothing, as the socket stores it.
    status, back = await post(
        client, "webhook-entry_prio", {"action": "update_charger_priority", "expected": "first", "priority": "normal"}
    )
    assert status == 200 and back["charger_priority"]["value"] == "normal"
    assert CONF_CHARGER_PRIORITY not in charger.data

    # The socket refuses the same stale write with the same code.
    frame = (
        await ws_call(
            socket,
            {
                "type": "spotnav/update_entity_config",
                "api_version": 1,
                "charger_id": charger.entry_id,
                "scope": "charger",
                "expected": {"charger_priority": "first"},
                "changes": {"charger_priority": "last"},
            },
        )
    )["result"]
    assert frame["error"] == "spotnav_conflict"

    _pinned("update_charger_priority_success.json", _pin(success))
    _pinned("update_charger_priority_conflict.json", _pin(conflict))
    _pinned("update_charger_priority_invalid_value.json", _pin(invalid))
    _pinned("update_charger_priority_no_site.json", _pin(no_site))


# --------------------------------------------------------- the electricity price


async def test_choosing_the_price_over_the_webhook_shares_the_sockets_core(
    hass: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """The webhook's own charger chooses its electricity price as the socket does: a charger on a site points
    the site, and the answer is the charger's `price_source` block re-read; an entry that is no price, or a
    field that is not text, is refused and nothing moves."""
    await two_chargers(hass)
    site = await setup_site(hass, charger_entry_ids=["entry_a", "entry_b"])
    client = await hass_client_no_auth()
    freezer.move_to(PRICE_NOW)
    await two_prices(hass)

    status, success = await post(client, "webhook-a", {"action": "choose_price_source", "price_entry_id": "price_b"})
    await hass.async_block_till_done()
    assert status == 200 and success["ok"] is True and success["error"] is None and success["field_errors"] == []
    assert success["action"] == "choose_price_source"
    assert pointer(hass, site.entry_id) == "price_b" and pointer(hass, "entry_a") is None
    block = success["price_source"]
    assert block["entry_id"] == "price_b" and block["charger_count"] == 2 and block["writable"] is True
    assert block["duplicate"]["area_id"] == SE4
    assert block == price_dashboard(hass)["price_source"]

    status, unknown = await post(client, "webhook-a", {"action": "choose_price_source", "price_entry_id": "entry_b"})
    assert status == 400 and unknown["ok"] is False and unknown["error"] == "spotnav_unknown_price_entry"
    assert unknown["field_errors"] == [] and unknown["price_source"]["entry_id"] == "price_b"
    assert pointer(hass, site.entry_id) == "price_b"

    for body in ({}, {"price_entry_id": 5}, {"price_entry_id": None}, {"price_entry_id": ["price_a"]}):
        status, invalid = await post(client, "webhook-a", {"action": "choose_price_source", **body})
        assert status == 400 and invalid["error"] == "spotnav_invalid_value", body
        assert invalid["field_errors"] == [{"field": "price_entry_id", "code": "invalid_value"}]
    assert pointer(hass, site.entry_id) == "price_b"

    _pinned("choose_price_source_success.json", _pin(success))
    _pinned("choose_price_source_unknown_entry.json", _pin(unknown))
    _pinned("choose_price_source_invalid_value.json", _pin(invalid))


async def test_choosing_the_price_without_a_site_points_the_charger(
    hass: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    await two_chargers(hass)
    client = await hass_client_no_auth()
    freezer.move_to(PRICE_NOW)
    await two_prices(hass)

    status, body = await post(client, "webhook-b", {"action": "choose_price_source", "price_entry_id": "price_b"})
    await hass.async_block_till_done()

    assert status == 200 and body["price_source"]["entry_id"] == "price_b"
    assert pointer(hass, "entry_b") == "price_b" and pointer(hass, "entry_a") is None


async def test_answering_the_same_area_question_over_the_webhook_shares_the_sockets_core(
    hass: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """The question answered over the webhook as over the socket: an entry it does not ask about, an area it
    does not ask about and fields of the wrong types are refused and it is still asked; using one moves every
    pointer, and the answer is the charger's block without the question."""
    await two_chargers(hass)
    client = await hass_client_no_auth()
    freezer.move_to(PRICE_NOW)
    await two_prices(hass)
    await add_price(hass, entry_id="price_c", area="DK1")
    for charger in ("entry_a", "entry_b"):
        entry = hass.config_entries.async_get_entry(charger)
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PRICE_ENTRY_ID: "price_b"})

    async def answer(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return await post(client, "webhook-a", {"action": "answer_price_duplicate", **body})

    status, unknown_area = await answer({"area_id": "NO1", "keep_entry_id": None})
    assert status == 400 and unknown_area["error"] == "spotnav_unknown_price_duplicate"
    assert unknown_area["price_source"]["duplicate"]["area_id"] == SE4
    status, unknown_entry = await answer({"area_id": SE4, "keep_entry_id": "price_c"})
    assert status == 400 and unknown_entry["error"] == "spotnav_unknown_price_entry"
    for body, field in (
        ({"area_id": 4, "keep_entry_id": None}, "area_id"),
        ({"keep_entry_id": None}, "area_id"),
        ({"area_id": SE4, "keep_entry_id": 3}, "keep_entry_id"),
        ({"area_id": SE4}, "keep_entry_id"),
    ):
        status, invalid = await answer(body)
        assert status == 400 and invalid["error"] == "spotnav_invalid_value", body
        assert invalid["field_errors"] == [{"field": field, "code": "invalid_value"}]
    assert len(price_duplicates(hass)) == 1, "still asked"

    status, success = await answer({"area_id": SE4, "keep_entry_id": "price_a"})
    await hass.async_block_till_done()
    assert status == 200 and success["ok"] is True and success["error"] is None
    assert success["action"] == "answer_price_duplicate"
    assert pointer(hass, "entry_a") == "price_a" and pointer(hass, "entry_b") == "price_a"
    assert price_duplicates(hass) == []
    assert success["price_source"]["entry_id"] == "price_a" and success["price_source"]["duplicate"] is None
    assert success["price_source"] == price_dashboard(hass)["price_source"]

    _pinned("answer_price_duplicate_success.json", _pin(success))
    _pinned("answer_price_duplicate_unknown_duplicate.json", _pin(unknown_area))
    _pinned("answer_price_duplicate_unknown_entry.json", _pin(unknown_entry))
    _pinned("answer_price_duplicate_invalid_value.json", _pin(invalid))


async def test_keeping_both_over_the_webhook_answers_and_moves_nothing(
    hass: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    await two_chargers(hass)
    client = await hass_client_no_auth()
    freezer.move_to(PRICE_NOW)
    await two_prices(hass)
    entry = hass.config_entries.async_get_entry("entry_a")
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PRICE_ENTRY_ID: "price_b"})

    status, body = await post(
        client, "webhook-a", {"action": "answer_price_duplicate", "area_id": SE4, "keep_entry_id": None}
    )

    assert status == 200 and body["ok"] is True and body["price_source"]["duplicate"] is None
    assert pointer(hass, "entry_a") == "price_b"
    assert price_duplicates(hass, unanswered_only=False)[0]["answered"] is True


# --------------------------------------------------------- phase switching for solar


async def test_phase_switching_over_the_webhook_shares_the_sockets_core(
    hass: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """The webhook dashboard's `phase_switching` block, only for an app that reads it, and its write through the
    entity config core: a stale `expected` is a conflict, a non-boolean is refused field by field, and the
    charger is not reloaded for it."""
    w = await _phase_world(hass, freezer, option=False)
    charger = w.charger
    client = await hass_client_no_auth()
    webhook_id = "webhook-solar"

    status, plain = await post(client, webhook_id, {"action": "dashboard", "api_version": 1})
    assert status == 200 and "phase_switching" not in plain
    status, read = await post(
        client, webhook_id, {"action": "dashboard", "api_version": 1, "reads": ["phase_switching"]}
    )
    assert status == 200 and read["phase_switching"] == {"value": False, "writable": True}
    assert {key: value for key, value in read.items() if key != "phase_switching"}.keys() == plain.keys()

    status, success = await post(
        client, webhook_id, {"action": "update_phase_switching", "api_version": 1, "enabled": True, "expected": False}
    )
    assert status == 200 and success["ok"] is True and success["error"] is None and success["field_errors"] == []
    assert success["action"] == "update_phase_switching"
    assert success["phase_switching"] == {"value": True, "writable": True}
    assert charger.data[CONF_PHASE_SWITCHING] is True
    assert controller_for(hass, charger.entry_id) is w.controller, "no reload"
    assert w.controller.phase_switching_option is True

    status, conflict = await post(
        client, webhook_id, {"action": "update_phase_switching", "enabled": False, "expected": False}
    )
    assert status == 409 and conflict["error"] == "spotnav_conflict"
    assert conflict["phase_switching"]["value"] is True and charger.data[CONF_PHASE_SWITCHING] is True

    status, invalid = await post(
        client, webhook_id, {"action": "update_phase_switching", "enabled": "off", "expected": True}
    )
    assert status == 400 and invalid["error"] == "spotnav_invalid_value"
    assert invalid["field_errors"] == [{"field": "phase_switching", "code": "invalid_value"}]
    status, missing = await post(client, webhook_id, {"action": "update_phase_switching", "expected": True})
    assert status == 400 and missing["field_errors"] == [{"field": "phase_switching", "code": "invalid_value"}]
    for body in ({"enabled": False}, {"enabled": False, "expected": "on"}, {"enabled": False, "expected": 1}):
        status, refused = await post(client, webhook_id, {"action": "update_phase_switching", **body})
        assert status == 400 and refused["error"] == "spotnav_invalid_value", body
        assert refused["field_errors"] == [{"field": "expected", "code": "invalid_value"}]
    status, other_version = await post(
        client, webhook_id, {"action": "update_phase_switching", "api_version": 2, "enabled": False, "expected": True}
    )
    assert status == 400 and other_version["error"] == "spotnav_unsupported_api_version"
    assert charger.data[CONF_PHASE_SWITCHING] is True

    status, off = await post(
        client, webhook_id, {"action": "update_phase_switching", "enabled": False, "expected": True}
    )
    assert status == 200 and off["phase_switching"] == {"value": False, "writable": True}
    assert CONF_PHASE_SWITCHING not in charger.data, "stored only while on"

    _pinned("update_phase_switching_success.json", _pin(success))
    _pinned("update_phase_switching_conflict.json", _pin(conflict))
    _pinned("update_phase_switching_invalid_value.json", _pin(invalid))


async def test_phase_switching_where_it_is_not_offered(hass: HomeAssistant, hass_client_no_auth, freezer) -> None:
    """No block even for an app that reads it, turning it on is the core's not-writable refusal, and turning a
    setting left on off is allowed."""
    w = await _phase_world(hass, freezer, option=True)
    charger = w.charger
    site_entry = hass.config_entries.async_get_entry(w.site.entry_id)
    wiring = dict(site_entry.data.get(CONF_PHASE_WIRING) or {})
    wiring[charger.entry_id] = {**(wiring.get(charger.entry_id) or {}), "phases": 1}
    hass.config_entries.async_update_entry(site_entry, data={**site_entry.data, CONF_PHASE_WIRING: wiring})
    client = await hass_client_no_auth()
    webhook_id = "webhook-solar"

    status, read = await post(
        client, webhook_id, {"action": "dashboard", "api_version": 1, "reads": ["phase_switching"]}
    )
    assert status == 200 and "phase_switching" not in read

    hass.config_entries.async_update_entry(charger, data={k: v for k, v in charger.data.items() if k != CONF_PHASE_SWITCHING})
    status, not_offered = await post(
        client, webhook_id, {"action": "update_phase_switching", "enabled": True, "expected": False}
    )
    assert status == 400 and not_offered["error"] == "spotnav_invalid_value"
    assert not_offered["field_errors"] == [{"field": "phase_switching", "code": "not_writable"}]
    assert not_offered["phase_switching"] is None and CONF_PHASE_SWITCHING not in charger.data

    hass.config_entries.async_update_entry(charger, data={**charger.data, CONF_PHASE_SWITCHING: True})
    status, off = await post(
        client, webhook_id, {"action": "update_phase_switching", "enabled": False, "expected": True}
    )
    assert status == 200 and off["ok"] is True and off["phase_switching"] is None
    assert CONF_PHASE_SWITCHING not in charger.data

    _pinned("update_phase_switching_not_writable.json", _pin(not_offered))


async def test_no_fixture_is_left_unwritten() -> None:
    if WRITE:
        return
    assert sorted(p.name for p in FIXTURE_DIR.glob("*.json")) == sorted(
        [
            "answer_price_duplicate_invalid_value.json",
            "answer_price_duplicate_success.json",
            "answer_price_duplicate_unknown_duplicate.json",
            "answer_price_duplicate_unknown_entry.json",
            "choose_price_source_invalid_value.json",
            "choose_price_source_success.json",
            "choose_price_source_unknown_entry.json",
            "dashboard.json",
            "dashboard_unsupported_version.json",
            "price_days_no_price_area.json",
            "price_days_no_prices.json",
            "price_days_nordpool_fi.json",
            "price_days_relay_gb.json",
            "price_days_relay_se4.json",
            "push_register_invalid.json",
            "push_register_success.json",
            "update_charger_priority_conflict.json",
            "update_charger_priority_invalid_value.json",
            "update_charger_priority_no_site.json",
            "update_charger_priority_success.json",
            "update_site_settings_conflict.json",
            "update_site_settings_invalid_value.json",
            "update_site_settings_not_permitted.json",
            "update_phase_switching_conflict.json",
            "update_phase_switching_invalid_value.json",
            "update_phase_switching_not_writable.json",
            "update_phase_switching_success.json",
            "update_site_settings_success.json",
            "update_vehicle_conflict.json",
            "update_vehicle_refused.json",
            "update_vehicle_success.json",
            "update_vehicle_unknown_vehicle.json",
        ]
    )
