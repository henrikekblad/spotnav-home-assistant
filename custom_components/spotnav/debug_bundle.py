"""The one-click debug bundle: everything a support question usually needs, in one redacted JSON.

Built by `async_build_debug_bundle` for the whole installation (every SpotNav entry) and offered two
ways: inside the diagnostics of the site entry (Home Assistant's own "Download diagnostics"), and by
the admin-only `spotnav/get_debug_bundle` command the card's "Download debug info" button uses.

Redaction is one pass over the finished bundle (`redact_bundle`), so no section can forget it:
webhook ids, tokens, secrets and the OCPP charge point id by key, coordinates rounded to one decimal,
and every string scrubbed of the known webhook ids, webhook paths, bearer tokens, the home location
and the names of Home Assistant's users. Entity ids are kept; they are what a support answer needs.

Every fact is in the bundle once (version 2): the price data at the top; a site's `result` and
`capability` and a charger's command log (`controller.adapter.commands`) inside the entry's
`diagnostics`; a charger's plan, strategy and progress inside its `dashboard`.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .api.dashboard import capture_dashboard, serialize_dashboard
from .card_asset import CARD_ASSET_PATH, read_bundle_digest
from .const import (
    CONF_CHARGER_PLATFORM,
    CONF_ENTRY_TYPE,
    CONF_OCPP_CHARGE_POINT_ID,
    DOMAIN,
    ENTRY_TYPE_SITE,
)
from .diagnostics import _price_data, entry_diagnostics, TO_REDACT
from .execution.other_controllers import CONTROLLERS
from .planning.hybrid_forecast import async_forecast_capable_domains
from .runtime import domain_data

_LOGGER = logging.getLogger(__name__)

BUNDLE_VERSION: Final = 2
REDACTED: Final = "**REDACTED**"

#: Keys whose value is never shown, wherever they sit in the bundle.
SECRET_KEYS: Final = frozenset(
    {
        *TO_REDACT,
        "webhook_id",
        "webhook_url",
        "pairing_url",
        "pairing_secret",
        "pairing_code",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "password",
        "secret",
        "client_secret",
        "authorization",
    }
)
#: Keys whose number is a place on Earth: kept to one decimal (about 10 km).
#: Secret keys whose stored value is also removed from free text. The charge point id is left out: it
#: is part of entity ids, which the bundle keeps.
_SCRUB_VALUE_KEYS: Final = SECRET_KEYS - {CONF_OCPP_CHARGE_POINT_ID, "charge_point_id"}
COORDINATE_KEYS: Final = frozenset({"latitude", "longitude", "lat", "lon", "lng"})

_WEBHOOK_PATH = re.compile(r"(/api/webhook/)[A-Za-z0-9_\-]+")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]+")
#: Shortest user name worth scrubbing; shorter ones would mangle ordinary words.
_MIN_NAME_LENGTH = 3


class Scrubber:
    """What to remove from free text: the known secrets, the home location and people's names."""

    def __init__(self, secrets: Iterable[str] = (), names: Iterable[str] = (), places: Iterable[float] = ()) -> None:
        self._secrets = sorted({s for s in secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True)
        self._names = sorted({n for n in names if isinstance(n, str) and len(n) >= _MIN_NAME_LENGTH}, key=len, reverse=True)
        self._places = [(repr(p), repr(round(p, 1))) for p in places if isinstance(p, float)]

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        for full, rounded in self._places:
            value = value.replace(full, rounded)
        for name in self._names:
            value = value.replace(name, "<user>")
        value = _WEBHOOK_PATH.sub(rf"\1{REDACTED}", value)
        return _BEARER.sub(rf"\1{REDACTED}", value)


def redact_bundle(value: Any, scrubber: Scrubber) -> Any:
    """A copy of `value` with every secret key replaced, coordinates rounded and strings scrubbed."""
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            name = str(key).lower()
            if name in SECRET_KEYS:
                result[key] = REDACTED if item is not None else None
            elif name in COORDINATE_KEYS and isinstance(item, (int, float)) and not isinstance(item, bool):
                result[key] = round(float(item), 1)
            else:
                result[key] = redact_bundle(item, scrubber)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_bundle(item, scrubber) for item in value]
    if isinstance(value, str):
        return scrubber.text(value)
    return value


def _secret_values(entries: Iterable[ConfigEntry]) -> list[str]:
    """Every value stored under a secret key in an entry's data or options."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, item in node.items():
                if str(key).lower() in _SCRUB_VALUE_KEYS:
                    if isinstance(item, str):
                        found.append(item)
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    for entry in entries:
        walk(entry.data)
        walk(entry.options)
    return found


async def _scrubber(hass: HomeAssistant, entries: list[ConfigEntry]) -> Scrubber:
    names: list[str] = []
    try:
        names = [user.name for user in await hass.auth.async_get_users() if user.name]
    except Exception:  # noqa: BLE001 - a missing name list must not stop the bundle
        _LOGGER.debug("Could not read the user names to scrub", exc_info=True)
    return Scrubber(
        secrets=_secret_values(entries),
        names=names,
        places=[hass.config.latitude, hass.config.longitude],
    )


def _entity_ids(node: Any) -> Iterable[str]:
    if isinstance(node, str):
        if re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", node):
            yield node
    elif isinstance(node, Mapping):
        for item in node.values():
            yield from _entity_ids(item)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _entity_ids(item)


async def _related_integrations(
    hass: HomeAssistant, entries: list[ConfigEntry], extra_entity_ids: Iterable[str], forecast: Iterable[str]
) -> list[dict[str, Any]]:
    """The other integrations SpotNav leans on or collides with: name, version and why. No settings."""
    from homeassistant.helpers import entity_registry as er

    roles: dict[str, set[str]] = {}

    def note(domain: str | None, role: str) -> None:
        if domain and domain != DOMAIN:
            roles.setdefault(domain, set()).add(role)

    for entry in entries:
        note(entry.data.get(CONF_CHARGER_PLATFORM), "charger")
    registry = er.async_get(hass)
    entity_ids = {*extra_entity_ids}
    for entry in entries:
        entity_ids.update(_entity_ids(dict(entry.data)))
        entity_ids.update(_entity_ids(dict(entry.options)))
    for entity_id in entity_ids:
        registered = registry.async_get(entity_id)
        if registered is not None:
            note(registered.platform, "entity_source")
    for spec in CONTROLLERS:
        if hass.config_entries.async_entries(spec.domain):
            note(spec.domain, "other_controller")
    for domain in ("ocpp", "evcc", "openwb"):
        if hass.config_entries.async_entries(domain):
            note(domain, "charger")
    for domain in forecast:
        note(domain, "solar_forecast")

    result: list[dict[str, Any]] = []
    for domain in sorted(roles):
        name: str | None = None
        version: str | None = None
        try:
            integration = await async_get_integration(hass, domain)
            name = integration.name
            version = integration.version.string if integration.version is not None else None
        except Exception:  # noqa: BLE001 - not loadable: still worth naming
            pass
        result.append({"domain": domain, "name": name, "version": version, "roles": sorted(roles[domain])})
    return result


async def _versions(hass: HomeAssistant) -> dict[str, Any]:
    integration = await async_get_integration(hass, DOMAIN)
    digest = await hass.async_add_executor_job(read_bundle_digest)
    installation_type: str | None = None
    try:
        from homeassistant.helpers.system_info import async_get_system_info

        installation_type = (await async_get_system_info(hass)).get("installation_type")
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not read the installation type", exc_info=True)
    return {
        "spotnav": integration.version.string if integration.version is not None else None,
        "card_bundle_hash": digest,
        "card_bundle_file": CARD_ASSET_PATH.name,
        "home_assistant": HA_VERSION,
        "python": sys.version.split()[0],
        "installation_type": installation_type,
    }


def _trim_dashboard(dashboard: dict[str, Any]) -> dict[str, Any]:
    """The dashboard as the card gets it, minus the chart rows (the bundle states the price status)."""
    trimmed = dict(dashboard)
    prices = trimmed.get("prices")
    if isinstance(prices, dict):
        trimmed["prices"] = {key: value for key, value in prices.items() if key != "intervals"}
    return trimmed


def _entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """An entry's diagnostics without the price data, which the bundle states once at its top."""
    diagnostics = entry_diagnostics(hass, entry)
    diagnostics.pop("price_data", None)
    return diagnostics


async def _charger_section(
    hass: HomeAssistant, entry: ConfigEntry, forecast: frozenset[str]
) -> dict[str, Any]:
    section: dict[str, Any] = {"entry_id": entry.entry_id, "title": entry.title}
    section["diagnostics"] = _entry_diagnostics(hass, entry)
    try:
        dashboard = serialize_dashboard(
            capture_dashboard(hass, entry, forecast_domains=forecast), can_act=False
        )
    except Exception as err:  # noqa: BLE001 - one broken charger must not cost the whole bundle
        _LOGGER.warning("The debug bundle could not capture a charger's dashboard: %s", type(err).__name__)
        section["dashboard"] = {"available": False, "reason": "capture_failed"}
        section["status"] = None
        return section
    section["dashboard"] = _trim_dashboard(dashboard)
    section["status"] = dashboard.get("status")
    return section


async def async_build_debug_bundle(hass: HomeAssistant) -> dict[str, Any]:
    """The whole installation as one redacted, JSON-safe document. Never raises for a bad entry."""
    entries = list(hass.config_entries.async_entries(DOMAIN))
    forecast = await async_forecast_capable_domains(hass)
    chargers = [e for e in entries if e.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SITE]
    sites = [e for e in entries if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE]
    charger_sections = [await _charger_section(hass, entry, forecast) for entry in chargers]
    site_sections = []
    for entry in sites:
        site_sections.append(
            {"entry_id": entry.entry_id, "title": entry.title, "diagnostics": _entry_diagnostics(hass, entry)}
        )
    vehicle_entities = [
        vehicle.get("soc_entity_id")
        for section in charger_sections
        for vehicle in (section.get("dashboard", {}).get("vehicles") or [])
        if isinstance(vehicle, dict)
    ]
    buffer = domain_data(hass).log_buffer
    bundle = {
        "bundle_version": BUNDLE_VERSION,
        "generated_at": dt_util.utcnow().isoformat(),
        "versions": await _versions(hass),
        "related_integrations": await _related_integrations(
            hass, entries, [v for v in vehicle_entities if isinstance(v, str)], forecast
        ),
        "price_data": _price_data(hass),
        "sites": site_sections,
        "chargers": charger_sections,
        "log": {
            "note": "The last SpotNav log records at INFO and above; DEBUG ones only if DEBUG was enabled.",
            "records": [] if buffer is None else buffer.records(),
            "debug_records": [] if buffer is None else buffer.debug_records(),
        },
    }
    return redact_bundle(bundle, await _scrubber(hass, entries))

