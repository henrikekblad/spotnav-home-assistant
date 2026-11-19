"""The shared settings popover's site reads, for a home with or without a charger.

`spotnav/list_sites {api_version: 1}`: every loaded site, by name: `{api_version, sites: [{site_id, name,
charger_ids}]}`. The card editors and pickers find a site with it, as they find a charger with `list_chargers`.

`spotnav/get_site_settings_view {api_version: 1, site_id | charger_id}`: what the popover shows for the site, the
charger's site when a charger is named:

* `site_id`, `name`, `charger_ids`; `writable` (an administrator's connection).
* `price`: the electricity price entry the site reads (`site_price_source`): `{entry_id, title, area_id, area_name}`,
  `area_name` `null` while the area list does not name it; `null` when no price applies to the site.
* `battery`: `{found}`, whether the site's battery facts name a battery (the battery section is shown then).
* `price_settings` (additive): what the popover's price section shows and edits, `null` exactly where `price` is:
  `market` (the area's facts: `area_id`, `area_name`, `countries`, `timezone`, `currency`, `major_unit`,
  `minor_unit`, each `null` while the area list does not name the area, `area_id` never), `fiscal` (the dashboard's
  `fiscal` block, `null` while the area is not listed), `fees` (the price entry's stored row as the settings body
  states one, `{area_id, vat, tax, transfer}`, which `spotnav/update_price` writes), `provider` (the dashboard's
  `prices.provider`), `price_source` and `grid_tariff` (the dashboard's blocks, read for the site:
  `api/price_source.py`, `api/grid_tariff.py`).
* `site` (additive): the site's own `site` block as the dashboard and `spotnav/update_site_settings` state it (solar
  priority, forecast sources and their choices, load balancing), `null` while the site is not running.
* `setup`: the site's own "Get started" rows (`setup_checklist.capture_site_setup`), in the dashboard's shape.

Refusals are WebSocket errors with stable codes: `spotnav_unsupported_api_version`, `spotnav_unknown_site`,
`spotnav_no_site` (the site is not loaded, or the charger is in none), and the charger codes of `lookup_charger`.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from ..const import CONF_CHARGER_ENTRY_IDS, CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE
from ..planning.auto_settings import AutoSettings
from ..planning.hybrid_forecast import async_forecast_capable_domains
from ..pricing.price_source import PriceSource, site_price_source
from ..runtime import domain_data
from ..runtime import site_controller_for
from ..setup_checklist import capture_site_setup, serialize_setup
from .dashboard import serialize_fiscal, capture_site_block
from .grid_tariff import capture_site_grid_tariff, serialize_grid_tariff
from .price_source import capture_site_price_source, serialize_price_source
from .settings import encoded_override
from .common import (
    ERROR_NO_SITE,
    is_admin,
    lookup_charger,
    lookup_site,
    send_unsupported_version,
    site_of_charger,
)

SITE_VIEW_API_VERSION: Final = 1


def _version_ok(connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> bool:
    version = msg.get("api_version")
    if isinstance(version, bool) or version != SITE_VIEW_API_VERSION:
        send_unsupported_version(connection, msg, SITE_VIEW_API_VERSION)
        return False
    return True


def loaded_sites(hass: HomeAssistant) -> list[ConfigEntry]:
    """Every loaded site entry, by name."""
    return sorted(
        (
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE and entry.state is ConfigEntryState.LOADED
        ),
        key=lambda entry: (entry.title.casefold(), entry.entry_id),
    )


def site_list(hass: HomeAssistant) -> dict[str, Any]:
    return {
        "api_version": SITE_VIEW_API_VERSION,
        "sites": [
            {
                "site_id": entry.entry_id,
                "name": entry.title,
                "charger_ids": list(entry.data.get(CONF_CHARGER_ENTRY_IDS) or []),
            }
            for entry in loaded_sites(hass)
        ],
    }


def resolve_site(hass: HomeAssistant, msg: dict[str, Any]) -> tuple[ConfigEntry | None, str | None]:
    """The loaded site a request names: `site_id`, else the site of `charger_id`; or the stable code."""
    if "site_id" in msg:
        return lookup_site(hass, msg.get("site_id"))
    charger, code = lookup_charger(hass, msg.get("charger_id"))
    if charger is None:
        return None, code
    site = site_of_charger(hass, charger.entry_id)
    if site is None or site.state is not ConfigEntryState.LOADED:
        return None, ERROR_NO_SITE
    return site, None


def _price_block(hass: HomeAssistant, site: ConfigEntry) -> dict[str, Any] | None:
    source = site_price_source(hass, site.entry_id)
    if source is None or source.entry_id is None:
        return None
    entry = hass.config_entries.async_get_entry(source.entry_id)
    return {
        "entry_id": source.entry_id,
        "title": None if entry is None else entry.title,
        "area_id": source.area_id,
        "area_name": None if source.area is None else source.area.name,
    }


def _market(source: PriceSource) -> dict[str, Any]:
    area = source.area
    return {
        "area_id": source.area_id,
        "area_name": None if area is None else area.name,
        "countries": None if area is None else list(area.countries),
        "timezone": None if area is None else area.tz,
        "currency": None if area is None else area.currency,
        "major_unit": None if area is None else area.major_unit,
        "minor_unit": None if area is None else area.minor_unit,
    }


def price_settings_block(hass: HomeAssistant, site: ConfigEntry, *, writable: bool) -> dict[str, Any] | None:
    """The popover's price section for the site (see the module), or `None` while no price applies to it."""
    source = site_price_source(hass, site.entry_id)
    if source is None or source.entry_id is None:
        return None
    repository = domain_data(hass).price_repository
    status = None if repository is None else repository.source_status(source.area_id)
    return {
        "market": _market(source),
        "fiscal": serialize_fiscal(AutoSettings(area_id=source.area_id, overrides=(source.overrides,)), source.area),
        "fees": encoded_override(source.overrides),
        "provider": None if status is None else status.as_dict(),
        "price_source": serialize_price_source(capture_site_price_source(hass, site), can_act=writable),
        "grid_tariff": serialize_grid_tariff(capture_site_grid_tariff(hass, site), can_act=writable),
    }


def battery_found(hass: HomeAssistant, site: ConfigEntry) -> bool:
    """Whether the site's battery facts name a battery."""
    controller = site_controller_for(hass, site.entry_id)
    return controller is not None and bool(controller.battery_shadow.facts.battery_entity_id)


def site_settings_view(
    hass: HomeAssistant,
    site: ConfigEntry,
    *,
    writable: bool,
    forecast_domains: frozenset[str] = frozenset(),
    active_control_writable: bool | None = None,
    price_writable: bool | None = None,
) -> dict[str, Any]:
    """The whole answer; `forecast_domains` (`async_forecast_capable_domains`, read first) lists the solar forecast
    sources the `site` block offers. `active_control_writable` (default: as `writable`): `False` for the charger
    webhook, whose secret never switches load balancing (`site.active_control.writable`). `price_writable` (default:
    as `writable`): `False` for the site webhook, where the price and grid tariff are read-only (`can_act`)."""
    return {
        "api_version": SITE_VIEW_API_VERSION,
        "site_id": site.entry_id,
        "name": site.title,
        "charger_ids": list(site.data.get(CONF_CHARGER_ENTRY_IDS) or []),
        "writable": writable,
        "price": _price_block(hass, site),
        "battery": {"found": battery_found(hass, site)},
        "setup": serialize_setup(capture_site_setup(hass, site)),
        "price_settings": price_settings_block(
            hass, site, writable=writable if price_writable is None else price_writable
        ),
        "site": capture_site_block(
            hass,
            site,
            forecast_domains=forecast_domains,
            can_act=writable,
            active_control_writable=active_control_writable,
        ),
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/list_sites",
        vol.Optional("api_version"): object,
    }
)
@callback
def websocket_list_sites(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Every loaded site. Any signed-in user may read."""
    if _version_ok(connection, msg):
        connection.send_result(msg["id"], site_list(hass))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_site_settings_view",
        vol.Optional("api_version"): object,
        vol.Optional("site_id"): object,
        vol.Optional("charger_id"): object,
    }
)
@websocket_api.async_response
async def websocket_get_site_settings_view(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The popover's site block. Any signed-in user may read; `writable` says whether this one may change it."""
    if not _version_ok(connection, msg):
        return
    site, code = resolve_site(hass, msg)
    if site is None:
        connection.send_error(msg["id"], code or ERROR_NO_SITE, "No such site")
        return
    forecast_domains = await async_forecast_capable_domains(hass)
    connection.send_result(
        msg["id"], site_settings_view(hass, site, writable=is_admin(connection), forecast_domains=forecast_domains)
    )


@callback
def async_setup_site_view_api(hass: HomeAssistant) -> None:
    """Register both reads once for the domain."""
    websocket_api.async_register_command(hass, websocket_list_sites)
    websocket_api.async_register_command(hass, websocket_get_site_settings_view)
