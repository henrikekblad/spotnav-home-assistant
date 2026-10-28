"""Where a charger's electricity price lives, as the card shows and changes it (`api/price_source.py`): the
dashboard's `price_source` block, the choice of price entry and the answer to the same-area question.

Real config entries and the real authenticated socket on the offline relay (SE4), at a frozen clock.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.const import CONF_PRICE_ENTRY_ID
from custom_components.spotnav.pricing.price_source import price_source_for, VIA_CHARGER, VIA_SITE
from custom_components.spotnav.pricing.price_store import price_duplicates
from custom_components.spotnav.runtime import domain_data

from .helpers import webhook_dashboard
from .relay import SE4
from .test_price_device import add_price, NOW
from .world import admin, go_auto, non_admin, setup_charger, setup_site, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

OWN_FEE = {"enabled": True, "value": 24.5}


def dashboard(hass: HomeAssistant, charger: str = "entry_a", *, can_act: bool = True) -> dict[str, Any]:
    entry = hass.config_entries.async_get_entry(charger)
    return dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, entry), can_act=can_act)


def pointer(hass: HomeAssistant, entry_id: str) -> str | None:
    return hass.config_entries.async_get_entry(entry_id).data.get(CONF_PRICE_ENTRY_ID)


async def two_chargers(hass: HomeAssistant) -> None:
    await setup_charger(hass)
    await setup_charger(hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b")


async def two_prices(hass: HomeAssistant) -> None:
    """SE4 with the suggested fees, and SE4 with a grid fee of the company's own: recorded as the same area."""
    await add_price(hass)
    await add_price(hass, entry_id="price_b", title="Electricity price SE4 (company)", transfer=OWN_FEE)
    await domain_data(hass).price_store.async_record_duplicates(SE4, ["price_a", "price_b"])


async def set_source(client: Any, charger: str, price_entry_id: str) -> dict[str, Any]:
    return await ws_call(
        client, {"type": "spotnav/set_price_source", "entry_id": charger, "price_entry_id": price_entry_id}
    )


async def answer(client: Any, area_id: str, keep: str | None) -> dict[str, Any]:
    return await ws_call(
        client, {"type": "spotnav/answer_price_duplicate", "area_id": area_id, "keep_entry_id": keep}
    )


# ------------------------------------------------------------------ the dashboard


async def test_a_charger_with_its_own_settings_has_no_block(hass: HomeAssistant, freezer) -> None:
    await setup_charger(hass)
    freezer.move_to(NOW)
    await go_auto(hass)

    assert "price_source" not in dashboard(hass)


async def test_one_price_used_by_both_chargers(hass: HomeAssistant, freezer) -> None:
    await two_chargers(hass)
    freezer.move_to(NOW)
    await add_price(hass)

    assert dashboard(hass)["price_source"] == {
        "entry_id": "price_a",
        "title": "Electricity price SE4",
        "charger_count": 2,
        "choices": [],
        "writable": True,
        "duplicate": None,
    }
    assert dashboard(hass, "entry_b", can_act=False)["price_source"]["writable"] is False


async def test_several_prices_offer_a_choice_and_ask_the_question(hass: HomeAssistant, freezer) -> None:
    await two_chargers(hass)
    freezer.move_to(NOW)
    await two_prices(hass)
    hass.config_entries.async_update_entry(hass.config_entries.async_get_entry("entry_a"), data={
        **hass.config_entries.async_get_entry("entry_a").data, CONF_PRICE_ENTRY_ID: "price_b",
    })

    block = dashboard(hass)["price_source"]
    assert block["entry_id"] == "price_b" and block["charger_count"] == 1
    assert block["choices"] == [
        {"entry_id": "price_a", "title": "Electricity price SE4"},
        {"entry_id": "price_b", "title": "Electricity price SE4 (company)"},
    ]
    assert block["duplicate"] == {
        "area_id": SE4,
        "entry_ids": ["price_a", "price_b"],
        "titles": ["Electricity price SE4", "Electricity price SE4 (company)"],
    }
    # The question is an administrator's: nobody else is asked.
    assert dashboard(hass, can_act=False)["price_source"]["duplicate"] is None
    # A charger with no pointer among several prices prices with its own settings: no block.
    assert "price_source" not in dashboard(hass, "entry_b")


async def test_the_apps_webhook_sends_the_block_only_when_read(hass: HomeAssistant, hass_client_no_auth, freezer) -> None:
    await setup_charger(hass)
    client = await hass_client_no_auth()
    freezer.move_to(NOW)
    await add_price(hass)

    assert "price_source" not in await webhook_dashboard(client, "webhook-a")
    response = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "dashboard", "api_version": 1, "reads": ["price_source"]},
    )
    body = await response.json()
    assert body["price_source"]["entry_id"] == "price_a" and body["price_source"]["writable"] is True


# ------------------------------------------------------------------ the choice


async def test_a_choice_on_a_site_points_the_site_and_every_charger_follows(
    hass: HomeAssistant, hass_ws_client, freezer
) -> None:
    await two_chargers(hass)
    site = await setup_site(hass, charger_entry_ids=["entry_a", "entry_b"])
    client = await admin(hass, hass_ws_client)
    freezer.move_to(NOW)
    await two_prices(hass)
    # As the migration leaves a site whose chargers disagree: each charger points at its own.
    for charger, price in (("entry_a", "price_a"), ("entry_b", "price_b")):
        entry = hass.config_entries.async_get_entry(charger)
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PRICE_ENTRY_ID: price})

    frame = await set_source(client, "entry_a", "price_b")
    await hass.async_block_till_done()

    assert frame["success"] is True and frame["result"] == {"ok": True}, frame
    assert pointer(hass, site.entry_id) == "price_b"
    assert pointer(hass, "entry_a") is None and pointer(hass, "entry_b") is None
    for charger in ("entry_a", "entry_b"):
        source = price_source_for(hass, charger)
        assert (source.entry_id, source.via) == ("price_b", VIA_SITE)
    assert dashboard(hass)["price_source"]["charger_count"] == 2


async def test_a_choice_without_a_site_points_the_charger(hass: HomeAssistant, hass_ws_client, freezer) -> None:
    await two_chargers(hass)
    client = await admin(hass, hass_ws_client)
    freezer.move_to(NOW)
    await two_prices(hass)

    frame = await set_source(client, "entry_b", "price_b")
    await hass.async_block_till_done()

    assert frame["success"] is True, frame
    assert pointer(hass, "entry_b") == "price_b" and pointer(hass, "entry_a") is None
    assert price_source_for(hass, "entry_b").via == VIA_CHARGER


async def test_the_choice_is_checked(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, freezer
) -> None:
    await two_chargers(hass)
    owner = await admin(hass, hass_ws_client)
    user = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    freezer.move_to(NOW)
    await two_prices(hass)

    refused = await set_source(user, "entry_a", "price_b")
    assert refused["error"]["code"] == "spotnav_not_admin"
    not_a_price = await set_source(owner, "entry_a", "entry_b")
    assert not_a_price["error"]["code"] == "spotnav_unknown_price_entry"
    gone = await set_source(owner, "entry_a", "price_x")
    assert gone["error"]["code"] == "spotnav_unknown_price_entry"
    not_a_charger = await set_source(owner, "price_a", "price_b")
    assert not_a_charger["error"]["code"] == "spotnav_unknown_charger"
    assert pointer(hass, "entry_a") is None


# ------------------------------------------------------------------ the question


async def test_using_one_moves_every_pointer_and_keeps_the_other(hass: HomeAssistant, hass_ws_client, freezer) -> None:
    await two_chargers(hass)
    site = await setup_site(hass, entry_id="site_a", charger_entry_ids=["entry_a"])
    client = await admin(hass, hass_ws_client)
    freezer.move_to(NOW)
    await two_prices(hass)
    for holder, price in ((site.entry_id, "price_b"), ("entry_b", "price_b")):
        entry = hass.config_entries.async_get_entry(holder)
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PRICE_ENTRY_ID: price})

    frame = await answer(client, SE4, "price_a")
    await hass.async_block_till_done()

    assert frame["success"] is True and frame["result"] == {"ok": True}, frame
    assert pointer(hass, site.entry_id) == "price_a" and pointer(hass, "entry_b") == "price_a"
    assert hass.config_entries.async_get_entry("price_b") is not None, "never removed"
    assert price_duplicates(hass) == []
    assert dashboard(hass)["price_source"]["duplicate"] is None


async def test_keeping_both_answers_and_moves_nothing(hass: HomeAssistant, hass_ws_client, freezer) -> None:
    await two_chargers(hass)
    client = await admin(hass, hass_ws_client)
    freezer.move_to(NOW)
    await two_prices(hass)
    entry = hass.config_entries.async_get_entry("entry_a")
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PRICE_ENTRY_ID: "price_b"})

    frame = await answer(client, SE4, None)

    assert frame["success"] is True, frame
    assert pointer(hass, "entry_a") == "price_b"
    assert price_duplicates(hass) == []
    assert price_duplicates(hass, unanswered_only=False)[0]["answered"] is True


async def test_the_answer_is_checked(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, freezer
) -> None:
    await two_chargers(hass)
    owner = await admin(hass, hass_ws_client)
    user = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    freezer.move_to(NOW)
    await two_prices(hass)
    await add_price(hass, entry_id="price_c", area="DK1")

    assert (await answer(user, SE4, "price_a"))["error"]["code"] == "spotnav_not_admin"
    assert (await answer(owner, "NO1", None))["error"]["code"] == "spotnav_unknown_price_duplicate"
    assert (await answer(owner, SE4, "price_c"))["error"]["code"] == "spotnav_unknown_price_entry"
    assert len(price_duplicates(hass)) == 1, "still asked"
