"""The phases a charge uses: the smaller of the charger's wiring and the vehicle's onboard charger.

* The charger's wiring is the site's phase wiring for the charger (`CONF_PHASE_WIRING`), else the
  charger's own answer (`CONF_CHARGER_PHASES`, what a charger in no site holds), else three.
* The vehicle is the one the charger plans for (`vehicle_properties.resolved_vehicle_id`); its onboard
  charger is 1 or 3 phases, three until told. With no vehicle the wiring alone decides.

What charges show is learned here too (`PhaseObserver`, `async_record_charge_phases`): three phases confirm
the wiring, one phase on three-phase wiring twice running for the same vehicle becomes a suggestion about
its onboard charger. Nothing but a site-less charger's unset wiring is ever changed by itself.

The stored settings' `phases` is not read any more: it is what an older release recorded, moved here by
`async_migrate_settings_phases` once and kept on the wire (as this effective value) for older clients.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from homeassistant.core import HomeAssistant

from ..const import CONF_CHARGER_PHASES
from ..runtime import domain_data
from ..vehicles import vehicle_properties
from ..vehicles.discovery_decisions import DECISION_DOMAIN_PHASE_OBSERVATIONS
from ..vehicles.vehicle_discovery import charger_vehicle_ids, resolve_target_vehicle
from .first_run import wired_phases, charger_phases_from_entry, site_for_charger


_LOGGER = logging.getLogger(__name__)

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
            else resolve_target_vehicle(hass, vehicle_id, charger_vehicle_ids(hass, entry_id))[0]
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


#: A charge counts only while the highest phase carries more than this.
CHARGING_CURRENT_A: Final = 1.0
#: A phase counts as carrying the charge from this current up.
PHASE_CARRIES_A: Final = 2.0
#: Samples (a sample is taken about every half minute) a charge needs before it says anything.
MIN_SAMPLES: Final = 3
#: One-phase charges of the same vehicle, running, before its onboard charger is suggested to be 1-phase.
SUGGEST_AFTER_CHARGES: Final = 2

KEY_ONE_PHASE: Final = "one_phase_charges"
KEY_THREE_PHASE: Final = "three_phase_charges"


def phases_carrying(currents: Sequence[float | None]) -> int | None:
    """How many of the three phases carry the charge (at least `PHASE_CARRIES_A`), or `None` when one of
    them is unreadable: a missing phase may be the one that carries it.
    """
    if len(currents) != 3 or any(value is None for value in currents):
        return None
    return sum(1 for value in currents if value is not None and value >= PHASE_CARRIES_A)


class PhaseObserver:
    """Counts the phases one charge uses from the charger's measured current per phase.

    Feed it `sample(charging, currents)` about every half minute. A sample counts when all three phases
    are readable and the highest carries more than `CHARGING_CURRENT_A`. The charge's answer is the most
    phases seen carrying it, given once `MIN_SAMPLES` samples counted: three is returned as soon as it is
    certain (`sample` returns 3), one only when the charge ends (`charging` false), so a ramp-up that has
    not reached its other phases yet is never taken for a one-phase charge.
    """

    def __init__(self) -> None:
        self._samples = 0
        self._best = 0
        self._announced = False

    def sample(self, charging: bool, currents: Sequence[float | None] | None) -> int | None:
        """The finished observation (1, 2 or 3 phases) this sample completes, else `None`."""
        if not charging:
            result = self._best if self._samples >= MIN_SAMPLES and not self._announced else None
            self._reset()
            return result
        count = None if currents is None else phases_carrying(currents)
        if count is None or max(value or 0.0 for value in (currents or ())) <= CHARGING_CURRENT_A:
            return None
        self._samples += 1
        self._best = max(self._best, count)
        if self._best == 3 and self._samples >= MIN_SAMPLES and not self._announced:
            self._announced = True
            return 3
        return None

    def _reset(self) -> None:
        self._samples = 0
        self._best = 0
        self._announced = False


def _observation_key(vehicle_id: str | None, entry_id: str) -> str:
    return f"vehicle:{vehicle_id}" if vehicle_id is not None else f"charger:{entry_id}"


def _counts(hass: HomeAssistant, key: str) -> dict[str, int]:
    store = domain_data(hass).decision_store
    payload = {} if store is None else store.confirmed_payload(DECISION_DOMAIN_PHASE_OBSERVATIONS, key) or {}
    def count(name: str) -> int:
        value = payload.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    return {KEY_ONE_PHASE: count(KEY_ONE_PHASE), KEY_THREE_PHASE: count(KEY_THREE_PHASE)}


async def _async_store_counts(hass: HomeAssistant, key: str, counts: dict[str, int]) -> None:
    store = domain_data(hass).decision_store
    if store is not None:
        await store.async_confirm(DECISION_DOMAIN_PHASE_OBSERVATIONS, key, dict(counts))


async def async_record_charge_phases(hass: HomeAssistant, entry_id: str, phases: int) -> None:
    """Take one finished charge's observed phases (1, 2 or 3) into what is known.

    * Three phases confirm the wiring and the car: the vehicle's one-phase run starts over, and a charger
      in no site whose wiring nothing says is set to three (logged). A stated wiring is never touched.
    * One phase on wiring of three counts against the planned vehicle (unless it already says one), which
      `onboard_suggestion` turns into a question after `SUGGEST_AFTER_CHARGES` in a row. On a charger in
      no site whose wiring is unset it is only counted for the charger: wiring and car cannot be told apart.
    * Two phases say nothing either way.
    """
    if phases not in (1, 3) or domain_data(hass).decision_store is None:
        return
    wiring = charger_wiring(hass, entry_id)
    vehicle_id = vehicle_properties.resolved_vehicle_id(hass, entry_id)
    charger_key = _observation_key(None, entry_id)
    charger = _counts(hass, charger_key)
    if phases == 3:
        charger[KEY_THREE_PHASE] += 1
        await _async_store_counts(hass, charger_key, charger)
        if vehicle_id is not None:
            key = _observation_key(vehicle_id, entry_id)
            vehicle = _counts(hass, key)
            vehicle[KEY_THREE_PHASE] += 1
            vehicle[KEY_ONE_PHASE] = 0
            await _async_store_counts(hass, key, vehicle)
        if wiring is None:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is not None and site_for_charger(hass, entry_id) is None:
                _LOGGER.info("Charger %s charged on three phases: wiring set to three phases", entry.title)
                hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CHARGER_PHASES: 3})
        return
    if wiring == 1:
        return  # one phase is what the wiring says
    charger[KEY_ONE_PHASE] += 1
    await _async_store_counts(hass, charger_key, charger)
    if wiring is None or vehicle_id is None:
        return
    if vehicle_properties.stored_properties(hass, vehicle_id).onboard_phases is not None:
        return
    key = _observation_key(vehicle_id, entry_id)
    vehicle = _counts(hass, key)
    vehicle[KEY_ONE_PHASE] += 1
    await _async_store_counts(hass, key, vehicle)


def onboard_suggestion(hass: HomeAssistant, vehicle_id: str) -> int | None:
    """`1` when this vehicle's onboard charger looks single-phase and nobody has answered yet, else `None`.

    Answering at all ends the question: "1-phase" is the suggestion taken, "3-phase" the one dismissed
    (`update_vehicle` stores either), and a three-phase charge starts the count over.
    """
    if vehicle_properties.stored_properties(hass, vehicle_id).onboard_phases is not None:
        return None
    if _counts(hass, _observation_key(vehicle_id, "")).get(KEY_ONE_PHASE, 0) >= SUGGEST_AFTER_CHARGES:
        return 1
    return None


__all__ = [
    "ChargingPhases",
    "PhaseObserver",
    "async_record_charge_phases",
    "onboard_suggestion",
    "phases_carrying",
    "async_migrate_settings_phases",
    "charger_wiring",
    "charging_phases",
    "effective_phases",
]
