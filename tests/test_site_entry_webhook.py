"""The site webhook (`api/site_entry_webhook.py`): the app's single door to a home in SpotNav 3.0.

It answers the site's own actions for its own site (a home without a charger too) and every charger action for a
charger of that site named by `charger_id`, each exactly as the charger's own webhook does; a charger of another site
is refused. The answers new to it are pinned under `tests/fixtures/webhook/` (`SPOTNAV_WRITE_FIXTURES=1` rewrites
them); the paired app's tests read the same files.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.site_entry_webhook import GET_SITE_CAPABILITIES, SITE_ENTRY_ACTIONS
from custom_components.spotnav.api.webhook import ACTIONS
from custom_components.spotnav.const import CONF_CHARGER_ENTRY_IDS, CONF_WEBHOOK_ID

from .helpers import make_site_entry
from .test_webhook_parity import _pin, _pinned, post
from .test_webhook_site_parity import DAY, HOOK, READS, _site
from .world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")


def _hook(site: Any) -> str:
    return site.data[CONF_WEBHOOK_ID]


async def test_the_site_actions_answer_what_the_charger_webhook_answers_for_the_same_site(
    hass: HomeAssistant, freezer, hass_client_no_auth
) -> None:
    site = await _site(hass, freezer)
    client = await hass_client_no_auth()
    for action, _command, request, _admin in READS:
        status, by_charger = await post(client, HOOK, {"action": action, **request})
        assert status == 200, (action, by_charger)
        status, by_site = await post(client, _hook(site), {"action": action, **request})
        assert (status, by_site) == (200, by_charger), action
        # A `site_id` in the body is never read: the webhook is its own site's.
        status, again = await post(client, _hook(site), {"action": action, "site_id": "other", **request})
        assert (status, again) == (200, by_charger), action
    for action in ("get_power_month", "suggest_import_target"):
        status, by_charger = await post(client, HOOK, {"action": action})
        status_site, by_site = await post(client, _hook(site), {"action": action})
        assert (status_site, by_site) == (status, by_charger), action


async def test_get_site_names_the_site_and_its_chargers(hass: HomeAssistant, freezer, hass_client_no_auth) -> None:
    site = await _site(hass, freezer)
    client = await hass_client_no_auth()
    status, body = await post(client, _hook(site), {"action": "get_site"})
    assert status == 200
    assert body == {
        "ok": True,
        "action": "get_site",
        "api_version": 1,
        "site_id": site.entry_id,
        "name": site.title,
        "loaded": True,
        "capabilities": {name: True for name in GET_SITE_CAPABILITIES},
        "chargers": [{"id": "entry_a", "name": "entry_a"}],
    }
    _pinned("get_site.json", _pin(body, {site.entry_id: "<site_id>", "entry_a": "<charger_id>"}))
    status, body = await post(client, _hook(site), {"action": "get_site", "api_version": 2})
    assert (status, body) == (400, {"ok": False, "error": "spotnav_unsupported_api_version", "action": "get_site"})


async def test_a_charger_action_names_its_charger_and_answers_as_the_chargers_own_webhook(
    hass: HomeAssistant, freezer, hass_client_no_auth
) -> None:
    site = await _site(hass, freezer)
    client = await hass_client_no_auth()
    for request in (
        {"action": "dashboard", "api_version": 1},
        {"action": "sessions"},
        {"action": "price_days"},
    ):
        status, by_charger = await post(client, HOOK, request)
        status_site, by_site = await post(client, _hook(site), {**request, "charger_id": "entry_a"})
        assert (status_site, by_site) == (status, by_charger), request
    # Without a charger it is not asked for.
    status, body = await post(client, _hook(site), {"action": "dashboard", "api_version": 1})
    assert (status, body) == (400, {"ok": False, "error": "spotnav_charger_required", "action": "dashboard"})
    _pinned("site_webhook_charger_required.json", body)
    # Every charger action is answered here with a charger, and every site action without one.
    assert set(ACTIONS) - set(SITE_ENTRY_ACTIONS) >= {"start", "stop", "settings", "dashboard", "push_register"}


async def test_a_charger_of_another_site_is_refused(hass: HomeAssistant, freezer, hass_client_no_auth) -> None:
    site = await _site(hass, freezer)
    _charger_b, site_b = await setup_charger_and_site(hass, "entry_b", title="Cabin")
    _charger_c, _ = await setup_charger_and_site(hass, "entry_c", site=False)
    client = await hass_client_no_auth()
    for charger_id, status, code in (
        ("entry_b", 403, "spotnav_charger_not_in_site"),
        ("entry_c", 403, "spotnav_charger_not_in_site"),
        (site_b.entry_id, 403, "spotnav_charger_not_in_site"),
        ("no_such_entry", 404, "spotnav_unknown_charger"),
        (17, 404, "spotnav_unknown_charger"),
    ):
        for action in ("dashboard", "start", "get_house", "get_site"):
            answer = await post(client, _hook(site), {"action": action, "api_version": 1, "charger_id": charger_id})
            assert answer == (status, {"ok": False, "error": code, "action": action}), (charger_id, action)
    status, body = await post(client, _hook(site), {"action": "dashboard", "charger_id": "entry_b"})
    _pinned("site_webhook_charger_not_in_site.json", body)
    # Nothing was started at the other site's charger.
    assert hass.states.get("switch.entry_b_control").state != "on"
    # Site B's own webhook answers for its own charger.
    status, body = await post(client, _hook(site_b), {"action": "dashboard", "api_version": 1, "charger_id": "entry_b"})
    assert status == 200 and body["charger"]["charger_id"] == "entry_b"


async def test_the_price_and_the_control_opt_ins_are_read_only(hass: HomeAssistant, freezer, hass_client_no_auth) -> None:
    site = await _site(hass, freezer)
    client = await hass_client_no_auth()
    for action in ("choose_price_source", "answer_price_duplicate", "update_grid_tariff"):
        status, body = await post(client, _hook(site), {"action": action, "price_entry_id": "price_a"})
        assert (status, body) == (403, {"ok": False, "error": "spotnav_not_permitted_over_webhook", "action": action})
    _pinned("site_webhook_price_not_permitted.json", body)
    status, view = await post(client, _hook(site), {"action": "get_site_settings_view"})
    status_c, by_charger = await post(client, HOOK, {"action": "get_site_settings_view"})
    assert status == status_c == 200
    prices = view["price_settings"]
    assert prices["price_source"]["writable"] is False and prices["grid_tariff"]["writable"] is False
    assert by_charger["price_settings"]["price_source"]["writable"] is True
    # Everything else is the charger webhook's view.
    assert {key: value for key, value in view.items() if key != "price_settings"} == {
        key: value for key, value in by_charger.items() if key != "price_settings"
    }
    assert view["site"]["active_control"]["writable"] is False
    _pinned("site_webhook_get_site_settings_view.json", _pin(view))

    status, body = await post(client, _hook(site), {"action": "update_site_settings", "changes": {"active_control_enabled": True}})
    assert status == 403 and body["error"] == "spotnav_not_permitted_over_webhook", body
    status, body = await post(client, _hook(site), {"action": "update_heat", "changes": {"control_enabled": True}})
    assert status == 403 and body["error"] == "spotnav_not_permitted_over_webhook", body
    status, body = await post(client, _hook(site), {"action": "battery_control", "changes": {"battery_control_enabled": True}})
    assert status == 403 and body["error"] == "spotnav_not_permitted_over_webhook", body


async def test_the_site_settings_are_written_for_the_site(hass: HomeAssistant, freezer, hass_client_no_auth) -> None:
    site = await _site(hass, freezer)
    client = await hass_client_no_auth()
    status, view = await post(client, _hook(site), {"action": "get_site_settings_view"})
    before = view["site"]["solar_priority"]
    after = "battery_first" if before == "car_first" else "car_first"
    status, body = await post(
        client,
        _hook(site),
        {"action": "update_site_settings", "changes": {"solar_priority": after}, "expected": {"solar_priority": before}},
    )
    assert status == 200 and body["ok"] is True and body["site"]["solar_priority"] == after, body
    _pinned("site_webhook_update_site_settings.json", _pin(body))
    status, body = await post(
        client,
        _hook(site),
        {"action": "update_site_settings", "changes": {"solar_priority": before}, "expected": {"solar_priority": before}},
    )
    assert status == 409 and body["error"] == "spotnav_conflict", body


async def test_a_home_without_a_charger(hass: HomeAssistant, hass_client_no_auth) -> None:
    home = make_site_entry(hass, entry_id="home_only", charger_entry_ids=[], title="Villa")
    assert await hass.config_entries.async_setup(home.entry_id)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    status, body = await post(client, _hook(home), {"action": "get_site"})
    assert status == 200 and body["chargers"] == [] and body["loaded"] is True and body["name"] == "Villa"
    _pinned("get_site_no_charger.json", _pin(body, {home.entry_id: "<site_id>"}))
    for action, request in (
        ("get_house", {}),
        ("get_house_now", {}),
        ("get_heat", {}),
        ("get_house_history", {"period": "day", "date": DAY}),
        ("battery_control", {}),
        ("get_site_settings_view", {}),
    ):
        status, body = await post(client, _hook(home), {"action": action, **request})
        assert status == 200 and body["ok"] is True and body["action"] == action, (action, body)
    for action in ("dashboard", "start", "push_register", "settings"):
        status, body = await post(client, _hook(home), {"action": action})
        assert (status, body["error"]) == (400, "spotnav_charger_required"), action
    status, body = await post(client, _hook(home), {"action": "no_such_action"})
    assert status == 400 and body["ok"] is False


async def test_a_home_that_gains_a_charger_shows_it_without_a_new_pairing(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    home = make_site_entry(hass, entry_id="home_grows", charger_entry_ids=[], title="Villa")
    assert await hass.config_entries.async_setup(home.entry_id)
    await hass.async_block_till_done()
    secret = _hook(home)
    client = await hass_client_no_auth()
    status, body = await post(client, secret, {"action": "dashboard", "api_version": 1, "charger_id": "entry_new"})
    assert status == 404 and body["error"] == "spotnav_unknown_charger"

    charger, _ = await setup_charger_and_site(hass, "entry_new", site=False)
    hass.config_entries.async_update_entry(home, data={**home.data, CONF_CHARGER_ENTRY_IDS: ["entry_new"]})
    assert await hass.config_entries.async_reload(home.entry_id)
    await hass.async_block_till_done()

    assert _hook(home) == secret, "the phone's secret is the same"
    status, body = await post(client, secret, {"action": "get_site"})
    assert status == 200 and body["chargers"] == [{"id": "entry_new", "name": "entry_new"}]
    status, body = await post(client, secret, {"action": "dashboard", "api_version": 1, "charger_id": "entry_new"})
    assert status == 200 and body["charger"]["charger_id"] == "entry_new"


async def test_a_charger_of_the_site_that_is_not_loaded_says_so(hass: HomeAssistant, freezer, hass_client_no_auth) -> None:
    site = await _site(hass, freezer)
    client = await hass_client_no_auth()
    assert await hass.config_entries.async_unload("entry_a")
    await hass.async_block_till_done()
    status, body = await post(client, _hook(site), {"action": "dashboard", "charger_id": "entry_a"})
    assert (status, body) == (503, {"ok": False, "error": "spotnav_charger_unloaded", "action": "dashboard"})


async def test_unloading_the_site_unregisters_its_webhook(hass: HomeAssistant, freezer, hass_client_no_auth) -> None:
    site = await _site(hass, freezer)
    client = await hass_client_no_auth()
    secret = _hook(site)
    status, _body = await post(client, secret, {"action": "get_site"})
    assert status == 200
    assert await hass.config_entries.async_unload(site.entry_id)
    await hass.async_block_till_done()
    response = await client.post(f"/api/webhook/{secret}", json={"version": 1, "action": "get_site"})
    # Home Assistant's answer for an id it does not know: an empty 200, as for a charger's.
    assert response.status == 200 and await response.text() == ""
