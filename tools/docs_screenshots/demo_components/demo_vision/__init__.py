"""A camera that sees the charger's parking spot and an AI Task entity that can look at pictures, so SpotNav's
camera identification can be set up in the screenshots.

The camera's picture is drawn (`scene.py`): no photograph, no number plate. `demo_vision.docs_park`
(`vehicle`: `family_car`, `city_car` or `none`) chooses which car stands in it. The AI Task entity answers
every comparison with the first car and "medium", so the camera never decides alone and the question is still
asked. Both exist only in the screenshot tool.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr

DOMAIN = "demo_vision"
PLATFORMS = ["camera", "ai_task"]
#: The car in the camera's picture now (`None`: the bay is empty).
PARKED = {"vehicle": "family_car"}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    devices = dr.async_get(hass)
    devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "camera")}, name="Driveway camera",
        manufacturer="Demo", model="Camera",
    )
    devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "ai_task")}, name="Local vision model",
        manufacturer="Demo", model="AI Task",
    )

    async def park(call: ServiceCall) -> None:
        vehicle = call.data["vehicle"]
        PARKED["vehicle"] = None if vehicle == "none" else vehicle

    if not hass.services.has_service(DOMAIN, "docs_park"):
        hass.services.async_register(DOMAIN, "docs_park", park, schema=vol.Schema({vol.Required("vehicle"): str}))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
