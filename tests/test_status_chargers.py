"""The dashboard's `chargers` field: the instance's shape, reported on every poll.

Pairing hands over the *credentials* once, when a human approves them. This
field is the other half: which chargers exist right now, so an app that was
paired once learns about a charger added, renamed or removed afterwards -- the
way it already learns about vehicles.

It carries no secret. Every response here is asserted to contain no webhook id
at all, because this travels on every poll.
"""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.vehicles.charger_inventory import charger_entries
from custom_components.spotnav.vehicles.charger_inventory import charger_pairing_payload

from .helpers import make_entry, make_site_entry, setup_two_chargers, webhook_dashboard


async def _status(client, webhook_id: str) -> dict:
    return await webhook_dashboard(client, webhook_id)


async def test_one_charger_is_listed(hass: HomeAssistant, hass_client_no_auth) -> None:
    hass.states.async_set("switch.charger_a", "off")
    async_mock_service(hass, "switch", "turn_on")
    entry = make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    status = await _status(await hass_client_no_auth(), "webhook-a")

    # One charger, one element: never empty and never omitted.
    assert status["chargers"] == [{"id": "entry_charger_a", "name": "Garage"}]
    assert status["api_version"] == 1


async def test_several_chargers_are_listed_in_creation_order(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry_a, entry_b, *_ = await setup_two_chargers(hass)
    client = await hass_client_no_auth()

    status = await _status(client, "webhook-a")
    again = await _status(client, "webhook-a")

    assert status["chargers"] == [
        {"id": entry_a.entry_id, "name": "Charger A"},
        {"id": entry_b.entry_id, "name": "Charger B"},
    ]
    # Deterministic: the same answer, in the same order, every time.
    assert again["chargers"] == status["chargers"]
    assert [entry.entry_id for entry in charger_entries(hass)] == [entry_a.entry_id, entry_b.entry_id]


async def test_a_renamed_charger_reports_its_new_name(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    # The point of polling the shape: a rename reaches the app without pairing
    # again, because the name is the entry's own title, read every time.
    entry_a, _entry_b, *_ = await setup_two_chargers(hass)
    hass.config_entries.async_update_entry(entry_a, title="Renamed in HA")
    await hass.async_block_till_done()

    status = await _status(await hass_client_no_auth(), "webhook-a")

    assert status["chargers"][0] == {"id": entry_a.entry_id, "name": "Renamed in HA"}


async def test_every_chargers_dashboard_reports_the_same_list(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    # System-wide, like `vehicles`: which chargers exist is not a property of
    # the one being asked about.
    _entry_a, _entry_b, *_ = await setup_two_chargers(hass)
    client = await hass_client_no_auth()

    from_a = await _status(client, "webhook-a")
    from_b = await _status(client, "webhook-b")

    assert from_a["chargers"] == from_b["chargers"]


async def test_a_charger_lists_itself_without_being_marked(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    # The asked-for charger is simply present with the others: `charger_id`
    # already names it, and the list is a catalogue of the instance rather than
    # a statement about the caller. Each entry is exactly id and name -- no
    # "current", no "self", nothing an app could misread.
    entry_a, entry_b, *_ = await setup_two_chargers(hass)

    status = await _status(await hass_client_no_auth(), "webhook-a")

    assert status["charger"]["charger_id"] == entry_a.entry_id
    assert entry_a.entry_id in [charger["id"] for charger in status["chargers"]]
    assert entry_b.entry_id in [charger["id"] for charger in status["chargers"]]
    assert all(set(charger) == {"id", "name"} for charger in status["chargers"])


async def test_a_site_entry_contributes_nothing(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry_a, entry_b, *_ = await setup_two_chargers(hass)
    site = make_site_entry(hass, entry_id="site_1", charger_entry_ids=[entry_a.entry_id])
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()

    status = await _status(await hass_client_no_auth(), "webhook-a")

    assert [charger["id"] for charger in status["chargers"]] == [entry_a.entry_id, entry_b.entry_id]


async def test_no_secret_reaches_the_response(hass: HomeAssistant, hass_client_no_auth) -> None:
    _entry_a, _entry_b, *_ = await setup_two_chargers(hass)

    status = await _status(await hass_client_no_auth(), "webhook-a")
    dumped = json.dumps(status)

    # The webhook ids, their URLs and the pairing values are all absent: this
    # response is polled, and those cross exactly once, deliberately.
    assert "webhook-a" not in dumped
    assert "webhook-b" not in dumped
    assert "spotnav://" not in dumped
    assert "webhook" not in dumped
    assert "url" not in dumped.lower()


async def test_the_two_pairing_values_agree_with_the_dashboard_field(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    # One definition of "the chargers on this instance": the dashboard field is the
    # pairing payload minus the secret, in the same order. (The pairing values'
    # own guarantees -- byte-identical single-charger URI, round-tripping
    # instance payload -- are asserted in tests/test_instance_pairing.py.)
    _entry_a, _entry_b, *_ = await setup_two_chargers(hass)

    status = await _status(await hass_client_no_auth(), "webhook-a")
    payload = charger_pairing_payload(hass)

    assert [charger["id"] for charger in payload] == [charger["id"] for charger in status["chargers"]]
    assert [charger["name"] for charger in payload] == [charger["name"] for charger in status["chargers"]]
    assert all("webhook" in charger for charger in payload)
