"""The shared settings popover's site reads and the site-keyed variants: `spotnav/list_sites`,
`spotnav/get_site_settings_view`, `get_entity_config`/`update_entity_config` by `site_id`, and
`spotnav/battery_control` by `charger_id`."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.setup_checklist import capture_site_setup

from .world import setup_charger, setup_site, setup_site_with_charger

pytestmark = pytest.mark.usefixtures("offline_relay")


async def _ask(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    _ask.counter = getattr(_ask, "counter", 0) + 1  # type: ignore[attr-defined]
    await client.send_json({"id": _ask.counter, **request})  # type: ignore[attr-defined]
    return await client.receive_json()


async def test_list_sites_names_every_loaded_site_with_its_chargers(hass: HomeAssistant, hass_ws_client: Any) -> None:
    await setup_site_with_charger(hass, site_entry_id="site_b", title="Zeta")
    await setup_site(hass, entry_id="site_empty", title="Alfa")
    client = await hass_ws_client(hass)
    answer = await _ask(client, {"type": "spotnav/list_sites", "api_version": 1})
    assert answer["result"] == {
        "api_version": 1,
        "sites": [
            {"site_id": "site_empty", "name": "Alfa", "charger_ids": []},
            {"site_id": "site_b", "name": "Zeta", "charger_ids": ["entry_a"]},
        ],
    }
    wrong = await _ask(client, {"type": "spotnav/list_sites", "api_version": 2})
    assert wrong["error"]["code"] == "spotnav_unsupported_api_version"


async def test_the_view_of_a_site_without_a_charger_offers_adding_one(hass: HomeAssistant, hass_ws_client: Any) -> None:
    await setup_site(hass, entry_id="site_empty", title="Hemma")
    client = await hass_ws_client(hass)
    answer = (await _ask(client, {"type": "spotnav/get_site_settings_view", "api_version": 1, "site_id": "site_empty"}))
    view = answer["result"]
    assert (view["site_id"], view["name"], view["charger_ids"], view["writable"]) == ("site_empty", "Hemma", [], True)
    assert view["battery"] == {"found": False}
    ids = [item["id"] for item in view["setup"]["items"]]
    assert ids[0] == "charger"
    assert {"id": "charger", "dismissible": True} in view["setup"]["items"]
    assert not {"vehicle", "car_level", "phone", "app", "phase_switching", "site"} & set(ids)


async def test_the_view_by_a_charger_is_its_sites(hass: HomeAssistant, hass_ws_client: Any) -> None:
    await setup_site_with_charger(hass)
    client = await hass_ws_client(hass)
    view = (await _ask(client, {"type": "spotnav/get_site_settings_view", "api_version": 1, "charger_id": "entry_a"}))[
        "result"
    ]
    assert view["site_id"] == "site_a" and view["charger_ids"] == ["entry_a"]
    assert "charger" not in [item["id"] for item in view["setup"]["items"]]


async def test_the_view_refuses_what_is_no_site(hass: HomeAssistant, hass_ws_client: Any) -> None:
    await setup_charger(hass)
    client = await hass_ws_client(hass)
    by_charger_id = await _ask(client, {"type": "spotnav/get_site_settings_view", "api_version": 1, "site_id": "entry_a"})
    assert by_charger_id["error"]["code"] == "spotnav_unknown_site"
    lone = await _ask(client, {"type": "spotnav/get_site_settings_view", "api_version": 1, "charger_id": "entry_a"})
    assert lone["error"]["code"] == "spotnav_no_site"


async def test_a_dismissed_charger_row_stays_away(hass: HomeAssistant, hass_ws_client: Any) -> None:
    site = await setup_site(hass, entry_id="site_empty")
    client = await hass_ws_client(hass)
    answer = await _ask(client, {"type": "spotnav/dismiss_setup_item", "item": "charger"})
    assert answer["result"] == {"ok": True}
    assert "charger" not in [item.id for item in capture_site_setup(hass, site)]


async def test_entity_config_by_site_id_reads_and_writes_the_site_alone(
    hass: HomeAssistant, hass_ws_client: Any
) -> None:
    site = await setup_site(hass, entry_id="site_empty", title="Hemma", main_fuse_a=25.0)
    client = await hass_ws_client(hass)
    read = (await _ask(client, {"type": "spotnav/get_entity_config", "api_version": 1, "site_id": "site_empty"}))[
        "result"
    ]
    assert read["ok"] is True
    config = read["config"]
    assert (config["charger_id"], config["control"], config["vehicles"]) == (None, None, [])
    assert config["site"]["name"] == "Hemma" and config["site"]["charger_count"] == 0
    fields = {field["field"] for field in config["fields"]}
    assert "main_fuse_a" in fields and "charge_control" not in fields

    charger_scope = (
        await _ask(
            client,
            {
                "type": "spotnav/update_entity_config", "api_version": 1, "site_id": "site_empty", "scope": "charger",
                "expected": {}, "changes": {"charge_control": "switch.x"},
            },
        )
    )["result"]
    assert charger_scope["error"] == "spotnav_invalid_value"

    written = (
        await _ask(
            client,
            {
                "type": "spotnav/update_entity_config", "api_version": 1, "site_id": "site_empty", "scope": "site",
                "expected": {"import_target_kw": None}, "changes": {"import_target_kw": 5.0},
            },
        )
    )["result"]
    assert written["ok"] is True, written
    assert float(site.data["import_target_kw"]) == 5.0

    unknown = (await _ask(client, {"type": "spotnav/get_entity_config", "api_version": 1, "site_id": "nope"}))["result"]
    assert unknown["error"] == "spotnav_unknown_site"


async def test_battery_control_by_a_charger_reads_its_site(hass: HomeAssistant, hass_ws_client: Any) -> None:
    await setup_site_with_charger(hass)
    client = await hass_ws_client(hass)
    by_charger = (await _ask(client, {"type": "spotnav/battery_control", "api_version": 1, "charger_id": "entry_a"}))[
        "result"
    ]
    by_site = (await _ask(client, {"type": "spotnav/battery_control", "api_version": 1, "site_id": "site_a"}))["result"]
    assert by_charger["ok"] is True and by_charger == by_site
    lone = await setup_charger(hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b")
    alone = (await _ask(client, {"type": "spotnav/battery_control", "api_version": 1, "charger_id": lone.entry_id}))[
        "result"
    ]
    assert alone["error"] == "spotnav_no_site"
