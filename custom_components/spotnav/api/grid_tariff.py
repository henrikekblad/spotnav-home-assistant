"""The grid tariff beyond the fixed transfer fee, as the card and the app show and change it.

The tariff is the electricity price entry's (`planning/auto_settings.py:GridTariff`): a grid fee that is a
percentage of the spot price (`transfer_percent`), a grid fee inside a high-load window that replaces the transfer
fee there (`transfer_high`), and that window. Every charger pricing with the entry follows it.

The dashboard's optional `grid_tariff` block (`serialize_grid_tariff`), present exactly where `price_source` is
(the charger prices with an electricity price entry):

    {entry_id, write_revision, transfer_percent, transfer_high, high_load_window, writable}

* `transfer_percent`/`transfer_high`: `{policy, explicit_value, effective_value, unit}`. `policy` is `off`,
  `manual` or `included` (the area's published price already holds the grid fee: locked like `transfer`, nothing
  added, whatever is stored). `explicit_value` is what is stored, `effective_value` what is added (`null` while
  off or included). `unit` is `%` for the percentage and the area's minor unit per kWh for the high-load fee.
* `high_load_window`: `{weekdays_only, from_hour, to_hour, winter_only}` or `null`. Whole hours of the area's own
  clock, `from_hour` 0-23 and `to_hour` 1-24 after it; `winter_only` is November to March.
* `write_revision`: the price entry's write counter, which a write names as `expected_write_revision`.
* `writable`: whether this caller may change it (an administrator; the paired app).

One write, a full replacement of the three parts, compare-and-set against the entry's write counter:

* WebSocket `spotnav/update_grid_tariff {charger_id, expected_write_revision, grid_tariff}`, administrators only,
  answering `{ok: true, grid_tariff: <block>}` or a stable error code;
* the webhook action `update_grid_tariff {expected_write_revision, grid_tariff}` for its own charger, answering
  `{ok, error, field_errors, grid_tariff}` with the block re-read now.

`grid_tariff` is `{transfer_percent: {enabled, value}, transfer_high: {enabled, value}, high_load_window}`. A fee
that is on needs its figure (no area suggests one); the high-load fee needs a window. A tariff equal to the stored
one (all off equals none stored) writes nothing and leaves the write counter. Refusals: `invalid_fiscal`
(the body), `revision_conflict` (the entry was written since), `spotnav_no_price_entry` (the charger prices with
its own settings; the block is absent then), and the charger lookup's codes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Final, Mapping

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant

from ..planning.auto_settings import (
    AutoSettingsError,
    FiscalOverride,
    GridTariff,
    high_load_window_dict,
    high_load_window_from,
)
from ..pricing.price_source import async_write_price_entry, entry_source_for, PriceSource
from .common import ERROR_NOT_ADMIN, is_admin, lookup_charger
from .entity_fields import ERR_INVALID_VALUE

#: The charger prices with its own settings: there is no electricity price entry to hold a grid tariff.
ERROR_NO_PRICE_ENTRY: Final = "spotnav_no_price_entry"

#: The body's keys, all required.
GRID_TARIFF_KEYS: Final = frozenset({"transfer_percent", "transfer_high", "high_load_window"})
FEE_KEYS: Final = frozenset({"enabled", "value"})

POLICY_OFF: Final = "off"
POLICY_MANUAL: Final = "manual"
POLICY_INCLUDED: Final = "included"


@dataclass(frozen=True, slots=True)
class CapturedGridTariff:
    """One charger's price entry's grid tariff, read once."""

    entry_id: str
    write_revision: int
    tariff: GridTariff
    #: The area's published price already includes the grid fee.
    included: bool
    #: The area's minor unit, `None` while the area is not listed.
    minor_unit: str | None


def capture_grid_tariff(hass: HomeAssistant, charger_id: str) -> CapturedGridTariff | None:
    """The charger's price entry's grid tariff, or `None` while it prices with its own settings."""
    source = entry_source_for(hass, charger_id)
    if source is None or source.entry_id is None:
        return None
    return _captured(source)


def _captured(source: PriceSource) -> CapturedGridTariff:
    assert source.entry_id is not None
    area = source.area
    return CapturedGridTariff(
        entry_id=source.entry_id,
        write_revision=source.write_revision,
        tariff=source.overrides.grid_tariff or GridTariff(),
        included=area is not None and area.includes("grid_fee"),
        minor_unit=None if area is None or not area.minor_unit else area.minor_unit,
    )


def _fee(fee: FiscalOverride, unit: str | None, included: bool) -> dict[str, Any]:
    if included:
        policy = POLICY_INCLUDED
    else:
        policy = POLICY_MANUAL if fee.enabled else POLICY_OFF
    return {
        "policy": policy,
        "explicit_value": fee.value,
        "effective_value": fee.value if policy == POLICY_MANUAL else None,
        "unit": unit,
    }


def serialize_grid_tariff(grid_tariff: CapturedGridTariff | None, *, can_act: bool) -> dict[str, Any] | None:
    """The dashboard's `grid_tariff` block (see the module), or `None` for none."""
    if grid_tariff is None:
        return None
    captured = grid_tariff
    tariff = captured.tariff
    window = tariff.high_load_window
    return {
        "entry_id": captured.entry_id,
        "write_revision": captured.write_revision,
        "transfer_percent": _fee(tariff.transfer_percent, "%", captured.included),
        "transfer_high": _fee(
            tariff.transfer_high,
            None if captured.minor_unit is None else f"{captured.minor_unit}/kWh",
            captured.included,
        ),
        "high_load_window": None if window is None else high_load_window_dict(window),
        "writable": bool(can_act),
    }


class GridTariffRefusal(Exception):
    """A stable-code refusal of a write; nothing was written."""

    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        #: The request field that is not of its type, else `None`.
        self.field = field


def _invalid(message: str, field: str = "grid_tariff") -> GridTariffRefusal:
    return GridTariffRefusal("invalid_fiscal", message, field)


def _decode_fee(raw: Any, name: str) -> FiscalOverride:
    if not isinstance(raw, Mapping) or set(raw) != FEE_KEYS:
        raise _invalid(f"{name} must be exactly {{enabled, value}}", name)
    enabled, value = raw["enabled"], raw["value"]
    if not isinstance(enabled, bool):
        raise _invalid(f"{name}.enabled must be a boolean", name)
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise _invalid(f"{name}.value must be a number or null", name)
    try:
        return FiscalOverride(enabled=enabled, value=None if value is None else float(value)).validated(name)
    except AutoSettingsError as err:
        raise _invalid(str(err), name) from err


def decode_grid_tariff(raw: Any) -> GridTariff:
    """A write's `grid_tariff`: exactly its three parts, validated, or `invalid_fiscal`."""
    if not isinstance(raw, Mapping) or set(raw) != GRID_TARIFF_KEYS:
        raise _invalid(f"grid_tariff must be exactly {sorted(GRID_TARIFF_KEYS)}")
    window = None
    if raw["high_load_window"] is not None:
        try:
            window = high_load_window_from(raw["high_load_window"])
        except AutoSettingsError as err:
            raise _invalid(str(err), "high_load_window") from err
    tariff = GridTariff(
        transfer_percent=_decode_fee(raw["transfer_percent"], "transfer_percent"),
        transfer_high=_decode_fee(raw["transfer_high"], "transfer_high"),
        high_load_window=window,
    )
    try:
        return tariff.validated()
    except AutoSettingsError as err:
        raise _invalid(str(err)) from err


def _expected(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise GridTariffRefusal(
            "invalid_number", "expected_write_revision must be a non-negative whole number", "expected_write_revision"
        )
    return raw


def update_grid_tariff(hass: HomeAssistant, charger_id: str, expected_write_revision: Any, body: Any) -> None:
    """Store the grid tariff on the charger's electricity price entry, in place, compare-and-set (see the module).

    Every charger pricing with the entry replans (its revision moves). A tariff equal to the stored one (all off
    equals none stored) passes the compare-and-set and writes nothing: the write counter stays.
    """
    expected = _expected(expected_write_revision)
    tariff = decode_grid_tariff(body)
    source = entry_source_for(hass, charger_id)
    if source is None or source.entry_id is None:
        raise GridTariffRefusal(ERROR_NO_PRICE_ENTRY, "This charger has no electricity price entry")
    entry = hass.config_entries.async_get_entry(source.entry_id)
    if entry is None:
        raise GridTariffRefusal(ERROR_NO_PRICE_ENTRY, "The electricity price entry is gone")
    if expected != source.write_revision:
        raise GridTariffRefusal(
            "revision_conflict", f"the electricity price is at write {source.write_revision}, not {expected}"
        )
    if tariff == (source.overrides.grid_tariff or GridTariff()):
        # The tariff as stored (nothing stored reads as all off): nothing to write, and no replan.
        return
    try:
        async_write_price_entry(
            hass, entry, replace(source.overrides, grid_tariff=tariff), expected_write_revision=expected
        )
    except AutoSettingsError as err:
        raise GridTariffRefusal(err.code, str(err)) from err


async def async_webhook_update_grid_tariff(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """`update_grid_tariff {expected_write_revision, grid_tariff}` over the webhook, for its own charger. Returns
    `(http_status, body)`: 200, 400 for a bad body or no price entry, 409 for a conflict."""
    refusal: GridTariffRefusal | None = None
    try:
        update_grid_tariff(hass, entry.entry_id, payload.get("expected_write_revision"), payload.get("grid_tariff"))
    except GridTariffRefusal as refused:
        refusal = refused
    body = {
        "ok": refusal is None,
        "error": None if refusal is None else refusal.code,
        "field_errors": []
        if refusal is None or refusal.field is None
        else [{"field": refusal.field, "code": ERR_INVALID_VALUE}],
        "grid_tariff": serialize_grid_tariff(capture_grid_tariff(hass, entry.entry_id), can_act=True),
    }
    if refusal is None:
        return 200, body
    return (409 if refusal.code == "revision_conflict" else 400), body


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/update_grid_tariff",
        vol.Required("charger_id"): str,
        vol.Required("expected_write_revision"): object,
        vol.Required("grid_tariff"): object,
    }
)
@websocket_api.async_response
async def websocket_update_grid_tariff(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The grid tariff of the charger's electricity price entry, replaced (see the module)."""
    if not is_admin(connection):
        connection.send_error(msg["id"], ERROR_NOT_ADMIN, "Only administrators can change the electricity price")
        return
    charger, failure = lookup_charger(hass, msg["charger_id"], require_loaded=False)
    if failure is not None or charger is None:
        connection.send_error(msg["id"], failure or ERROR_NOT_ADMIN, "Not a charger")
        return
    try:
        update_grid_tariff(hass, charger.entry_id, msg["expected_write_revision"], msg["grid_tariff"])
    except GridTariffRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(
        msg["id"],
        {"ok": True, "grid_tariff": serialize_grid_tariff(capture_grid_tariff(hass, charger.entry_id), can_act=True)},
    )


@callback
def async_setup_grid_tariff_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_update_grid_tariff)
