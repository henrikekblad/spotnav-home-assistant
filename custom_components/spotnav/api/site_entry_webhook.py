"""The site webhook (SpotNav 3.0): one POST endpoint per site entry, the app's single door to a home.

Its id is the site entry's `CONF_WEBHOOK_ID` (`entry_migration.py`), as long and as random as a charger's, registered
like the charger webhook (`local_only=False`, POST only, the same envelope, the same refusals and log budget:
`api/webhook.py:async_answer_webhook`). It answers:

* **The site's own actions**, bound to this site (a `site_id` in the body is never read): `get_site` (below), the
  site actions of the charger webhook (`api/site_webhook.py`: `get_house`, `get_house_now`, `get_house_history`,
  `get_house_loads`, `get_heat`, `get_heat_history`, `get_site_settings_view`, `battery_control`, `update_heat`,
  `heat_boost`), `update_site_settings` (the site-bound path, load balancing refused), `get_power_month` and
  `suggest_import_target`. A site without a charger answers them all.
* **Every action of the charger webhook** (`api/webhook.py:ACTIONS`) for one charger of this site, named by
  `charger_id` in the body: the same answer the charger's own webhook gives. A body without `charger_id` is
  `spotnav_charger_required` (400); an id that names no SpotNav charger `spotnav_unknown_charger` (404); a charger of
  another site, or of none, `spotnav_charger_not_in_site` (403); a charger of this site that is not loaded
  `spotnav_charger_unloaded` (503). A `charger_id` beside a site action is checked the same way.

The price and the grid tariff are read-only here in the beta: `choose_price_source`, `answer_price_duplicate` and
`update_grid_tariff` without a `charger_id` are `spotnav_not_permitted_over_webhook` (403) and `get_site_settings_view`
says `can_act` false on its price blocks. The control opt-ins stay refused as on the charger webhook. There is no
`push_register` for the site itself: notifications are a charger's (with a `charger_id`, as on its webhook).

`get_site` (`api_version` 1, optional in the body):

    {ok, action, api_version: 1, site_id, name, loaded, capabilities: {get_house, get_heat, battery_control,
     get_site_settings_view, update_site_settings, get_power_month, suggest_import_target}, chargers: [{id, name}]}

`chargers` lists this site's chargers in the site's order, each addressable by its `id` as `charger_id`; a charger
added to the site later shows up here, with no new pairing. The capabilities are true while the site is loaded.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiohttp import web
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from ..const import CONF_CHARGER_ENTRY_IDS, CONF_ENTRY_TYPE, CONF_WEBHOOK_ID, DOMAIN, ENTRY_TYPE_CHARGER
from .common import (
    ERROR_CHARGER_REQUIRED,
    ERROR_CHARGER_UNLOADED,
    ERROR_NOT_PERMITTED_OVER_WEBHOOK,
    ERROR_SITE_NOT_CHARGER,
    ERROR_UNKNOWN_CHARGER,
    ERROR_UNSUPPORTED_VERSION,
    lookup_charger,
    site_of_charger,
)
from .power_month import POWER_MONTH_API_VERSION, PowerMonthRefusal, power_month_answer
from .site_settings import async_site_webhook_update_site_settings
from .site_webhook import SiteHandler, site_handlers
from .target_suggestion import SUGGEST_API_VERSION, suggestion_answer
from .webhook import Call, ACTIONS, async_answer_webhook, async_register_entry_webhook, charger_call

#: The name Home Assistant lists the site webhook by.
SITE_WEBHOOK_NAME: Final = "SpotNav home"
GET_SITE_API_VERSION: Final = 1
#: A charger of another site, or of none, named on this site's webhook.
ERROR_CHARGER_NOT_IN_SITE: Final = "spotnav_charger_not_in_site"
#: The price writes, read-only on the site webhook in the beta (allowed with a `charger_id`, as a charger's action).
PRICE_WRITES: Final = ("choose_price_source", "answer_price_duplicate", "update_grid_tariff")
#: `get_site.capabilities`, in order.
GET_SITE_CAPABILITIES: Final = (
    "get_house",
    "get_heat",
    "battery_control",
    "get_site_settings_view",
    "update_site_settings",
    "get_power_month",
    "suggest_import_target",
)


def _refused(action: str, code: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": code, "action": action}, status=status)


def _speaks(payload: dict[str, Any], version: int) -> bool:
    asked = payload.get("api_version", version)
    return type(asked) is int and asked == version


def site_chargers(hass: HomeAssistant, site: ConfigEntry) -> list[ConfigEntry]:
    """The charger entries this site lists, in the site's order; an id that names no charger (any more) is left out."""
    chargers = []
    for charger_id in site.data.get(CONF_CHARGER_ENTRY_IDS) or []:
        entry = hass.config_entries.async_get_entry(charger_id)
        if entry is not None and entry.domain == DOMAIN and entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_CHARGER) == ENTRY_TYPE_CHARGER:
            chargers.append(entry)
    return chargers


def get_site_answer(hass: HomeAssistant, site: ConfigEntry) -> dict[str, Any]:
    """`get_site`'s answer without the routing keys."""
    loaded = site.state is ConfigEntryState.LOADED
    return {
        "api_version": GET_SITE_API_VERSION,
        "site_id": site.entry_id,
        "name": site.title,
        "loaded": loaded,
        "capabilities": {name: loaded for name in GET_SITE_CAPABILITIES},
        "chargers": [{"id": charger.entry_id, "name": charger.title} for charger in site_chargers(hass, site)],
    }


async def _get_site(hass: HomeAssistant, site: ConfigEntry, payload: dict[str, Any]) -> web.Response:
    action = "get_site"
    if not _speaks(payload, GET_SITE_API_VERSION):
        return _refused(action, ERROR_UNSUPPORTED_VERSION, 400)
    return web.json_response({"ok": True, "action": action, **get_site_answer(hass, site)})


async def _update_site_settings(hass: HomeAssistant, site: ConfigEntry, payload: dict[str, Any]) -> web.Response:
    status, envelope = await async_site_webhook_update_site_settings(hass, site, payload)
    return web.json_response({**envelope, "action": "update_site_settings"}, status=status)


def _site_read(action: str, version: int, read: Callable[[HomeAssistant, ConfigEntry, dict[str, Any]], Awaitable[dict[str, Any]]]):
    async def handler(hass: HomeAssistant, site: ConfigEntry, payload: dict[str, Any]) -> web.Response:
        if not _speaks(payload, version):
            return _refused(action, ERROR_UNSUPPORTED_VERSION, 400)
        try:
            answer = await read(hass, site, payload)
        except PowerMonthRefusal as refusal:
            return _refused(action, refusal.code, 400)
        return web.json_response({"ok": True, "action": action, **answer})

    return handler


async def _power_month(hass: HomeAssistant, site: ConfigEntry, payload: dict[str, Any]) -> dict[str, Any]:
    return power_month_answer(hass, site, payload.get("month"))


async def _suggestion(hass: HomeAssistant, site: ConfigEntry, payload: dict[str, Any]) -> dict[str, Any]:
    return await suggestion_answer(hass, site)


type _SiteAction = Callable[[HomeAssistant, ConfigEntry, dict[str, Any]], Awaitable[web.Response]]

#: The site's own actions on its webhook, bound to the site.
SITE_ENTRY_ACTIONS: Final[dict[str, SiteHandler | _SiteAction]] = {
    "get_site": _get_site,
    **site_handlers(price_writable=False),
    "update_site_settings": _update_site_settings,
    "get_power_month": _site_read("get_power_month", POWER_MONTH_API_VERSION, _power_month),
    "suggest_import_target": _site_read("suggest_import_target", SUGGEST_API_VERSION, _suggestion),
}


def _charger_of_site(
    hass: HomeAssistant, site: ConfigEntry, action: str, charger_id: Any
) -> tuple[ConfigEntry | None, web.Response | None]:
    """The loaded charger of this site a body names, or the refusal to answer with."""
    charger, code = lookup_charger(hass, charger_id, require_loaded=False)
    if charger is None:
        if code == ERROR_SITE_NOT_CHARGER:
            # A site's id is no charger of this one.
            return None, _refused(action, ERROR_CHARGER_NOT_IN_SITE, 403)
        return None, _refused(action, code or ERROR_UNKNOWN_CHARGER, 404)
    owner = site_of_charger(hass, charger.entry_id)
    if owner is None or owner.entry_id != site.entry_id:
        return None, _refused(action, ERROR_CHARGER_NOT_IN_SITE, 403)
    if charger.state is not ConfigEntryState.LOADED:
        return None, _refused(action, ERROR_CHARGER_UNLOADED, 503)
    return charger, None


def site_call(hass: HomeAssistant, site: ConfigEntry, action: str, payload: dict[str, Any]) -> Call | None:
    """The site webhook's binding of one action (see the module), `None` for one it does not answer."""
    site_action = SITE_ENTRY_ACTIONS.get(action)
    charger_action = ACTIONS.get(action)
    if site_action is None and charger_action is None:
        return None
    named = "charger_id" in payload
    charger: ConfigEntry | None = None
    if named:
        charger, refusal = _charger_of_site(hass, site, action, payload.get("charger_id"))
        if refusal is not None:
            return lambda: _answered(refusal)
    if site_action is not None:
        return lambda: site_action(hass, site, payload)
    if charger is None:
        if action in PRICE_WRITES:
            return lambda: _answered(_refused(action, ERROR_NOT_PERMITTED_OVER_WEBHOOK, 403))
        return lambda: _answered(_refused(action, ERROR_CHARGER_REQUIRED, 400))
    return charger_call(hass, charger, action, payload)


async def _answered(response: web.Response) -> web.Response:
    return response


def site_webhook_handler(site: ConfigEntry) -> Callable[..., Awaitable[web.Response]]:
    """The site webhook's request handler, for registering it again under a new id (`api/phones.py`)."""

    async def async_handle_webhook(hass: HomeAssistant, _webhook_id: str, request: web.Request) -> web.Response:
        return await async_answer_webhook(
            hass, request, lambda action, payload: site_call(hass, site, action, payload)
        )

    return async_handle_webhook


def async_register_site_webhook(hass: HomeAssistant, site: ConfigEntry) -> None:
    """Register this site's webhook (when it holds an id), and unregister it when the entry unloads or fails to set
    up."""
    if not site.data.get(CONF_WEBHOOK_ID):
        return
    async_register_entry_webhook(hass, site, SITE_WEBHOOK_NAME, site_webhook_handler(site))
