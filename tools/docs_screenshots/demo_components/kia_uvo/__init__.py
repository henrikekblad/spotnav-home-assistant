"""Stand-in for the Kia Uvo integration: two cars with the entity keys the vehicle catalogue was tuned on.

Each car has its charge level, capacity, range and charge limits, its own "plugged in" sensor and a tracker at
home, so a charger both can charge at identifies which one is plugged in. `kia_uvo.docs_set_plug`
(`vehicle`: `family_car` or `city_car`, `plugged`: true or false) moves a car's plug sensor, as the car's own
report would; this service exists only in the screenshot tool.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

DOMAIN = "kia_uvo"
PLATFORMS = ["sensor", "number", "binary_sensor", "device_tracker"]
SIGNAL_PLUG = f"{DOMAIN}_docs_plug"

#: (vehicle id, entity id prefix, name, model, charge %, capacity kJ, range km)
VEHICLES = (
    ("KNAC1234567890123", "family_car", "Family car", "EV6", 64, 230400, 310),
    ("KNDC4567890123456", "city_car", "City car", "Niro EV", 48, 232200, 220),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    devices = dr.async_get(hass)
    for vehicle_id, _prefix, name, model, *_ in VEHICLES:
        devices.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, vehicle_id)}, name=name,
            manufacturer="Kia", model=model,
        )

    async def force_update(call: ServiceCall) -> None:
        return None

    async def set_plug(call: ServiceCall) -> None:
        async_dispatcher_send(hass, SIGNAL_PLUG, call.data["vehicle"], call.data["plugged"])

    if not hass.services.has_service(DOMAIN, "force_update"):
        hass.services.async_register(DOMAIN, "force_update", force_update)
        hass.services.async_register(
            DOMAIN, "docs_set_plug", set_plug,
            schema=vol.Schema({vol.Required("vehicle"): str, vol.Required("plugged"): bool}),
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
