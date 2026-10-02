"""Zaptec: its limit number sits on the Installation device and caps every charger under it."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .registry import register_external_balancer, register_installation_check

#: Home Assistant integrations whose load balancer writes the Zaptec installation's available current
#: through Zaptec's cloud (Perific/Enegic: its reporter runs in Enegic's cloud, the installation is
#: put in Manual Power Control and Enegic is its owner). Two writers on one field: the last one wins,
#: and a value above the balancer's is a fuse risk until its next write.
EXTERNAL_BALANCERS = ("perific",)


def zaptec_balanced_elsewhere(hass: HomeAssistant) -> str | None:
    """The domain of the integration that balances Zaptec through its own cloud, when it is set up."""
    for domain in EXTERNAL_BALANCERS:
        if hass.config_entries.async_entries(domain):
            return domain
    return None


def single_charger_installation(hass: HomeAssistant, number_entity_id: str) -> bool:
    """Whether the installation a limit number belongs to has exactly one charger.

    Zaptec's number sits on the Installation device and caps every charger under it. The chargers are
    the devices that name it as `via_device`; an installation with none that can be found, or several,
    is not single, so the write is refused rather than lowering someone else's charger.
    """
    entity = er.async_get(hass).async_get(number_entity_id)
    if entity is None or entity.device_id is None:
        return False
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    children = [
        device
        for device in devices.devices
        if device.via_device_id == entity.device_id
        and any(
            candidate.platform == entity.platform
            for candidate in er.async_entries_for_device(registry, device.id)
        )
    ]
    return len(children) == 1


register_installation_check(single_charger_installation)
register_external_balancer(zaptec_balanced_elsewhere)
