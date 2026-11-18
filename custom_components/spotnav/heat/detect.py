"""The heat pumps on the site, read from the registry (the thin Home Assistant shell around `facts.py`).

Found by the integrations' presence alone, as the battery is: every config entry of a supported integration
(`adapters.PLATFORMS`) is read, nothing is asked. Read only, and cheap enough for the diagnostics: the entries'
registry entities and their states, no I/O.
"""

from __future__ import annotations

import math
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .adapters import PLATFORMS, adapters_for
from .facts import HeatEntity, HeatPumpFacts, find_heat_pumps

#: Other heat pump controllers SpotNav steps back for (plan section 6): their integration's domain.
OTHER_CONTROLLERS = ("effektguard",)


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _heat_entity(entry: er.RegistryEntry, hass: HomeAssistant) -> HeatEntity:
    state = hass.states.get(entry.entity_id)
    attributes = {} if state is None else state.attributes
    options = attributes.get("options")
    return HeatEntity(
        entity_id=entry.entity_id,
        platform=entry.platform,
        unique_id=str(entry.unique_id or ""),
        device_id=entry.device_id,
        config_entry_id=entry.config_entry_id,
        disabled=entry.disabled_by is not None,
        state=None if state is None else state.state,
        unit=attributes.get("unit_of_measurement"),
        minimum=_float(attributes.get("min", attributes.get("min_temp"))),
        maximum=_float(attributes.get("max", attributes.get("max_temp"))),
        step=_float(attributes.get("step", attributes.get("target_temp_step"))),
        options=tuple(str(option) for option in options) if isinstance(options, (list, tuple)) else (),
    )


def other_controllers(hass: HomeAssistant) -> tuple[str, ...]:
    """The other heat controllers set up and not disabled here."""
    return tuple(
        domain
        for domain in OTHER_CONTROLLERS
        if any(entry.disabled_by is None for entry in hass.config_entries.async_entries(domain))
    )


def offset_capability(hass: HomeAssistant, entity_id: str) -> tuple[str, bool, bool] | None:
    """An offset number SpotNav knows, by its integration and unique id: `(brand, volatile, cloud)`; `None` for any
    other number (a person's own choice: treated as a stored setting). A registry read only."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    for adapter in adapters_for(entry.platform):
        for capability in adapter.capabilities:
            if capability.kind == "offset" and capability.match.matches(entry.domain, str(entry.unique_id or "")):
                return adapter.brand, capability.volatile, adapter.cloud
    return None


def read_heat_pumps(hass: HomeAssistant) -> list[HeatPumpFacts]:
    """Every heat pump SpotNav can read here, with its facts."""
    registry = er.async_get(hass)
    entities: list[HeatEntity] = []
    for domain in sorted(PLATFORMS):
        for config_entry in hass.config_entries.async_entries(domain):
            if config_entry.disabled_by is not None:
                continue
            entities.extend(
                _heat_entity(entry, hass)
                for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id)
                if entry.platform == domain
            )
    return find_heat_pumps(entities, other_controllers=other_controllers(hass))


def heat_pump_diagnostics(hass: HomeAssistant) -> dict[str, Any]:
    """The diagnostics block: the integrations looked for and each pump's facts. Nothing is controlled."""
    pumps = read_heat_pumps(hass)
    return {
        "integrations": sorted(PLATFORMS),
        "other_controllers": list(other_controllers(hass)),
        "pumps": [pump.as_dict() for pump in pumps],
    }
