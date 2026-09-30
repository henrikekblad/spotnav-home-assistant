"""Detect whether a candidate charger configuration collides with an existing entry.

Two `ChargingController` instances commanding the same charge-control switch or current-limit
number could send contradictory commands to one charger. This only detects the risk during
config/options flow validation; it changes no entry and never runs at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
)


ConflictField = Literal["charge_control", "current_limit"]

# Looked up under config.error / options.error in the translation files.
CHARGE_CONTROL_ERROR = "charge_control_in_use"
CURRENT_LIMIT_ERROR = "current_limit_in_use"


@dataclass(frozen=True, slots=True)
class EntityConflict:
    """One existing SpotNav entry that already owns a candidate entity."""

    field: ConflictField
    conflicting_entry_id: str
    conflicting_entry_title: str

    @property
    def error_code(self) -> str:
        return CHARGE_CONTROL_ERROR if self.field == "charge_control" else CURRENT_LIMIT_ERROR


def find_conflict(
    hass: HomeAssistant,
    *,
    charge_control: str,
    current_limit: str | None,
    exclude_entry_id: str | None = None,
) -> EntityConflict | None:
    """The first existing SpotNav entry that conflicts with this candidate.

    Compares full entity IDs only, never names or serial numbers. An empty `current_limit`
    never conflicts. `exclude_entry_id` skips the entry being edited.
    """
    normalized_current_limit = current_limit or None
    for entry in _other_entries(hass, exclude_entry_id):
        if entry.data.get(CONF_CHARGE_CONTROL) == charge_control:
            return EntityConflict("charge_control", entry.entry_id, entry.title)
        other_current_limit = entry.data.get(CONF_CURRENT_LIMIT) or None
        if (
            normalized_current_limit is not None
            and other_current_limit is not None
            and other_current_limit == normalized_current_limit
        ):
            return EntityConflict("current_limit", entry.entry_id, entry.title)
    return None


def conflict_errors(
    hass: HomeAssistant,
    *,
    charge_control: str,
    current_limit: str | None,
    exclude_entry_id: str | None = None,
) -> dict[str, str]:
    """A ready-to-use `errors` dict for `async_show_form`, or `{}` if none.

    Shared by the generic step, the OCPP step and the options flow.
    """
    conflict = find_conflict(
        hass,
        charge_control=charge_control,
        current_limit=current_limit,
        exclude_entry_id=exclude_entry_id,
    )
    if conflict is None:
        return {}
    return {conflict.field: conflict.error_code}


def _other_entries(hass: HomeAssistant, exclude_entry_id: str | None) -> list[ConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.entry_id != exclude_entry_id
        # Explicitly charger entries only: other entry kinds have nothing to conflict over.
        and entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_CHARGER
    ]
