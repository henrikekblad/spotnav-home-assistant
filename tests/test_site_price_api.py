"""The price and solar settings by site, for the shared settings popover of a home with or without a charger:
`spotnav/get_market_options`, `spotnav/update_grid_tariff`, `spotnav/choose_price_source` and
`spotnav/update_site_settings` with `site_id` in place of the charger, the new `spotnav/update_price`, and the
`price_settings` and `site` blocks of `spotnav/get_site_settings_view`.

Real config entries over the real authenticated socket, on the offline relay (SE4)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import site_settings as site_settings_module
from custom_components.spotnav.api import site_view as site_view_module
from custom_components.spotnav.const import CONF_PRICE_ENTRY_ID, CONF_SOLAR_FORECAST_ENTRIES
from custom_components.spotnav.pricing.price_source import price_write_revision, site_price_source

from .relay import SE4
from .test_price_device import add_price
from .world import admin, forecast_entry, non_admin, setup_site, setup_site_with_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

FIXTURES = Path(__file__).parent / "fixtures"
OFF = {"enabled": False, "value": None}
OWNER = {
    "transfer_percent": {"enabled": True, "value": 5.0},
    "transfer_high": OFF,
    "high_load_window": None,
    "supplier_markup": {"enabled": True, "value": 4.5},
    "sell_adjustment": {"enabled": True, "value": -2.0},
}


@pytest.fixture(autouse=True)
def _forecast_capable_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """`roof` is the one Energy-dashboard-capable domain these tests offer."""

    async def _fake(_hass: HomeAssistant) -> frozenset[str]:
        return frozenset({"roof"})

    monkeypatch.setattr(site_settings_module, "async_forecast_capable_domains", _fake)
    monkeypatch.setattr(site_view_module, "async_forecast_capable_domains", _fake)


async def _home(hass: HomeAssistant, charger: bool) -> str:
    """A site with or without one charger; its id."""
    if charger:
        _, site = await setup_site_with_charger(hass, site_entry_id="site_home")
    else:
        site = await setup_site(hass, entry_id="site_home", title="Hemma")
    return site.entry_id


async def _view(client: Any, site_id: str = "site_home") -> dict[str, Any]:
    frame = await ws_call(client, {"type": "spotnav/get_site_settings_view", "api_version": 1, "site_id": site_id})
    assert frame["success"] is True, frame
    return frame["result"]


# ------------------------------------------------------------------ the market options


@pytest.mark.parametrize("charger", [False, True])
async def test_the_market_options_by_site_name_the_sites_area(hass: HomeAssistant, hass_ws_client, charger: bool) -> None:
    site_id = await _home(hass, charger)
    client = await admin(hass, hass_ws_client)
    ask = {"type": "spotnav/get_market_options", "api_version": 1, "site_id": site_id}
    before = await ws_call(client, ask)
    assert before["success"] is True and before["result"]["configured_area"] is None
    assert before["result"]["areas"], "the bundled list"
    await add_price(hass)
    after = await ws_call(client, ask)
    assert after["result"]["configured_area"] == SE4
    unknown = await ws_call(client, {**ask, "site_id": "nope"})
    assert unknown["error"]["code"] == "spotnav_unknown_site"
    neither = await ws_call(client, {"type": "spotnav/get_market_options", "api_version": 1})
    assert neither["error"]["code"] == "spotnav_charger_required"


# ------------------------------------------------------------------ the grid tariff


@pytest.mark.parametrize("charger", [False, True])
async def test_the_grid_tariff_by_site_writes_the_sites_price(hass: HomeAssistant, hass_ws_client, charger: bool) -> None:
    site_id = await _home(hass, charger)
    client = await admin(hass, hass_ws_client)
    write = {
        "type": "spotnav/update_grid_tariff",
        "site_id": site_id,
        "expected_write_revision": 0,
        "grid_tariff": OWNER,
    }
    none = await ws_call(client, write)
    assert none["error"]["code"] == "spotnav_no_price_entry"

    price = await add_price(hass)
    frame = await ws_call(client, write)
    await hass.async_block_till_done()
    assert frame["success"] is True, frame
    block = frame["result"]["grid_tariff"]
    assert block["entry_id"] == price.entry_id and block["write_revision"] == 1
    assert block["supplier_markup"]["effective_value"] == 4.5
    assert block["sell_adjustment"]["effective_value"] == -2.0
    # The site measures its own demand charge, with or without a charger.
    assert block["demand"]["reason"] != "no_site"
    assert price_write_revision(hass.config_entries.async_get_entry(price.entry_id)) == 1

    stale = await ws_call(client, write)
    assert stale["error"]["code"] == "revision_conflict"


async def test_the_grid_tariff_by_site_is_for_administrators(hass: HomeAssistant, hass_ws_client, hass_read_only_access_token) -> None:
    await _home(hass, False)
    await add_price(hass)
    client = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    frame = await ws_call(
        client,
        {"type": "spotnav/update_grid_tariff", "site_id": "site_home", "expected_write_revision": 0, "grid_tariff": OWNER},
    )
    assert frame["error"]["code"] == "spotnav_not_admin"


async def test_a_sold_kwh_counts_on_a_site_without_a_charger(hass: HomeAssistant, hass_ws_client) -> None:
    roof = forecast_entry(hass, entry_id="roof_a", title="Roof A")
    await setup_site(hass, entry_id="site_home", solar_forecast_entries=[roof.entry_id])
    await add_price(hass)
    client = await admin(hass, hass_ws_client)
    assert (await _view(client))["price_settings"]["grid_tariff"]["sells"] is True


# ------------------------------------------------------------------ the price source


async def test_choosing_the_price_by_site_points_the_site_and_its_chargers_follow(
    hass: HomeAssistant, hass_ws_client
) -> None:
    await _home(hass, True)
    await add_price(hass, entry_id="price_a")
    await add_price(hass, entry_id="price_b", title="Hemma SE4")
    client = await admin(hass, hass_ws_client)
    charger = hass.config_entries.async_get_entry("entry_a")
    hass.config_entries.async_update_entry(charger, data={**charger.data, CONF_PRICE_ENTRY_ID: "price_a"})

    frame = await ws_call(
        client, {"type": "spotnav/choose_price_source", "site_id": "site_home", "price_entry_id": "price_b"}
    )
    assert frame["result"] == {"ok": True}
    assert hass.config_entries.async_get_entry("site_home").data[CONF_PRICE_ENTRY_ID] == "price_b"
    assert CONF_PRICE_ENTRY_ID not in hass.config_entries.async_get_entry("entry_a").data or (
        hass.config_entries.async_get_entry("entry_a").data[CONF_PRICE_ENTRY_ID] is None
    )
    view = await _view(client)
    assert view["price"]["entry_id"] == "price_b"
    assert [choice["entry_id"] for choice in view["price_settings"]["price_source"]["choices"]] == ["price_a", "price_b"]


async def test_choosing_the_price_by_site_without_a_charger(hass: HomeAssistant, hass_ws_client) -> None:
    await _home(hass, False)
    await add_price(hass, entry_id="price_a")
    await add_price(hass, entry_id="price_b", title="Hemma SE4")
    client = await admin(hass, hass_ws_client)
    assert (await _view(client))["price"] is None, "two prices and no pointer: none applies yet"

    ask = {"type": "spotnav/choose_price_source", "site_id": "site_home", "price_entry_id": "price_b"}
    assert (await ws_call(client, ask))["result"] == {"ok": True}
    assert site_price_source(hass, "site_home").entry_id == "price_b"
    assert (await ws_call(client, {**ask, "price_entry_id": "nope"}))["error"]["code"] == "spotnav_unknown_price_entry"
    assert (await ws_call(client, {**ask, "site_id": "entry_x"}))["error"]["code"] == "spotnav_unknown_site"


# ------------------------------------------------------------------ the area and fees


def _row(area: str = SE4, **fees: Any) -> dict[str, Any]:
    return {"area_id": area, "vat": OFF, "tax": OFF, "transfer": OFF, **fees}


@pytest.mark.parametrize("charger", [False, True])
async def test_the_area_and_fees_by_site_write_the_price_entry(hass: HomeAssistant, hass_ws_client, charger: bool) -> None:
    site_id = await _home(hass, charger)
    client = await admin(hass, hass_ws_client)
    write = {
        "type": "spotnav/update_price",
        "site_id": site_id,
        "expected_write_revision": 0,
        "price": _row(vat={"enabled": True, "value": 25.0}, transfer={"enabled": True, "value": 31.5}),
    }
    assert (await ws_call(client, write))["error"]["code"] == "spotnav_no_price_entry"

    price = await add_price(hass)
    frame = await ws_call(client, write)
    await hass.async_block_till_done()
    assert frame["result"] == {"ok": True}, frame
    stored = hass.config_entries.async_get_entry(price.entry_id)
    assert stored.data["vat"] == {"enabled": True, "value": 25.0}
    assert stored.data["transfer"] == {"enabled": True, "value": 31.5}
    assert price_write_revision(stored) == 1
    fees = (await _view(client))["price_settings"]["fees"]
    assert fees["transfer"] == {"enabled": True, "value": 31.5}

    assert (await ws_call(client, write))["error"]["code"] == "revision_conflict"
    moved = await ws_call(client, {**write, "expected_write_revision": 1, "price": _row("SE3")})
    assert moved["result"] == {"ok": True}
    assert hass.config_entries.async_get_entry(price.entry_id).data["area_id"] == "SE3"
    assert hass.config_entries.async_get_entry(price.entry_id).title == "Electricity price SE3"


async def test_the_area_and_fees_by_site_are_checked(hass: HomeAssistant, hass_ws_client, hass_read_only_access_token) -> None:
    await _home(hass, False)
    await add_price(hass)
    client = await admin(hass, hass_ws_client)
    write = {"type": "spotnav/update_price", "site_id": "site_home", "expected_write_revision": 0, "price": _row()}
    assert (await ws_call(client, {**write, "price": _row("XX9")}))["error"]["code"] == "invalid_area"
    negative = _row(tax={"enabled": True, "value": -1.0})
    assert (await ws_call(client, {**write, "price": negative}))["error"]["code"] == "invalid_fiscal"
    assert (await ws_call(client, {**write, "price": {"area_id": SE4}}))["error"]["code"] == "missing_field"
    assert (await ws_call(client, {**write, "expected_write_revision": "0"}))["error"]["code"] == "invalid_number"
    assert (await ws_call(client, {**write, "site_id": "nope"}))["error"]["code"] == "spotnav_unknown_site"
    reader = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    assert (await ws_call(reader, write))["error"]["code"] == "spotnav_not_admin"


# ------------------------------------------------------------------ solar


@pytest.mark.parametrize("charger", [False, True])
async def test_the_solar_forecast_by_site(hass: HomeAssistant, hass_ws_client, charger: bool) -> None:
    site_id = await _home(hass, charger)
    roof = forecast_entry(hass, entry_id="roof_a", title="Roof A")
    client = await admin(hass, hass_ws_client)
    block = (await _view(client, site_id))["site"]
    assert block["solar_forecast"] == {"selected": [], "choices": [{"id": "roof_a", "title": "Roof A"}]}
    assert block["charger_count"] == (1 if charger else 0) and block["writable"] is True

    frame = await ws_call(
        client,
        {
            "type": "spotnav/update_site_settings",
            "api_version": 1,
            "site_id": site_id,
            "expected": {"solar_forecast": []},
            "changes": {"solar_forecast": [roof.entry_id]},
        },
    )
    await hass.async_block_till_done()
    answer = frame["result"]
    assert answer["ok"] is True, answer
    assert answer["site"]["solar_forecast"]["selected"] == [roof.entry_id]
    assert hass.config_entries.async_get_entry(site_id).data[CONF_SOLAR_FORECAST_ENTRIES] == [roof.entry_id]

    stale = (
        await ws_call(
            client,
            {
                "type": "spotnav/update_site_settings",
                "api_version": 1,
                "site_id": site_id,
                "expected": {"solar_forecast": []},
                "changes": {"solar_forecast": []},
            },
        )
    )["result"]
    assert stale["error"] == "spotnav_conflict" and stale["site"]["solar_forecast"]["selected"] == [roof.entry_id]
    unknown = (
        await ws_call(
            client,
            {"type": "spotnav/update_site_settings", "api_version": 1, "site_id": "nope", "expected": {}, "changes": {}},
        )
    )["result"]
    assert unknown["error"] == "spotnav_unknown_site"


# ------------------------------------------------------------------ the view


async def test_the_view_has_no_price_settings_without_a_price(hass: HomeAssistant, hass_ws_client) -> None:
    await _home(hass, False)
    client = await admin(hass, hass_ws_client)
    view = await _view(client)
    assert view["price"] is None and view["price_settings"] is None
    assert view["site"]["name"] == "Hemma"


async def test_the_view_states_the_price_settings_and_pins_the_fixture(hass: HomeAssistant, hass_ws_client) -> None:
    """`tests/fixtures/site_view/with_price.json` is this answer (`SPOTNAV_WRITE_FIXTURES=1` rewrites it): the card
    decodes it strictly."""
    roof = forecast_entry(hass, entry_id="roof_a", title="Roof A")
    await setup_site(hass, entry_id="site_home", title="Hemma", solar_forecast_entries=[roof.entry_id])
    await add_price(hass, transfer={"enabled": True, "value": 31.5})
    client = await admin(hass, hass_ws_client)
    await ws_call(
        client,
        {"type": "spotnav/update_grid_tariff", "site_id": "site_home", "expected_write_revision": 0, "grid_tariff": OWNER},
    )
    view = await _view(client)
    settings = view["price_settings"]
    assert settings["market"]["area_id"] == SE4 and settings["market"]["minor_unit"] == "öre"
    assert settings["fees"]["transfer"] == {"enabled": True, "value": 31.5}
    assert settings["price_source"]["charger_count"] == 0
    assert settings["grid_tariff"]["sells"] is True
    path = FIXTURES / "site_view" / "with_price.json"
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        path.write_text(json.dumps(view, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert view == json.loads(path.read_text(encoding="utf-8"))


async def test_the_battery_status_names_the_plans_two_fixed_costs_in_the_areas_money(
    hass: HomeAssistant, hass_ws_client
) -> None:
    await _home(hass, False)
    client = await admin(hass, hass_ws_client)
    ask = {"type": "spotnav/battery_control", "api_version": 1, "site_id": "site_home"}
    assert (await ws_call(client, ask))["result"]["status"]["plan_costs"] is None, "no price, no money to name"
    await add_price(hass, entry_id="price_a")
    costs = (await ws_call(client, ask))["result"]["status"]["plan_costs"]
    assert costs == {
        "wear_minor_per_kwh": 15.0,
        "balanced_margin_minor_per_kwh": 100.0,
        "major_unit": "kr",
        "minor_unit": "öre",
    }
