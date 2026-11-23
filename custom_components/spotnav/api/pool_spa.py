"""The pool's and the spa's household controls (spec `plans/v3_pool_spa_spec.md` section 10.2): any user who can see the
heat card, as the dial and the Boost button are. Nothing here writes to a device: the settings are the heat settings'
(`heat/config.py`, judged by `pool/config.py` and `spa/config.py`), the extra filtering and the bath now are the
shadow's (`pool/runtime.py`).

`spotnav/pool` (API v1):

    {type: "spotnav/pool", api_version: 1, site_id | charger_id,
     action: "target" | "season" | "extra_filtering" | "cancel_extra", target_c?, open?, hours?: 2 | 4}
    -> {api_version, ok, error, pool}

`target` sets the wanted water temperature (18..40 in half degrees), `season` opens or closes the season (closed:
SpotNav touches nothing, and an extra filtering ends), `extra_filtering` adds 2 or 4 hours of filtering placed in the
cheapest or sunniest hours of the next 24 (a second press replaces the first), `cancel_extra` ends it. `pool` is
`get_heat`'s `pool` after the change. Refusals (`error`): `spotnav_not_supported` (no pool), `spotnav_invalid_value`.
The webhook's `pool` is the same body without `type` (`pool_action` routes it, as `boost_action` does).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from ..const import CONF_HEAT
from ..heat.config import HeatConfig
from ..heat.view import HEAT_API_VERSION
from ..pool.config import apply_pool
from ..pool.plan import EXTRA_HOURS
from ..runtime import site_controller_for
from .common import send_unsupported_version
from .heat import ERROR_INVALID_VALUE, entity_check, stored_config
from .power_month import PowerMonthRefusal, _site_of_request

_LOGGER = logging.getLogger(__name__)

ERROR_NOT_SUPPORTED: Final = "spotnav_not_supported"
POOL_ACTIONS: Final = ("target", "season", "extra_filtering", "cancel_extra")


def _envelope(error: str | None, key: str, block: Any) -> dict[str, Any]:
    return {"api_version": HEAT_API_VERSION, "ok": error is None, "error": error, key: block}


def write_heat_config(hass: HomeAssistant, site: ConfigEntry, config: HeatConfig) -> None:
    """Store the heat settings (one synchronous step on the event loop, so two writes cannot interleave)."""
    data = {**site.data, CONF_HEAT: config.as_dict()}
    if data != dict(site.data):
        hass.config_entries.async_update_entry(site, data=data)


def _shadow(hass: HomeAssistant, site: ConfigEntry) -> Any:
    controller = site_controller_for(hass, site.entry_id)
    return None if controller is None else getattr(controller, "pool_spa", None)


def pool_answer(hass: HomeAssistant, site: ConfigEntry, msg: dict[str, Any]) -> dict[str, Any]:
    """One press of a pool control (see the module)."""
    config = stored_config(site)
    shadow = _shadow(hass, site)
    if config.pool is None or shadow is None:
        return _envelope(ERROR_NOT_SUPPORTED, "pool", None)
    action = msg.get("action")
    now = dt_util.utcnow()
    if action == "target":
        changes: dict[str, Any] = {"target_c": msg.get("target_c")}
    elif action == "season":
        changes = {"season_open": msg.get("open")}
    elif action == "extra_filtering":
        hours = msg.get("hours")
        if type(hours) is not int or hours not in EXTRA_HOURS or not config.pool.season_open or config.pool.circulation_id is None:
            return _envelope(ERROR_INVALID_VALUE, "pool", shadow.pool_status(config, now))
        shadow.set_extra(hours, now)
        _LOGGER.info("SpotNav pool: extra filtering of %s hours", hours)
        return _envelope(None, "pool", shadow.pool_status(config, now))
    elif action == "cancel_extra":
        shadow.set_extra(None, now)
        return _envelope(None, "pool", shadow.pool_status(config, now))
    else:
        return _envelope(ERROR_INVALID_VALUE, "pool", shadow.pool_status(config, now))
    pool, errors = apply_pool(config.pool, changes, entity_check(hass))
    if errors or pool is None:
        return _envelope(ERROR_INVALID_VALUE, "pool", shadow.pool_status(config, now))
    write_heat_config(hass, site, replace(config, pool=pool))
    if not pool.season_open:
        # Closed: SpotNav touches nothing, and the extra filtering ends with the season.
        shadow.set_extra(None, now)
    shadow.request_look()
    _LOGGER.info("SpotNav pool: %s", action)
    return _envelope(None, "pool", shadow.pool_status(stored_config(site), now))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/pool",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
        vol.Optional("action"): object,
        vol.Optional("target_c"): object,
        vol.Optional("open"): object,
        vol.Optional("hours"): object,
    }
)
@websocket_api.async_response
async def websocket_pool(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A pool control (see the module)."""
    version = msg.get("api_version")
    if type(version) is not int or version != HEAT_API_VERSION:
        send_unsupported_version(connection, msg, HEAT_API_VERSION)
        return
    try:
        site = _site_of_request(hass, msg)
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], pool_answer(hass, site, msg))


@callback
def async_setup_pool_spa_api(hass: HomeAssistant) -> None:
    """Register the pool's and the spa's commands once for the domain."""
    websocket_api.async_register_command(hass, websocket_pool)
