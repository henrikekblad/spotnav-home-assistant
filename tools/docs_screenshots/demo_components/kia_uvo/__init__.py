"""Stand-in for the Kia Uvo integration: one EV6 with the entity keys the vehicle catalogue was tuned on."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr

DOMAIN = "kia_uvo"
PLATFORMS = ["sensor", "number"]
VEHICLE_ID = "KNAC1234567890123"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, VEHICLE_ID)}, name="Family car",
        manufacturer="Kia", model="EV6",
    )

    async def force_update(call: ServiceCall) -> None:
        return None

    if not hass.services.has_service(DOMAIN, "force_update"):
        hass.services.async_register(DOMAIN, "force_update", force_update)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
