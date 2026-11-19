""""Disconnect all phones" (`api/phones.py`): every charger and site gets a new webhook id, the old ones stop
answering at once (a site id a charger handed over too), every phone's notification registration is cleared, and
nothing is reloaded. Administrators only."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.pairing import PairingRegister
from custom_components.spotnav.api.phones import websocket_disconnect_phones
from custom_components.spotnav.const import CONF_WEBHOOK_ID
from custom_components.spotnav.notifications.push import PushRegistration
from custom_components.spotnav.runtime import charger_data, domain_data

from .helpers import answers_of, make_site_entry
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


async def _disconnect(hass: HomeAssistant, *, admin: bool = True, **request: Any) -> dict[str, Any]:
    connection = _Connection(admin)
    websocket_disconnect_phones(hass, connection, {"id": 1, "type": "spotnav/disconnect_phones", "api_version": 1, **request})
    (answer,) = await answers_of(hass, connection)
    return answer


async def _gone(client: Any, webhook_id: str) -> bool:
    """Home Assistant's answer for an id it does not know: an empty 200."""
    response = await client.post(f"/api/webhook/{webhook_id}", json={"version": 1, "action": "get_site"})
    return response.status == 200 and await response.text() == ""


async def _world(hass: HomeAssistant) -> tuple[Any, Any, Any]:
    charger, site = await setup_charger_and_site(hass)
    home = make_site_entry(hass, entry_id="home_only", charger_entry_ids=[], title="Cabin")
    assert await hass.config_entries.async_setup(home.entry_id)
    await hass.async_block_till_done()
    return charger, site, home


async def test_every_webhook_is_replaced_and_the_old_ones_stop_answering(hass: HomeAssistant, hass_client_no_auth) -> None:
    charger, site, home = await _world(hass)
    client = await hass_client_no_auth()
    status, access = await post(client, "webhook-entry_a", {"action": "get_site_access"})
    assert status == 200 and access["site"]["webhook"] == site.data[CONF_WEBHOOK_ID]
    old = {entry.entry_id: entry.data[CONF_WEBHOOK_ID] for entry in (charger, site, home)}
    push = charger_data(hass, "entry_a").push
    await push.async_register(PushRegistration(push_ref="abc"))
    domain_data(hass).pairing.open("123456", "Pixel")
    states = {entry.entry_id: entry.state for entry in (charger, site, home)}

    answer = await _disconnect(hass)

    assert answer == {"success": True, "result": {"api_version": 1, "ok": True, "error": None, "chargers": 1, "sites": 2}}
    for entry in (charger, site, home):
        new = entry.data[CONF_WEBHOOK_ID]
        assert new != old[entry.entry_id] and len(new) >= 43
        assert await _gone(client, old[entry.entry_id]), entry.entry_id
        assert entry.state is states[entry.entry_id], "nothing is reloaded"
    # The id the charger handed over is gone with the rest.
    assert await _gone(client, access["site"]["webhook"])
    # The new ids answer, and the charger now hands over the new site id.
    status, body = await post(client, charger.data[CONF_WEBHOOK_ID], {"action": "get_site_access"})
    assert status == 200 and body["site"]["webhook"] == site.data[CONF_WEBHOOK_ID]
    for entry in (site, home):
        status, body = await post(client, entry.data[CONF_WEBHOOK_ID], {"action": "get_site"})
        assert status == 200 and body["site_id"] == entry.entry_id
    # No phone is woken any more, and no pairing request from before can fetch the new ids.
    assert push.registration is None
    assert domain_data(hass).pairing.pending_count() == 0
    assert isinstance(domain_data(hass).pairing, PairingRegister)


async def test_a_reload_after_it_answers_at_the_new_id_and_unregisters_it_on_unload(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    charger, site, _home = await _world(hass)
    client = await hass_client_no_auth()
    await _disconnect(hass)
    for entry in (charger, site):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    status, _body = await post(client, site.data[CONF_WEBHOOK_ID], {"action": "get_site"})
    assert status == 200
    status, _body = await post(client, charger.data[CONF_WEBHOOK_ID], {"action": "get_site_access"})
    assert status == 200
    assert await hass.config_entries.async_unload(site.entry_id)
    await hass.async_block_till_done()
    status, body = await post(client, site.data[CONF_WEBHOOK_ID], {"action": "get_site"})
    assert (status, body["error"]) == (503, "spotnav_site_unloaded"), "not loaded is never gone"


async def test_an_unloaded_site_gets_a_new_id_that_says_it_is_not_loaded(hass: HomeAssistant, hass_client_no_auth) -> None:
    _charger, _site, home = await _world(hass)
    client = await hass_client_no_auth()
    assert await hass.config_entries.async_unload(home.entry_id)
    await hass.async_block_till_done()
    old = home.data[CONF_WEBHOOK_ID]
    await _disconnect(hass)
    assert home.data[CONF_WEBHOOK_ID] != old
    assert await _gone(client, old)
    status, body = await post(client, home.data[CONF_WEBHOOK_ID], {"action": "get_site"})
    assert (status, body["error"]) == (503, "spotnav_site_unloaded")
    assert await hass.config_entries.async_setup(home.entry_id)
    await hass.async_block_till_done()
    assert await _gone(client, old)
    status, _body = await post(client, home.data[CONF_WEBHOOK_ID], {"action": "get_site"})
    assert status == 200


async def test_only_an_administrator_may_and_nothing_changes_otherwise(hass: HomeAssistant) -> None:
    charger, site, home = await _world(hass)
    before = [entry.data[CONF_WEBHOOK_ID] for entry in (charger, site, home)]
    answer = await _disconnect(hass, admin=False)
    assert answer["result"] == {"api_version": 1, "ok": False, "error": "spotnav_not_admin", "chargers": 0, "sites": 0}
    answer = await _disconnect(hass, api_version=2)
    assert answer == {"success": False, "error": {"code": "spotnav_unsupported_api_version"}}
    assert [entry.data[CONF_WEBHOOK_ID] for entry in (charger, site, home)] == before
