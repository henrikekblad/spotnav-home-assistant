"""The card's "Get started" rows (`setup_checklist.py`): each from state the integration holds, gone once done,
the optional ones dismissible for the whole installation, and the app's row done once the app has spoken."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.notifications.push import PushRegistration
from custom_components.spotnav.notifications.settings import NotificationSettings
from custom_components.spotnav.planning.auto_settings import DRIVER_TARGET_SOC
from custom_components.spotnav.runtime import charger_data, domain_data
from tests.helpers import webhook_dashboard
from tests.world import add_car, admin, non_admin, setup_charger, setup_site_with_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

REF = "abcdefghijklmnopqrstuvwxyz0123456789_-ABCD"


def items(hass: HomeAssistant, entry: Any) -> list[dict[str, Any]]:
    body = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, entry), can_act=True)
    return body["setup"]["items"]


def ids(hass: HomeAssistant, entry: Any) -> list[str]:
    return [item["id"] for item in items(hass, entry)]


async def update(hass: HomeAssistant, entry_id: str, **changes: Any) -> None:
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(entry_id, mutate=lambda settings: replace(settings, **changes))
    await hass.async_block_till_done()


async def test_a_new_charger_lists_the_car_the_site_a_phone_and_the_app_all_dismissible(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    assert items(hass, entry) == [
        {"id": "vehicle", "dismissible": True},
        {"id": "site", "dismissible": True},
        {"id": "phone", "dismissible": True},
        {"id": "app", "dismissible": True},
    ]


async def test_a_car_takes_its_row_away(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    add_car(hass, "family")
    await hass.async_block_till_done()
    assert "vehicle" not in ids(hass, entry)


async def test_a_site_takes_its_row_away(hass: HomeAssistant) -> None:
    charger, _site = await setup_site_with_charger(hass)
    assert "site" not in ids(hass, charger)


async def test_the_phone_row_goes_once_a_phone_is_chosen(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    # Asked for even with no phone at all: the notification settings say how to get one, through either app.
    assert "phone" in ids(hass, entry)
    async_mock_service(hass, "notify", "mobile_app_pixel")
    await update(hass, entry.entry_id, notifications=NotificationSettings(targets=("mobile_app_pixel",)))
    assert "phone" not in ids(hass, entry)


async def test_the_paired_apps_notifications_take_the_phone_and_the_app_rows_away(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    await charger_data(hass, entry.entry_id).push.async_register(PushRegistration(push_ref=REF, events=("plan_stopped",)))
    assert ids(hass, entry) == ["vehicle", "site"]


async def test_a_solar_site_without_a_forecast_asks_for_one_and_phase_switching_when_it_is_off(
    hass: HomeAssistant, freezer
) -> None:
    from homeassistant.config_entries import ConfigEntry

    from custom_components.spotnav.const import CONF_PHASE_SWITCHING, CONF_SOLAR_FORECAST_ENTRIES
    from tests.test_solar_phase_execution import _world as phase_world

    w = await phase_world(hass, freezer, option=False)
    charger = w.charger
    assert "solar_forecast" in ids(hass, charger)
    assert "phase_switching" in ids(hass, charger)
    site: ConfigEntry = dashboard_api.site_binding(hass, charger.entry_id)
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_SOLAR_FORECAST_ENTRIES: ["roof"]})
    hass.config_entries.async_update_entry(charger, data={**charger.data, CONF_PHASE_SWITCHING: True})
    assert "solar_forecast" not in ids(hass, charger)
    assert "phase_switching" not in ids(hass, charger)


async def test_a_site_that_cannot_measure_solar_asks_for_neither(hass: HomeAssistant) -> None:
    charger, _site = await setup_site_with_charger(hass)
    assert not {"solar_forecast", "phase_switching"} & set(ids(hass, charger))


async def test_the_app_row_is_done_for_good_once_the_app_spoke(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await setup_charger(hass)
    assert "app" in ids(hass, entry)
    client = await hass_client_no_auth()
    body = await webhook_dashboard(client, "webhook-a")
    # The app does not read the block until it opts in.
    assert "setup" not in body
    assert "app" not in ids(hass, entry)
    store = domain_data(hass).decision_store
    assert store is not None and store.confirmed_payload("setup_done", "app") is not None


async def test_an_app_that_opts_in_reads_the_block(hass: HomeAssistant, hass_client_no_auth) -> None:
    await setup_charger(hass)
    client = await hass_client_no_auth()
    response = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "dashboard", "api_version": 1, "reads": ["setup"]},
    )
    body = await response.json()
    assert [item["id"] for item in body["setup"]["items"]] == ["vehicle", "site", "phone"]


async def test_without_a_car_a_charge_level_target_asks_for_the_car_not_its_level(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    await update(hass, entry.entry_id, driver=DRIVER_TARGET_SOC)
    assert ids(hass, entry)[0] == "vehicle"
    assert "car_level" not in ids(hass, entry)


async def test_a_charge_level_target_without_a_level_source_asks_for_one_and_cannot_be_dismissed(
    hass: HomeAssistant, hass_ws_client, monkeypatch
) -> None:
    entry = await setup_charger(hass)
    add_car(hass, "family")
    await hass.async_block_till_done()
    await update(hass, entry.entry_id, driver=DRIVER_TARGET_SOC)
    # A car is set up, but nothing reports its level to this charger.
    monkeypatch.setattr(dashboard_api, "capture_soc", lambda *_args: None)
    assert items(hass, entry)[0] == {"id": "car_level", "dismissible": False}
    socket = await admin(hass, hass_ws_client)
    answer = await ws_call(socket, {"type": "spotnav/dismiss_setup_item", "item": "car_level"})
    assert answer["success"] is False and answer["error"]["code"] == "spotnav_setup_item_not_dismissible"
    assert ids(hass, entry)[0] == "car_level"


async def test_an_energy_target_never_asks_for_the_cars_level(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    assert "car_level" not in ids(hass, entry)


async def test_a_dismissal_holds_for_every_charger_of_the_installation(hass: HomeAssistant, hass_ws_client) -> None:
    first = await setup_charger(hass)
    second = await setup_charger(hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b")
    socket = await admin(hass, hass_ws_client)
    answer = await ws_call(socket, {"type": "spotnav/dismiss_setup_item", "item": "site"})
    assert answer["success"] is True and answer["result"] == {"ok": True}
    assert "site" not in ids(hass, first) + ids(hass, second)
    assert "app" in ids(hass, first) and "app" in ids(hass, second)
    unknown = await ws_call(socket, {"type": "spotnav/dismiss_setup_item", "item": "prices"})
    assert unknown["success"] is False


async def test_only_an_administrator_dismisses(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    entry = await setup_charger(hass)
    socket = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    answer = await ws_call(socket, {"type": "spotnav/dismiss_setup_item", "item": "site"})
    assert answer["success"] is False and answer["error"]["code"] == "spotnav_not_admin"
    assert "site" in ids(hass, entry)
