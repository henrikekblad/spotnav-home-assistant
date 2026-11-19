"""`spotnav/make_home` (API v1, SpotNav 3.0): "Make a home of this", the charger settings' one action for an install
whose chargers have no home (site) yet.

A home is never made silently: the card asks first, in one small dialog that says what it does and asks the one thing
SpotNav cannot know, the main fuse. Everything else a home needs starts at what the site flow would start at:

* `main_fuse_a` (asked; more than the safety margin, at most `MAX_MAIN_FUSE_A`);
* the safety margin `DEFAULT_SAFETY_MARGIN_A`, the voltage between phases of a TN network, and load balancing off
  (turning it on is a later, separate decision, as in the flow);
* the charger, wired for the phases it says it has (`CONF_CHARGER_PHASES`, else three), its phase followed
  automatically;
* no grid meter yet: direct mode with no entities, as the flow's "configure later". The house page, the heat and the
  battery work from what the home finds; load balancing waits for a meter, chosen in the home's settings;
* the home's name: Home Assistant's own (`location_name`), else "Home"; and its own webhook id.

The entry is created through the config flow (`async_step_make_home`), as every entry is.

`{type: "spotnav/make_home", api_version: 1, charger_id, main_fuse_a}` answers `{api_version: 1, ok, error,
site_id}`. Refusals (`ok: false`, nothing created): `spotnav_not_admin`, the charger lookup's codes
(`spotnav_unknown_charger`, `spotnav_site_not_charger`), `spotnav_home_exists` (the installation has a home already:
add the charger to it there), `spotnav_invalid_value` (the fuse). Another `api_version` is
`spotnav_unsupported_api_version`.
"""

from __future__ import annotations

import math
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType

from ..const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_CHARGER_ENTRY_IDS,
    CONF_CHARGER_PHASES,
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_ENTRY_TYPE,
    CONF_MAIN_FUSE_A,
    CONF_MAX_AGE_S,
    CONF_MEASUREMENT_MODE,
    CONF_PHASE_WIRING,
    CONF_SAFETY_MARGIN_A,
    CONF_SITE_CURRENT_SOURCE,
    CONF_VOLTAGE_BETWEEN_PHASES_V,
    CONF_WEBHOOK_ID,
    DEFAULT_MAX_AGE_S,
    DEFAULT_SAFETY_MARGIN_A,
    DEFAULT_VOLTAGE_BETWEEN_PHASES_V,
    DOMAIN,
    ENTRY_TYPE_SITE,
    MEASUREMENT_MODE_DIRECT,
)
from ..entry_migration import new_webhook_id
from .common import ERROR_NOT_ADMIN, is_admin, lookup_charger, send_unsupported_version

MAKE_HOME_API_VERSION: Final = 1
#: The config flow's source for a home made from the card (`flows/flow.py:async_step_make_home`).
SOURCE_MAKE_HOME: Final = "make_home"
ERROR_HOME_EXISTS: Final = "spotnav_home_exists"
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"
#: Above any household's main fuse; a typo, not a fuse.
MAX_MAIN_FUSE_A: Final = 1000.0
#: The name of a home when Home Assistant has none.
DEFAULT_HOME_NAME: Final = "Home"


def _fuse(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) if DEFAULT_SAFETY_MARGIN_A < value <= MAX_MAIN_FUSE_A else None


def home_data(hass: HomeAssistant, charger_id: str, main_fuse_a: float) -> dict[str, Any]:
    """The new home's entry data (see the module)."""
    charger = hass.config_entries.async_get_entry(charger_id)
    phases = None if charger is None else charger.data.get(CONF_CHARGER_PHASES)
    return {
        CONF_ENTRY_TYPE: ENTRY_TYPE_SITE,
        CONF_ACTIVE_CONTROL_ENABLED: False,
        CONF_MAIN_FUSE_A: main_fuse_a,
        CONF_SAFETY_MARGIN_A: DEFAULT_SAFETY_MARGIN_A,
        CONF_VOLTAGE_BETWEEN_PHASES_V: DEFAULT_VOLTAGE_BETWEEN_PHASES_V,
        CONF_MEASUREMENT_MODE: MEASUREMENT_MODE_DIRECT,
        CONF_CHARGER_ENTRY_IDS: [charger_id],
        CONF_PHASE_WIRING: {charger_id: {"phases": phases if phases in (1, 3) else 3, "phase": None}},
        CONF_DIRECT_ENTITIES: {},
        CONF_DERIVED_ENTITIES: {},
        CONF_SITE_CURRENT_SOURCE: None,
        CONF_MAX_AGE_S: DEFAULT_MAX_AGE_S,
        CONF_WEBHOOK_ID: new_webhook_id(),
    }


def home_title(hass: HomeAssistant) -> str:
    name = hass.config.location_name
    return name.strip() if isinstance(name, str) and name.strip() else DEFAULT_HOME_NAME


def _answer(error: str | None, site_id: str | None = None) -> dict[str, Any]:
    return {"api_version": MAKE_HOME_API_VERSION, "ok": error is None, "error": error, "site_id": site_id}


async def async_make_home(hass: HomeAssistant, charger_id: Any, main_fuse_a: Any) -> dict[str, Any]:
    """Make the home (see the module), or the refusal; the answer's envelope."""
    charger, code = lookup_charger(hass, charger_id, require_loaded=False)
    if charger is None:
        return _answer(code)
    if any(entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE for entry in hass.config_entries.async_entries(DOMAIN)):
        return _answer(ERROR_HOME_EXISTS)
    fuse = _fuse(main_fuse_a)
    if fuse is None:
        return _answer(ERROR_INVALID_VALUE)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_MAKE_HOME},
        data={"title": home_title(hass), "data": home_data(hass, charger.entry_id, fuse)},
    )
    if result["type"] is not FlowResultType.CREATE_ENTRY:
        return _answer(ERROR_HOME_EXISTS)
    return _answer(None, result["result"].entry_id)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/make_home",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("main_fuse_a"): object,
    }
)
@websocket_api.async_response
async def websocket_make_home(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """"Make a home of this" (see the module). Administrators only."""
    version = msg.get("api_version")
    if type(version) is not int or version != MAKE_HOME_API_VERSION:
        send_unsupported_version(connection, msg, MAKE_HOME_API_VERSION)
        return
    if not is_admin(connection):
        connection.send_result(msg["id"], _answer(ERROR_NOT_ADMIN))
        return
    connection.send_result(msg["id"], await async_make_home(hass, msg.get("charger_id"), msg.get("main_fuse_a")))


@callback
def async_setup_make_home_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_make_home)
