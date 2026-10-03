"""The notify services a person can choose from: the Companion app's, named after their phones."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
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
