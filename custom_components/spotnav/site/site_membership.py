"""Detect chargers that already belong to a different site config entry.

Two site entries listing the same charger would each allocate its full spare
capacity, double-allocating fuse headroom. Detection only; nothing is changed.
Shared by the create and options flows.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import CONF_CHARGER_ENTRY_IDS, CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE


# Looked up under config.error / options.error in the translation files.
SITE_MEMBERSHIP_ERROR = "charger_already_in_another_site"


@dataclass(frozen=True, slots=True)
class SiteMembershipConflict:
    """One charger a candidate site shares with an already-existing site."""

    charger_entry_id: str
    conflicting_site_entry_id: str
    conflicting_site_title: str


def find_site_membership_conflicts(
    hass: HomeAssistant,
    *,
    charger_entry_ids: list[str],
    exclude_entry_id: str | None = None,
) -> list[SiteMembershipConflict]:
    """Every candidate charger already claimed by a different site entry.

    `exclude_entry_id` lets a site compare against every *other* entry.
    """
    conflicts: list[SiteMembershipConflict] = []
    for other in _other_site_entries(hass, exclude_entry_id):
        other_chargers = set(other.data.get(CONF_CHARGER_ENTRY_IDS) or [])
        for charger_entry_id in charger_entry_ids:
            if charger_entry_id in other_chargers:
                conflicts.append(
                    SiteMembershipConflict(charger_entry_id, other.entry_id, other.title)
                )
    return conflicts


def site_membership_errors(
    hass: HomeAssistant,
    *,
    charger_entry_ids: list[str],
    exclude_entry_id: str | None = None,
) -> dict[str, str]:
    """A ready-to-use `errors` dict for `async_show_form`, or `{}` if none."""
    conflicts = find_site_membership_conflicts(
        hass, charger_entry_ids=charger_entry_ids, exclude_entry_id=exclude_entry_id
    )
    if not conflicts:
        return {}
    return {CONF_CHARGER_ENTRY_IDS: SITE_MEMBERSHIP_ERROR}


def chargers_claimed_by_other_sites(
    hass: HomeAssistant, *, exclude_entry_id: str | None = None
) -> set[str]:
    """Charger entry IDs already claimed by some other site entry.

    Filters the charger picker; `site_membership_errors` stays the
    authoritative check on submit (races, stale state).
    """
    claimed: set[str] = set()
    for other in _other_site_entries(hass, exclude_entry_id):
        claimed.update(other.data.get(CONF_CHARGER_ENTRY_IDS) or [])
    return claimed


def _other_site_entries(hass: HomeAssistant, exclude_entry_id: str | None) -> list[ConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.entry_id != exclude_entry_id
        and entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE
    ]
