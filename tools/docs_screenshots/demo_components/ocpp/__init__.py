"""Stand-in for the OCPP integration: a central system, one charge point, two connectors.

Only the shapes SpotNav reads are reproduced: the device tree, the entity ids and unique ids, and
the `get_configuration` / `configure` services. Nothing talks to a charger.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import device_registry as dr

DOMAIN = "ocpp"
PLATFORMS = ["switch", "number", "sensor"]

#: (charge point id, display name, connectors)
CHARGE_POINTS = (
    ("halo_charger", "Garage charger"),
    ("workshop_charger", "Workshop charger"),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    devices = dr.async_get(hass)
    devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "central")}, name="Central System",
        manufacturer="OCPP", model="Central system",
    )
    for cpid, name in CHARGE_POINTS:
        devices.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, cpid)}, name=name,
            manufacturer="Charge Amps", model="HALO", via_device=(DOMAIN, "central"),
        )
        devices.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{cpid}-1")}, name=f"{name} Connector 1",
            via_device=(DOMAIN, cpid),
        )

    async def get_configuration(call: ServiceCall) -> dict[str, str]:
        return {"key": call.data.get("ocpp_key", ""), "value": "1.16,2.10"}

    async def configure(call: ServiceCall) -> None:
        return None

    if not hass.services.has_service(DOMAIN, "get_configuration"):
        hass.services.async_register(
            DOMAIN, "get_configuration", get_configuration, supports_response=SupportsResponse.ONLY
        )
        hass.services.async_register(DOMAIN, "configure", configure)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
