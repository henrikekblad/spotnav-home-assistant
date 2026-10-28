"""Where a charger's electricity price lives, as the card shows and changes it.

The dashboard's optional `price_source` block (`serialize_price_source`), absent while the charger prices with its
own settings:

    {entry_id, title, charger_count, choices, writable, duplicate}

* `entry_id`/`title`: the electricity price entry the charger uses (`pricing/price_source.py`);
* `charger_count`: how many chargers use that entry;
* `choices`: `[{entry_id, title}]` of every electricity price entry, only when there is more than one;
* `writable`: whether this caller may change it (an administrator);
* `duplicate`: `{area_id, entry_ids, titles}` while a recorded set of same-area entries with different fees that
  holds this charger's entry is unanswered (`pricing/price_store.py`), for an administrator only; else `null`.

Two WebSocket commands, administrators only, each answering `{"ok": true}` or a stable error code:

* `spotnav/set_price_source {entry_id, price_entry_id}`: the charger (`entry_id`) uses that price entry. A charger
  on a site points its site at it, so it applies to every charger there; one without a site points at it itself.
* `spotnav/answer_price_duplicate {area_id, keep_entry_id}`: the card's question answered. With an entry, every
  site and charger using one of the others in the record moves to it (the others stay, removable under Devices &
  services); with `null` both are kept. Either way the question is not asked again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant

from ..pricing.price_setup import choose_price_entry, use_price_entry
from ..pricing.price_source import entry_source_for, price_entries
from ..pricing.price_store import price_duplicates
from ..runtime import domain_data
from ..vehicles.charger_inventory import charger_entries
from .common import ERROR_NOT_ADMIN, is_admin, lookup_charger

ERROR_UNKNOWN_PRICE_ENTRY: Final = "spotnav_unknown_price_entry"
ERROR_UNKNOWN_PRICE_DUPLICATE: Final = "spotnav_unknown_price_duplicate"


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
    for item in price_duplicates(hass):
        if entry_id in item["entry_ids"]:
            ids = tuple(item["entry_ids"])
            duplicate = (item["area_id"], ids, tuple(_title(hass, found) for found in ids))
            break
    return CapturedPriceSource(
        entry_id=entry_id,
        title=_title(hass, entry_id),
        charger_count=count,
        choices=choices,
        duplicate=duplicate,
    )


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
        else {"area_id": duplicate[0], "entry_ids": list(duplicate[1]), "titles": list(duplicate[2])},
    }


def _price_entry_ids(hass: HomeAssistant) -> set[str]:
    return {entry.entry_id for entry in price_entries(hass)}


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/set_price_source",
        vol.Required("entry_id"): str,
        vol.Required("price_entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_set_price_source(
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
    if msg["price_entry_id"] not in _price_entry_ids(hass):
        connection.send_error(msg["id"], ERROR_UNKNOWN_PRICE_ENTRY, "Not an electricity price")
        return
    choose_price_entry(hass, charger.entry_id, msg["price_entry_id"])
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
    area_id, keep = msg["area_id"], msg["keep_entry_id"]
    record = next(
        (item for item in price_duplicates(hass, unanswered_only=False) if item["area_id"] == area_id), None
    )
    store = domain_data(hass).price_store
    if record is None or store is None:
        connection.send_error(msg["id"], ERROR_UNKNOWN_PRICE_DUPLICATE, "No such question")
        return
    if keep is not None:
        if keep not in record["entry_ids"] or keep not in _price_entry_ids(hass):
            connection.send_error(msg["id"], ERROR_UNKNOWN_PRICE_ENTRY, "Not one of the electricity prices asked about")
            return
        use_price_entry(hass, keep, record["entry_ids"])
    await store.async_answer_duplicates(area_id)
    connection.send_result(msg["id"], {"ok": True})


@callback
def async_setup_price_source_api(hass: HomeAssistant) -> None:
    """Register both commands once for the domain."""
    websocket_api.async_register_command(hass, websocket_set_price_source)
    websocket_api.async_register_command(hass, websocket_answer_price_duplicate)
