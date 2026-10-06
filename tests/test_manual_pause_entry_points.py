"""Every manual path takes the same pause: the card's action, the app's webhook, the charger's buttons."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.api.manual_action import async_perform_action
from custom_components.spotnav.planning.auto_settings import MANUAL_START, MANUAL_STOP, PAUSE_MANUAL
from custom_components.spotnav.runtime import executor_for

from .world import entity_id, setup_charger, settings_of

pytestmark = pytest.mark.usefixtures("offline_relay")


async def _charger(hass: HomeAssistant) -> Any:
    entry = await setup_charger(hass)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    executor_for(hass, entry.entry_id).controller.adapter.vehicle_connected = lambda: True
    return entry


def _pause(hass: HomeAssistant, entry: Any) -> tuple[str | None, str | None]:
    pause = settings_of(hass, entry.entry_id).pause
    return pause.choice, pause.action


@pytest.mark.parametrize(("key", "action"), [("start", MANUAL_START), ("stop", MANUAL_STOP), ("cancel", MANUAL_STOP)])
async def test_a_button(hass: HomeAssistant, key: str, action: str) -> None:
    entry = await _charger(hass)

    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id(hass, entry.entry_id, key, "button")}, blocking=True
    )

    assert _pause(hass, entry) == (PAUSE_MANUAL, action)


@pytest.mark.parametrize(("name", "action"), [("start", MANUAL_START), ("stop", MANUAL_STOP)])
async def test_the_webhook(hass: HomeAssistant, hass_client_no_auth, name: str, action: str) -> None:
    entry = await _charger(hass)
    client = await hass_client_no_auth()

    response = await client.post("/api/webhook/webhook-a", json={"version": 1, "action": name})

    assert response.status == 200, await response.text()
    assert _pause(hass, entry) == (PAUSE_MANUAL, action)


async def test_the_cards_action(hass: HomeAssistant) -> None:
    entry = await _charger(hass)

    await async_perform_action(hass, entry.entry_id, action="start")
    assert _pause(hass, entry) == (PAUSE_MANUAL, MANUAL_START)
    hass.states.async_set("switch.charger_a", "on")
    await async_perform_action(hass, entry.entry_id, action="stop")
    assert _pause(hass, entry) == (PAUSE_MANUAL, MANUAL_STOP)
    # Resume is offered once the charger reports the Stop, as after a Start.
    hass.states.async_set("switch.charger_a", "off")
    await hass.async_block_till_done()
    await async_perform_action(hass, entry.entry_id, action="resume")
    assert _pause(hass, entry) == (None, None)
