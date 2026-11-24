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

`spotnav/spa` and `spotnav/sauna` (API v1), the deadline heat loads (the sauna is the spa without an idle temperature):

    {type: "spotnav/spa", api_version: 1, site_id | charger_id,
     action: "bath_c" | "baths" | "bath_once" | "bath_now" | "cancel_now" | "take_what_fits" | "later_bath"
             | "accept_peak", value?}
    -> {api_version, ok, error, spa}
    {type: "spotnav/sauna", ..., action: "target_c" | "sessions" | "session_once" | "session_now" | "cancel_now"
             | "take_what_fits" | "later_session" | "accept_peak", value?}
    -> {api_version, ok, error, sauna}

`value` is the setting's new value for the first three (the temperature, the weekly sessions `[{weekdays, time}]`, the
one-off as an ISO instant or `null`), judged as the settings are. The spa's temperature between baths is an
administrator's setting (`spotnav/update_heat`, administrators only), not a household control. `bath_now`/`session_now` heats at once, at any price
(still under the fuse), until the temperature is reached and held two hours, or `cancel_now`; once per ten minutes
(`spotnav_rate_limited`). The last three answer a session that will not be ready (`not_ready`): take what fits (it
heats as far as it can), move it to the earliest later hour that fits (`spotnav_no_later_bath` when there is none, the
sauna's `spotnav_no_later_session`), or allow the peak (the import target or the power level, never the fuse).
Refusals: `spotnav_not_supported` (no spa or sauna), `spotnav_invalid_value`. Nothing is written to a device. The webhook's `spa` and `sauna` are the same bodies
without `type`, routed by `spa_action` and `sauna_action`.
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


#: Per load: its settings field, the shadow's part, the status reader, and each action's setting key or press.
DEADLINE_ACTIONS: Final[dict[str, dict[str, str]]] = {
    "spa": {
        "bath_c": "bath_c", "baths": "baths", "bath_once": "bath_once",
        "bath_now": "!now", "cancel_now": "!cancel", "take_what_fits": "!fits", "later_bath": "!later",
        "accept_peak": "!peak",
    },
    "sauna": {
        "target_c": "target_c", "sessions": "sessions", "session_once": "session_once",
        "session_now": "!now", "cancel_now": "!cancel", "take_what_fits": "!fits", "later_session": "!later",
        "accept_peak": "!peak",
    },
}


def deadline_answer(hass: HomeAssistant, site: ConfigEntry, kind: str, msg: dict[str, Any]) -> dict[str, Any]:
    """One press of a spa or sauna control (see the module)."""
    from ..sauna.config import apply_sauna  # noqa: PLC0415
    from ..spa.config import apply_spa  # noqa: PLC0415

    config = stored_config(site)
    block = config.spa if kind == "spa" else config.sauna
    shadow = _shadow(hass, site)
    if block is None or shadow is None:
        return _envelope(ERROR_NOT_SUPPORTED, kind, None)
    part = shadow.spa if kind == "spa" else shadow.sauna
    now = dt_util.utcnow()

    def status() -> dict[str, Any] | None:
        current = stored_config(site)
        return shadow.spa_status(current, now) if kind == "spa" else shadow.sauna_status(current, now)

    step = DEADLINE_ACTIONS[kind].get(msg.get("action"))  # type: ignore[arg-type]
    if step is None:
        return _envelope(ERROR_INVALID_VALUE, kind, status())
    if not step.startswith("!"):
        changes = {step: msg.get("value")}
        if kind == "spa":
            updated, errors = apply_spa(block, changes, entity_check(hass), now=now)  # type: ignore[arg-type]
        else:
            updated, errors = apply_sauna(block, changes, entity_check(hass), now=now)  # type: ignore[arg-type]
        if errors or updated is None:
            return _envelope(ERROR_INVALID_VALUE, kind, status())
        write_heat_config(hass, site, replace(config, **{kind: updated}))
    else:
        plan = part.plan
        target = block.bath_c if kind == "spa" else block.target_c  # type: ignore[union-attr]
        error = None
        if step == "!now":
            error = part.press_now(now, target, part.reading.current_c)
        elif step == "!cancel":
            part.cancel_now()
        elif plan is None or plan.not_ready is None:
            error = ERROR_INVALID_VALUE
        elif step == "!fits":
            part.take_what_fits(plan.not_ready.at)
        elif step == "!peak":
            if plan.not_ready.cause not in ("import_target", "power_level"):
                error = ERROR_INVALID_VALUE
            else:
                part.allow_peak(plan.not_ready.at)
        else:
            _later, error = part.move_later()
        if error is not None:
            return _envelope(error, kind, status())
    shadow.request_look()
    _LOGGER.info("SpotNav %s: %s", kind, msg.get("action"))
    return _envelope(None, kind, status())


def _deadline_command(kind: str) -> Any:
    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"spotnav/{kind}",
            vol.Optional("api_version"): object,
            vol.Optional("charger_id"): object,
            vol.Optional("site_id"): object,
            vol.Optional("action"): object,
            vol.Optional("value"): object,
        }
    )
    @websocket_api.async_response
    async def handler(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
        version = msg.get("api_version")
        if type(version) is not int or version != HEAT_API_VERSION:
            send_unsupported_version(connection, msg, HEAT_API_VERSION)
            return
        try:
            site = _site_of_request(hass, msg)
        except PowerMonthRefusal as refusal:
            connection.send_error(msg["id"], refusal.code, refusal.message)
            return
        connection.send_result(msg["id"], deadline_answer(hass, site, kind, msg))

    return handler


websocket_spa = _deadline_command("spa")
websocket_sauna = _deadline_command("sauna")


@callback
def async_setup_pool_spa_api(hass: HomeAssistant) -> None:
    """Register the pool's, the spa's and the sauna's commands once for the domain."""
    websocket_api.async_register_command(hass, websocket_pool)
    websocket_api.async_register_command(hass, websocket_spa)
    websocket_api.async_register_command(hass, websocket_sauna)
