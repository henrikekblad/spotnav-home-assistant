"""Config entry minor version 2 (SpotNav 3.0): every site entry gets its own webhook id, the site webhook's secret.

The migration runs before setup, gives a site an id once, leaves chargers and prices as they are, and is safe both
ways: a newer minor version is left alone, and a release that knows minor 1 ignores the extra key.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.const import (
    CONF_ENTRY_TYPE,
    CONF_WEBHOOK_ID,
    CONFIG_ENTRY_MINOR_VERSION,
    DOMAIN,
    ENTRY_TYPE_PRICE,
)
from custom_components.spotnav.entry_migration import async_migrate_entry
from custom_components.spotnav.flows.flow import SpotNavChargingConfigFlow

from .helpers import make_entry, make_site_entry


def test_the_flow_writes_the_current_minor_version() -> None:
    assert (SpotNavChargingConfigFlow.VERSION, SpotNavChargingConfigFlow.MINOR_VERSION) == (1, 2)
    assert CONFIG_ENTRY_MINOR_VERSION == 2


async def test_a_site_of_minor_version_1_gets_its_own_webhook_id_once(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.mig_charger", "off")
    charger = make_entry(
        hass, entry_id="mig_charger", charge_control="switch.mig_charger", current_limit=None,
        webhook_id="charger-secret", title="Garage",
    )
    site = make_site_entry(hass, entry_id="mig_site", charger_entry_ids=["mig_charger"])
    price = MockConfigEntry(domain=DOMAIN, entry_id="mig_price", data={CONF_ENTRY_TYPE: ENTRY_TYPE_PRICE})
    price.add_to_hass(hass)
    assert site.minor_version == 1 and CONF_WEBHOOK_ID not in site.data
    charger_data = dict(charger.data)
    site_data = dict(site.data)

    # Setting up the integration sets up every entry of it.
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    assert site.state is ConfigEntryState.LOADED
    secret = site.data[CONF_WEBHOOK_ID]
    assert isinstance(secret, str) and len(secret) >= 43, "32 random bytes, url-safe"
    assert {key: value for key, value in site.data.items() if key != CONF_WEBHOOK_ID} == site_data
    assert dict(charger.data) == charger_data
    assert (site.minor_version, charger.minor_version) == (2, 2)

    # Again: nothing changes.
    assert await async_migrate_entry(hass, site)
    assert site.data[CONF_WEBHOOK_ID] == secret
    # A price never gets one.
    assert await async_migrate_entry(hass, price)
    assert CONF_WEBHOOK_ID not in price.data and price.minor_version == 2


async def test_two_sites_get_two_ids(hass: HomeAssistant) -> None:
    first = make_site_entry(hass, entry_id="mig_one", charger_entry_ids=[])
    second = make_site_entry(hass, entry_id="mig_two", charger_entry_ids=[])
    assert await async_migrate_entry(hass, first) and await async_migrate_entry(hass, second)
    assert first.data[CONF_WEBHOOK_ID] != second.data[CONF_WEBHOOK_ID]


async def test_a_site_that_already_has_an_id_keeps_it(hass: HomeAssistant) -> None:
    """Upgraded, downgraded to a release that knows minor 1, and upgraded again: the id the phone holds stays."""
    site = make_site_entry(hass, entry_id="mig_kept", charger_entry_ids=[], extra_data={CONF_WEBHOOK_ID: "kept"})
    assert await async_migrate_entry(hass, site)
    assert site.data[CONF_WEBHOOK_ID] == "kept" and site.minor_version == 2


async def test_a_newer_minor_version_is_left_alone(hass: HomeAssistant) -> None:
    site = MockConfigEntry(
        domain=DOMAIN, entry_id="mig_newer", data={CONF_ENTRY_TYPE: "site"}, version=1, minor_version=3
    )
    site.add_to_hass(hass)
    assert await async_migrate_entry(hass, site)
    assert CONF_WEBHOOK_ID not in site.data and site.minor_version == 3


async def test_a_release_that_knows_minor_1_loads_a_migrated_entry(hass: HomeAssistant, monkeypatch) -> None:
    """A downgrade to 2.5: its handler is minor 1 and it has no `async_migrate_entry`. Home Assistant refuses only a
    higher major version, so an entry of minor 2 loads there with its id untouched (checked on Home Assistant's own
    rule, with the handler's minor lowered and the hook taken away)."""
    import custom_components.spotnav as integration

    site = make_site_entry(hass, entry_id="mig_down", charger_entry_ids=[])
    assert await async_migrate_entry(hass, site)
    data = dict(site.data)
    monkeypatch.setattr(SpotNavChargingConfigFlow, "MINOR_VERSION", 1)
    monkeypatch.delattr(integration, "async_migrate_entry")
    assert await site.async_migrate(hass) is True
    assert dict(site.data) == data and site.minor_version == 2
