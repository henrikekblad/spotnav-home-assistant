"""Settings writes go to the price source: a charger's area and fees, written by the card or the app, change its
electricity price entry in place, never the charger's own record (`api/settings.py:PriceRoute`).

Real config entries, the real authenticated socket and the real webhook, on the offline relay (SE4, NO1, DK1),
at a frozen clock.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.planning.auto_settings import AreaAutoSettings, FiscalOverride
from custom_components.spotnav.pricing.price_source import fees_from_data, price_write_revision
from custom_components.spotnav.runtime import domain_data, preview_for

from .helpers import webhook_dashboard
from .messages import read_settings_message, update_settings_message
from .relay import SE4
from .test_price_device import add_price, NOW
from .world import admin, call, entity_id, go_auto, non_admin, settings_of, setup_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

OFF = {"enabled": False, "value": None}
SUGGESTED = {"enabled": True, "value": None}
#: The charger's own rows before the price entry: SE4 with its own tax, and DK1 from an earlier address.
OWN_SE4 = AreaAutoSettings(area_id=SE4, vat=FiscalOverride(enabled=True), tax=FiscalOverride(enabled=True, value=42.0))
OWN_DK1 = AreaAutoSettings(area_id="DK1", transfer=FiscalOverride(enabled=True, value=5.0))


async def read(client: Any, charger: str = "entry_a") -> dict[str, Any]:
    frame = await ws_call(client, read_settings_message(charger))
    assert frame["success"] is True, frame
    return frame["result"]["settings"]


def body_of(record: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """What the card sends back: the record it read, without `revision` and the read-only facts, edited."""
    body = {key: value for key, value in record.items() if key not in ("revision", "fiscal_included")}
    body.update(changes)
    return body


def with_row(record: dict[str, Any], area: str, **fees: Any) -> list[dict[str, Any]]:
    """The record's rows with one area's row changed (or added)."""
    rows = [dict(row) for row in record["overrides"] if row["area_id"] != area]
    base = next((row for row in record["overrides"] if row["area_id"] == area), {"area_id": area, "vat": OFF, "tax": OFF, "transfer": OFF})
    return sorted([*rows, {**base, **fees}], key=lambda row: row["area_id"])


async def write(client: Any, revision: int, body: dict[str, Any], charger: str = "entry_a") -> dict[str, Any]:
    return await ws_call(client, update_settings_message(charger, revision, body))


def fees(hass: HomeAssistant, entry_id: str = "price_a") -> AreaAutoSettings:
    entry = hass.config_entries.async_get_entry(entry_id)
    return fees_from_data(entry.data["area_id"], entry.data)


async def two_chargers(hass: HomeAssistant) -> None:
    """Two chargers, set up before a socket connects (the socket's server takes no new routes after)."""
    await setup_charger(hass)
    await setup_charger(hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b")


async def on_one_price(hass: HomeAssistant, freezer) -> Any:
    """Both chargers in Auto, then one electricity price entry for them, at the fixture's morning. The clock moves
    only once the sockets are connected: a token issued "later" than the clock is refused."""
    freezer.move_to(NOW)
    await go_auto(hass, "entry_a", overrides=(OWN_SE4, OWN_DK1))
    await go_auto(hass, "entry_b")
    return await add_price(hass)


# ------------------------------------------------------------------ the card


async def test_a_vat_change_from_the_card_changes_the_price_entry_and_every_charger_follows(
    hass: HomeAssistant, hass_ws_client, freezer
) -> None:
    await two_chargers(hass)
    client = await admin(hass, hass_ws_client)
    price = await on_one_price(hass, freezer)
    record = await read(client)
    own_before = settings_of(hass, "entry_a")
    now = entity_id(hass, price.entry_id, "price_now", "sensor")
    price_before = float(hass.states.get(now).state)
    before = {charger: preview_for(hass, charger).snapshot() for charger in ("entry_a", "entry_b")}
    states: list[str] = []

    def record_state(event: Any) -> None:
        if event.data["entity_id"] == now:
            states.append(event.data["new_state"].state)

    hass.bus.async_listen("state_changed", record_state)

    vat = {"enabled": True, "value": 50.0}
    frame = await write(client, record["revision"], body_of(record, overrides=with_row(record, SE4, vat=vat)))
    await hass.async_block_till_done()

    assert frame["success"] is True and frame["result"]["ok"] is True, frame
    # The price entry holds the new VAT, counted once, updated in place (never reloaded).
    assert fees(hass).vat == FiscalOverride(enabled=True, value=50.0)
    assert price_write_revision(price) == 1 and price.state.value == "loaded"
    # The charger's own area and rows are as they were; only its revision moved (the full replacement).
    own = settings_of(hass, "entry_a")
    assert (own.area_id, own.overrides) == (own_before.area_id, own_before.overrides)
    # The answer reads the price entry, and its revision moved with both writes.
    answer = frame["result"]["settings"]
    assert next(row for row in answer["overrides"] if row["area_id"] == SE4)["vat"] == vat
    assert answer["revision"] == own.revision + 1 == record["revision"] + 2
    assert (await read(client))["revision"] == answer["revision"]
    # Every charger on the entry replans; the price device's "price now" moves at once, never unavailable.
    for charger in ("entry_a", "entry_b"):
        after = preview_for(hass, charger).snapshot()
        assert after.price_revision != before[charger].price_revision, charger
        assert after.proposal.estimated_cost > before[charger].proposal.estimated_cost, charger
    assert float(hass.states.get(now).state) > price_before
    assert states and "unavailable" not in states


async def test_rows_for_other_areas_stay_the_chargers_own(hass: HomeAssistant, hass_ws_client, freezer) -> None:
    await two_chargers(hass)
    client = await admin(hass, hass_ws_client)
    price = await on_one_price(hass, freezer)
    record = await read(client)

    transfer = {"enabled": True, "value": 9.0}
    frame = await write(client, record["revision"], body_of(record, overrides=with_row(record, "DK1", transfer=transfer)))

    assert frame["result"]["ok"] is True, frame
    assert settings_of(hass, "entry_a").override_for("DK1").transfer == FiscalOverride(enabled=True, value=9.0)
    assert settings_of(hass, "entry_a").override_for(SE4) == OWN_SE4, "the price entry's area stays as stored"
    assert price_write_revision(price) == 0, "nothing of the price entry's moved"


async def test_an_area_change_moves_the_price_entry_and_resubscribes_it(
    hass: HomeAssistant, hass_ws_client, freezer
) -> None:
    await two_chargers(hass)
    client = await admin(hass, hass_ws_client)
    price = await on_one_price(hass, freezer)
    record = await read(client)
    manager = domain_data(hass).price_refresh
    assert manager.owner_area("price:price_a") == SE4

    no1 = {"vat": SUGGESTED, "tax": {"enabled": True, "value": 7.0}, "transfer": OFF}
    frame = await write(client, record["revision"], body_of(record, area_id="NO1", overrides=with_row(record, "NO1", **no1)))
    await hass.async_block_till_done()

    assert frame["result"]["ok"] is True, frame
    assert price.data["area_id"] == "NO1" and price.title == "Electricity price NO1"
    assert fees(hass).tax == FiscalOverride(enabled=True, value=7.0)
    assert manager.owner_area("price:price_a") == "NO1", "the device resubscribed to the new area"
    assert settings_of(hass, "entry_a").area_id == SE4, "the charger's own area is not the price entry's"
    assert "NO1" not in {row.area_id for row in settings_of(hass, "entry_a").overrides}
    for charger in ("entry_a", "entry_b"):
        assert preview_for(hass, charger).snapshot().area_id == "NO1"
    assert frame["result"]["settings"]["area_id"] == "NO1"


async def test_a_body_read_before_the_price_entry_changed_is_a_conflict(
    hass: HomeAssistant, hass_ws_client, freezer
) -> None:
    await two_chargers(hass)
    client = await admin(hass, hass_ws_client)
    price = await on_one_price(hass, freezer)
    record = await read(client)
    own_before = settings_of(hass, "entry_a")

    # The fees change elsewhere (the price device's own number) after the card read the record.
    await call(hass, "number", "set_value", {"entity_id": entity_id(hass, price.entry_id, "energy_tax", "number"), "value": 40.0})
    assert price_write_revision(price) == 1

    stale = body_of(record, amps=16, overrides=with_row(record, SE4, vat=OFF))
    frame = await write(client, record["revision"], stale)

    assert frame["success"] is True and frame["result"]["ok"] is False
    assert frame["result"]["error"] == "revision_conflict"
    assert fees(hass).vat == FiscalOverride(enabled=True) and fees(hass).tax.value == 40.0, "the newer fees stand"
    assert settings_of(hass, "entry_a") == own_before, "nothing written to the charger either"
    # The conflict carries the current record; a retry against it goes through.
    current = frame["result"]["settings"]
    assert current["revision"] == record["revision"] + 1
    retried = await write(client, current["revision"], body_of(current, amps=16))
    assert retried["result"]["ok"] is True


async def test_the_area_and_fees_stay_an_administrators(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, freezer
) -> None:
    await two_chargers(hass)
    owner = await admin(hass, hass_ws_client)
    user = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    price = await on_one_price(hass, freezer)
    record = await read(owner)
    allowed = await write(owner, record["revision"], body_of(record, users_change_plan=True))
    assert allowed["result"]["ok"] is True
    record = allowed["result"]["settings"]

    refused = await write(user, record["revision"], body_of(record, overrides=with_row(record, SE4, vat=OFF)))
    assert refused["success"] is False and refused["error"]["code"] == "spotnav_not_admin"
    assert price_write_revision(price) == 0

    # The plan itself, with the price entry's area and fees echoed as read, is the user's.
    planned = await write(user, record["revision"], body_of(record, amps=16))
    assert planned["success"] is True and planned["result"]["ok"] is True, planned
    assert settings_of(hass, "entry_a").amps == 16 and price_write_revision(price) == 0


async def test_without_a_price_entry_the_body_is_the_chargers_own(hass: HomeAssistant, hass_ws_client, freezer) -> None:
    await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    freezer.move_to(NOW)
    await go_auto(hass, overrides=(OWN_SE4,))
    record = await read(client)
    assert record["revision"] == settings_of(hass, "entry_a").revision

    frame = await write(client, record["revision"], body_of(record, overrides=with_row(record, SE4, vat=OFF)))

    assert frame["result"]["ok"] is True
    assert settings_of(hass, "entry_a").override_for(SE4).vat == FiscalOverride()
    assert frame["result"]["settings"]["revision"] == settings_of(hass, "entry_a").revision


# ------------------------------------------------------------------ the app


async def test_the_apps_webhook_routes_the_same_way(hass: HomeAssistant, hass_client_no_auth, freezer) -> None:
    await two_chargers(hass)
    client = await hass_client_no_auth()
    price = await on_one_price(hass, freezer)
    record = (await webhook_dashboard(client, "webhook-a"))["settings"]
    own_before = settings_of(hass, "entry_a")

    tax = {"enabled": True, "value": 12.0}
    response = await client.post(
        "/api/webhook/webhook-a",
        json={
            "version": 1,
            "action": "settings",
            "expected_revision": record["revision"],
            "settings": body_of(record, overrides=with_row(record, SE4, tax=tax)),
        },
    )
    await hass.async_block_till_done()

    assert response.status == 200, await response.text()
    assert fees(hass).tax == FiscalOverride(enabled=True, value=12.0)
    assert settings_of(hass, "entry_a").overrides == own_before.overrides

    # The same body again is stale: the price entry moved since the record it was read from.
    again = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "settings", "expected_revision": record["revision"], "settings": body_of(record)},
    )
    assert again.status == 409
    assert (await again.json())["error"] == "revision_conflict"
    assert fees(hass).tax == FiscalOverride(enabled=True, value=12.0)
    assert price_write_revision(price) == 1


# ------------------------------------------------------------------ the options flow


async def test_the_options_flow_saves_in_place_without_a_reload(hass: HomeAssistant, freezer, monkeypatch) -> None:
    freezer.move_to(NOW)
    await setup_charger(hass)
    await go_auto(hass)
    price = await add_price(hass)
    now = entity_id(hass, price.entry_id, "price_now", "sensor")
    before = float(hass.states.get(now).state)
    reloads: list[str] = []
    original = hass.config_entries.async_reload

    async def counted(entry_id: str) -> bool:
        reloads.append(entry_id)
        return await original(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", counted)
    states: list[str] = []

    def record_state(event: Any) -> None:
        if event.data["entity_id"] == now:
            states.append(event.data["new_state"].state)

    hass.bus.async_listen("state_changed", record_state)
    plan_before = preview_for(hass, "entry_a").snapshot()

    result = await hass.config_entries.options.async_init(price.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"area_id": SE4})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"vat_mode": "suggested", "tax_mode": "suggested", "transfer_mode": "manual", "transfer_value": 90.0},
    )
    await hass.async_block_till_done()

    assert reloads == []
    assert price.data["transfer"] == {"enabled": True, "value": 90.0} and price_write_revision(price) == 1
    assert float(hass.states.get(now).state) > before
    assert states and "unavailable" not in states
    assert preview_for(hass, "entry_a").snapshot().price_revision != plan_before.price_revision
    assert hass.config_entries.async_get_entry(price.entry_id).domain == DOMAIN
