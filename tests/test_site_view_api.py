"""The shared settings popover's site reads and the site-keyed variants: `spotnav/list_sites`,
`spotnav/get_site_settings_view`, `get_entity_config`/`update_entity_config` by `site_id`, and
`spotnav/battery_control` by `charger_id`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.setup_checklist import capture_site_setup
from custom_components.spotnav.site.phase_peaks import Learned

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


FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(*parts: str) -> dict[str, Any]:
    return json.loads((FIXTURES.joinpath(*parts)).read_text(encoding="utf-8"))


async def test_the_card_fixtures_have_the_answers_shapes(hass: HomeAssistant, hass_ws_client: Any) -> None:
    """The card's contract fixtures (`tests/fixtures/site_view/`, `tests/fixtures/battery_control/`) carry exactly
    the keys the real answers carry; `no_charger.json` is the real answer itself (`SPOTNAV_WRITE_FIXTURES=1`
    rewrites it)."""
    await setup_site(hass, entry_id="site_empty", title="Hemma")
    client = await hass_ws_client(hass)
    view = (await _ask(client, {"type": "spotnav/get_site_settings_view", "api_version": 1, "site_id": "site_empty"}))[
        "result"
    ]
    path = FIXTURES / "site_view" / "no_charger.json"
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        path.write_text(json.dumps(view, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert view == _fixture("site_view", "no_charger.json")
    for name in ("villa.json", "with_price.json"):
        _same_keys(_fixture("site_view", name), view, name)

    battery = (await _ask(client, {"type": "spotnav/battery_control", "api_version": 1, "site_id": "site_empty"}))[
        "result"
    ]
    # The learned peaks are `None` on an empty site, so their keys come from the dataclass that builds them.
    learned = Learned(ready=True, raw_a=None, factor=1.0, peaks_a=None, days=0, hours=0, minutes=0, heat_w=None).as_dict()
    for name in ("needs_ack.json", "needs_strategy_ack.json", "checking.json", "live.json", "needs_enable.json"):
        fixture = _fixture("battery_control", name)
        _same_keys(fixture, battery, name)
        cap_learned = fixture["status"]["grid_charge_cap"]["learned"]
        if cap_learned is not None:
            _same_keys(cap_learned, learned, f"{name} learned")


def _same_keys(fixture: Any, answer: Any, where: str) -> None:
    """The same keys at every nesting level. A `None` on either side, a free-form value (a dict keyed by data) or an
    empty list ends the comparison there; lists are compared by their first element."""
    if isinstance(fixture, dict) and isinstance(answer, dict):
        assert set(fixture) == set(answer), where
        for key in fixture:
            _same_keys(fixture[key], answer[key], f"{where}.{key}")
    elif isinstance(fixture, list) and isinstance(answer, list) and fixture and answer:
        _same_keys(fixture[0], answer[0], f"{where}[0]")
