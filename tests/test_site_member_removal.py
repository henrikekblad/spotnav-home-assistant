"""A charger entry deleted for good leaves its site, and a site drops members that no longer exist."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.const import CONF_CHARGER_ENTRY_IDS, CONF_PHASE_WIRING, DOMAIN
from custom_components.spotnav.site.site_join import async_leave_sites, prune_missing_members

from .helpers import make_site_entry

_WIRING = {"phase": None, "phases": 3}


def _charger(hass: HomeAssistant, entry_id: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, entry_id=entry_id, data={"entry_type": "charger"})
    entry.add_to_hass(hass)
    return entry


async def test_a_deleted_charger_leaves_its_site_with_its_wiring(hass: HomeAssistant) -> None:
    _charger(hass, "garage")
    _charger(hass, "driveway")
    site = make_site_entry(
        hass,
        entry_id="site",
        charger_entry_ids=["garage", "driveway"],
        phase_wiring={"garage": _WIRING, "driveway": _WIRING},
    )

    async_leave_sites(hass, "garage")

    assert site.data[CONF_CHARGER_ENTRY_IDS] == ["driveway"]
    assert set(site.data[CONF_PHASE_WIRING]) == {"driveway"}


async def test_a_charger_on_no_site_changes_nothing(hass: HomeAssistant) -> None:
    _charger(hass, "driveway")
    site = make_site_entry(hass, entry_id="site", charger_entry_ids=["driveway"], phase_wiring={"driveway": _WIRING})
    before = dict(site.data)

    async_leave_sites(hass, "elsewhere")

    assert dict(site.data) == before


async def test_a_site_drops_members_whose_entry_no_longer_exists(hass: HomeAssistant) -> None:
    _charger(hass, "driveway")
    site = make_site_entry(
        hass,
        entry_id="site",
        charger_entry_ids=["ghost", "driveway"],
        phase_wiring={"ghost": _WIRING, "driveway": _WIRING},
    )

    prune_missing_members(hass, site)

    assert site.data[CONF_CHARGER_ENTRY_IDS] == ["driveway"]
    assert set(site.data[CONF_PHASE_WIRING]) == {"driveway"}


async def test_a_site_whose_members_all_exist_is_left_alone(hass: HomeAssistant) -> None:
    _charger(hass, "driveway")
    site = make_site_entry(hass, entry_id="site", charger_entry_ids=["driveway"], phase_wiring={"driveway": _WIRING})
    before = dict(site.data)

    prune_missing_members(hass, site)

    assert dict(site.data) == before
