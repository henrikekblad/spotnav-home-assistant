"""A charger behind a smart plug, or one whose integration has no lifetime register: its power sensor,
read and integrated to energy.

A plug reports power, not an energy register, and so do some chargers' integrations (zappi, ABB Terra AC,
Ohme, ChargePoint, Hypervolt: `PlatformProfile.energy_power_keys`). SpotNav integrates the power itself
(trapezoid) into a cumulative kWh counter, which then stands in for the energy register everything else
reads (`sensor.py`'s integrated-energy sensor, restored across restarts). Two rules keep it honest: an
interval is integrated only when both of its samples are fresh and at most `max_age_s` apart, and a
restart begins with no previous sample, so nothing is integrated over time SpotNav did not see.

A sample is fresh while its sensor last reported (`last_reported`, which a polled integration refreshes at
every poll, changed or not) within `max_age_s`. A cloud charger is polled every one to ten minutes
(ChargePoint offers up to ten, myenergi and Ohme one), so its own power sensor is given
`CHARGER_POWER_MAX_AGE_S`: its reading stands until the next poll, as the integration means it to. An
integration whose poll fails makes its entities unavailable, which is a gap at once; only one that goes
silent while still available is integrated on, for at most that long.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from ..const import CONF_ENERGY_REGISTER_ENTITY, CONF_POWER_ENTITY, DOMAIN

#: The unique id suffix of the integrated-energy sensor, `{entry_id}_{suffix}`.
INTEGRATED_ENERGY_KEY: Final = "integrated_energy"

#: How long a charger's own power sensor's reading stands: one poll of the slowest cloud integration (ten
#: minutes) and a minute to spare.
CHARGER_POWER_MAX_AGE_S: Final = 660.0

#: The power units a plug's sensor may report, in watts per unit.
_WATTS_PER_UNIT: Final = {"W": 1.0, "kW": 1000.0}


def integrated_energy_unique_id(entry_id: str) -> str:
    return f"{entry_id}_{INTEGRATED_ENERGY_KEY}"


def configured_register_entity(hass: HomeAssistant, entry_id: str) -> str | None:
    """The energy register a charger entry's stored settings name now: its stored register, else the energy
    integrated from its smart plug, else `None` (an OCPP connector's automatic register, or none).

    For a stored count from before counts recorded their register: the stores read it when they load, before any
    charger is set up (and so before setup may replace a register, `energy_register.py`), so it names the register
    that count's readings came from."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return None
    stored = entry.data.get(CONF_ENERGY_REGISTER_ENTITY)
    if stored:
        return str(stored)
    if entry.data.get(CONF_POWER_ENTITY):
        return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, integrated_energy_unique_id(entry_id))
    return None


def power_max_age_s(measurement_max_age_s: float, *, smart_plug: bool) -> float:
    """How old a power sample may be and still be integrated: a smart plug's, the measurement age; a charger's
    own sensor, at least `CHARGER_POWER_MAX_AGE_S` (polled by its cloud)."""
    return measurement_max_age_s if smart_plug else max(measurement_max_age_s, CHARGER_POWER_MAX_AGE_S)


def power_w_of(state: State | None) -> float | None:
    """One power sensor's value in watts, or `None` when unreadable, not a number, or not W or kW.

    Requires device class `power`: an energy or current sensor read as power would integrate nonsense.
    A negative value is exported power, which is not the charger drawing, so it reads as 0.
    """
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    attributes = state.attributes or {}
    if attributes.get("device_class") != "power":
        return None
    factor = _WATTS_PER_UNIT.get(str(attributes.get("unit_of_measurement")))
    if factor is None:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return max(value, 0.0) * factor


def read_power_w(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """The sensor's power in watts now, or `None`."""
    return power_w_of(hass.states.get(entity_id)) if entity_id else None


def fresh_power_w(hass: HomeAssistant, entity_id: str | None, now: datetime, max_age_s: float) -> float | None:
    """The power in watts, only while the sensor last reported within `max_age_s` (a plug that went
    quiet is a gap, never a steady value)."""
    state = hass.states.get(entity_id) if entity_id else None
    if state is None or (now - state.last_reported).total_seconds() > max_age_s:
        return None
    return power_w_of(state)


class PowerIntegrator:
    """Trapezoid integration of power samples into cumulative kWh; pure and clock-free.

    `sample(at, power_w)` takes the next sample (`None` for no usable one). An interval counts only
    when it is positive and at most `max_age_s` long and both ends are usable, so a gap is never
    filled with a guess.
    """

    def __init__(self, max_age_s: float, total_kwh: float = 0.0) -> None:
        self.max_age_s = max_age_s
        self.total_kwh = total_kwh
        self._last: tuple[datetime, float] | None = None

    def sample(self, at: datetime, power_w: float | None) -> None:
        if power_w is None:
            self._last = None
            return
        last = self._last
        if last is not None:
            seconds = (at - last[0]).total_seconds()
            if 0 < seconds <= self.max_age_s:
                self.total_kwh += (last[1] + power_w) / 2.0 * seconds / 3_600_000.0
        if last is None or at >= last[0]:
            self._last = (at, power_w)
