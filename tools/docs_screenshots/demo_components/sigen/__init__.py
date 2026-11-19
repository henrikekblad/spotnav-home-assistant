"""Stand-in for the Sigenergy integration: a plant with per-phase grid power and an inverter with voltages.

Entity ids and unique ids follow the recorded shape SpotNav's detection table is written against. Values
move a little every few seconds so the meter reads as live.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

DOMAIN = "sigen"
def _control(key: str, value) -> None:
    """A command the plant took: the demo house's battery follows it (`demo_house/model.py`)."""
    try:
        from custom_components.demo_house import model
    except ImportError:  # pragma: no cover
        return
    model.CONTROL[key] = value


PLATFORMS = ["sensor", "number", "select", "switch"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    devices = dr.async_get(hass)
    devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "plant")}, name="Grid meter",
        manufacturer="Sigenergy", model="Plant",
    )
    devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "inverter")}, name="Inverter",
        manufacturer="Sigenergy", model="Inverter",
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
