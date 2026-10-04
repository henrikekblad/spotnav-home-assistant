"""One authenticated manual-action command: the card's Start now, Stop (a typed pause) and Resume.

It routes once, to the boundary that owns the behaviour:

* `start` -> `AutoExecutor.async_manual_start` (same as a button press), and `stop` with no choice ->
  the immediate stop: a person's Start or Stop, which pauses Auto for the plug-in session (`manual`);
* `stop` with a choice -> `AutoPlannerController.async_pause(choice)`, which stores the typed pause before
  it stops anything and reports a failed stop as `pause_stop_failed`;
* `resume` -> `AutoPlannerController.async_resume`.

Properties: no direct effect (one request is at most one call to one boundary, no retry);
stable codes and no prose (exception text never reaches a client); admin for every mutation.

The envelope reports whether the boundary accepted the action, not what it later achieved; a
failed physical stop shows up as `pause_stop_failed` in the next dashboard read.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant

from ..execution.auto_execution import (
    AutoControlCommitted,
    AutoControlRefused,
    EXECUTION_ACTION_FAILED,
    EXECUTION_ACTION_UNAVAILABLE,
)
from ..planning.auto_settings import AutoSettingsError, PAUSE_CHOICES, PauseChoice
from ..runtime import preview_for
from .common import send_unsupported_version
from .dashboard import (
    ACTION_RESUME,
    ACTION_START,
    ACTION_STOP,
    DashboardFailure,
    resolve_charger_request,
)
from .settings import SettingsRefusal


_LOGGER = logging.getLogger(__name__)

#: This contract's own version, separate from the settings and dashboard versions.
ACTION_API_VERSION: Final = 1

#: The actions a caller may ask for (`none` describes a state and is never a request).
ACTIONS: Final = (ACTION_START, ACTION_STOP, ACTION_RESUME)

#: Stable codes of this contract.
ERROR_INVALID_ACTION: Final = "spotnav_invalid_action"
ERROR_ACTION_UNAVAILABLE: Final = "spotnav_action_unavailable"
#: An action that failed before it had any effect (distinct from the post-commit code below).
ERROR_ACTION_FAILED: Final = "spotnav_action_failed"
#: The effect is durable and its follow-up failed (cf. `spotnav_settings_reconcile_failed`).
ERROR_ACTION_RECONCILE_FAILED: Final = "spotnav_action_reconcile_failed"

#: The boundary's two codes renamed here; every other domain code (`invalid_pause`,
#: `switch_unavailable`, ...) travels unchanged.
_CODE_ALIASES: Final = {
    EXECUTION_ACTION_UNAVAILABLE: ERROR_ACTION_UNAVAILABLE,
    EXECUTION_ACTION_FAILED: ERROR_ACTION_FAILED,
}



def action_request(action: Any, choice: Any) -> tuple[str, str | None]:
    """The action and its choice, or a stable-code refusal for a malformed request.

    `choice` belongs to `stop` only; a `start` or `resume` carrying one is refused. A choice outside
    the vocabulary is refused by the store's own `PauseIntent` code. A `stop` with no choice is the
    immediate stop, leaving automatic planning as it is; a pause is a `stop` that names its choice.
    """
    if action not in ACTIONS:
        raise SettingsRefusal(ERROR_INVALID_ACTION)
    if action != ACTION_STOP:
        if choice is not None:
            raise SettingsRefusal(ERROR_INVALID_ACTION)
        return action, None
    if choice is None:
        return action, None
    if choice not in PAUSE_CHOICES:
        raise SettingsRefusal("invalid_pause")
    return action, choice


async def async_perform_action(
    hass: HomeAssistant, entry_id: Any, *, action: Any, choice: Any = None
) -> tuple[str, PauseChoice | None]:
    """Route one manual action to its one boundary, and report acceptance.

    The entry is resolved by the shared scoping rules first (unknown, foreign, site and unloaded
    entries are refused). Then `AutoPlannerController.async_manual_action` re-reads the facts inside
    the execution lock and executes only what the canonical decision admits; this module decides
    nothing, and the boundary's refusals travel by their own stable codes.
    """
    resolved_action, resolved_choice = action_request(action, choice)
    resolved = resolve_charger_request(hass, {"charger_id": entry_id})
    if isinstance(resolved, DashboardFailure):
        raise SettingsRefusal(resolved.code)
    preview = preview_for(hass, resolved.entry_id)
    if preview is None:
        raise SettingsRefusal(ERROR_ACTION_UNAVAILABLE)
    await preview.async_manual_action(resolved_action, resolved_choice)
    return resolved_action, resolved_choice


def action_envelope(action: str, choice: str | None) -> dict[str, Any]:
    """The success answer: this contract's version, the action, and the choice when there was one."""
    return {
        "api_version": ACTION_API_VERSION,
        "ok": True,
        "error": None,
        "action": action,
        "choice": choice,
    }


def action_failure(code: str) -> dict[str, Any]:
    """The refusal: a stable code and no prose, in the same envelope shape as a success."""
    return {
        "api_version": ACTION_API_VERSION,
        "ok": False,
        "error": code,
        "action": None,
        "choice": None,
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/manual_action",
        # Version is judged in the handler so a wrong one gets this contract's stable code.
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        # Action and choice are judged in the handler for one stable code each.
        vol.Optional("action"): object,
        vol.Optional("choice"): object,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_manual_action(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Carry out one immediate manual action: an administrative operation, at most one write.

    `AutoControlRefused` means nothing moved; `AutoControlCommitted` means the effect is durable and
    its follow-up failed. Anything else is logged by type and answered with the generic
    action-failure code; exception prose never reaches a client.
    """
    if msg.get("api_version") != ACTION_API_VERSION:
        send_unsupported_version(connection, msg, ACTION_API_VERSION)
        return
    try:
        action, choice = await async_perform_action(
            hass, msg.get("charger_id"), action=msg.get("action"), choice=msg.get("choice")
        )
    except SettingsRefusal as refusal:
        connection.send_result(msg["id"], action_failure(refusal.code))
        return
    except AutoControlRefused as refusal:
        connection.send_result(
            msg["id"], action_failure(_CODE_ALIASES.get(refusal.code, refusal.code))
        )
        return
    except AutoControlCommitted:
        connection.send_result(msg["id"], action_failure(ERROR_ACTION_RECONCILE_FAILED))
        return
    except AutoSettingsError as refusal:
        connection.send_result(msg["id"], action_failure(refusal.code))
        return
    except Exception as err:  # noqa: BLE001 - logged by type, answered by a stable code
        _LOGGER.error("A manual action failed unexpectedly (%s)", type(err).__name__)
        connection.send_result(msg["id"], action_failure(ERROR_ACTION_FAILED))
        return
    connection.send_result(msg["id"], action_envelope(action, choice))


@callback
def async_setup_manual_action_api(hass: HomeAssistant) -> None:
    """Register this contract's one command once for the domain, not once per config entry."""
    websocket_api.async_register_command(hass, websocket_manual_action)
