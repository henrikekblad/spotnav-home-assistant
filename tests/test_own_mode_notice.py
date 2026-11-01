"""The charger's own mode that works against SpotNav: a live notice on the card while it is on, and the one
guarded write (`spotnav/fix_own_mode`) an administrator may make from it."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service, MockConfigEntry

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.dashboard import serialize_dashboard
from custom_components.spotnav.api.webhook import _for_app
from custom_components.spotnav.const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_CHARGER
from custom_components.spotnav.execution.charger_entities import fix_value
from custom_components.spotnav.execution.charger_profiles import OwnModeRule
from custom_components.spotnav.flows.charger_detection import detect_charger

from .charger_helpers import detected_config
from .charger_shapes import E, register_shape, Shape, SHAPES
from .relay import serve, StubTransport, TODAY
from .world import admin, go_auto, non_admin, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

LATE = "2026-09-22 20:30:00"
SMART = "select.peblar_smart_charging"
OPTIONS = {"options": ["default", "pure_solar", "comfort_solar"]}
#: Easee with its own smart charging switch, and not charging (a disabled charger is said only then).
EASEE = replace(
    SHAPES["easee"],
    entities=tuple(
        replace(item, state="ready_to_charge") if item.key == "status" else item for item in SHAPES["easee"].entities
    )
    + (E("switch", "EH123_smart_charging", "smart_charging", "off"),),
)


async def _charger(
    hass: HomeAssistant, platform: str, shape: Shape | None = None, transport: StubTransport | None = None
) -> MockConfigEntry:
    """A detected charger, loaded; with `transport`, also in Auto with today's prices, so its notices show."""
    ids = register_shape(hass, shape or SHAPES[platform])
    found = detect_charger(hass, ids["device_id"])
    data = {CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER, "webhook_id": f"hook-{platform}", **detected_config(found)}
    entry = MockConfigEntry(domain=DOMAIN, entry_id=f"det_{platform}", title=platform, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if transport is not None:
        serve(transport, days=(TODAY,), listed=(TODAY,))
        await go_auto(hass, entry.entry_id)
    return entry


def _lines(hass: HomeAssistant, entry: MockConfigEntry, code: str) -> list[dict[str, Any]]:
    body = serialize_dashboard(dashboard_api.capture_dashboard(hass, entry), can_act=True)
    return [line for line in body["status"]["lines"] if line["code"] == code]


def _fix(entry_id: str, entity_id: object, value: object) -> dict[str, Any]:
    return {"type": "spotnav/fix_own_mode", "api_version": 1, "charger_id": entry_id, "entity_id": entity_id, "value": value}


@freeze_time(LATE)
async def test_peblars_own_smart_charging_is_a_notice_until_it_is_default_again(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    entry = await _charger(hass, "peblar", transport=transport)
    assert _lines(hass, entry, "charger_own_mode") == []

    hass.states.async_set(SMART, "pure_solar", OPTIONS)
    assert _lines(hass, entry, "charger_own_mode") == [
        {
            "code": "charger_own_mode",
            "params": {"entity": SMART, "label": "smart charging", "state": "pure_solar", "value": "default"},
        }
    ]

    hass.states.async_set(SMART, "default", OPTIONS)
    assert _lines(hass, entry, "charger_own_mode") == []


async def test_an_administrator_sets_it_back_and_the_write_is_logged(
    hass: HomeAssistant, hass_ws_client, caplog: pytest.LogCaptureFixture
) -> None:
    entry = await _charger(hass, "peblar")
    hass.states.async_set(SMART, "pure_solar", OPTIONS)
    calls = async_mock_service(hass, "select", "select_option")
    caplog.set_level(logging.INFO, logger="custom_components.spotnav.api.own_mode")

    answer = (await ws_call(await admin(hass, hass_ws_client), _fix(entry.entry_id, SMART, "default")))["result"]

    assert answer == {"api_version": 1, "ok": True, "error": None}
    assert [call.data for call in calls] == [{"entity_id": SMART, "option": "default"}]
    assert any(SMART in record.getMessage() and record.levelno == logging.INFO for record in caplog.records)


async def test_nothing_but_the_conflicts_own_fix_is_ever_written(hass: HomeAssistant, hass_ws_client) -> None:
    """Another entity of the charger, another device's entity, another value, and a conflict that has ended:
    each refused with one code, nothing written."""
    entry = await _charger(hass, "peblar")
    other = Shape(
        "peblar",
        "PB999",
        (E("select", "PB999_smart_charging", "smart_charging", "pure_solar", OPTIONS, translation_key=True),),
        {},
    )
    register_shape(hass, other, suffix="_2")
    elsewhere = next(state.entity_id for state in hass.states.async_all("select") if state.entity_id != SMART)
    hass.states.async_set(elsewhere, "pure_solar", OPTIONS)
    hass.states.async_set(SMART, "pure_solar", OPTIONS)
    selects = async_mock_service(hass, "select", "select_option")
    switches = async_mock_service(hass, "switch", "turn_off")
    numbers = async_mock_service(hass, "number", "set_value")
    socket = await admin(hass, hass_ws_client)

    for entity_id, value in (
        ("number.peblar_charge_current_limit", "default"),
        ("switch.peblar_charge", "off"),
        (elsewhere, "default"),
        (SMART, "pure_solar"),
        (SMART, "comfort_solar"),
        (SMART, None),
        (None, "default"),
    ):
        refused = (await ws_call(socket, _fix(entry.entry_id, entity_id, value)))["result"]
        assert refused == {"api_version": 1, "ok": False, "error": "spotnav_no_conflict"}, entity_id

    hass.states.async_set(SMART, "default", OPTIONS)
    gone = (await ws_call(socket, _fix(entry.entry_id, SMART, "default")))["result"]
    assert gone["error"] == "spotnav_no_conflict"
    assert selects == [] and switches == [] and numbers == []

    unknown = (await ws_call(socket, _fix("no-such-charger", SMART, "default")))["result"]
    assert unknown == {"api_version": 1, "ok": False, "error": "spotnav_unknown_charger"}


async def test_only_an_administrator_may_set_it(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    entry = await _charger(hass, "peblar")
    hass.states.async_set(SMART, "pure_solar", OPTIONS)
    calls = async_mock_service(hass, "select", "select_option")

    socket = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    denied = (await ws_call(socket, _fix(entry.entry_id, SMART, "default")))["result"]

    assert denied == {"api_version": 1, "ok": False, "error": "spotnav_not_admin"}
    assert calls == []


async def test_another_version_of_the_command_is_refused(hass: HomeAssistant, hass_ws_client) -> None:
    entry = await _charger(hass, "peblar")
    reply = await ws_call(await admin(hass, hass_ws_client), {**_fix(entry.entry_id, SMART, "default"), "api_version": 2})
    assert reply["success"] is False
    assert reply["error"]["code"] == "spotnav_unsupported_api_version"


@freeze_time(LATE)
async def test_easees_own_switches_are_notices_with_their_fix(hass: HomeAssistant, transport: StubTransport) -> None:
    entry = await _charger(hass, "easee", EASEE, transport)
    hass.states.async_set("switch.easee_is_enabled", "off")
    hass.states.async_set("switch.easee_smart_charging", "on")

    assert _lines(hass, entry, "charger_disabled") == [
        {"code": "charger_disabled", "params": {"entity": "switch.easee_is_enabled"}}
    ]
    assert [line["params"]["value"] for line in _lines(hass, entry, "charger_own_mode")] == ["off"]


async def test_easees_enable_switch_is_turned_on_and_its_smart_charging_off(hass: HomeAssistant, hass_ws_client) -> None:
    entry = await _charger(hass, "easee", EASEE)
    hass.states.async_set("switch.easee_is_enabled", "off")
    hass.states.async_set("switch.easee_smart_charging", "on")
    on = async_mock_service(hass, "switch", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")
    socket = await admin(hass, hass_ws_client)

    assert (await ws_call(socket, _fix(entry.entry_id, "switch.easee_is_enabled", "off")))["result"]["ok"] is False
    assert (await ws_call(socket, _fix(entry.entry_id, "switch.easee_smart_charging", "on")))["result"]["ok"] is False
    assert (await ws_call(socket, _fix(entry.entry_id, "switch.easee_is_enabled", "on")))["result"]["ok"] is True
    assert (await ws_call(socket, _fix(entry.entry_id, "switch.easee_smart_charging", "off")))["result"]["ok"] is True
    assert [call.data for call in on] == [{"entity_id": "switch.easee_is_enabled"}]
    assert [call.data for call in off] == [{"entity_id": "switch.easee_smart_charging"}]


async def test_only_a_single_safe_value_is_offered(hass: HomeAssistant) -> None:
    schedule = OwnModeRule("select", ("mode",), ("always_on", "always_off"), "schedule")
    hass.states.async_set("select.garo_mode", "schema", {"options": ["ALWAYS_ON", "ALWAYS_OFF", "SCHEMA"]})
    # "Always on" and "always off" both end the schedule and mean opposite things: no button.
    assert fix_value(hass, "select.garo_mode", schedule) is None
    hass.states.async_set("select.garo_mode", "schema", {"options": ["ALWAYS_OFF", "SCHEMA"]})
    assert fix_value(hass, "select.garo_mode", schedule) == "ALWAYS_OFF"
    hass.states.async_set("select.garo_mode", "schema", {"options": ["SCHEMA"]})
    assert fix_value(hass, "select.garo_mode", schedule) is None
    assert fix_value(hass, "switch.x", OwnModeRule("switch", ("x",), ("off",), "x")) == "off"
    assert fix_value(hass, "switch.x", OwnModeRule("switch", ("x",), ("off", "standby"), "x")) is None


def test_an_app_that_does_not_read_the_line_is_not_sent_it_and_its_tone_follows() -> None:
    line = {
        "code": "charger_own_mode",
        "params": {"entity": SMART, "label": "smart charging", "state": "pure_solar", "value": "default"},
    }
    body = {"status": {"tone": "notice", "lines": [{"code": "waiting_for_tomorrow", "params": {}}, line]}}

    assert _for_app(body, {"action": "dashboard"})["status"] == {
        "tone": "normal",
        "lines": [{"code": "waiting_for_tomorrow", "params": {}}],
    }
    assert _for_app(body, {"action": "dashboard", "reads": ["own_mode_status"]}) == body
    unpriced = replace_lines(body, [{"code": "unpriced", "params": {}}, line])
    assert _for_app(unpriced, {"action": "dashboard"})["status"]["tone"] == "notice"


def replace_lines(body: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    return {**body, "status": {**body["status"], "lines": lines}}

