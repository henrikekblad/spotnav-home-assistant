"""The charger's start/stop diagnostics: the adapter's bounded command log, the diagnostics block it
feeds, and the Easee enable-switch warning (entity configuration and dashboard status).
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.execution.charger_profiles import PROFILES
from custom_components.spotnav.execution.chargers.adapter import COMMAND_AFTER_S, COMMAND_LOG_SIZE
from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_UNCHANGED,
    loggable_data,
    WRITE_REGULATOR,
)
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.runtime import controller_for

from .charger_helpers import adapter_for, Clock
from .charger_shapes import register_shape, SHAPES
from .relay import serve, StubTransport, TODAY
from .world import go_auto
from .test_charger_config_flow import _loaded_detected_charger, async_get_entity_config

LATE = "2026-09-22 20:30:00"


async def _easee(hass: HomeAssistant):
    ids = register_shape(hass, SHAPES["easee"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=Clock())
    commands = async_mock_service(hass, "easee", "action_command")
    return ids, adapter, commands


# --- the ring buffer -----------------------------------------------------------------------------


async def test_the_command_log_keeps_the_last_twenty_oldest_first(hass: HomeAssistant) -> None:
    _, adapter, _ = await _easee(hass)
    clock = Clock()
    adapter._now = clock  # noqa: SLF001 - distinct times make the order visible

    for _ in range(COMMAND_LOG_SIZE + 5):
        clock.advance(1)
        await adapter.async_stop()

    log = adapter.command_log()
    assert len(log) == COMMAND_LOG_SIZE == 20
    times = [record["at"] for record in log]
    assert times == sorted(times) and len(set(times)) == 20
    # The five oldest were dropped: the first one kept is the sixth command sent.
    assert times[0] == (clock.current - timedelta(seconds=19)).isoformat()
    assert times[-1] == clock.current.isoformat()


async def test_a_command_is_logged_with_its_kind_service_data_and_result(hass: HomeAssistant) -> None:
    ids, adapter, _ = await _easee(hass)
    hass.states.async_set("sensor.easee_status", "awaiting_start", {"config_authorizationRequired": False})

    assert await adapter.async_start() is True

    (record,) = adapter.command_log()
    assert record["kind"] == "start" and record["result"] == "sent" and record["error"] is None
    # An unauthorized charger waiting for a start is authorized first, then resumed.
    assert [call["service"] for call in record["calls"]] == ["easee.action_command", "easee.action_command"]
    assert [call["data"] for call in record["calls"]] == [
        {"device_id": ids["device_id"], "action_command": "start"},
        {"device_id": ids["device_id"], "action_command": "resume"},
    ]
    assert record["status_before"] == "awaiting_start"
    assert record["at"].endswith("+00:00")


async def test_an_unavailable_control_is_logged_as_not_executed(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=Clock())
    hass.states.async_set("switch.wallbox_pause_resume", "unavailable")

    assert await adapter.async_start() is False

    (record,) = adapter.command_log()
    assert record["kind"] == "start" and record["result"] == "not_executed" and record["calls"] == []


async def test_a_failing_service_is_logged_with_the_exception_class_and_message(hass: HomeAssistant) -> None:
    ids, adapter, _ = await _easee(hass)

    async def fail(call: ServiceCall) -> None:
        raise HomeAssistantError("cloud said no")

    hass.services.async_register("easee", "action_command", fail)

    with pytest.raises(HomeAssistantError):
        await adapter.async_stop()

    (record,) = adapter.command_log()
    assert record["kind"] == "stop" and record["result"] == "error"
    assert record["error"] == {"type": "HomeAssistantError", "message": "cloud said no"}
    # The call is there, so the log shows what was attempted.
    assert record["calls"] == [
        {"service": "easee.action_command", "data": {"device_id": ids["device_id"], "action_command": "pause"}}
    ]


async def test_the_status_is_read_again_about_five_seconds_later_without_blocking(hass: HomeAssistant) -> None:
    _, adapter, _ = await _easee(hass)
    hass.states.async_set("sensor.easee_status", "ready_to_charge", {"config_authorizationRequired": False})

    await adapter.async_start()
    (record,) = adapter.command_log()
    assert record["status_before"] == "ready_to_charge" and record["status_after"] is None

    hass.states.async_set("sensor.easee_status", "charging", {"config_authorizationRequired": False})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=COMMAND_AFTER_S + 1))
    await hass.async_block_till_done()

    (record,) = adapter.command_log()
    assert record["status_before"] == "ready_to_charge" and record["status_after"] == "charging"


async def test_secrets_never_reach_the_log() -> None:
    data = loggable_data(
        {"entity_id": "switch.a", "device_id": "dev1", "token": "t0k", "devid": "CP-1", "password": "pw", "value": 8}
    )

    assert data == {
        "entity_id": "switch.a",
        "device_id": "dev1",
        "token": "**REDACTED**",
        "devid": "**REDACTED**",
        "password": "**REDACTED**",
        "value": 8,
    }


async def test_only_a_current_write_that_sent_something_is_a_command_but_the_outcome_is_always_kept(
    hass: HomeAssistant,
) -> None:
    _, adapter, _ = await _easee(hass)
    limits = async_mock_service(hass, "easee", "set_charger_dynamic_limit")

    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_UNCHANGED

    assert len(limits) == 1
    (record,) = adapter.command_log()
    assert record["kind"] == "current" and record["result"] == "sent" and record["outcome"] == ASSIGN_ASSIGNED
    assert record["calls"][0]["service"] == "easee.set_charger_dynamic_limit"
    last = adapter.diagnostics("sensor.easee_status")["last_current"]
    assert last["outcome"] == ASSIGN_UNCHANGED and last["reason"] == WRITE_REGULATOR and last["amps"] == 12


# --- the diagnostics block -----------------------------------------------------------------------


async def test_the_charger_diagnostics_carry_the_start_stop_state_and_the_log(hass: HomeAssistant) -> None:
    entry = await _loaded_detected_charger(hass, "easee")
    hass.states.async_set("sensor.easee_status", "awaiting_start", {"config_authorizationRequired": True})
    async_mock_service(hass, "easee", "action_command")
    live = controller_for(hass, entry.entry_id)
    assert await live.async_start(manual=True) is True

    block = (await async_get_config_entry_diagnostics(hass, entry))["controller"]["adapter"]

    assert block["status"] == {"entity_id": "sensor.easee_status", "state": "awaiting_start"}
    assert block["charge_control"] == {"entity_id": "sensor.easee_status", "state": "awaiting_start"}
    assert block["start_stop_state"] == {
        "paused": False,
        "authorization_required": True,
        "start_owed": True,
        "limit_read_back_a": None,
    }
    assert set(block["current_state"]) == {
        "last_written_a",
        "read_back_a",
        "limit_sensor_enabled",
        "cache_suspect",
        "holding_start_floor",
    }
    assert block["charger_disabled"] is False
    assert [(c["kind"], c["result"]) for c in block["commands"]] == [("start", "sent")]
    assert block["commands"][0]["status_before"] == "awaiting_start"
    assert set(block["commands"][0]) == {
        "at", "kind", "calls", "result", "error", "status_before", "status_after"
    }
    # The existing facts are still there, and the redaction still applies.
    assert block["platform"] == "easee" and block["start_stop"] == "easee"
    assert "hook-easee" not in json.dumps(block)
    assert "hook-easee" not in json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    await live.async_shutdown()


async def test_a_path_without_state_of_its_own_reports_an_empty_one(hass: HomeAssistant) -> None:
    entry = await _loaded_detected_charger(hass, "wallbox")

    block = (await async_get_config_entry_diagnostics(hass, entry))["controller"]["adapter"]

    assert block["start_stop_state"] == {} and block["current_state"] == {}
    assert block["charge_control"]["entity_id"] == "switch.wallbox_pause_resume"
    assert block["commands"] == [] and block["last_current"] is None


# --- the charger's own enable switch ---------------------------------------------------------------


def test_only_easee_names_an_enable_switch_and_never_the_one_it_starts_with() -> None:
    named = {name for name, profile in PROFILES.items() if profile.enable_switch_keys}
    assert named == {"easee"}
    for profile in PROFILES.values():
        if profile.start_stop is not None:
            assert not set(profile.enable_switch_keys) & set(profile.start_stop.keys)


async def test_the_entity_config_warns_while_easees_enable_switch_is_off(hass: HomeAssistant) -> None:
    entry = await _loaded_detected_charger(hass, "easee")
    assert (await async_get_entity_config(hass, entry.entry_id))["control"]["conflicts"] == []

    hass.states.async_set("switch.easee_is_enabled", "off")

    assert (await async_get_entity_config(hass, entry.entry_id))["control"]["conflicts"] == [
        {"kind": "disabled", "entity_id": "switch.easee_is_enabled", "label": "enabled", "state": "off"}
    ]

    hass.states.async_set("switch.easee_is_enabled", "unavailable")
    assert (await async_get_entity_config(hass, entry.entry_id))["control"]["conflicts"] == []


@pytest.mark.usefixtures("offline_relay")
@freeze_time(LATE)
async def test_the_dashboard_status_explains_a_charger_that_is_disabled(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    entry = await _loaded_detected_charger(hass, "easee")
    hass.states.async_set("sensor.easee_status", "awaiting_start", {"config_authorizationRequired": False})
    serve(transport, days=(TODAY,), listed=(TODAY,))
    await go_auto(hass, entry.entry_id)

    def codes() -> list[str]:
        capture = dashboard_api.capture_dashboard(hass, entry)
        status = dashboard_api.serialize_dashboard(capture, can_act=False)["status"]
        return [line["code"] for line in status["lines"]]

    assert "charger_disabled" not in codes()

    hass.states.async_set("switch.easee_is_enabled", "off")
    assert "charger_disabled" in codes()

    # A charge that is running is never "disabled", whatever a stale switch says.
    hass.states.async_set("sensor.easee_status", "charging", {"config_authorizationRequired": False})
    assert "charger_disabled" not in codes()
