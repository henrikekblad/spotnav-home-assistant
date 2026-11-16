"""The area and fees of the electricity price a site reads, written by the site: `spotnav/update_price`.

A charger's card writes the area and the three fees through the charger's settings (`spotnav/update_settings`,
routed to the price entry in place). A home without a charger has no settings record, so the shared settings
popover writes the site's price entry (`site_price_source`) directly, through the one writer every other route uses
(`pricing/price_source.async_write_price_entry`): in place, no reload, counted, every charger on the entry replans.

    {"type": "spotnav/update_price", "site_id", "expected_write_revision", "price": {"area_id", "vat", "tax",
     "transfer"}}

`price` is one area's override row exactly as the settings body states it (`{enabled, value}` per fee). The area is
one the bundled area list names, or the one stored. The grid tariff and the supplier's figures stay as stored (an
area change turns the figures in the old area's unit off, keeping them; see `async_write_price_entry`). Administrators
only; answers `{"ok": true}` or the stable error codes `spotnav_not_admin`, `spotnav_unknown_site`,
`spotnav_no_price_entry` (no electricity price applies to the site), `revision_conflict`, `invalid_number` (the
revision), `invalid_area`, `invalid_fiscal`, `missing_field` and `unknown_field`. The app's webhook has no twin: the
app writes through its charger's settings.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant

from ..planning.auto_settings import AutoSettingsError
from ..pricing.price_source import async_write_price_entry, site_price_source
from ..runtime import domain_data
from .common import ERROR_NOT_ADMIN, is_admin, lookup_site
from .grid_tariff import ERROR_NO_PRICE_ENTRY
from .settings import decode_override


class SitePriceRefusal(Exception):
    """A stable-code refusal; nothing was written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _area_listed(hass: HomeAssistant, area_id: str) -> bool:
    repository = domain_data(hass).price_repository
    return repository is not None and repository.catalogue_snapshot().area(area_id) is not None


def update_site_price(hass: HomeAssistant, site: ConfigEntry, expected_write_revision: Any, raw: Any) -> None:
    """Write the area and fees of the site's electricity price, compare-and-set (see the module)."""
    if isinstance(expected_write_revision, bool) or not isinstance(expected_write_revision, int) or expected_write_revision < 0:
        raise SitePriceRefusal("invalid_number", "expected_write_revision must be a non-negative whole number")
    try:
        row = decode_override(raw)
    except AutoSettingsError as err:
        raise SitePriceRefusal(err.code, str(err)) from err
    source = site_price_source(hass, site.entry_id)
    entry = None if source is None or source.entry_id is None else hass.config_entries.async_get_entry(source.entry_id)
    if source is None or entry is None:
        raise SitePriceRefusal(ERROR_NO_PRICE_ENTRY, "No electricity price applies to this site")
    if not row.area_id or (row.area_id != source.area_id and not _area_listed(hass, row.area_id)):
        raise SitePriceRefusal("invalid_area", f"{row.area_id!r} is not a listed area")
    try:
        async_write_price_entry(hass, entry, row, expected_write_revision=expected_write_revision)
    except AutoSettingsError as err:
        raise SitePriceRefusal(err.code, str(err)) from err


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/update_price",
        vol.Required("site_id"): str,
        vol.Required("expected_write_revision"): object,
        vol.Required("price"): object,
    }
)
@websocket_api.async_response
async def websocket_update_price(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The site's electricity price's area and fees, replaced (see the module)."""
    if not is_admin(connection):
        connection.send_error(msg["id"], ERROR_NOT_ADMIN, "Only administrators can change the electricity price")
        return
    site, code = lookup_site(hass, msg["site_id"], require_loaded=False)
    if site is None:
        connection.send_error(msg["id"], code or ERROR_NO_PRICE_ENTRY, "Not a site")
        return
    try:
        update_site_price(hass, site, msg["expected_write_revision"], msg["price"])
    except SitePriceRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], {"ok": True})


@callback
def async_setup_site_price_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_update_price)
