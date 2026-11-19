""""Make a home of this" (`api/make_home.py`): an administrator, asked first in the card and giving the main fuse,
turns an install whose chargers have no home into one with a home around that charger. Never silent, never a second
home, never without a usable fuse."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.spotnav.api.common import site_of_charger
from custom_components.spotnav.api.make_home import websocket_make_home
from custom_components.spotnav.const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_CHARGER_ENTRY_IDS,
    CONF_CHARGER_PHASES,
    CONF_ENTRY_TYPE,
    CONF_MAIN_FUSE_A,
    CONF_PHASE_WIRING,
    CONF_VOLTAGE_BETWEEN_PHASES_V,
    CONF_WEBHOOK_ID,
    CONF_YIELD_STEPPING_ENABLED,
    DOMAIN,
    ENTRY_TYPE_SITE,
)

from homeassistant.util import slugify

from custom_components.spotnav.api.make_home import async_make_home
from custom_components.spotnav.flows.site_form import default_site_name

from .helpers import answers_of
from .test_webhook_parity import post
from .world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")


class _Connection:
    def __init__(self, admin: bool) -> None:
        self.answers: list[dict[str, Any]] = []
        self.user = SimpleNamespace(is_admin=admin)

    def send_result(self, message_id: int, result: Any) -> None:
        self.answers.append({"success": True, "result": result})

    def send_error(self, message_id: int, code: str, message: str) -> None:
        self.answers.append({"success": False, "error": {"code": code}})


async def _make(hass: HomeAssistant, *, admin: bool = True, **request: Any) -> dict[str, Any]:
    connection = _Connection(admin)
    websocket_make_home(hass, connection, {"id": 1, "type": "spotnav/make_home", "api_version": 1, **request})
    (answer,) = await answers_of(hass, connection)
    return answer


def _sites(hass: HomeAssistant) -> list[Any]:
    return [entry for entry in hass.config_entries.async_entries(DOMAIN) if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE]


async def test_a_home_is_made_around_the_charger_with_the_fuse_asked(hass: HomeAssistant, hass_client_no_auth) -> None:
    charger, _ = await setup_charger_and_site(hass, site=False)
    hass.config_entries.async_update_entry(charger, data={**charger.data, CONF_CHARGER_PHASES: 1})

    answer = await _make(hass, charger_id="entry_a", main_fuse_a=20)

    assert answer["success"] and answer["result"]["ok"] is True and answer["result"]["error"] is None
    await hass.async_block_till_done()
    (home,) = _sites(hass)
    assert answer["result"]["site_id"] == home.entry_id
    # Named, identified and wired as the site flow would: its translated default name, its unique id, the network's
    # default voltage between phases and load balancing's stepping off.
    assert home.title == default_site_name(hass) and home.state is ConfigEntryState.LOADED
    assert home.unique_id == f"site:{slugify(home.title)}"
    assert home.data[CONF_VOLTAGE_BETWEEN_PHASES_V] == 400
    assert home.data[CONF_YIELD_STEPPING_ENABLED] is False
    assert home.data[CONF_MAIN_FUSE_A] == 20.0 and home.data[CONF_ACTIVE_CONTROL_ENABLED] is False
    assert home.data[CONF_CHARGER_ENTRY_IDS] == ["entry_a"]
    assert home.data[CONF_PHASE_WIRING] == {"entry_a": {"phases": 1, "phase": None}}
    assert site_of_charger(hass, "entry_a") is home
    # A home without a meter raises no Repairs issue: nothing there for a person to act on yet.
    assert not ir.async_get(hass).issues
    # Its own door answers, and the charger hands it over.
    client = await hass_client_no_auth()
    status, body = await post(client, home.data[CONF_WEBHOOK_ID], {"action": "get_site"})
    assert status == 200 and body["chargers"] == [{"id": "entry_a", "name": "entry_a"}]
    status, body = await post(client, "webhook-entry_a", {"action": "get_site_access"})
    assert status == 200 and body["site"]["id"] == home.entry_id

    # Never a second home.
    await setup_charger_and_site(hass, "entry_b", site=False)
    answer = await _make(hass, charger_id="entry_b", main_fuse_a=20)
    assert answer["result"] == {"api_version": 1, "ok": False, "error": "spotnav_home_exists", "site_id": None}
    assert len(_sites(hass)) == 1


async def test_nothing_is_made_without_an_administrator_a_charger_or_a_usable_fuse(hass: HomeAssistant) -> None:
    await setup_charger_and_site(hass, site=False)
    for request, code in (
        ({"charger_id": "entry_a", "main_fuse_a": 25}, "spotnav_not_admin"),
    ):
        answer = await _make(hass, admin=False, **request)
        assert answer["result"]["error"] == code
    for request, code in (
        ({"charger_id": "nope", "main_fuse_a": 25}, "spotnav_unknown_charger"),
        ({"main_fuse_a": 25}, "spotnav_unknown_charger"),
        ({"charger_id": "entry_a"}, "spotnav_invalid_value"),
        ({"charger_id": "entry_a", "main_fuse_a": 0}, "spotnav_invalid_value"),
        ({"charger_id": "entry_a", "main_fuse_a": 1.0}, "spotnav_invalid_value"),
        # "2" typed for "25": below any household's main fuse.
        ({"charger_id": "entry_a", "main_fuse_a": 5.9}, "spotnav_invalid_value"),
        ({"charger_id": "entry_a", "main_fuse_a": 5000}, "spotnav_invalid_value"),
        ({"charger_id": "entry_a", "main_fuse_a": True}, "spotnav_invalid_value"),
        ({"charger_id": "entry_a", "main_fuse_a": "25"}, "spotnav_invalid_value"),
        ({"charger_id": "entry_a", "main_fuse_a": float("nan")}, "spotnav_invalid_value"),
    ):
        answer = await _make(hass, **request)
        assert answer["result"] == {"api_version": 1, "ok": False, "error": code, "site_id": None}, request
    answer = await _make(hass, api_version=2, charger_id="entry_a", main_fuse_a=25)
    assert answer == {"success": False, "error": {"code": "spotnav_unsupported_api_version"}}
    assert _sites(hass) == []


async def test_a_charger_of_an_install_that_has_a_home_is_refused(hass: HomeAssistant) -> None:
    await setup_charger_and_site(hass)
    await setup_charger_and_site(hass, "entry_b", site=False)
    answer = await _make(hass, charger_id="entry_b", main_fuse_a=25)
    assert answer["result"]["error"] == "spotnav_home_exists"
    assert len(_sites(hass)) == 1


async def test_a_norwegian_home_takes_the_it_networks_voltage(hass: HomeAssistant) -> None:
    hass.config.country = "NO"
    await setup_charger_and_site(hass, site=False)
    answer = await _make(hass, charger_id="entry_a", main_fuse_a=6)
    assert answer["result"]["ok"] is True
    (home,) = _sites(hass)
    assert home.data[CONF_VOLTAGE_BETWEEN_PHASES_V] == 230


async def test_two_presses_at_once_make_one_home(hass: HomeAssistant) -> None:
    import asyncio

    await setup_charger_and_site(hass, site=False)
    first, second = await asyncio.gather(
        async_make_home(hass, "entry_a", 25), async_make_home(hass, "entry_a", 25)
    )
    await hass.async_block_till_done()
    assert sorted([first["ok"], second["ok"]]) == [False, True]
    assert {first["error"], second["error"]} == {None, "spotnav_home_exists"}
    assert len(_sites(hass)) == 1
