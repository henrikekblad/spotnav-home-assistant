"""`get_site_access` on the charger webhook (SpotNav 3.0): the charger's site, with the site webhook's secret.

An app paired before 3.0 holds charger webhooks only. 3.0 asks each of them once, on start, and moves to the site
webhook (`api/site_entry_webhook.py`) without a new pairing. The charger webhook already answers that site's house,
heat and battery actions, so its holder is already trusted for the site: handing over the site's own door grants
nothing new. A charger only ever yields its own site.

Request: `{version: 1, action: "get_site_access", api_version?: 1}`. Answers:

* 200 `{ok: true, action, api_version: 1, site: {id, name, webhook, chargers: [{id, name}]}}`: the same shape as one
  element of the pairing hand-over's `sites` (`vehicles/charger_inventory.py:site_pairing_entry`).
* 400 `spotnav_no_site`: the charger is in no site; the app keeps using the charger webhook.
* 503 `spotnav_site_unloaded`: its site exists but is not loaded (starting, failed to set up): ask again later.
* 400 `spotnav_unsupported_api_version`.
* 429 `spotnav_rate_limited` with `retry_after_s` and `Retry-After`: more than `ACCESS_LIMIT` asks per charger in
  `ACCESS_WINDOW_S` (the app asks once per start).

The secret is never logged. "Disconnect all phones" (`api/phones.py`) replaces it, so an id handed over here stops
answering with every other.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Final

from aiohttp import web
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from ..const import CONF_WEBHOOK_ID
from ..runtime import domain_data
from ..vehicles.charger_inventory import site_pairing_entry
from .common import ERROR_NO_SITE, ERROR_UNSUPPORTED_VERSION, site_of_charger

ACTION: Final = "get_site_access"
SITE_ACCESS_API_VERSION: Final = 1
ERROR_RATE_LIMITED: Final = "spotnav_rate_limited"
#: As the site webhook's (`api/site_entry_webhook.py`): the charger's home exists but is not loaded.
ERROR_SITE_UNLOADED: Final = "spotnav_site_unloaded"
#: At most this many asks per charger in `ACCESS_WINDOW_S`.
ACCESS_LIMIT: Final = 6
ACCESS_WINDOW_S: Final = 60.0


def _retry_after_s(hass: HomeAssistant, charger_id: str, now: float) -> float:
    """`0` when this ask may go ahead (and it is counted), else how long to wait."""
    times = domain_data(hass).site_access_asks.setdefault(charger_id, deque())
    while times and times[0] <= now - ACCESS_WINDOW_S:
        times.popleft()
    if len(times) >= ACCESS_LIMIT:
        return times[0] + ACCESS_WINDOW_S - now
    times.append(now)
    return 0.0


async def webhook_get_site_access(hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]) -> web.Response:
    """The charger webhook's `get_site_access` (see the module)."""
    version = payload.get("api_version", SITE_ACCESS_API_VERSION)
    if type(version) is not int or version != SITE_ACCESS_API_VERSION:
        return web.json_response({"ok": False, "error": ERROR_UNSUPPORTED_VERSION, "action": ACTION}, status=400)
    wait_s = _retry_after_s(hass, entry.entry_id, time.monotonic())
    if wait_s > 0:
        retry_after_s = max(1, math.ceil(wait_s))
        return web.json_response(
            {"ok": False, "error": ERROR_RATE_LIMITED, "action": ACTION, "retry_after_s": retry_after_s},
            status=429,
            headers={"Retry-After": str(retry_after_s)},
        )
    site = site_of_charger(hass, entry.entry_id)
    if site is None or not site.data.get(CONF_WEBHOOK_ID):
        return web.json_response({"ok": False, "error": ERROR_NO_SITE, "action": ACTION}, status=400)
    if site.state is not ConfigEntryState.LOADED:
        # The home exists: never "no home" while it starts or fails to set up.
        return web.json_response({"ok": False, "error": ERROR_SITE_UNLOADED, "action": ACTION}, status=503)
    return web.json_response(
        {"ok": True, "action": ACTION, "api_version": SITE_ACCESS_API_VERSION, "site": site_pairing_entry(hass, site)}
    )
