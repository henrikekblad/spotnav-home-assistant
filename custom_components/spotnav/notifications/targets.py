"""The notify services a person can choose from: the Companion app's, named after their phones."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import slugify

from .settings import MOBILE_APP_PREFIX, NOTIFY_DOMAIN

#: The Companion app's integration, whose config entries name the phones.
MOBILE_APP_DOMAIN = "mobile_app"


def available_targets(hass: HomeAssistant) -> tuple[tuple[str, str], ...]:
    """Every `notify.mobile_app_*` service that exists now and the phone's name, ordered by name.

    The name is the Companion app's device name where its config entry says it, else the service name
    without its prefix.
    """
    names: dict[str, str] = {}
    for entry in hass.config_entries.async_entries(MOBILE_APP_DOMAIN):
        device_name = entry.data.get("device_name")
        if isinstance(device_name, str) and device_name:
            names[f"{MOBILE_APP_PREFIX}{slugify(device_name)}"] = device_name
    services = hass.services.async_services_for_domain(NOTIFY_DOMAIN)
    found = [
        (service, names.get(service, service.removeprefix(MOBILE_APP_PREFIX).replace("_", " ")))
        for service in services
        if service.startswith(MOBILE_APP_PREFIX)
    ]
    return tuple(sorted(found, key=lambda item: (item[1].casefold(), item[0])))


def encoded_available(available: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    return [{"service": service, "name": name} for service, name in available]


@dataclass(frozen=True, slots=True)
class CompanionDevice:
    """A Companion phone as the device registry knows it: what an app phone is matched by (`phones.match_companion`)."""

    service: str
    #: The device's name and the name a person gave it, where set.
    names: tuple[str, ...]
    model: str | None = None


def companion_devices(hass: HomeAssistant) -> tuple[CompanionDevice, ...]:
    """Every Companion phone whose `notify.mobile_app_*` service exists now, with its names and model."""
    registry = dr.async_get(hass)
    found: dict[str, CompanionDevice] = {}
    for entry in hass.config_entries.async_entries(MOBILE_APP_DOMAIN):
        device_name = entry.data.get("device_name")
        if not isinstance(device_name, str) or not device_name:
            continue
        service = f"{MOBILE_APP_PREFIX}{slugify(device_name)}"
        if service in found or not hass.services.has_service(NOTIFY_DOMAIN, service):
            continue
        names = [device_name]
        model = entry.data.get("model")
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            names.extend(name for name in (device.name, device.name_by_user) if isinstance(name, str) and name)
            if isinstance(device.model, str) and device.model:
                model = device.model
        found[service] = CompanionDevice(
            service=service,
            names=tuple(dict.fromkeys(names)),
            model=model if isinstance(model, str) and model else None,
        )
    return tuple(found.values())
