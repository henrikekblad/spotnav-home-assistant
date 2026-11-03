"""The grid tariff as the card and the app read and write it: the dashboard's `grid_tariff` block, the WebSocket
command `spotnav/update_grid_tariff` and the webhook action `update_grid_tariff` (`api/grid_tariff.py`).

Real config entries, the real authenticated socket and the real webhook, on the offline relay (SE4), at a frozen
clock (moved only once the sockets are connected).
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.pricing.price_source import price_source_for, price_write_revision

from .helpers import webhook_dashboard
from .relay import SE4
from .test_price_device import add_price, NOW
from .world import admin, go_auto, non_admin, setup_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

OFF = {"enabled": False, "value": None}
WINDOW = {"days": "weekdays", "from_month": 11, "to_month": 3, "from_hour": 7, "to_hour": 19}
OWNER = {
    "transfer_percent": {"enabled": True, "value": 5.0},
    "transfer_high": {"enabled": True, "value": 60.0},
    "high_load_window": WINDOW,
}


#: The demand block of a charger in no site with nothing set: what every charger had before a component.
NO_DEMAND = {
    "components": [],
    "available": False,
    "reason": "no_site",
    "price_unit": "kr/kW",
    "per_component": [],
    "hour": None,
    "import_target_kw": None,
    "effective_target_kw": None,
}
#: The pilot's tariff (Tekniska verken "alternativ"): a day peak 06-23 and a night peak 23-06, all year.
DAY = {"days": "all", "from_month": 1, "to_month": 12, "from_hour": 6, "to_hour": 23}
NIGHT = {"days": "all", "from_month": 1, "to_month": 12, "from_hour": 23, "to_hour": 6}
PILOT = [
    {"window": DAY, "top_n": 1, "price_per_kw": {"winter": 45.0, "summer": 23.0}},
    {"window": NIGHT, "top_n": 1, "price_per_kw": {"winter": 12.0, "summer": 8.0}},
]


#: The owner's tariff with the pilot's components, as an entry stores it.
STORED = {**OWNER, "demand_components": PILOT}


def update(charger: str, revision: Any, tariff: Any) -> dict[str, Any]:
    return {
        "type": "spotnav/update_grid_tariff",
        "charger_id": charger,
        "expected_write_revision": revision,
        "grid_tariff": tariff,
    }


async def dashboard(client: Any, charger: str = "entry_a") -> dict[str, Any]:
    frame = await ws_call(client, {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": charger})
    assert frame["success"] is True, frame
    return frame["result"]


async def test_the_block_sits_beside_the_price_source_and_is_off_until_written(
    hass: HomeAssistant, hass_ws_client, freezer
) -> None:
    await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    freezer.move_to(NOW)
    await go_auto(hass)

    # No price entry: neither block, as before.
    before = await dashboard(client)
    assert "price_source" not in before and "grid_tariff" not in before

    price = await add_price(hass)
    block = (await dashboard(client))["grid_tariff"]
    assert block == {
        "entry_id": price.entry_id,
        "write_revision": 0,
        "transfer_percent": {"policy": "off", "explicit_value": None, "effective_value": None, "unit": "%"},
        "transfer_high": {"policy": "off", "explicit_value": None, "effective_value": None, "unit": "öre/kWh"},
        "high_load_window": None,
        "demand": NO_DEMAND,
        "writable": True,
    }


async def test_an_administrator_writes_the_owners_tariff_and_every_price_follows(
    hass: HomeAssistant, hass_ws_client, freezer
) -> None:
    await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    freezer.move_to(NOW)
    await go_auto(hass)
    price = await add_price(hass)
    revision = price_source_for(hass, "entry_a").revision
    settings_revision = (await dashboard(client))["settings"]["revision"]

    frame = await ws_call(client, update("entry_a", 0, OWNER))
    await hass.async_block_till_done()

    assert frame["success"] is True, frame
    block = frame["result"]["grid_tariff"]
    assert frame["result"]["ok"] is True
    assert block["write_revision"] == 1
    assert block["transfer_percent"] == {"policy": "manual", "explicit_value": 5.0, "effective_value": 5.0, "unit": "%"}
    assert block["transfer_high"]["effective_value"] == 60.0
    assert block["high_load_window"] == WINDOW
    assert price.data["transfer_percent"] == OWNER["transfer_percent"]
    assert price_write_revision(price) == 1
    assert price_source_for(hass, "entry_a").revision != revision
    # The settings record's rows do not carry it, and its revision moves with the entry's write counter.
    after = await dashboard(client)
    assert after["grid_tariff"] == block
    assert after["settings"]["revision"] == settings_revision + 1
    for row in after["settings"]["overrides"]:
        assert set(row) == {"area_id", "vat", "tax", "transfer"}

    # A window over midnight, every day, all year, reads back as written.
    night = {"days": "all", "from_month": 1, "to_month": 12, "from_hour": 22, "to_hour": 6}
    frame = await ws_call(client, update("entry_a", 1, {**OWNER, "high_load_window": night}))
    assert frame["success"] is True, frame
    assert frame["result"]["grid_tariff"]["high_load_window"] == night
    assert price.data["high_load_window"] == night
    frame = await ws_call(client, update("entry_a", 2, OWNER))
    assert frame["success"] is True, frame

    # The same write again names an old counter: a conflict, and nothing moves.
    stale = await ws_call(client, update("entry_a", 0, {**OWNER, "transfer_percent": OFF}))
    assert stale["success"] is False and stale["error"]["code"] == "revision_conflict"
    assert price.data["transfer_percent"] == OWNER["transfer_percent"]

    # The card's "Remove the high-load fee": the fee off (its figure kept) and the window gone, in one write.
    removed = {**OWNER, "transfer_high": {"enabled": False, "value": 60.0}, "high_load_window": None}
    frame = await ws_call(client, update("entry_a", 3, removed))
    assert frame["success"] is True, frame
    block = frame["result"]["grid_tariff"]
    assert block["transfer_high"]["policy"] == "off" and block["high_load_window"] is None
    assert price.data["high_load_window"] is None


@pytest.mark.parametrize(
    ("tariff", "code"),
    [
        ({**OWNER, "high_load_window": None}, "invalid_fiscal"),
        ({**OWNER, "transfer_percent": {"enabled": True, "value": None}}, "invalid_fiscal"),
        ({**OWNER, "transfer_percent": {"enabled": "true", "value": 5}}, "invalid_fiscal"),
        ({**OWNER, "high_load_window": {**WINDOW, "to_hour": 7}}, "invalid_fiscal"),
        ({**OWNER, "high_load_window": {**WINDOW, "from_hour": 7.5}}, "invalid_fiscal"),
        ({**OWNER, "high_load_window": {**WINDOW, "days": "weekends"}}, "invalid_fiscal"),
        ({**OWNER, "high_load_window": {**WINDOW, "to_month": 13}}, "invalid_fiscal"),
        # The shape before the window took months and days: refused, never read as a window.
        (
            {**OWNER, "high_load_window": {"weekdays_only": True, "from_hour": 7, "to_hour": 19, "winter_only": True}},
            "invalid_fiscal",
        ),
        ({**OWNER, "demand": None}, "invalid_fiscal"),
        ({**OWNER, "demand_components": None}, "invalid_fiscal"),
        ({**OWNER, "demand_components": [{**PILOT[0], "top_n": 2}]}, "invalid_fiscal"),
        ({**OWNER, "demand_components": [{**PILOT[0], "price_per_kw": {"winter": -1, "summer": 0}}]}, "invalid_fiscal"),
        ({**OWNER, "demand_components": [{**PILOT[1], "window": {**NIGHT, "to_hour": 23}}]}, "invalid_fiscal"),
        ({**OWNER, "demand_components": PILOT * 2}, "invalid_fiscal"),
        ({"transfer_percent": OFF}, "invalid_fiscal"),
        (None, "invalid_fiscal"),
    ],
)
async def test_a_body_that_cannot_be_priced_is_refused_and_nothing_is_written(
    hass: HomeAssistant, hass_ws_client, freezer, tariff: Any, code: str
) -> None:
    await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    freezer.move_to(NOW)
    await go_auto(hass)
    price = await add_price(hass)

    frame = await ws_call(client, update("entry_a", 0, tariff))

    assert frame["success"] is False and frame["error"]["code"] == code
    assert price_write_revision(price) == 0 and "transfer_percent" not in price.data


async def test_only_an_administrator_and_only_with_a_price_entry(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, freezer
) -> None:
    await setup_charger(hass)
    owner = await admin(hass, hass_ws_client)
    user = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    freezer.move_to(NOW)
    await go_auto(hass, area_id=SE4)

    missing = await ws_call(owner, update("entry_a", 0, OWNER))
    assert missing["success"] is False and missing["error"]["code"] == "spotnav_no_price_entry"
    bad_revision = await ws_call(owner, update("entry_a", True, OWNER))
    assert bad_revision["success"] is False and bad_revision["error"]["code"] == "invalid_number"

    price = await add_price(hass)
    refused = await ws_call(user, update("entry_a", 0, OWNER))
    assert refused["success"] is False and refused["error"]["code"] == "spotnav_not_admin"
    assert price_write_revision(price) == 0
    assert (await dashboard(user))["grid_tariff"]["writable"] is False


async def test_the_app_reads_the_block_only_when_it_asks_and_writes_it_over_the_webhook(
    hass: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    await setup_charger(hass)
    client = await hass_client_no_auth()
    freezer.move_to(NOW)
    await go_auto(hass)
    price = await add_price(hass)

    # An app that does not read it never meets the key.
    assert "grid_tariff" not in await webhook_dashboard(client, "webhook-a")
    asked = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "dashboard", "api_version": 1, "reads": ["grid_tariff"]},
    )
    asked_block = (await asked.json())["grid_tariff"]
    assert asked_block["writable"] is True
    # The level the running hour is held under only for an app that reads it (its decoder refuses unknown keys).
    assert "effective_target_kw" not in asked_block["demand"]
    level = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "dashboard", "api_version": 1, "reads": ["grid_tariff", "effective_target"]},
    )
    assert (await level.json())["grid_tariff"]["demand"]["effective_target_kw"] is None

    response = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "update_grid_tariff", "expected_write_revision": 0, "grid_tariff": OWNER},
    )
    body = await response.json()
    assert response.status == 200, body
    assert (body["ok"], body["error"], body["field_errors"], body["action"]) == (True, None, [], "update_grid_tariff")
    assert body["grid_tariff"]["transfer_high"]["effective_value"] == 60.0
    assert "effective_target_kw" not in body["grid_tariff"]["demand"]
    assert price.data["transfer_high"] == OWNER["transfer_high"]

    conflict = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "update_grid_tariff", "expected_write_revision": 0, "grid_tariff": OWNER},
    )
    assert conflict.status == 409 and (await conflict.json())["error"] == "revision_conflict"
    invalid = await client.post(
        "/api/webhook/webhook-a",
        json={
            "version": 1, "action": "update_grid_tariff", "expected_write_revision": 1,
            "grid_tariff": {**OWNER, "high_load_window": {**WINDOW, "to_hour": 25}},
        },
    )
    invalid_body = await invalid.json()
    assert invalid.status == 400 and invalid_body["error"] == "invalid_fiscal"
    assert invalid_body["field_errors"] == [{"field": "high_load_window", "code": "invalid_value"}]


async def test_an_older_apps_full_settings_write_keeps_the_grid_tariff(
    hass: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    await setup_charger(hass)
    client = await hass_client_no_auth()
    freezer.move_to(NOW)
    await go_auto(hass)
    price = await add_price(hass, **STORED)
    record = (await webhook_dashboard(client, "webhook-a"))["settings"]
    body = {key: value for key, value in record.items() if key != "revision"}
    rows = [
        {**row, "vat": {"enabled": True, "value": 12.0}} if row["area_id"] == SE4 else row for row in body["overrides"]
    ]

    response = await client.post(
        "/api/webhook/webhook-a",
        json={
            "version": 1, "action": "settings", "expected_revision": record["revision"],
            "settings": {**body, "overrides": rows},
        },
    )
    await hass.async_block_till_done()

    assert response.status == 200, await response.text()
    assert price.data["vat"] == {"enabled": True, "value": 12.0}
    for key in ("transfer_percent", "transfer_high", "high_load_window", "demand_components"):
        assert price.data[key] == STORED[key], key
    # An echo of the record as read moves no fee: the price entry is not written at all.
    record = (await webhook_dashboard(client, "webhook-a"))["settings"]
    written = price_write_revision(price)
    echo = await client.post(
        "/api/webhook/webhook-a",
        json={
            "version": 1, "action": "settings", "expected_revision": record["revision"],
            "settings": {key: value for key, value in record.items() if key != "revision"},
        },
    )
    assert echo.status == 200, await echo.text()
    assert price_write_revision(price) == written


async def test_a_write_of_the_tariff_as_stored_counts_nothing(hass: HomeAssistant, hass_ws_client, freezer) -> None:
    await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    freezer.move_to(NOW)
    await go_auto(hass)
    price = await add_price(hass)
    revision = price_source_for(hass, "entry_a").revision

    # All off on an entry that stores nothing is the same tariff: no keys, no count, no replan.
    all_off = {"transfer_percent": OFF, "transfer_high": OFF, "high_load_window": None}
    frame = await ws_call(client, update("entry_a", 0, all_off))
    assert frame["success"] is True and frame["result"]["grid_tariff"]["write_revision"] == 0
    assert price_write_revision(price) == 0 and "transfer_percent" not in price.data
    assert price_source_for(hass, "entry_a").revision == revision

    assert (await ws_call(client, update("entry_a", 0, OWNER)))["success"] is True
    assert price_write_revision(price) == 1
    again = await ws_call(client, update("entry_a", 1, OWNER))
    assert again["success"] is True and again["result"]["grid_tariff"]["write_revision"] == 1
    assert price_write_revision(price) == 1
    # The compare-and-set still judges an identical write.
    stale = await ws_call(client, update("entry_a", 0, OWNER))
    assert stale["success"] is False and stale["error"]["code"] == "revision_conflict"


async def test_the_apps_area_change_keeps_the_high_fees_figure_off(
    hass: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    await setup_charger(hass)
    client = await hass_client_no_auth()
    freezer.move_to(NOW)
    await go_auto(hass)
    price = await add_price(hass, **OWNER)
    record = (await webhook_dashboard(client, "webhook-a"))["settings"]
    body = {key: value for key, value in record.items() if key != "revision"}
    rows = [row for row in body["overrides"] if row["area_id"] != "NO1"]
    rows.append({"area_id": "NO1", "vat": OFF, "tax": OFF, "transfer": OFF})

    response = await client.post(
        "/api/webhook/webhook-a",
        json={
            "version": 1, "action": "settings", "expected_revision": record["revision"],
            "settings": {**body, "area_id": "NO1", "overrides": sorted(rows, key=lambda row: row["area_id"])},
        },
    )
    await hass.async_block_till_done()

    assert response.status == 200, await response.text()
    assert price.data["area_id"] == "NO1"
    assert price.data["transfer_high"] == {"enabled": False, "value": 60.0}
    assert price.data["transfer_percent"] == OWNER["transfer_percent"]
    assert price.data["high_load_window"] == WINDOW


async def test_the_demand_components_are_written_read_back_and_kept_by_a_writer_that_does_not_know_them(
    hass: HomeAssistant, hass_ws_client, hass_client_no_auth, freezer
) -> None:
    await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    app = await hass_client_no_auth()
    freezer.move_to(NOW)
    await go_auto(hass)
    price = await add_price(hass)
    revision = price_source_for(hass, "entry_a").revision

    frame = await ws_call(client, update("entry_a", 0, {**OWNER, "demand_components": PILOT}))
    assert frame["success"] is True, frame
    demand = frame["result"]["grid_tariff"]["demand"]
    assert demand == {**NO_DEMAND, "components": PILOT}
    assert price.data["demand_components"] == PILOT
    assert price_source_for(hass, "entry_a").revision != revision

    # A body without the components (an older card or app) changes the fees and keeps them.
    frame = await ws_call(client, update("entry_a", 1, {**OWNER, "transfer_percent": OFF}))
    assert frame["success"] is True, frame
    assert frame["result"]["grid_tariff"]["demand"]["components"] == PILOT
    assert price.data["demand_components"] == PILOT
    # The same body again is the tariff as stored: nothing written.
    again = await ws_call(client, update("entry_a", 2, {**OWNER, "transfer_percent": OFF}))
    assert again["result"]["grid_tariff"]["write_revision"] == 2

    # The app writes them over the webhook; a bad one is refused naming the field.
    one = [{"window": None, "top_n": 3, "price_per_kw": {"winter": None, "summer": None}}]
    response = await app.post(
        "/api/webhook/webhook-a",
        json={
            "version": 1, "action": "update_grid_tariff", "expected_write_revision": 2,
            "grid_tariff": {**OWNER, "demand_components": one},
        },
    )
    body = await response.json()
    assert response.status == 200, body
    assert body["grid_tariff"]["demand"]["components"] == one
    refused = await app.post(
        "/api/webhook/webhook-a",
        json={
            "version": 1, "action": "update_grid_tariff", "expected_write_revision": 3,
            "grid_tariff": {**OWNER, "demand_components": [{**one[0], "top_n": 4}]},
        },
    )
    refused_body = await refused.json()
    assert refused.status == 400 and refused_body["error"] == "invalid_fiscal"
    assert refused_body["field_errors"] == [{"field": "demand_components", "code": "invalid_value"}]

    # Removing every component is a write of an empty list.
    frame = await ws_call(client, update("entry_a", 3, {**OWNER, "demand_components": []}))
    assert frame["result"]["grid_tariff"]["demand"]["components"] == []
    assert price.data["demand_components"] == []
