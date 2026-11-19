"""The home battery control's switch and status for the card (none shows them yet): `spotnav/battery_control`.

`{api_version: 1, site_id}` (or `charger_id`, which names the charger's site) answers the site's battery control status (`BatteryControl.status`): its options, the
state and the one line under the switch as stable codes, the verification report for the plant's firmware, the
actions it allows, the routine's progress, what the person still has to do (`needs`) and the notes. Any user may read.

`{api_version: 1, site_id, changes: {battery_control_*: bool, battery_mode: str}}` writes some options first (administrators only):
`battery_control_enabled` (the switch), `battery_control_ai_takeover_ack` (the explanation was read: Sigenergy's AI mode taken over, or a plant's own
strategy that cannot be read overridden),
`battery_mode` (`self_consumption`, `balanced` or `max_profit`: the only one that may sell; the status's `options`
carry it); the owner's `battery_control_shadow_only` and `battery_control_import_limit_acts` too.
Written to the site entry without a reload; turning off hands the plant back before the answer. Refusals are stable
codes in `error`: `spotnav_no_site`, `spotnav_not_admin`, `spotnav_invalid_value`.

`enable_control_entities: true` (administrators only, never over the webhook) is the person's confirm in the opt-in dialog:
SpotNav enables the brand's control entities that are disabled in the entity registry (the status's `enable_entities`
lists them: `{integration, entities: [{entity_id, name}]}`, `null` when none is) and reloads their integration once,
before `changes` (when given) is written. Turning control off leaves them enabled. A failed reload is
`spotnav_enable_failed`.

`clear_outside_control: true` (administrators only, never over the webhook) is the person's "Det var jag – rensa":
the outside-control verdict found by the battery's own settings or behaviour ends now, logged as cleared by a person
(the diagnostics' `outside_control.history`), and the changes counted so far are forgotten. A verdict from an
integration or a planner lasts while it is there: `spotnav_not_clearable`, as when none stands. It runs before
`changes`.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..battery import gate
from ..runtime import site_controller_for
from .common import (
    ERROR_NOT_ADMIN,
    ERROR_NOT_PERMITTED_OVER_WEBHOOK,
    is_admin,
    lookup_charger,
    lookup_site,
    send_unsupported_version,
    site_of_charger,
)

_LOGGER = logging.getLogger(__name__)

BATTERY_CONTROL_API_VERSION: Final = 1
ERROR_NO_SITE: Final = "spotnav_no_site"
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"
ERROR_ENABLE_FAILED: Final = "spotnav_enable_failed"
ERROR_NOT_CLEARABLE: Final = "spotnav_not_clearable"


def _answer(status: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
    return {"api_version": BATTERY_CONTROL_API_VERSION, "ok": error is None, "error": error, "status": status}


#: What the charger webhook's secret may write: the everyday choice of what the battery is for. The opt-in
#: (`battery_control_enabled`) and the other options need a signed-in administrator.
WEBHOOK_WRITABLE: Final = (gate.CONF_MODE,)


async def async_battery_control_answer(
    hass: HomeAssistant,
    entry: ConfigEntry | None,
    changes: Any,
    *,
    admin: bool,
    over_webhook: bool = False,
    enable_entities: bool = False,
    clear_outside_control: bool = False,
) -> dict[str, Any]:
    """The answer for one site (the WebSocket command and the webhook's `battery_control` alike): its status, after
    writing `changes` when given. `over_webhook`: only `WEBHOOK_WRITABLE` may be named; another option is
    `spotnav_not_permitted_over_webhook` and nothing is written."""
    controller = None if entry is None else site_controller_for(hass, entry.entry_id)
    if controller is None:
        return _answer(error=ERROR_NO_SITE)
    control = controller.battery_control
    if clear_outside_control:
        # A person's "it was me": never over the webhook, never without an administrator.
        if over_webhook or not admin:
            return _answer(error=ERROR_NOT_PERMITTED_OVER_WEBHOOK if over_webhook else ERROR_NOT_ADMIN)
        if not control.clear_outside_control():
            return _answer(error=ERROR_NOT_CLEARABLE)
    if enable_entities:
        # The person's confirm of the opt-in dialog: never over the webhook, never without an administrator.
        if over_webhook or not admin:
            return _answer(error=ERROR_NOT_PERMITTED_OVER_WEBHOOK if over_webhook else ERROR_NOT_ADMIN)
        if changes is not None and (not isinstance(changes, dict) or not changes or any(
            not gate.valid_change(key, value) for key, value in changes.items()
        )):
            return _answer(error=ERROR_INVALID_VALUE)
        try:
            await control.async_enable_entities()
        except Exception:  # noqa: BLE001 - the answer says so; the options below are not written
            _LOGGER.exception("SpotNav battery control: enabling the control entities failed")
            return _answer(error=ERROR_ENABLE_FAILED)
    if changes is None:
        return _answer(control.status())
    if over_webhook:
        if isinstance(changes, dict) and any(
            key in gate.WRITABLE and key not in WEBHOOK_WRITABLE for key in changes
        ):
            return _answer(error=ERROR_NOT_PERMITTED_OVER_WEBHOOK)
    elif not admin:
        return _answer(error=ERROR_NOT_ADMIN)
    if not isinstance(changes, dict) or not changes or any(
        not gate.valid_change(key, value) for key, value in changes.items()
    ):
        return _answer(error=ERROR_INVALID_VALUE)
    return _answer(await control.async_update_options(dict(changes)))


def _site_of(hass: HomeAssistant, msg: dict[str, Any]) -> ConfigEntry | None:
    """The site the request names: `site_id`, else the site of `charger_id`."""
    if "site_id" in msg:
        entry, _code = lookup_site(hass, msg.get("site_id"), require_loaded=False)
        return entry
    charger, _code = lookup_charger(hass, msg.get("charger_id"), require_loaded=False)
    return None if charger is None else site_of_charger(hass, charger.entry_id)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/battery_control",
        vol.Optional("api_version"): object,
        vol.Optional("site_id"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("changes"): object,
        vol.Optional("enable_control_entities"): bool,
        vol.Optional("clear_outside_control"): bool,
    }
)
@websocket_api.async_response
async def websocket_battery_control(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The site's battery control status, after writing `changes` when given."""
    version = msg.get("api_version")
    if isinstance(version, bool) or version != BATTERY_CONTROL_API_VERSION:
        send_unsupported_version(connection, msg, BATTERY_CONTROL_API_VERSION)
        return
    answer = await async_battery_control_answer(
        hass,
        _site_of(hass, msg),
        msg.get("changes"),
        admin=is_admin(connection),
        enable_entities=msg.get("enable_control_entities") is True,
        clear_outside_control=msg.get("clear_outside_control") is True,
    )
    connection.send_result(msg["id"], answer)


@callback
def async_setup_battery_control_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_battery_control)
