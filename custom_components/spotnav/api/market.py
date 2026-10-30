"""The market editor's read: which areas there are, and what is suggested for each.

`spotnav/get_market_options` answers from the area list bundled with the integration (`pricing/areas.json`,
`pricing/area_catalogue.py`; parsed `AreaEntry` objects) plus the charger's own stored area, so a person's choice
never disappears merely because the list has moved on. It writes nothing and asks nothing of the network.
Deliberately absent: any other list (a bundled file that could not be read is an honest non-ready answer); a
second contract (the settings record is the only authority on what a charger chose); and any conversion (VAT is a
percent, tax and transfer the minor currency unit, and currency and both display units travel as separate facts).
"""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant

from ..pricing.price_repository import CatalogueSnapshot
from ..pricing.price_source import price_source_for
from ..pricing.relay_contract import AreaEntry
from ..runtime import domain_data
from .common import send_unsupported_version
from .dashboard import DashboardFailure, resolve_charger_request
from .settings import included_components


_LOGGER = logging.getLogger(__name__)

#: Contract version, independent of the dashboard's, the settings' and the action's.
MARKET_API_VERSION: Final = 1

#: What a non-ready catalogue is reported as instead of a repository failure code (kept for the contract; the
#: bundled list has no attempt that could fail).
REASON_OFFLINE: Final = "offline"
REASON_INVALID: Final = "invalid"

#: Transport failures; everything else is a rejection of what was read.
OFFLINE_CODES: Final = ("timeout", "network", "http_status", "too_large")


def _reason_for(snapshot: CatalogueSnapshot) -> str | None:
    """The bounded public reason a non-ready catalogue carries, or `None` when nothing failed."""
    error = snapshot.attempt_error
    if error is None:
        return None
    return REASON_OFFLINE if error in OFFLINE_CODES else REASON_INVALID


def _suggestions(entry: AreaEntry) -> dict[str, Any]:
    """The list's suggestions for one area, as three independent nullable figures.

    `None` is "nothing published" and a literal `0.0` is a published zero; the relay keeps those apart
    and so does this. VAT is a percent; tax and transfer are in the currency's minor unit. Nothing is
    converted, derived or defaulted.
    """
    return {
        "vat_percent": entry.vat_percent,
        "tax_minor": entry.suggested_tax,
        "transfer_minor": entry.suggested_grid_fee,
    }


def _area(entry: AreaEntry) -> dict[str, Any]:
    """One published area: name, place, time zone and how its money is named.

    Three money facts, never collapsed: `currency` is the ISO identity, `major_unit` and `minor_unit`
    are the relay's display labels (`kr` is SEK, NOK and DKK alike, so a label is not an identity). The
    EIC is deliberately absent.
    """
    return {
        "area_id": entry.id,
        "name": entry.name,
        "countries": list(entry.countries),
        "timezone": entry.tz,
        "currency": entry.currency,
        "major_unit": entry.major_unit,
        "minor_unit": entry.minor_unit,
        "suggestions": _suggestions(entry),
        # Contract v2: the market calendar, the fiscal components the price already includes (in the
        # settings' own names, locked in the editor), and the attribution: always `null`, because the bundled list
        # states only a placeholder; where the prices come from is the area's provider (the dashboard's
        # `prices.provider`).
        "market_timezone": entry.market_tz,
        "included": list(included_components(entry)),
        "source": None,
    }


def market_options_payload(
    snapshot: CatalogueSnapshot | None, configured_area: str | None
) -> dict[str, Any]:
    """The one success shape, from the held catalogue and the charger's stored choice.

    `areas` keeps the list's order and is empty when nothing is held. `configured_area` is the stored choice,
    returned whether or not the catalogue still lists it, so a person's area does not vanish; the card can say "no longer listed" without this command inventing
    a name, currency or suggestion.
    """
    catalogue = None if snapshot is None else snapshot.catalogue
    return {
        "api_version": MARKET_API_VERSION,
        "state": "unavailable" if snapshot is None else snapshot.state,
        "reason": None if snapshot is None else _reason_for(snapshot),
        "areas": [] if catalogue is None else [_area(entry) for entry in catalogue.areas],
        "configured_area": configured_area,
    }


async def async_market_options(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """One charger's market choices, from the bundled area list. No timer, listener or download is created."""
    source = price_source_for(hass, entry.entry_id)
    configured_area = None if source is None else source.area_id
    manager = domain_data(hass).price_refresh
    snapshot = None if manager is None else manager.catalogue_snapshot()
    if snapshot is None:
        repository = domain_data(hass).price_repository
        if repository is not None:
            snapshot = repository.catalogue_snapshot()
    return market_options_payload(snapshot, configured_area)


@websocket_api.websocket_command(
    {
        "type": "spotnav/get_market_options",
        # Version is judged in the handler so a wrong or missing one gets this contract's stable
        # code, not voluptuous' generic error.
        vol.Optional("api_version"): object,
        "charger_id": str,
    }
)
@websocket_api.async_response
async def websocket_get_market_options(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Read the catalogue's area choices and their suggestions. Every authenticated user may read.

    Charger scope resolves by the same rules as the dashboard and settings commands.
    """
    version = msg.get("api_version")
    # The version type is judged exactly: JSON `1.0` and `True` compare equal to `1` but are not
    # versions this contract speaks; only an integer equal to the supported version passes.
    if type(version) is not int or version != MARKET_API_VERSION:
        send_unsupported_version(connection, msg, MARKET_API_VERSION)
        return
    charger = resolve_charger_request(hass, msg)
    if isinstance(charger, DashboardFailure):
        connection.send_error(msg["id"], charger.code, "That charger is not available")
        return
    connection.send_result(msg["id"], await async_market_options(hass, charger))


@callback
def async_setup_market_api(hass: HomeAssistant) -> None:
    """Register the market read once for the domain, not once per config entry."""
    websocket_api.async_register_command(hass, websocket_get_market_options)
