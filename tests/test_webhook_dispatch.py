"""The webhook is a dispatch table with one place that answers refusals."""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.api import webhook as webhook_module

from .world import entity_id, setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")


async def test_every_documented_action_has_a_handler() -> None:
    assert set(webhook_module.ACTIONS) == {
        "start",
        "stop",
        "resume",
        "refresh_vehicle",
        "set_charge_limit",
        "dashboard",
        "sessions",
        "settings",
        "update_vehicle",
        "update_site_settings",
        "update_charger_priority",
        "push_register",
        "identify_vehicle",
        "choose_vehicle_identification",
    }


async def test_an_unknown_action_is_a_bad_request(hass: HomeAssistant, hass_client_no_auth) -> None:
    await setup_charger(hass)
    client = await hass_client_no_auth()

    response = await client.post("/api/webhook/webhook-a", json={"version": 1, "action": "status"})

    assert response.status == 400
    assert await response.json() == {"ok": False, "error": "Unsupported action"}


@pytest.mark.parametrize("body", [[1, 2], "text", 7])
async def test_a_body_that_is_not_an_object_is_a_bad_request_with_a_stable_code(
    hass: HomeAssistant, hass_client_no_auth, body
) -> None:
    await setup_charger(hass)
    client = await hass_client_no_auth()

    response = await client.post("/api/webhook/webhook-a", json=body)

    assert response.status == 400
    assert await response.json() == {"ok": False, "error": "payload_not_object"}


async def test_rejected_commands_warn_once_and_then_only_debug(
    hass: HomeAssistant,
    hass_client_no_auth,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webhook_module, "_last_rejected_warning", None)
    await setup_charger(hass)
    client = await hass_client_no_auth()

    with caplog.at_level(logging.DEBUG, logger=webhook_module.__name__):
        for _ in range(3):
            await client.post("/api/webhook/webhook-a", json={"version": 2, "action": "start"})

    rejected = [r for r in caplog.records if "Rejected SpotNav webhook command" in r.getMessage()]
    assert [r.levelno for r in rejected] == [logging.WARNING, logging.DEBUG, logging.DEBUG]


async def test_configuration_and_diagnostic_entities_are_categorised(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    registry = er.async_get(hass)

    def category(key: str, platform: str):
        return registry.async_get(entity_id(hass, entry.entry_id, key, platform)).entity_category

    assert category("price_area", "select").value == "config"
    assert category("charging_current", "number").value == "config"
    assert category("departure_enabled", "switch").value == "config"
    assert category("connection", "sensor").value == "diagnostic"
    assert category("auto_settings_revision", "sensor").value == "diagnostic"
    assert category("start", "sensor") is None
    assert category("auto_plan_state", "sensor") is None
