"""Where a charger's electricity price lives, as the card shows and changes it.

The dashboard's optional `price_source` block (`serialize_price_source`), absent while the charger prices with its
own settings:

    {entry_id, title, charger_count, choices, writable, duplicate}

* `entry_id`/`title`: the electricity price entry the charger uses (`pricing/price_source.py`);
* `charger_count`: how many chargers use that entry;
* `choices`: `[{entry_id, title}]` of every electricity price entry, only when there is more than one;
* `writable`: whether this caller may change it (an administrator);
* `duplicate`: `{area_id, entry_ids, titles, fees}` while a recorded set of same-area entries with different fees
  that holds this charger's entry is unanswered (`pricing/price_store.py`), for an administrator only; else `null`.
  `fees` is aligned with `entry_ids`: each entry's fees as the dashboard's `fiscal` block
  (`api/dashboard.py:serialize_fiscal`), or `null` while that entry cannot be read or its area is not listed.

Two WebSocket commands, administrators only, each answering `{"ok": true}` or a stable error code:

* `spotnav/choose_price_source {entry_id, price_entry_id}` (named `choose_`, as the card's bundle carries no `set_`
  command): the charger (`entry_id`) uses that price entry. A charger
  on a site points its site at it, so it applies to every charger there; one without a site points at it itself.
* `spotnav/answer_price_duplicate {area_id, keep_entry_id}`: the card's question answered. With an entry, every
  site and charger using one of the others in the record moves to it (the others stay, removable under Devices &
  services); with `null` both are kept. Either way the question is not asked again.

The webhook twins both for its own charger (`choose_price_source {price_entry_id}`, `answer_price_duplicate {area_id,
keep_entry_id}`), through the same cores, answering `{ok, error, field_errors, price_source}` with the charger's block
re-read now; a field not of its type is `spotnav_invalid_value` with `field_errors` `[{field, code: "invalid_value"}]`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant

from ..pricing.price_setup import choose_price_entry, use_price_entry
from ..planning.auto_settings import AutoSettings
from ..pricing.price_source import entry_source_for, price_entries, price_entry_source, PriceSource
from ..pricing.price_store import price_duplicates
from ..runtime import domain_data
from ..vehicles.charger_inventory import charger_entries
from .common import ERROR_NOT_ADMIN, is_admin, lookup_charger
from .entity_fields import ERR_INVALID_VALUE

ERROR_UNKNOWN_PRICE_ENTRY: Final = "spotnav_unknown_price_entry"
ERROR_UNKNOWN_PRICE_DUPLICATE: Final = "spotnav_unknown_price_duplicate"
#: A webhook request whose fields are not of their types (`field_errors` names them).
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"


@dataclass(frozen=True, slots=True)
class CapturedPriceSource:
    """One charger's electricity price entry and what the card may ask about it, read once."""

    entry_id: str
    title: str
    charger_count: int
    #: Every electricity price entry as `(entry_id, title)`, empty when there is only one.
    choices: tuple[tuple[str, str], ...]
    #: `(area_id, entry_ids, titles)` of the unanswered duplicates holding this entry, or `None`.
    duplicate: tuple[str, tuple[str, ...], tuple[str, ...]] | None
    #: Each duplicate entry's own area and fees, aligned with its `entry_ids` (`None` for one that cannot be read).
    duplicate_sources: tuple[PriceSource | None, ...] = ()


def _title(hass: HomeAssistant, entry_id: str) -> str:
    entry = hass.config_entries.async_get_entry(entry_id)
    return entry_id if entry is None else entry.title


def capture_price_source(hass: HomeAssistant, charger_id: str) -> CapturedPriceSource | None:
    """The charger's electricity price entry, or `None` while it prices with its own settings."""
    source = entry_source_for(hass, charger_id)
    if source is None or source.entry_id is None:
        return None
    entry_id = source.entry_id
    count = 0
    for charger in charger_entries(hass):
        used = entry_source_for(hass, charger.entry_id)
        if used is not None and used.entry_id == entry_id:
            count += 1
    entries = price_entries(hass)
    choices = (
        tuple(sorted(((entry.entry_id, entry.title) for entry in entries), key=lambda item: (item[1], item[0])))
        if len(entries) > 1
        else ()
    )
    duplicate = None
    duplicate_sources: tuple[PriceSource | None, ...] = ()
    for item in price_duplicates(hass):
        if entry_id in item["entry_ids"]:
            ids = tuple(item["entry_ids"])
            duplicate = (item["area_id"], ids, tuple(_title(hass, found) for found in ids))
            duplicate_sources = tuple(price_entry_source(hass, found) for found in ids)
            break
    return CapturedPriceSource(
        entry_id=entry_id,
        title=_title(hass, entry_id),
        charger_count=count,
        choices=choices,
        duplicate=duplicate,
        duplicate_sources=duplicate_sources,
    )


def _fees(source: PriceSource | None) -> dict[str, Any] | None:
    """One price entry's fees as the dashboard's `fiscal` block, or `None`."""
    from .dashboard import serialize_fiscal  # noqa: PLC0415 - the dashboard imports this module

    if source is None:
        return None
    return serialize_fiscal(AutoSettings(area_id=source.area_id, overrides=(source.overrides,)), source.area)


def serialize_price_source(price_source: CapturedPriceSource | None, *, can_act: bool) -> dict[str, Any] | None:
    """The dashboard's `price_source` block (see the module), or `None` for none."""
    if price_source is None:
        return None
    duplicate = price_source.duplicate if can_act else None
    return {
        "entry_id": price_source.entry_id,
        "title": price_source.title,
        "charger_count": price_source.charger_count,
        "choices": [{"entry_id": entry_id, "title": title} for entry_id, title in price_source.choices],
        "writable": bool(can_act),
        "duplicate": None
        if duplicate is None
        else {
            "area_id": duplicate[0],
            "entry_ids": list(duplicate[1]),
            "titles": list(duplicate[2]),
            "fees": [_fees(source) for source in price_source.duplicate_sources],
        },
    }


def _price_entry_ids(hass: HomeAssistant) -> set[str]:
    return {entry.entry_id for entry in price_entries(hass)}


class PriceSourceRefusal(Exception):
    """A stable-code refusal of a choice or an answer; nothing was written."""

    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        #: The request field that is not of its type (`spotnav_invalid_value`), else `None`.
        self.field = field


def choose_price_source(hass: HomeAssistant, charger_id: str, price_entry_id: str) -> None:
    """The charger uses this electricity price entry: through its site when it has one (see the module)."""
    if price_entry_id not in _price_entry_ids(hass):
        raise PriceSourceRefusal(ERROR_UNKNOWN_PRICE_ENTRY, "Not an electricity price")
    choose_price_entry(hass, charger_id, price_entry_id)


async def async_answer_price_duplicate(hass: HomeAssistant, area_id: str, keep: str | None) -> None:
    """The question for one area answered: use one of the entries, or (`None`) keep them all (see the module)."""
    record = next(
        (item for item in price_duplicates(hass, unanswered_only=False) if item["area_id"] == area_id), None
    )
    store = domain_data(hass).price_store
    if record is None or store is None:
        raise PriceSourceRefusal(ERROR_UNKNOWN_PRICE_DUPLICATE, "No such question")
    if keep is not None:
        if keep not in record["entry_ids"] or keep not in _price_entry_ids(hass):
            raise PriceSourceRefusal(ERROR_UNKNOWN_PRICE_ENTRY, "Not one of the electricity prices asked about")
        use_price_entry(hass, keep, record["entry_ids"])
    await store.async_answer_duplicates(area_id)


def _webhook_answer(
    hass: HomeAssistant, entry: ConfigEntry, refusal: PriceSourceRefusal | None = None
) -> tuple[int, dict[str, Any]]:
    """A webhook write's envelope: `ok`, the stable `error` and its `field_errors`, and this charger's
    `price_source` block re-read now (`null` while it prices with its own settings)."""
    block = serialize_price_source(capture_price_source(hass, entry.entry_id), can_act=True)
    body = {
        "ok": refusal is None,
        "error": None if refusal is None else refusal.code,
        "field_errors": []
        if refusal is None or refusal.field is None
        else [{"field": refusal.field, "code": ERR_INVALID_VALUE}],
        "price_source": block,
    }
    return (200 if refusal is None else 400), body


async def async_webhook_choose_price_source(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """`choose_price_source {price_entry_id}` over the webhook: `spotnav/choose_price_source` for the
    webhook's own charger. Returns `(http_status, body)`."""
    price_entry_id = payload.get("price_entry_id")
    try:
        if not isinstance(price_entry_id, str):
            raise PriceSourceRefusal(ERROR_INVALID_VALUE, "Not an entry id", "price_entry_id")
        choose_price_source(hass, entry.entry_id, price_entry_id)
    except PriceSourceRefusal as refusal:
        return _webhook_answer(hass, entry, refusal)
    return _webhook_answer(hass, entry)


async def async_webhook_answer_price_duplicate(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """`answer_price_duplicate {area_id, keep_entry_id}` over the webhook: `spotnav/answer_price_duplicate`,
    answered with the webhook's own charger's block. Returns `(http_status, body)`."""
    area_id = payload.get("area_id")
    keep = payload.get("keep_entry_id", ...)
    try:
        if not isinstance(area_id, str):
            raise PriceSourceRefusal(ERROR_INVALID_VALUE, "Not an area", "area_id")
        if keep is not None and not isinstance(keep, str):
            raise PriceSourceRefusal(ERROR_INVALID_VALUE, "Not an entry id or null", "keep_entry_id")
        await async_answer_price_duplicate(hass, area_id, keep)
    except PriceSourceRefusal as refusal:
        return _webhook_answer(hass, entry, refusal)
    return _webhook_answer(hass, entry)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/choose_price_source",
        vol.Required("entry_id"): str,
        vol.Required("price_entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_choose_price_source(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The charger uses this electricity price entry: through its site when it has one (see the module)."""
    if not is_admin(connection):
        connection.send_error(msg["id"], ERROR_NOT_ADMIN, "Only administrators can change the electricity price")
        return
    charger, failure = lookup_charger(hass, msg["entry_id"], require_loaded=False)
    if failure is not None or charger is None:
        connection.send_error(msg["id"], failure or ERROR_NOT_ADMIN, "Not a charger")
        return
    try:
        choose_price_source(hass, charger.entry_id, msg["price_entry_id"])
    except PriceSourceRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/answer_price_duplicate",
        vol.Required("area_id"): str,
        vol.Required("keep_entry_id"): vol.Any(str, None),
    }
)
@websocket_api.async_response
async def websocket_answer_price_duplicate(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The card's question for one area answered: use one of the entries, or keep them all (see the module)."""
    if not is_admin(connection):
        connection.send_error(msg["id"], ERROR_NOT_ADMIN, "Only administrators can change the electricity price")
        return
    try:
        await async_answer_price_duplicate(hass, msg["area_id"], msg["keep_entry_id"])
    except PriceSourceRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], {"ok": True})


@callback
def async_setup_price_source_api(hass: HomeAssistant) -> None:
    """Register both commands once for the domain."""
    websocket_api.async_register_command(hass, websocket_choose_price_source)
    websocket_api.async_register_command(hass, websocket_answer_price_duplicate)
