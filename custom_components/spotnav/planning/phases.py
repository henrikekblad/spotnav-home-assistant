"""The phases a charge uses: the smaller of the charger's wiring and the vehicle's onboard charger.

* The charger's wiring is the site's phase wiring for the charger (`CONF_PHASE_WIRING`), else the
  charger's own answer (`CONF_CHARGER_PHASES`, what a charger in no site holds), else three.
* The vehicle is the one the charger plans for (`vehicle_properties.resolved_vehicle_id`); its onboard
  charger is 1 or 3 phases, three until told. With no vehicle the wiring alone decides.

The stored settings' `phases` is not read any more: it is what an older release recorded, moved here by
`async_migrate_settings_phases` once and kept on the wire (as this effective value) for older clients.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from homeassistant.core import HomeAssistant

from ..const import CONF_CHARGER_PHASES
from ..runtime import domain_data
from ..vehicles import vehicle_properties
from ..vehicles.vehicle_discovery import resolve_target_vehicle
from .first_run import wired_phases, charger_phases_from_entry, site_for_charger


#: What the wiring is taken to be when nothing says.
DEFAULT_WIRING_PHASES: Final = 3


@dataclass(frozen=True, slots=True)
class ChargingPhases:
    """The phases one charger's charge uses, and what limits them."""

    #: The smaller of `wiring` and `vehicle` (just `wiring` with no vehicle).
    phases: int
    #: The charger's wiring: 1 or 3.
    wiring: int
    #: The planned vehicle's onboard charger, or `None` with no vehicle.
    vehicle: int | None

    @property
    def limited_by_vehicle(self) -> bool:
        """Whether the car, not the wiring, sets the phases."""
        return self.phases < self.wiring


def charger_wiring(hass: HomeAssistant, entry_id: str) -> int | None:
    """The charger's wiring (1 or 3), or `None` when neither the site nor the charger says."""
    site = site_for_charger(hass, entry_id)
    wired = wired_phases(site, entry_id)
    if wired is not None:
        return wired
    if site is not None:
        return None
    entry = hass.config_entries.async_get_entry(entry_id)
    return None if entry is None else charger_phases_from_entry(entry.data)


def charging_phases(
    hass: HomeAssistant, entry_id: str, *, vehicle_id: str | None = None, with_vehicle: bool = True
) -> ChargingPhases:
    """The effective phases for this charger now. `vehicle_id` names the planned vehicle, else the one
    this charger resolves to; `with_vehicle=False` leaves the vehicle out.
    """
    wiring = charger_wiring(hass, entry_id) or DEFAULT_WIRING_PHASES
    vehicle: int | None = None
    if with_vehicle:
        resolved = (
            vehicle_properties.resolved_vehicle_id(hass, entry_id)
            if vehicle_id is None
            else resolve_target_vehicle(hass, vehicle_id)[0]
        )
        if resolved is not None:
            vehicle = vehicle_properties.onboard_phases(hass, resolved)
    return ChargingPhases(
        phases=wiring if vehicle is None else min(wiring, vehicle), wiring=wiring, vehicle=vehicle
    )


def effective_phases(hass: HomeAssistant, entry_id: str) -> int:
    """The phases a charge on this charger uses (1 or 3)."""
    return charging_phases(hass, entry_id).phases


async def async_migrate_settings_phases(hass: HomeAssistant, entry_id: str) -> bool:
    """Move an older release's settings `phases` to where it belongs, once, then stop reading it.

    A stored value below the charger's wiring becomes the planned vehicle's onboard phases, else (no
    vehicle, no site) the charger's own wiring answer. A value at or above the wiring says nothing the
    wiring does not, and a vehicle that already has an onboard answer keeps it. The stored field is
    cleared either way, which is also what makes this run only once. Returns whether anything moved.
    """
    store = domain_data(hass).auto_store
    if store is None:
        return False
    stored = store.settings(entry_id).phases
    if stored is None:
        return False
    moved = False
    wiring = charger_wiring(hass, entry_id) or DEFAULT_WIRING_PHASES
    if stored in (1, 3) and stored < wiring:
        moved = await _async_apply_lower_phases(
            hass, entry_id, stored, store.settings(entry_id).target.vehicle_id
        )
    await store.async_clear_legacy_phases(entry_id)
    return moved


async def _async_apply_lower_phases(
    hass: HomeAssistant, entry_id: str, phases: int, target_vehicle_id: str | None
) -> bool:
    decisions = domain_data(hass).decision_store
    # A vehicle the settings name counts even while its integration has not loaded yet at startup.
    vehicle_id = target_vehicle_id or vehicle_properties.resolved_vehicle_id(hass, entry_id)
    if vehicle_id is not None and decisions is not None:
        if vehicle_properties.stored_properties(hass, vehicle_id).onboard_phases is not None:
            return False
        await vehicle_properties.async_update_vehicle_properties(
            hass, decisions, vehicle_id, {vehicle_properties.KEY_ONBOARD_PHASES: phases}
        )
        return True
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or site_for_charger(hass, entry_id) is not None:
        return False
    data = dict(entry.data)
    data[CONF_CHARGER_PHASES] = phases
    hass.config_entries.async_update_entry(entry, data=data)
    return True


__all__ = [
    "ChargingPhases",
    "async_migrate_settings_phases",
    "charger_wiring",
    "charging_phases",
    "effective_phases",
]
