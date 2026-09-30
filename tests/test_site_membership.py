"""Tests for detecting duplicate site membership of the same charger.

Two independent site entries could each conclude they own a charger's full
spare capacity and double-allocate the same fuse headroom, so the same
charger config entry must never be linked to more than one site.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.site.site_membership import (
    SITE_MEMBERSHIP_ERROR,
    chargers_claimed_by_other_sites,
    find_site_membership_conflicts,
    site_membership_errors,
)

from .helpers import make_site_entry


async def test_two_sites_claiming_the_same_charger_conflict(hass: HomeAssistant) -> None:
    make_site_entry(hass, entry_id="site_1", charger_entry_ids=["charger_a"])

    conflicts = find_site_membership_conflicts(hass, charger_entry_ids=["charger_a"])

    assert len(conflicts) == 1
    assert conflicts[0].charger_entry_id == "charger_a"
    assert conflicts[0].conflicting_site_entry_id == "site_1"


async def test_two_sites_with_different_chargers_do_not_conflict(hass: HomeAssistant) -> None:
    make_site_entry(hass, entry_id="site_1", charger_entry_ids=["charger_a"])

    conflicts = find_site_membership_conflicts(hass, charger_entry_ids=["charger_b"])

    assert conflicts == []


async def test_a_site_never_conflicts_with_itself(hass: HomeAssistant) -> None:
    entry = make_site_entry(hass, entry_id="site_1", charger_entry_ids=["charger_a"])

    conflicts = find_site_membership_conflicts(
        hass, charger_entry_ids=["charger_a"], exclude_entry_id=entry.entry_id
    )

    assert conflicts == []


async def test_site_membership_errors_maps_to_the_chargers_field(hass: HomeAssistant) -> None:
    make_site_entry(hass, entry_id="site_1", charger_entry_ids=["charger_a"])

    errors = site_membership_errors(hass, charger_entry_ids=["charger_a"])

    assert errors == {"charger_entry_ids": SITE_MEMBERSHIP_ERROR}


async def test_no_conflict_gives_an_empty_errors_dict(hass: HomeAssistant) -> None:
    assert site_membership_errors(hass, charger_entry_ids=["charger_a"]) == {}


async def test_chargers_claimed_by_other_sites_reflects_every_other_site(
    hass: HomeAssistant,
) -> None:
    make_site_entry(hass, entry_id="site_1", charger_entry_ids=["charger_a", "charger_b"])
    make_site_entry(hass, entry_id="site_2", charger_entry_ids=["charger_c"])

    claimed = chargers_claimed_by_other_sites(hass)

    assert claimed == {"charger_a", "charger_b", "charger_c"}


async def test_chargers_claimed_by_other_sites_excludes_the_given_entry(
    hass: HomeAssistant,
) -> None:
    entry = make_site_entry(hass, entry_id="site_1", charger_entry_ids=["charger_a"])

    claimed = chargers_claimed_by_other_sites(hass, exclude_entry_id=entry.entry_id)

    assert claimed == set()
