"""No entity carries a webhook id or a pairing value.

A charger's webhook id is the only credential of an internet-reachable webhook, so it never sits in an
entity's state or attributes, which every user can read and the recorder keeps. The app gets it only
through the approved pairing handshake (`api/pairing.py`). The "App connection" and "App pairing"
sensors of earlier releases held it; they are removed from the registry, and their history purged.
"""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.vehicles.charger_inventory import charger_pairing_payload

from .helpers import make_entry, make_site_entry, setup_two_chargers

EXTERNAL_URL = "https://ha.example.com"

#: What a webhook id or a pairing value looks like in a state: the test ids, the URI and the path.
SECRET_MARKERS = ("webhook-a", "webhook-b", "spotnav://", "/api/webhook/")


async def _two_chargers_and_a_site(hass: HomeAssistant):
    hass.config.external_url = EXTERNAL_URL
    entry_a, entry_b, *_ = await setup_two_chargers(hass)
    site = make_site_entry(hass, entry_id="site_1", charger_entry_ids=[entry_a.entry_id, entry_b.entry_id])
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    return entry_a, entry_b, site


async def test_no_entity_state_or_attribute_carries_a_webhook_id(hass: HomeAssistant) -> None:
    await _two_chargers_and_a_site(hass)
    registry = er.async_get(hass)
    ours = [entity.entity_id for entity in registry.entities.values() if entity.platform == DOMAIN]
    assert ours

    for entity_id in ours:
        state = hass.states.get(entity_id)
        if state is None:
            continue
        dump = json.dumps({"state": state.state, "attributes": dict(state.attributes)}, default=str)
        for marker in SECRET_MARKERS:
            assert marker not in dump, f"{entity_id} carries {marker!r}"


async def test_there_are_no_pairing_sensors(hass: HomeAssistant) -> None:
    await _two_chargers_and_a_site(hass)
    registry = er.async_get(hass)

    assert not [
        entity
        for entity in registry.entities.values()
        if entity.platform == DOMAIN and entity.unique_id.endswith(("_connection", "_instance_connection"))
    ]


async def test_pairing_sensors_of_an_earlier_release_are_removed_and_purged(hass: HomeAssistant) -> None:
    purges = async_mock_service(hass, "recorder", "purge_entities")
    registry = er.async_get(hass)
    site = make_site_entry(hass, entry_id="site_1", charger_entry_ids=["entry_charger_a"])
    charger = make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )
    hass.states.async_set("switch.charger_a", "off")
    leftovers = [
        registry.async_get_or_create(
            "sensor", DOMAIN, f"{charger.entry_id}_connection", config_entry=charger
        ).entity_id,
        registry.async_get_or_create(
            "sensor", DOMAIN, f"{site.entry_id}_instance_connection", config_entry=site
        ).entity_id,
    ]

    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    for entity_id in leftovers:
        assert registry.async_get(entity_id) is None
        assert hass.states.get(entity_id) is None
    purged = sorted(entity_id for call in purges for entity_id in call.data["entity_id"])
    assert purged == sorted(leftovers)
    assert all(call.data["keep_days"] == 0 for call in purges)


async def test_without_the_recorder_the_leftovers_are_still_removed(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    charger = make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )
    hass.states.async_set("switch.charger_a", "off")
    leftover = registry.async_get_or_create(
        "sensor", DOMAIN, f"{charger.entry_id}_instance_connection", config_entry=charger
    ).entity_id

    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(leftover) is None


async def test_a_site_entry_contributes_nothing_to_the_pairing_payload(hass: HomeAssistant) -> None:
    # A site entry has no webhook and controls no charger by itself.
    make_site_entry(hass, entry_id="site_1", charger_entry_ids=["entry_charger_a"])
    make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )

    assert [charger["id"] for charger in charger_pairing_payload(hass)] == ["entry_charger_a"]


async def test_an_instance_with_no_chargers_offers_an_empty_list(hass: HomeAssistant) -> None:
    make_site_entry(hass, entry_id="site_1")

    assert charger_pairing_payload(hass) == []


async def test_diagnostics_never_carry_the_pairing_value_or_a_webhook_id(hass: HomeAssistant) -> None:
    # A diagnostics dump goes into public issue reports.
    entry_a, _entry_b, site = await _two_chargers_and_a_site(hass)

    for entry in (entry_a, site):
        dump = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
        assert "spotnav://" not in dump
        assert "webhook-a" not in dump
        assert "instance_connection" not in dump
