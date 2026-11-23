"""The charger webhook's site actions (`api/site_webhook.py`): each answers what its WebSocket twin answers for the
same moment, with `ok` and the routing `action` beside it; the everyday writes go through, the control opt-ins never.

Every case goes through Home Assistant's real `/api/webhook/<id>` route on the pilot's site. The answers are pinned
as contract fixtures under `tests/fixtures/webhook/` (`SPOTNAV_WRITE_FIXTURES=1` rewrites them); the paired app's
tests read the same files.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.api import house as house_api
from custom_components.spotnav.api.battery_control import websocket_battery_control
from custom_components.spotnav.api.dashboard import CAPABILITY_KEYS
from custom_components.spotnav.api.heat import websocket_get_heat, websocket_update_heat
from custom_components.spotnav.api.heat_history import websocket_get_heat_history
from custom_components.spotnav.api.house import websocket_get_house
from custom_components.spotnav.api.house_history import websocket_get_house_history
from custom_components.spotnav.api.house_loads import websocket_get_house_loads
from custom_components.spotnav.api.house_now import websocket_get_house_now
from custom_components.spotnav.api.plan import websocket_get_plan
from custom_components.spotnav.api.sessions import websocket_get_sessions
from custom_components.spotnav.api.grid_tariff import without_power_level
from custom_components.spotnav.api.site_view import websocket_get_site_settings_view
from custom_components.spotnav.api.site_webhook import SITE_ACTIONS
from custom_components.spotnav.battery import gate
from custom_components.spotnav.const import CONF_HEAT
from .helpers import answers_of

from .test_demand_site import pilot_site, run
from .test_webhook_parity import _pin, _pinned, post
from .world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")

HOOK = "webhook-entry_a"
DAY = "2026-09-22"


class _Connection:
    def __init__(self, admin: bool) -> None:
        self.answers: list[dict[str, Any]] = []
        self.user = SimpleNamespace(is_admin=admin)

    def send_result(self, message_id: int, result: Any) -> None:
        self.answers.append({"success": True, "result": json.loads(json.dumps(result))})

    def send_error(self, message_id: int, code: str, message: str) -> None:
        self.answers.append({"success": False, "error": {"code": code, "message": message}})


async def socket(hass: HomeAssistant, command: Any, admin: bool = True, **request: Any) -> dict[str, Any]:
    """The WebSocket command's answer for the pilot's charger, as the socket would carry it."""
    connection = _Connection(admin)
    command(hass, connection, {"id": 1, "type": "x", "api_version": 1, "charger_id": "entry_a", **request})
    (answer,) = await answers_of(hass, connection)
    return answer


async def result(hass: HomeAssistant, command: Any, admin: bool = True, **request: Any) -> dict[str, Any]:
    answer = await socket(hass, command, admin, **request)
    assert answer["success"], answer
    return answer["result"]


def _heat_sensors(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.kok", "21.3", {"device_class": "temperature", "unit_of_measurement": "°C"})
    hass.states.async_set("sensor.ovanvaning", "20.5", {"unit_of_measurement": "°C"})
    hass.states.async_set("switch.larm_rele_1", "off")


async def _site(hass: HomeAssistant, freezer: Any) -> Any:
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)
    _heat_sensors(hass)
    written = await result(
        hass, websocket_update_heat, changes={"indoor_sensors": ["sensor.kok", "sensor.ovanvaning"]}
    )
    assert written["ok"], written
    return site


#: Each read: its action, its WebSocket twin, the request both take, and whether the twin is asked as an administrator.
READS: tuple[tuple[str, Any, dict[str, Any], bool], ...] = (
    ("get_house", websocket_get_house, {}, True),
    ("get_house_now", websocket_get_house_now, {}, True),
    ("get_house_history", websocket_get_house_history, {"period": "day", "date": DAY}, True),
    ("get_house_loads", websocket_get_house_loads, {"period": "day", "date": DAY, "kind": "house"}, True),
    ("get_heat", websocket_get_heat, {}, False),
    ("get_heat_history", websocket_get_heat_history, {"period": "month", "date": DAY}, True),
    ("get_plan", websocket_get_plan, {}, False),
    ("battery_control", websocket_battery_control, {}, True),
)


async def test_every_read_answers_what_its_websocket_twin_answers(
    hass: HomeAssistant, freezer, hass_client_no_auth
) -> None:
    await _site(hass, freezer)
    client = await hass_client_no_auth()
    for action, command, request, as_admin in READS:
        expected = await result(hass, command, as_admin, **request)
        status, body = await post(client, HOOK, {"action": action, **request})
        assert status == 200, (action, body)
        assert body == {**expected, "ok": True, "action": action}, action
        # A body's `site_id` or `charger_id` is never read: the webhook is its own charger's.
        status, again = await post(client, HOOK, {"action": action, "site_id": "other", "charger_id": "x", **request})
        assert (status, again) == (200, body), action
        _pinned(f"{action}_success.json", _pin(body))
    # The heat card over the webhook is the card of someone who is no administrator: no entity choices.
    status, heat = await post(client, HOOK, {"action": "get_heat"})
    assert heat["writable"] is False and heat["choices"] is None
    assert heat["today"] is None or set(heat["today"]["money"]) == {"currency", "major_unit", "minor_unit"}


async def test_the_site_settings_view_is_the_administrators_bar_load_balancing(
    hass: HomeAssistant, freezer, hass_client_no_auth
) -> None:
    await _site(hass, freezer)
    client = await hass_client_no_auth()
    expected = await result(hass, websocket_get_site_settings_view)
    status, body = await post(client, HOOK, {"action": "get_site_settings_view"})
    assert status == 200
    assert expected["writable"] is True and expected["site"]["active_control"]["writable"] is True
    # As the webhook's dashboard: what its writes can change is writable, load balancing is not.
    expected["site"]["active_control"]["writable"] = False
    # The priority list is written over the WebSocket only: the apps read it.
    expected["priority"]["writable"] = False
    # An app that does not read the power level never meets its keys (its decoder refuses a demand block with them).
    reading = {**expected, "ok": True, "action": "get_site_settings_view"}
    prices = expected["price_settings"]
    expected["price_settings"] = {**prices, "grid_tariff": without_power_level(prices["grid_tariff"])}
    assert body == {**expected, "ok": True, "action": "get_site_settings_view"}
    assert "power_level" not in body["price_settings"]["grid_tariff"]["demand"]
    _pinned("get_site_settings_view_success.json", _pin(body))
    status, body = await post(client, HOOK, {"action": "get_site_settings_view", "reads": ["power_level"]})
    assert status == 200 and body == reading
    assert body["price_settings"]["grid_tariff"]["demand"]["power_level"] is None


async def test_refusals_carry_the_commands_own_codes(hass: HomeAssistant, freezer, hass_client_no_auth) -> None:
    site = await _site(hass, freezer)
    await setup_charger_and_site(hass, "entry_b", site=False)
    client = await hass_client_no_auth()
    for action in SITE_ACTIONS:
        status, body = await post(client, HOOK, {"action": action, "api_version": 2})
        assert (status, body) == (400, {"ok": False, "error": "spotnav_unsupported_api_version", "action": action})
        status, body = await post(client, HOOK, {"action": action, "api_version": True})
        assert status == 400 and body["error"] == "spotnav_unsupported_api_version", action
        # A charger in no site has no house, heat or battery to answer for.
        status, body = await post(client, "webhook-entry_b", {"action": action, "changes": {"target_c": 21}})
        assert status == 400 and body["ok"] is False and body["error"] == "spotnav_no_site", (action, body)
        assert body["action"] == action
        if action == "get_house":
            _pinned("get_house_no_site.json", body)
    cases = (
        ("get_house", {"date": "2031-01-01"}, "spotnav_invalid_date"),
        ("get_house_history", {"period": "week"}, "spotnav_invalid_date"),
        ("get_house_history", {}, "spotnav_invalid_date"),
        ("get_house_loads", {"kind": "car"}, "spotnav_invalid_kind"),
        ("get_heat_history", {"period": "day", "date": "tomorrow"}, "spotnav_invalid_date"),
    )
    for action, request, code in cases:
        status, body = await post(client, HOOK, {"action": action, **request})
        assert (status, body) == (400, {"ok": False, "error": code, "action": action}), (action, request)
    status, body = await post(client, HOOK, {"action": "get_house_history", "period": "week"})
    _pinned("get_house_history_invalid_date.json", body)
    assert await hass.config_entries.async_unload(site.entry_id)
    await hass.async_block_till_done()
    for action in ("get_house", "get_house_now", "get_heat", "get_plan", "get_site_settings_view", "battery_control"):
        status, body = await post(client, HOOK, {"action": action})
        assert status == 400 and body["error"] == "spotnav_no_site", (action, body)


async def test_the_dial_and_home_or_away_are_written_as_the_command_writes_them(
    hass: HomeAssistant, freezer, hass_client_no_auth
) -> None:
    site = await _site(hass, freezer)
    client = await hass_client_no_auth()
    status, heat = await post(client, HOOK, {"action": "get_heat"})
    before = heat["config"]
    status, body = await post(
        client, HOOK, {"action": "update_heat", "changes": {"target_c": 21.5}, "expected": {"target_c": before["target_c"]}}
    )
    assert status == 200 and body["ok"] is True and body["error"] is None and body["action"] == "update_heat"
    assert body["config"]["target_c"] == 21.5 and site.data[CONF_HEAT]["target_c"] == 21.5
    _pinned("update_heat_success.json", _pin(body))
    # The same write over the socket (nothing left to change) answers the same envelope.
    twin = await result(hass, websocket_update_heat, False, changes={"target_c": 21.5}, expected={"target_c": 21.5})
    assert body == {**twin, "action": "update_heat"}

    status, body = await post(
        client, HOOK, {"action": "update_heat", "changes": {"mode": "away", "away_until": "2026-09-25T16:00:00+02:00"}}
    )
    assert status == 200 and body["ok"] is True, body
    assert body["config"]["mode"] == "away" and body["config"]["away_until"] is not None
    _pinned("update_heat_away.json", _pin(body))
    status, body = await post(client, HOOK, {"action": "update_heat", "changes": {"mode": "home"}})
    assert status == 200 and body["config"]["mode"] == "home"

    # A stale `expected` is a conflict and writes nothing.
    stored = dict(site.data[CONF_HEAT])
    status, body = await post(
        client, HOOK, {"action": "update_heat", "changes": {"target_c": 20.0}, "expected": {"target_c": 19.0}}
    )
    assert status == 409 and body["error"] == "spotnav_conflict" and body["config"]["target_c"] == 21.5
    _pinned("update_heat_conflict.json", _pin(body))
    # Out of the dial's range, off its step, an unknown mode, a past return, an unknown field, no changes.
    for changes, field in (
        ({"target_c": 40}, "target_c"),
        ({"target_c": "warm"}, "target_c"),
        ({"mode": "party"}, "mode"),
        ({"mode": "away", "away_until": "2020-01-01T00:00:00+00:00"}, "away_until"),
        ({"dial": 21}, "dial"),
        ({}, "changes"),
        ("x", "changes"),
    ):
        status, body = await post(client, HOOK, {"action": "update_heat", "changes": changes})
        assert status == 400 and body["ok"] is False and body["error"] == "spotnav_invalid_value", (changes, body)
        assert [item["field"] for item in body["field_errors"]] == [field], (changes, body)
        if changes == {"target_c": 40}:
            _pinned("update_heat_invalid_value.json", _pin(body))
    assert dict(site.data[CONF_HEAT]) == stored


async def test_the_cooler_night_and_each_zones_offset_are_written_but_never_which_zones_there_are(
    hass: HomeAssistant, freezer, hass_client_no_auth
) -> None:
    site = await _site(hass, freezer)
    hass.states.async_set("climate.golvvarme", "heat", {"current_temperature": 20.4, "temperature": 17, "hvac_modes": ["heat"]})
    hass.states.async_set("climate.luft", "heat", {"current_temperature": 21.5, "temperature": 21, "hvac_modes": ["heat"]})
    hass.states.async_set("climate.gast", "heat", {"current_temperature": 19.0, "temperature": 19, "hvac_modes": ["heat"]})
    zones = [{"entity_id": "climate.golvvarme"}, {"entity_id": "climate.luft", "offset_c": -1.0}]
    assert (await result(hass, websocket_update_heat, changes={"zones": zones}))["ok"]
    client = await hass_client_no_auth()

    night = {"on": True, "delta_c": 1.5, "start": "22:30", "end": "05:30"}
    status, body = await post(
        client, HOOK, {"action": "update_heat", "changes": {"night": night}, "expected": {"night": {"on": False}}}
    )
    assert status == 409 and body["error"] == "spotnav_conflict", "the stored night has its defaults beside `on`"
    status, heat = await post(client, HOOK, {"action": "get_heat"})
    status, body = await post(
        client, HOOK, {"action": "update_heat", "changes": {"night": night}, "expected": {"night": heat["config"]["night"]}}
    )
    assert status == 200 and body["ok"] is True and body["config"]["night"] == night, body
    assert site.data[CONF_HEAT]["night"] == night
    _pinned("update_heat_night.json", _pin(body))
    twin = await result(hass, websocket_update_heat, changes={"night": night}, expected={"night": night})
    assert body == {**twin, "action": "update_heat"}
    for bad in ({"on": True, "delta_c": 5}, {"on": True, "delta_c": 0.7}, {"on": True, "start": "25:00"}, {"on": "yes"}, "on"):
        status, body = await post(client, HOOK, {"action": "update_heat", "changes": {"night": bad}})
        assert status == 400 and body["error"] == "spotnav_invalid_value", (bad, body)
        assert [item["field"] for item in body["field_errors"]] == ["night"], (bad, body)
    assert site.data[CONF_HEAT]["night"] == night

    status, body = await post(
        client,
        HOOK,
        {"action": "update_heat", "changes": {"away_delta_c": 4.5}, "expected": {"away_delta_c": heat["config"]["away_delta_c"]}},
    )
    assert status == 200 and body["ok"] is True and body["config"]["away_delta_c"] == 4.5, body
    assert site.data[CONF_HEAT]["away_delta_c"] == 4.5
    _pinned("update_heat_away_delta.json", _pin(body))
    twin = await result(hass, websocket_update_heat, changes={"away_delta_c": 4.5}, expected={"away_delta_c": 4.5})
    assert body == {**twin, "action": "update_heat"}
    status, body = await post(
        client, HOOK, {"action": "update_heat", "changes": {"away_delta_c": 5.0}, "expected": {"away_delta_c": 3.0}}
    )
    assert status == 409 and body["error"] == "spotnav_conflict", body
    for bad in (0.5, 6.5, 2.2, "warm", True):
        status, body = await post(client, HOOK, {"action": "update_heat", "changes": {"away_delta_c": bad}})
        assert status == 400 and body["error"] == "spotnav_invalid_value", (bad, body)
        assert [item["field"] for item in body["field_errors"]] == ["away_delta_c"], (bad, body)
    assert site.data[CONF_HEAT]["away_delta_c"] == 4.5

    offsets = [{"entity_id": "climate.golvvarme", "offset_c": 0.5}, {"entity_id": "climate.luft", "offset_c": -2.0}]
    status, body = await post(
        client, HOOK, {"action": "update_heat", "changes": {"zones": offsets}, "expected": {"zones": heat["config"]["zones"]}}
    )
    kept = [{**item, "control": False} for item in offsets]
    assert status == 200 and body["ok"] is True and body["config"]["zones"] == kept, body
    _pinned("update_heat_zone_offsets.json", _pin(body))
    twin = await result(hass, websocket_update_heat, changes={"zones": offsets}, expected={"zones": offsets})
    assert body == {**twin, "action": "update_heat"}
    # An offset out of range or off its step is the judge's refusal.
    for offset in (4.0, 0.3, "warm"):
        changed = [{"entity_id": "climate.golvvarme", "offset_c": offset}, offsets[1]]
        status, body = await post(client, HOOK, {"action": "update_heat", "changes": {"zones": changed}})
        assert status == 400 and [item["field"] for item in body["field_errors"]] == ["zones"], (offset, body)
    # Which entities are zones is configuration: another zone, one fewer, another order, none.
    for changed in (
        [*offsets, {"entity_id": "climate.gast", "offset_c": 0.0}],
        offsets[:1],
        offsets[::-1],
        [{"entity_id": "climate.gast", "offset_c": 0.5}, offsets[1]],
        [],
        None,
        # Letting SpotNav set a thermostat is an administrator's opt-in, never the webhook's.
        [{**offsets[0], "control": True}, offsets[1]],
    ):
        status, body = await post(client, HOOK, {"action": "update_heat", "changes": {"zones": changed}})
        assert status == 403 and body["error"] == "spotnav_not_permitted_over_webhook", (changed, body)
        assert site.data[CONF_HEAT]["zones"] == kept, changed
    _pinned("update_heat_zones_not_permitted.json", _pin(body))

    # Each indoor sensor's offset against the house temperature: sparse, as stored (0 or left out is none).
    sensors = list(site.data[CONF_HEAT]["indoor_sensors"])
    assert sensors == ["sensor.kok", "sensor.ovanvaning"]
    wanted = [{"entity_id": "sensor.kok", "offset_c": 0.5}, {"entity_id": "sensor.ovanvaning", "offset_c": -1.5}]
    status, body = await post(
        client, HOOK, {"action": "update_heat", "changes": {"indoor_offsets": wanted}, "expected": {"indoor_offsets": []}}
    )
    assert status == 200 and body["ok"] is True and body["config"]["indoor_offsets"] == wanted, body
    _pinned("update_heat_indoor_offsets.json", _pin(body))
    twin = await result(hass, websocket_update_heat, changes={"indoor_offsets": wanted}, expected={"indoor_offsets": wanted})
    assert body == {**twin, "action": "update_heat"}
    status, body = await post(
        client, HOOK, {"action": "update_heat", "changes": {"indoor_offsets": [{"entity_id": "sensor.kok", "offset_c": 0}, wanted[1]]}}
    )
    assert status == 200 and body["config"]["indoor_offsets"] == wanted[1:], "an offset of 0 is none"
    for offset in (4.0, 0.3, "warm"):
        changed = [{"entity_id": "sensor.kok", "offset_c": offset}]
        status, body = await post(client, HOOK, {"action": "update_heat", "changes": {"indoor_offsets": changed}})
        assert status == 400 and [item["field"] for item in body["field_errors"]] == ["indoor_offsets"], (offset, body)
    # A sensor that is not one of the indoor sensors is never brought in, alone or beside one that is.
    hass.states.async_set("sensor.sovrum", "19.8", {"device_class": "temperature", "unit_of_measurement": "°C"})
    for changed in (
        [{"entity_id": "sensor.sovrum", "offset_c": 1.0}],
        [wanted[0], {"entity_id": "sensor.sovrum", "offset_c": 1.0}],
        [{"entity_id": "switch.larm_rele_1", "offset_c": 1.0}],
    ):
        status, body = await post(client, HOOK, {"action": "update_heat", "changes": {"indoor_offsets": changed}})
        assert status == 403 and body["error"] == "spotnav_not_permitted_over_webhook", (changed, body)
        assert site.data[CONF_HEAT]["indoor_offsets"] == wanted[1:] and site.data[CONF_HEAT]["indoor_sensors"] == sensors
    _pinned("update_heat_indoor_offsets_not_permitted.json", _pin(body))
    status, body = await post(client, HOOK, {"action": "update_heat", "changes": {"indoor_offsets": None}})
    assert status == 200 and body["config"]["indoor_offsets"] == [] and body["config"]["indoor_sensors"] == sensors


async def test_the_heat_control_opt_in_and_the_configuration_are_never_written_over_the_webhook(
    hass: HomeAssistant, freezer, hass_client_no_auth
) -> None:
    site = await _site(hass, freezer)
    client = await hass_client_no_auth()
    stored = dict(site.data[CONF_HEAT])
    for changes in (
        {"control_enabled": True},
        {"control_enabled": False},
        {"target_c": 22.0, "control_enabled": True},
        {"control_path": "relay", "control_entity": "switch.larm_rele_1"},
        {"control_entity": "switch.larm_rele_1"},
        {"control_entity_b": "switch.larm_rele_1"},
        {"hot_water_entity": "switch.larm_rele_1"},
        {"indoor_sensors": ["sensor.kok"]},
        # A sensor's offset is allowed, for the indoor sensors chosen only, and never beside the sensors themselves.
        {"indoor_offsets": [{"entity_id": "sensor.cpu_load", "offset_c": 0.5}]},
        {"indoor_offsets": [{"entity_id": "sensor.kok", "offset_c": 0.5}, {"entity_id": "sensor.sovrum", "offset_c": 1.0}]},
        {"indoor_offsets": [{"entity_id": "sensor.kok", "offset_c": 0.5}], "indoor_sensors": ["sensor.kok"]},
        {"outdoor_sensor": "sensor.kok"},
        {"heat_meter_entity": "sensor.kok"},
        # The cooler night is allowed, but not beside the opt-in or a control path.
        {"night": {"on": True}, "control_enabled": True},
        {"night": {"on": True}, "control_path": "relay"},
        # A zone's offset is allowed, which entities are zones is not.
        {"zones": [{"entity_id": "climate.golvvarme", "offset_c": 1.0}]},
    ):
        status, body = await post(client, HOOK, {"action": "update_heat", "changes": changes})
        assert status == 403, (changes, body)
        assert body["ok"] is False and body["error"] == "spotnav_not_permitted_over_webhook", (changes, body)
        assert body["action"] == "update_heat" and body["field_errors"] == []
        assert dict(site.data[CONF_HEAT]) == stored, changes
    status, body = await post(client, HOOK, {"action": "update_heat", "changes": {"control_enabled": True}})
    assert status == 403
    _pinned("update_heat_not_permitted.json", _pin(body))
    assert site.data[CONF_HEAT].get("control_enabled", False) is False


async def test_the_battery_mode_is_written_and_the_battery_opt_in_never(
    hass: HomeAssistant, freezer, hass_client_no_auth
) -> None:
    site = await _site(hass, freezer)
    client = await hass_client_no_auth()
    status, body = await post(client, HOOK, {"action": "battery_control"})
    assert status == 200 and body["status"]["options"]["battery_mode"] == "balanced"
    for mode in ("max_profit", "self_consumption", "balanced", "max_profit"):
        status, body = await post(client, HOOK, {"action": "battery_control", "changes": {"battery_mode": mode}})
        assert status == 200 and body["ok"] is True and body["error"] is None, body
        assert body["status"]["options"]["battery_mode"] == mode and site.data[gate.CONF_MODE] == mode
    _pinned("battery_control_mode.json", _pin(body))
    twin = await result(hass, websocket_battery_control, changes={"battery_mode": "max_profit"})
    assert body == {**twin, "action": "battery_control"}

    kept = dict(site.data)
    for changes in (
        {gate.CONF_ENABLED: True},
        {gate.CONF_ENABLED: False},
        {gate.CONF_SHADOW_ONLY: False},
        {gate.CONF_AI_TAKEOVER_ACK: True},
        {gate.CONF_IMPORT_LIMIT_ACTS: True},
        {gate.CONF_MODE: "balanced", gate.CONF_ENABLED: True},
    ):
        status, body = await post(client, HOOK, {"action": "battery_control", "changes": changes})
        assert status == 403, (changes, body)
        assert body == {
            "api_version": 1,
            "ok": False,
            "error": "spotnav_not_permitted_over_webhook",
            "status": None,
            "action": "battery_control",
        }, changes
        assert dict(site.data) == kept, changes
    _pinned("battery_control_not_permitted.json", body)
    for changes in ({gate.CONF_MODE: "party"}, {gate.CONF_MODE: True}, {"battery_control_allow_sell": True}, {}, "x"):
        status, body = await post(client, HOOK, {"action": "battery_control", "changes": changes})
        assert status == 400 and body["error"] == "spotnav_invalid_value" and body["status"] is None, (changes, body)
        assert dict(site.data) == kept, changes
    _pinned("battery_control_invalid_value.json", body)
    status, body = await post(client, HOOK, {"action": "battery_control"})
    assert body["status"]["options"][gate.CONF_ENABLED] is False


async def test_a_charger_says_whether_its_webhook_answers_for_a_site(hass: HomeAssistant, freezer, hass_client_no_auth) -> None:
    await _site(hass, freezer)
    await setup_charger_and_site(hass, "entry_b", site=False)
    client = await hass_client_no_auth()
    flags = ("get_house", "get_heat", "battery_control", "get_site_settings_view")
    assert CAPABILITY_KEYS[-4:] == flags
    status, in_site = await post(client, HOOK, {"action": "dashboard", "api_version": 1})
    assert status == 200 and [in_site["charger"]["capabilities"][flag] for flag in flags] == [True] * 4
    status, alone = await post(client, "webhook-entry_b", {"action": "dashboard", "api_version": 1})
    assert status == 200 and [alone["charger"]["capabilities"][flag] for flag in flags] == [False] * 4


async def test_the_sessions_answer_has_the_plug_in_saving_for_an_app_that_reads_it(
    hass: HomeAssistant, freezer, hass_client_no_auth
) -> None:
    await _site(hass, freezer)
    client = await hass_client_no_auth()
    expected = await result(hass, websocket_get_sessions)
    assert "plug_in_saving" in expected["this_month"]
    status, body = await post(client, HOOK, {"action": "sessions", "reads": ["plug_in_saving"]})
    assert status == 200 and body == {**expected, "ok": True, "action": "sessions"}
    _pinned("sessions_plug_in_saving.json", _pin(body))
    # An app released before it gets the answer it knows.
    status, plain = await post(client, HOOK, {"action": "sessions"})
    assert status == 200 and "plug_in_saving" not in json.dumps(plain)
    _pinned("sessions_success.json", _pin(plain))
    # A CSV has no savings either way.
    status, csv = await post(client, HOOK, {"action": "sessions", "format": "csv", "reads": ["plug_in_saving"]})
    assert status == 200 and csv["format"] == "csv"


async def test_polling_house_now_reads_the_recorder_at_most_once_per_five_minute_row(
    hass: HomeAssistant, freezer, hass_client_no_auth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app polls `get_house_now` every 10 s. A site with sun and heat but no power sensor for them takes the last
    period of the day's energy flow: that read is kept per 5-minute row, so a poll costs no recorder query."""
    site = await _site(hass, freezer)
    client = await hass_client_no_auth()
    reads: list[Any] = []

    async def day_flow(*args: Any, **kwargs: Any) -> Any:
        reads.append(args[2])
        return None

    async def prefs(_hass: HomeAssistant) -> dict[str, Any]:
        return {"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.sun_energy"}]}

    monkeypatch.setattr(house_api, "async_day_flow", day_flow)
    from custom_components.spotnav.api import house_now as house_now_api

    monkeypatch.setattr(house_now_api, "async_energy_prefs", prefs)
    house_api.forget_cached_flow(hass, site.entry_id)
    for _ in range(30):
        status, body = await post(client, HOOK, {"action": "get_house_now"})
        assert status == 200 and body["has"]["sun"] is True and body["sun_kw"] is None
    assert len(reads) == 1, "one read for thirty polls inside one 5-minute row"
    freezer.tick(300)
    status, body = await post(client, HOOK, {"action": "get_house_now"})
    assert status == 200 and len(reads) == 2
    # What a poll costs in the handler itself, in processor time (the wall clock is frozen here); printed with `-s`,
    # and the bound is generous for a slow machine.
    handler = SITE_ACTIONS["get_house_now"]
    entry = hass.config_entries.async_get_entry("entry_a")
    started = time.process_time()
    for _ in range(200):
        response = await handler(hass, entry, {})
    per_call_ms = (time.process_time() - started) * 1000 / 200
    print(f"get_house_now: {per_call_ms:.3f} ms per call, {len(response.body)} bytes")
    assert len(reads) == 2 and per_call_ms < 20 and len(response.body) < 2048


# -- a charge that does not fit under the import target: the plan's car lane, the dashboard and the two choices


def _id_pins(world: Any) -> dict[str, str]:
    return {world.halo.charger.entry_id: "<charger_id>", world.halo.controller.entry_id: "<site_id>"}


async def test_the_does_not_fit_block_and_its_choices_are_the_apps(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_client_no_auth
) -> None:
    """The tight night of `tests/test_import_target_hold.py`, with prices held to the end of tomorrow so a later
    departure is proposed: the block in the plan's car lane and in the dashboard (for an app that reads it) is the
    card's, and "Take what fits" and "Later departure" are the app's to press."""
    from freezegun import freeze_time

    from custom_components.spotnav.site.does_not_fit import BLOCK_KEYS, CHOICE_KEYS

    from .test_import_target_hold import _HOUR, _tight_night
    from .test_later_departure import _prices_held, _replans

    _prices_held(monkeypatch)
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "whdnf")
        charger = world.halo.charger.entry_id
        hook = world.halo.charger.data["webhook_id"]
        replans = _replans(monkeypatch, hass, charger)
        await world.run(2)
        client = await hass_client_no_auth()
        pins = _id_pins(world)

        expected = await result(hass, websocket_get_plan, charger_id=charger)
        status, plan = await post(client, hook, {"action": "get_plan"})
        assert status == 200 and plan == {**expected, "ok": True, "action": "get_plan"}
        (car,) = [load for load in plan["loads"] if load["kind"] == "car"]
        block = car["does_not_fit"]
        assert block is not None and tuple(sorted(block)) == tuple(sorted(BLOCK_KEYS))
        assert block["chosen"] is None and block["set_kw"] == 6.0 and block["ceiling"] == "import_target"
        assert [choice["code"] for choice in block["choices"]] == ["takes_what_fits", "later", "accept_peak"]
        assert all(tuple(sorted(choice)) == tuple(sorted(CHOICE_KEYS)) for choice in block["choices"])
        later = block["choices"][1]["departure"]
        assert later is not None
        _pinned("get_plan_does_not_fit.json", _pin(plan, pins))

        # The dashboard carries the same block for an app that reads it, and none for one released before it.
        status, dashboard = await post(client, hook, {"action": "dashboard", "api_version": 1, "reads": ["does_not_fit"]})
        assert status == 200 and dashboard["does_not_fit"] == block
        _pinned("dashboard_does_not_fit.json", _pin(dashboard, pins))
        status, older = await post(client, hook, {"action": "dashboard", "api_version": 1})
        assert status == 200 and "does_not_fit" not in older

        # "Take what fits": held for this departure; the block says it chosen.
        status, body = await post(client, hook, {"action": "take_what_fits", "api_version": 2})
        assert (status, body["error"]) == (400, "spotnav_unsupported_api_version")
        _pinned("take_what_fits_unsupported_version.json", body)
        status, body = await post(client, hook, {"action": "take_what_fits"})
        assert (status, body) == (200, {"api_version": 1, "ok": True, "error": None, "action": "take_what_fits"})
        _pinned("take_what_fits_success.json", body)
        await world.run(2)
        status, plan = await post(client, hook, {"action": "get_plan"})
        (car,) = [load for load in plan["loads"] if load["kind"] == "car"]
        assert car["does_not_fit"]["chosen"] == "takes_what_fits"
        assert [choice["code"] for choice in car["does_not_fit"]["choices"]] == ["later", "accept_peak"]
        _pinned("get_plan_takes_what_fits.json", _pin(plan, pins))

        # "Later departure": this departure moved to the hour the block proposed, the plan made again for it.
        status, body = await post(client, hook, {"action": "later_departure"})
        assert (status, body) == (200, {"api_version": 1, "ok": True, "error": None, "action": "later_departure"})
        _pinned("later_departure_success.json", body)
        await hass.async_block_till_done()
        assert replans == ["recalculate"]
        moved = world.halo.controller.later_departure_for(charger)
        assert moved is not None and moved[1].isoformat() == dt_util.parse_datetime(later).astimezone(moved[1].tzinfo).isoformat()


async def test_the_does_not_fit_choices_are_refused_with_the_commands_codes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_client_no_auth
) -> None:
    from freezegun import freeze_time

    from .test_import_target_hold import _HOUR, _tight_night

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        # No prices held past the departure: nothing later is proposed, nor moved to.
        world = await _tight_night(hass, monkeypatch, frozen, "whdnfno")
        hook = world.halo.charger.data["webhook_id"]
        await world.run(2)
        client = await hass_client_no_auth()
        status, plan = await post(client, hook, {"action": "get_plan"})
        (car,) = [load for load in plan["loads"] if load["kind"] == "car"]
        assert car["does_not_fit"]["choices"][1] == {
            "code": "later", "kwh": None, "peak_kw": None, "cost": None, "departure": None
        }
        status, body = await post(client, hook, {"action": "later_departure"})
        assert (status, body) == (
            400, {"api_version": 1, "ok": False, "error": "spotnav_no_later_departure", "action": "later_departure"}
        )
        _pinned("later_departure_no_later_departure.json", body)
        # No departure ahead: neither choice has one to act on.
        world.halo.cc.plan = None
        for action in ("take_what_fits", "later_departure"):
            status, body = await post(client, hook, {"action": action})
            assert (status, body) == (
                400, {"api_version": 1, "ok": False, "error": "spotnav_no_departure", "action": action}
            ), action
            _pinned(f"{action}_no_departure.json", body)
