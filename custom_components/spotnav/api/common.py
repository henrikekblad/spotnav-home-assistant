"""What every wire contract shares: stable refusal codes, who may write, and how a charger id
resolves to a loaded charger entry. One definition each, so no contract answers the same bad
request differently.
"""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from ..const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE


#: Stable WebSocket error codes. Consumers switch on these, never on a message.
ERROR_UNSUPPORTED_VERSION: Final = "spotnav_unsupported_api_version"
ERROR_CHARGER_REQUIRED: Final = "spotnav_charger_required"
ERROR_UNKNOWN_CHARGER: Final = "spotnav_unknown_charger"
ERROR_SITE_NOT_CHARGER: Final = "spotnav_site_not_charger"
ERROR_CHARGER_UNLOADED: Final = "spotnav_charger_unloaded"
ERROR_NOT_ADMIN: Final = "spotnav_not_admin"


def unsupported_version_text(speaks: int) -> str:
    """The one sentence every contract answers a request for another version with."""
    return f"This integration speaks api_version {speaks}"


def send_unsupported_version(
    connection: websocket_api.ActiveConnection, msg: dict[str, Any], speaks: int
) -> None:
    """Refuse a WebSocket request that names a version this contract does not speak."""
    connection.send_error(msg["id"], ERROR_UNSUPPORTED_VERSION, unsupported_version_text(speaks))


def is_admin(connection: websocket_api.ActiveConnection) -> bool:
    """Whether this caller is an administrative connection, read from the connection, not the request."""
    user = connection.user
    return bool(user is not None and user.is_admin)


def lookup_charger(
    hass: HomeAssistant, charger_id: Any, *, require_loaded: bool = True
) -> tuple[ConfigEntry | None, str | None]:
    """The charger entry for an id, or the stable code that says why there is none.

    Unknown ids do not disclose whether another integration owns them; a *site* is a different kind
    of entry, not a missing one; a known but unloaded charger says so (skipped with `require_loaded=False`).
    """
    if not isinstance(charger_id, str) or not charger_id:
        return None, ERROR_UNKNOWN_CHARGER
    entry = hass.config_entries.async_get_entry(charger_id)
    if entry is None or entry.domain != DOMAIN:
        return None, ERROR_UNKNOWN_CHARGER
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        return None, ERROR_SITE_NOT_CHARGER
    if require_loaded and entry.state is not ConfigEntryState.LOADED:
        return None, ERROR_CHARGER_UNLOADED
    return entry, None
