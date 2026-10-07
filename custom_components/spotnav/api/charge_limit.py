"""`spotnav/set_charge_limit`: the card's twin of the webhook action `set_charge_limit`.

`{api_version: 1, charger_id, vehicle_id, percent}` writes the car's own charge-limit entity through
`vehicles/vehicle_charge_limit.async_set_charge_limit`, the app's write path: the same resolved entity,
the same range checks and the same per-vehicle interval. Administrators only, as every other vehicle write
from the card. The answer is `{"api_version": 1, "ok", "error", "retry_after_s"}`. Refusals:
`spotnav_not_admin`, `spotnav_invalid_value` (not a car with a writable limit, or a percent it cannot
take: one code for both, as the webhook's one message), `spotnav_too_soon` with `retry_after_s`,
`spotnav_charge_limit_failed` (the vehicle integration's own setter failed), and the charger lookup's codes.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant

from ..vehicles.vehicle_charge_limit import async_set_charge_limit, VehicleChargeLimitLimited
from .common import ERROR_NOT_ADMIN, is_admin, lookup_charger, send_unsupported_version

_LOGGER = logging.getLogger(__name__)

CHARGE_LIMIT_API_VERSION: Final = 1
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"
ERROR_TOO_SOON: Final = "spotnav_too_soon"
ERROR_FAILED: Final = "spotnav_charge_limit_failed"


def _answer(error: str | None = None, retry_after_s: int | None = None) -> dict[str, Any]:
    return {
        "api_version": CHARGE_LIMIT_API_VERSION,
        "ok": error is None,
        "error": error,
        "retry_after_s": retry_after_s,
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/set_charge_limit",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("vehicle_id"): object,
        vol.Optional("percent"): object,
    }
)
@websocket_api.async_response
async def websocket_set_charge_limit(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Write a car's own charge limit. `ok` means Home Assistant has written it, not that the car has it."""
    version = msg.get("api_version")
    if isinstance(version, bool) or version != CHARGE_LIMIT_API_VERSION:
        send_unsupported_version(connection, msg, CHARGE_LIMIT_API_VERSION)
        return
    if not is_admin(connection):
        connection.send_result(msg["id"], _answer(ERROR_NOT_ADMIN))
        return
    _entry, failure = lookup_charger(hass, msg.get("charger_id"))
    if failure is not None:
        connection.send_result(msg["id"], _answer(failure))
        return
    try:
        await async_set_charge_limit(hass, msg.get("vehicle_id"), msg.get("percent"))
    except VehicleChargeLimitLimited as limited:
        connection.send_result(msg["id"], _answer(ERROR_TOO_SOON, math.ceil(limited.retry_after_s)))
        return
    except ValueError:
        connection.send_result(msg["id"], _answer(ERROR_INVALID_VALUE))
        return
    except Exception as err:  # noqa: BLE001 - logged by type, answered by a stable code
        _LOGGER.warning("Setting a car's charge limit failed (%s)", type(err).__name__)
        connection.send_result(msg["id"], _answer(ERROR_FAILED))
        return
    connection.send_result(msg["id"], _answer())


@callback
def async_setup_charge_limit_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_set_charge_limit)
