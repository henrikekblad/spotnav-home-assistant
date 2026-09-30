"""Pairing for the whole instance, not one charger at a time.

The instance-wide payload: every charger entry, one base URL, each charger's own webhook id --
and the single-charger `pairing_uri` staying unchanged, because app builds parse it.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlencode, urlparse

from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.sensor import (
    ConnectionEntity,
    InstanceConnectionEntity,
    instance_owner_entry_id,
)
from custom_components.spotnav.vehicles.charger_inventory import charger_pairing_payload

from .helpers import make_entry, make_site_entry, setup_two_chargers
from .world import controller_of

EXTERNAL_URL = "https://ha.example.com"


def _chargers_in(uri: str) -> list[dict[str, str]]:
    """The payload as the app will read it back: percent-decode, then JSON."""
    assert uri.startswith("spotnav://home-assistant?")
    query = parse_qs(urlparse(uri).query)
    return json.loads(query["chargers"][0])


def _instance_attributes(hass: HomeAssistant, *entries) -> dict:
    owner = entries[0]
    return InstanceConnectionEntity(hass, owner).extra_state_attributes


async def test_one_charger_payload_names_it(hass: HomeAssistant) -> None:
    hass.config.external_url = EXTERNAL_URL
    entry = make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )

    attributes = _instance_attributes(hass, entry)

    assert attributes["home_assistant_url"] == EXTERNAL_URL
    assert _chargers_in(attributes["pairing_uri"]) == [
        {"id": "entry_charger_a", "name": "Garage", "webhook": "webhook-a"}
    ]
    # Names and ids are shown for checking; the secrets stay in the value above.
    assert attributes["chargers"] == [{"id": "entry_charger_a", "name": "Garage"}]


async def test_several_chargers_are_all_in_one_value(hass: HomeAssistant) -> None:
    hass.config.external_url = EXTERNAL_URL
    entry_a, entry_b, *_ = await setup_two_chargers(hass)

    attributes = _instance_attributes(hass, entry_a)

    assert _chargers_in(attributes["pairing_uri"]) == [
        {"id": entry_a.entry_id, "name": "Charger A", "webhook": "webhook-a"},
        {"id": entry_b.entry_id, "name": "Charger B", "webhook": "webhook-b"},
    ]
    # The base URL appears once, as the payload's own parameter -- never inside
    # the per-charger list, which is what "one URL for the instance" means.
    query = parse_qs(urlparse(attributes["pairing_uri"]).query)
    assert query["url"] == [EXTERNAL_URL]
    assert all("url" not in charger for charger in _chargers_in(attributes["pairing_uri"]))


async def test_a_site_entry_contributes_nothing(hass: HomeAssistant) -> None:
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


async def test_a_name_with_separators_spaces_and_non_ascii_round_trips(hass: HomeAssistant) -> None:
    hass.config.external_url = EXTERNAL_URL
    title = 'K\u00e4rra | #1 \u2013 "special" & co, \u00f6\u00e4\u00e5; 100%'
    entry = make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title=title,
    )

    payload = _chargers_in(_instance_attributes(hass, entry)["pairing_uri"])

    assert payload[0]["name"] == title
    # The URI itself stays ASCII, whatever the name holds.
    assert _instance_attributes(hass, entry)["pairing_uri"].isascii()


async def test_the_existing_single_charger_pairing_uri_is_unchanged(hass: HomeAssistant) -> None:
    # Shipped app builds parse this value: same two query parameters, order and encoding.
    hass.config.external_url = EXTERNAL_URL
    entry = make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )
    hass.states.async_set("switch.charger_a", "off")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    controller = controller_of(hass, entry.entry_id)

    attributes = ConnectionEntity(hass, entry, controller).extra_state_attributes

    webhook_url = webhook.async_generate_url(
        hass, "webhook-a", allow_internal=True, prefer_external=True
    )
    base_url = webhook_url.rsplit("/api/webhook/", 1)[0]
    expected = "spotnav://home-assistant?" + urlencode({"url": base_url, "webhook": "webhook-a"})
    assert attributes["pairing_uri"] == expected
    assert attributes["webhook_id"] == "webhook-a"
    assert attributes["webhook_url"] == webhook_url


async def test_eight_chargers_still_fit_in_one_value(hass: HomeAssistant) -> None:
    hass.config.external_url = EXTERNAL_URL
    entries = [
        make_entry(
            hass,
            entry_id=f"entry_charger_{index}",
            charge_control=f"switch.charger_{index}",
            current_limit=None,
            # A real webhook id is 43 characters (token_urlsafe(32)).
            webhook_id=f"webhook-{index}-" + "x" * 33,
            title=f"Workshop bay {index}",
        )
        for index in range(8)
    ]

    uri = _instance_attributes(hass, entries[0])["pairing_uri"]

    assert len(_chargers_in(uri)) == 8
    assert len(uri) < 2000

async def test_eight_chargers_report_their_real_length(hass: HomeAssistant) -> None:
    """The measured cost of the payload, so the choice of encoding is checked."""
    hass.config.external_url = EXTERNAL_URL
    entries = [
        make_entry(
            hass,
            entry_id=f"entry_charger_{index}",
            charge_control=f"switch.charger_{index}",
            current_limit=None,
            webhook_id=f"webhook-{index}-" + "x" * 33,
            title=f"Workshop bay {index}",
        )
        for index in range(8)
    ]

    uri = _instance_attributes(hass, entries[0])["pairing_uri"]

    # Eight chargers with 43-character webhook ids and 18-character entry ids:
    # 1230 characters as measured, about 150 per charger on top of the URL.
    # Linear in chargers and in name length, and nothing truncates it.
    print(f"eight-charger pairing URI length: {len(uri)}")
    assert len(uri) < 1500


async def test_a_payload_still_forms_without_an_external_url(hass: HomeAssistant) -> None:
    # No URL available is a state to report honestly, not to fail in: the
    # charger list is still worth having, and `url` is simply empty.
    entry = make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )

    attributes = _instance_attributes(hass, entry)

    assert attributes["home_assistant_url"] == ""
    query = parse_qs(urlparse(attributes["pairing_uri"]).query, keep_blank_values=True)
    assert query["url"] == [""]
    assert _chargers_in(attributes["pairing_uri"])[0]["webhook"] == "webhook-a"


async def test_the_site_entry_owns_the_instance_entity(hass: HomeAssistant) -> None:
    # A site entry *is* the instance-wide entry, so a person looking for
    # instance-wide things finds it there.
    site = make_site_entry(hass, entry_id="site_1", charger_entry_ids=["entry_charger_a"])
    make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )

    assert instance_owner_entry_id(hass) == site.entry_id


async def test_the_first_charger_owns_it_when_there_is_no_site(hass: HomeAssistant) -> None:
    # Where an instance has never made a site entry, the first charger still
    # gives the entity a device to appear on -- no trailing empty instance.
    first = make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )
    make_entry(
        hass,
        entry_id="entry_charger_b",
        charge_control="switch.charger_b",
        current_limit=None,
        webhook_id="webhook-b",
        title="Carport",
    )

    assert instance_owner_entry_id(hass) == first.entry_id


async def test_no_entries_means_no_owner(hass: HomeAssistant) -> None:
    assert instance_owner_entry_id(hass) is None


async def test_the_entity_is_added_once_on_the_owners_device(hass: HomeAssistant) -> None:
    entry_a, entry_b, *_ = await setup_two_chargers(hass)
    registry = er.async_get(hass)

    instances = [
        entity
        for entity in registry.entities.values()
        if entity.unique_id.endswith("_instance_connection")
    ]

    assert len(instances) == 1
    assert instances[0].config_entry_id == entry_a.entry_id
    assert instances[0].device_id == registry.async_get(instances[0].entity_id).device_id

    state = hass.states.get(instances[0].entity_id)
    assert state is not None
    assert state.attributes["pairing_uri"].startswith("spotnav://home-assistant?")
    assert [charger["name"] for charger in state.attributes["chargers"]] == ["Charger A", "Charger B"]


async def test_diagnostics_never_carry_the_pairing_value_or_a_webhook_id(hass: HomeAssistant) -> None:
    # A diagnostics dump goes into public issue reports.
    entry_a, _entry_b, *_ = await setup_two_chargers(hass)
    site = make_site_entry(hass, entry_id="site_1", charger_entry_ids=[entry_a.entry_id])
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()

    for entry in (entry_a, site):
        dump = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
        assert "spotnav://" not in dump
        assert "webhook-a" not in dump
        assert "instance_connection" not in dump
