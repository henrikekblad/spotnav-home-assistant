"""Setting the installation's ENTSO-E key: checked with one request, then stored (`price_store.py`).

The key is the person's own security token for ENTSO-E's Transparency Platform; with it the areas Nord Pool does
not trade (and Nord Pool's while no `nordpool` entry is loaded) are read from the platform. One per installation.
Set from the card (`spotnav/set_entsoe_key`, `api/entsoe_key.py`) and from the electricity price entry's
Configure (`flows/price.py`); both come here.

Saving asks the platform once for today's CET day of the given area (or of DE-LU when that area has no route):

* accepted: stored as `ok`;
* rejected (401/403): **not stored**; a key stored before stays as it was. Error `entsoe_key_rejected`;
* the platform could not be asked (no answer, a server error, a rate limit): stored as `unchecked`, and used at
  once (the first request for a day proves or rejects it). Error `entsoe_unreachable`.

Clearing (an empty value) stores nothing and needs no request. The key is never logged, never answered back and
never in diagnostics or a payload: only whether one is set and its state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from homeassistant.core import HomeAssistant

from ..const import CONF_PRICE_AREA
from ..runtime import domain_data
from .entsoe_parse import EntsoeToken
from .price_source import price_entries
from .price_store import async_setup_price_store, EntsoeKeyState

ERROR_KEY_REJECTED: Final = "entsoe_key_rejected"
ERROR_UNREACHABLE: Final = "entsoe_unreachable"


@dataclass(frozen=True, slots=True)
class KeySaved:
    """What saving a key did: `ok` when it is stored and accepted (or cleared); `error` a stable code otherwise;
    `state` the stored key's state afterwards (`None` when none is stored)."""

    ok: bool
    error: str | None
    state: EntsoeKeyState | None

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "error": self.error, "state": self.state}


def check_area(hass: HomeAssistant, area_id: str | None = None) -> str | None:
    """The area a key is checked against: the given one, else the first electricity price entry's."""
    if area_id:
        return area_id
    for entry in price_entries(hass):
        area = entry.data.get(CONF_PRICE_AREA)
        if isinstance(area, str) and area:
            return area
    return None


async def async_save_entsoe_key(hass: HomeAssistant, value: str | None, *, area_id: str | None = None) -> KeySaved:
    """Check and store [value] as the installation's key, or clear it when it is `None` or blank (see the module)."""
    store = await async_setup_price_store(hass)
    token = EntsoeToken(value or "")
    if token.empty:
        await store.async_set_entsoe_key(None)
        return KeySaved(ok=True, error=None, state=None)
    repository = domain_data(hass).price_repository
    provider = None if repository is None else repository.entsoe_provider()
    found = "unreachable" if provider is None else await provider.async_check_key(token, check_area(hass, area_id))
    if found == "rejected":
        return KeySaved(ok=False, error=ERROR_KEY_REJECTED, state=store.entsoe_key_state)
    if found == "ok":
        await store.async_set_entsoe_key(token, "ok")
        return KeySaved(ok=True, error=None, state="ok")
    await store.async_set_entsoe_key(token, "unchecked")
    return KeySaved(ok=False, error=ERROR_UNREACHABLE, state="unchecked")
