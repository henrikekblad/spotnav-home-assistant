"""FAKE stand-in for core Balboa: one spa that does not exist, for looking at SpotNav's Spa tab on a real Home
Assistant. Nothing reaches a network; the water is an in-memory model that starts over at every restart.

The climate has the shape SpotNav reads for core `balboa` (`spa/detect.py`, `spa/adapters.py`): presets `ready`,
`rest` and `ready_in_rest`, hvac `heat` (Ready) or `off` (Rest), `hvac_action` heating/idle, the bath temperature
as `temperature` and the water as `current_temperature`, range 26.5-40 °C. Remove the folder to remove it.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

DOMAIN = "balboa"
PLATFORMS = ["climate"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
