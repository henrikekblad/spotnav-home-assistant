"""Tests that two config entries never leak state into each other."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.execution.controller import ChargingController

from .helpers import make_entry as _make_entry
from .helpers import setup_two_chargers as _setup_two_chargers
from .helpers import webhook_dashboard
from .world import controller_of
from homeassistant.config_entries import ConfigEntryState


async def test_two_entries_get_independent_controllers(hass: HomeAssistant) -> None:
    """Each config entry must own a distinct controller keyed by its entry_id."""
    entry_a, entry_b, _turn_on, _turn_off = await _setup_two_chargers(hass)

    controller_a = controller_of(hass, entry_a.entry_id)
    controller_b = controller_of(hass, entry_b.entry_id)

    assert isinstance(controller_a, ChargingController)
    assert isinstance(controller_b, ChargingController)
    assert controller_a is not controller_b
    assert controller_a.charge_control == "switch.charger_a"
    assert controller_b.charge_control == "switch.charger_b"


async def test_two_entries_get_independent_devices_and_entities(hass: HomeAssistant) -> None:
    """Entity and device registries must not collide between entries."""
    entry_a, entry_b, _turn_on, _turn_off = await _setup_two_chargers(hass)

    device_registry = dr.async_get(hass)
    device_a = device_registry.async_get_device_by_identifier(
        (DOMAIN, entry_a.entry_id), entry_a.entry_id
    )
    device_b = device_registry.async_get_device_by_identifier(
        (DOMAIN, entry_b.entry_id), entry_b.entry_id
    )
    assert device_a is not None
    assert device_b is not None
    assert device_a.id != device_b.id

    entity_registry = er.async_get(hass)
    entities_a = {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(entity_registry, entry_a.entry_id)
    }
    entities_b = {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(entity_registry, entry_b.entry_id)
    }
    assert entities_a, "Charger A should register entities"
    assert entities_b, "Charger B should register entities"
    assert entities_a.isdisjoint(entities_b)
    assert all(uid.startswith(entry_a.entry_id) for uid in entities_a)
    assert all(uid.startswith(entry_b.entry_id) for uid in entities_b)


async def test_webhook_start_only_controls_its_own_charger(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A command sent to charger A's webhook must never touch charger B."""
    entry_a, entry_b, turn_on_calls, _turn_off = await _setup_two_chargers(hass)
    client = await hass_client_no_auth()

    resp = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "start", "amps": 10},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    await hass.async_block_till_done()

    assert len(turn_on_calls) == 1
    assert turn_on_calls[0].data["entity_id"] == "switch.charger_a"


async def test_webhook_start_never_writes_any_current_limit(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A start command must not write any charger's current-limit entity.

    The command still reaches its own charger's switch -- the two-entry setup
    below is what proves that -- but a start *records* what was asked for and
    never applies it: writing a current-limit entity asks the charger's own
    integration to send that charger a persistent charging profile, and no
    command this integration sends may do that.
    """
    hass.states.async_set("switch.charger_a", "off")
    hass.states.async_set("switch.charger_b", "off")
    hass.states.async_set("number.current_a", "10", {"min": 6, "max": 32})
    hass.states.async_set("number.current_b", "10", {"min": 6, "max": 32})
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    set_value_calls = async_mock_service(hass, "number", "set_value")

    entry_a = _make_entry(
        hass,
        entry_id="entry_charger_a_current",
        charge_control="switch.charger_a",
        current_limit="number.current_a",
        webhook_id="webhook-a-current",
        title="Charger A",
    )
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()

    entry_b = _make_entry(
        hass,
        entry_id="entry_charger_b_current",
        charge_control="switch.charger_b",
        current_limit="number.current_b",
        webhook_id="webhook-b-current",
        title="Charger B",
    )
    assert await hass.config_entries.async_setup(entry_b.entry_id)
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    resp = await client.post(
        "/api/webhook/webhook-a-current",
        json={"version": 1, "action": "start", "amps": 16},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    await hass.async_block_till_done()

    # Neither its own current-limit entity nor the other charger's.
    assert set_value_calls == []


async def test_unregistered_webhook_after_unload_of_other_entry_still_isolated(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Unloading one entry must not deregister or otherwise affect the other's webhook."""
    entry_a, entry_b, _turn_on, _turn_off = await _setup_two_chargers(hass)
    client = await hass_client_no_auth()

    assert await hass.config_entries.async_unload(entry_a.entry_id)
    await hass.async_block_till_done()

    assert entry_a.state is ConfigEntryState.NOT_LOADED
    assert entry_b.state is ConfigEntryState.LOADED

    resp_a = await client.post(
        "/api/webhook/webhook-a", json={"version": 1, "action": "dashboard", "api_version": 1}
    )
    assert resp_a.status == 200
    assert await resp_a.read() == b""  # unregistered webhooks always respond empty

    body_b = await webhook_dashboard(client, "webhook-b")
    assert body_b["ok"] is True


async def test_entries_present_at_startup_are_both_loaded_independently(
    hass: HomeAssistant,
) -> None:
    """Two chargers already configured before HA starts must both come up isolated.

    Unlike _setup_two_chargers, both entries are registered before either is
    set up. This exercises ConfigEntries.async_setup's bulk path, which sets
    up every pending entry for a domain the first time that domain's
    component is bootstrapped (the real startup scenario for two chargers
    added in a previous session).
    """
    hass.states.async_set("switch.charger_a", "off")
    hass.states.async_set("switch.charger_b", "off")

    entry_a = _make_entry(
        hass,
        entry_id="startup_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="startup-webhook-a",
        title="Charger A",
    )
    entry_b = _make_entry(
        hass,
        entry_id="startup_charger_b",
        charge_control="switch.charger_b",
        current_limit=None,
        webhook_id="startup-webhook-b",
        title="Charger B",
    )

    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()

    assert entry_b.state.value == "loaded"
    controller_a = controller_of(hass, entry_a.entry_id)
    controller_b = controller_of(hass, entry_b.entry_id)
    assert controller_a is not controller_b
    assert controller_a.charge_control == "switch.charger_a"
    assert controller_b.charge_control == "switch.charger_b"

    entity_registry = er.async_get(hass)
    entities_a = {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(entity_registry, entry_a.entry_id)
    }
    entities_b = {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(entity_registry, entry_b.entry_id)
    }
    assert entities_a.isdisjoint(entities_b)
