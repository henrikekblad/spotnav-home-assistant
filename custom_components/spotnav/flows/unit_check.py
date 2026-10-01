"""Checking the unit of a manually picked sensor, so a sensor without a device class (a DIY ESPHome
meter reader, say) can be chosen while a wrong quantity still cannot."""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er

#: The units each quantity may be reported in, by the quantity names the site form uses.
EXPECTED_UNITS: dict[str, tuple[str, ...]] = {
    "current": ("A", "mA"),
    "power": ("W", "kW"),
    "voltage": ("V",),
    "reactive_power": ("var", "kvar"),
    "apparent_power": ("VA", "kVA"),
}

#: The stable error code (under config.error / options.error) for a wrong or unknown unit, per quantity.
UNIT_ERRORS: dict[str, str] = {quantity: f"unit_expected_{quantity}" for quantity in EXPECTED_UNITS}


def entity_unit(hass, entity_id: str) -> str | None:
    """The unit an entity reports: its live state's, else the registry's (an entity not loaded yet)."""
    state = hass.states.get(entity_id)
    unit = state.attributes.get("unit_of_measurement") if state is not None else None
    if not unit:
        entry = er.async_get(hass).async_get(entity_id)
        unit = (entry.unit_of_measurement or entry.original_unit_of_measurement) if entry else None
    return unit if isinstance(unit, str) and unit else None


def unit_error(hass, entity_id: object, quantity: str) -> str | None:
    """The error code when `entity_id` does not report `quantity` in an accepted unit (or a live entity
    reports none at all), else `None`. Compared ignoring case, since integrations spell "VAr" and
    "kvar" both. An entity with no state and no registered unit (not loaded yet, say a meter entity
    that is disabled until the site is created) has nothing to contradict and is accepted."""
    if not isinstance(entity_id, str) or not entity_id:
        return None
    unit = entity_unit(hass, entity_id)
    if unit is None and hass.states.get(entity_id) is None:
        return None
    allowed = {candidate.casefold() for candidate in EXPECTED_UNITS[quantity]}
    if unit is not None and unit.strip().casefold() in allowed:
        return None
    return UNIT_ERRORS[quantity]
