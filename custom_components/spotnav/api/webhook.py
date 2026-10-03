"""The charger webhook: one POST endpoint per charger entry, one handler per action.

The transport for callers that cannot open a WebSocket. It is bound to its charger entry (a
`charger_id` in the body is never read) and every action shares a WebSocket command's core,
validation and envelope.

`ACTIONS` is the dispatch table. A handler returns a finished `web.Response` or the extra fields
of the shared `{"ok": true, "action": ...}` answer; refusals are exceptions, answered in
`async_handle_webhook`.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant

from ..const import CONF_WEBHOOK_ID, DOMAIN
from ..execution.auto_execution import ACTION_RESUME, ACTION_STOP, AutoControlError
from ..planning.auto_settings import AutoSettingsError
from ..planning.phases import effective_phases
from ..runtime import ChargerConfigEntry, domain_data
from ..vehicles.vehicle_charge_limit import async_set_charge_limit, VehicleChargeLimitLimited
from ..vehicles.vehicle_refresh import async_refresh_vehicle, VehicleRefreshLimited
from .common import ERROR_UNSUPPORTED_VERSION
from .dashboard import async_webhook_dashboard, DashboardFailure
from .entity_config import async_webhook_update_vehicle
from .sessions import SESSIONS_API_VERSION, sessions_answer, SessionsRefusal
from .settings import (
    async_update_settings,
    fiscal_included_for,
    settings_envelope,
    settings_failure,
    settings_version_of,
    SettingsNotCommitted,
    SettingsReconcileError,
    SettingsRefusal,
)
from .site_settings import async_webhook_update_site_settings


_LOGGER = logging.getLogger(__name__)

#: A handler's answer: a finished response, or the extra fields of the shared success body.
type Outcome = web.Response | dict[str, Any] | None
type Handler = Callable[[HomeAssistant, ChargerConfigEntry, dict[str, Any]], Awaitable[Outcome]]

#: Warning-level log budget for rejected commands; the rest are debug lines.
_REJECTED_WARNING_INTERVAL_S: Final = 60.0
_last_rejected_warning: float | None = None


#: Settings fields the paired Android app does not read yet. Its decoder refuses a settings record
#: with an unknown field, and with it the whole dashboard, so the webhook leaves them out until an
#: app that reads them is out. A request opts in per field with a top-level `reads` list. A
#: replacement without one keeps the stored value (`fiscal_included` is read-only and never stored).
APP_UNREAD_SETTINGS: Final = ("departure_date", "departure_weekdays", "fiscal_included")


def _for_app(body: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """`body` with the fields the app cannot read yet taken out of its settings record.

    A request opts in per field with a top-level `reads` list; anything else in it, or a `reads`
    that is not a list, is ignored.
    """
    settings = body.get("settings")
    if not isinstance(settings, dict):
        return body
    reads = payload.get("reads")
    opted_in = {name for name in reads if isinstance(name, str)} if isinstance(reads, list) else set()
    withheld = set(APP_UNREAD_SETTINGS) - opted_in
    return {**body, "settings": {key: value for key, value in settings.items() if key not in withheld}}


def _log_rejected(error: Exception) -> None:
    global _last_rejected_warning  # noqa: PLW0603 - one process-wide rate limit
    now = time.monotonic()
    if _last_rejected_warning is None or now - _last_rejected_warning >= _REJECTED_WARNING_INTERVAL_S:
        _last_rejected_warning = now
        _LOGGER.warning("Rejected SpotNav webhook command: %s", error)
    else:
        _LOGGER.debug("Rejected SpotNav webhook command: %s", error)


async def async_manual_action(
    entry: ChargerConfigEntry,
    action: str,
    *,
    amps: int | None = None,
    choice: str | None = None,
) -> None:
    """Route a manual charger action through the charger's one authority boundary.

    The boundary keeps a button press or webhook `cancel` from interleaving with an Auto
    installation, and a cancel's notification from reinstalling the plan it cancelled. `start`,
    `stop`, `follow` and `cancel` touch neither mode nor pause.

    `stop` with a typed `choice` is a pause and `resume` clears it; both go to the planner controller
    because they concern automatic execution. A `stop` without a choice is the immediate stop.
    A choice or action the boundary cannot honour is refused by its own code before anything changes.
    """
    data = entry.runtime_data
    executor = data.executor
    if executor is None:
        raise RuntimeError("this charger has no authority boundary")
    if action == "start":
        await executor.async_manual_start(amps)
    elif action == "stop" and choice is None:
        await executor.async_manual_stop()
    elif action in ("stop", "resume"):
        if data.preview is None:
            raise RuntimeError("this charger has no automatic planner to pause")
        if action == "stop":
            await data.preview.async_manual_action(ACTION_STOP, choice)
        else:
            await data.preview.async_manual_action(ACTION_RESUME, None)
    elif action == "cancel":
        await executor.async_manual_cancel()
    else:
        await executor.async_manual_follow()



async def _start(hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]) -> Outcome:
    """A human deciding to charge; a reading at or above the target does not veto it."""
    amps = int(payload["amps"]) if "amps" in payload else None
    await async_manual_action(entry, "start", amps=amps)
    return None


async def _stop(hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]) -> Outcome:
    """With a `choice` it is Auto's pause; without one, the immediate stop."""
    await async_manual_action(entry, "stop", choice=payload.get("choice"))
    return None


async def _resume(hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]) -> Outcome:
    """Clearing the typed pause is the whole act; a charger with no stored pause is still accepted."""
    await async_manual_action(entry, "resume")
    return None


async def _refresh_vehicle(
    hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]
) -> Outcome:
    """Ask Home Assistant to re-read the vehicle's own entities, never waking the car.

    The answer carries a count, not readings: the dashboard reports those.
    """
    return {"entity_count": await async_refresh_vehicle(hass, payload.get("vehicle_id"))}


async def _set_charge_limit(
    hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]
) -> Outcome:
    """Write the vehicle's own charge-limit entity; `ok` means the write happened."""
    await async_set_charge_limit(hass, payload.get("vehicle_id"), payload["percent"])
    return None


async def _dashboard(hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]) -> Outcome:
    """The one read: the dashboard payload the card reads over the WebSocket, for this charger."""
    dashboard = await async_webhook_dashboard(hass, entry, payload.get("api_version"))
    if isinstance(dashboard, DashboardFailure):
        return web.json_response(
            {"ok": False, "error": dashboard.code, "action": "dashboard"}, status=400
        )
    return web.json_response(_for_app({"ok": True, "action": "dashboard", **dashboard}, payload))


async def _sessions(hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]) -> Outcome:
    """A read twinning `spotnav/get_sessions`: the same request fields (`month`, `format`, `limit`, `from`,
    `to`) and the same answer, with the routing `action` beside it. Nothing is withheld."""
    version = payload.get("api_version", SESSIONS_API_VERSION)
    if type(version) is not int or version != SESSIONS_API_VERSION:
        return web.json_response(
            {"ok": False, "error": ERROR_UNSUPPORTED_VERSION, "action": "sessions"}, status=400
        )
    try:
        answer = sessions_answer(hass, entry.entry_id, payload)
    except SessionsRefusal as refusal:
        return web.json_response({"ok": False, "error": refusal.code, "action": "sessions"}, status=400)
    return web.json_response({"ok": True, "action": "sessions", **answer})


async def _settings(hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]) -> Outcome:
    """A full replacement at a revision the caller names, via the WebSocket command's function.

    The answer is the canonical settings envelope plus the routing `action`. HTTP separates a bad
    request (400), a conflict (409) and a server failure (500); inside the last, the stable code
    separates "nothing written" from "written but not recalculated".
    """
    action = "settings"
    if "api_version" in payload and settings_version_of(payload["api_version"]) is None:
        return web.json_response(
            {"ok": False, "error": ERROR_UNSUPPORTED_VERSION, "action": action}, status=400
        )
    try:
        committed = await async_update_settings(
            hass,
            entry.entry_id,
            expected_revision=payload.get("expected_revision"),
            replacement=payload.get("settings"),
        )
    except SettingsRefusal as refusal:
        return web.json_response(_for_app({**settings_failure(refusal.code, None), "action": action}, payload), status=400)
    except (SettingsReconcileError, SettingsNotCommitted) as failure:
        # Valid request, resolvable charger: our follow-up or persistence failed (502 here means
        # the charger's own command failed).
        return web.json_response(
            _for_app({**settings_failure(failure.code, failure.settings, effective_phases(hass, entry.entry_id), fiscal_included_for(hass, failure.settings)), "action": action}, payload), status=500
        )
    except AutoSettingsError as refusal:
        store = domain_data(hass).auto_store
        current = None if store is None else store.settings(entry.entry_id)
        return web.json_response(
            _for_app({**settings_failure(refusal.code, current, effective_phases(hass, entry.entry_id), fiscal_included_for(hass, current)), "action": action}, payload),
            status=409 if refusal.code == "revision_conflict" else 400,
        )
    return web.json_response(_for_app({**settings_envelope(committed, effective_phases(hass, entry.entry_id), fiscal_included_for(hass, committed)), "action": action}, payload))


def _bounded_write(
    write: Callable[[HomeAssistant, ChargerConfigEntry, dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]],
    action: str,
) -> Handler:
    """A write twinning a WebSocket command: its own envelope, the routing `action` beside it."""

    async def handler(hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]) -> Outcome:
        status, envelope = await write(hass, entry, payload)
        return web.json_response({**envelope, "action": action}, status=status)

    return handler


#: Every action the webhook answers. `dashboard` and `sessions` are the reads.
ACTIONS: Final[dict[str, Handler]] = {
    "start": _start,
    "stop": _stop,
    "resume": _resume,
    "refresh_vehicle": _refresh_vehicle,
    "set_charge_limit": _set_charge_limit,
    "dashboard": _dashboard,
    "sessions": _sessions,
    "settings": _settings,
    "update_vehicle": _bounded_write(async_webhook_update_vehicle, "update_vehicle"),
    "update_site_settings": _bounded_write(async_webhook_update_site_settings, "update_site_settings"),
}



def _handler_for(entry: ChargerConfigEntry) -> Callable[..., Awaitable[web.Response]]:
    async def async_handle_webhook(
        hass: HomeAssistant, _webhook_id: str, request: web.Request
    ) -> web.Response:
        try:
            payload: dict[str, Any] = await request.json()
            if not isinstance(payload, dict):
                # A list, string or number is a bad request with a stable code, not a failure.
                return web.json_response({"ok": False, "error": "payload_not_object"}, status=400)
            if payload.get("version") != 1:
                raise ValueError("Unsupported payload version")
            action = payload.get("action")
            handler = ACTIONS.get(action) if isinstance(action, str) else None
            if handler is None:
                raise ValueError("Unsupported action")
            outcome = await handler(hass, entry, payload)
            if isinstance(outcome, web.Response):
                return outcome
            return web.json_response({"ok": True, "action": action, **(outcome or {})})
        except (VehicleRefreshLimited, VehicleChargeLimitLimited) as limited:
            # Well-formed and the vehicle exists, but cannot be asked yet: 429 with Retry-After.
            retry_after_s = math.ceil(limited.retry_after_s)
            _LOGGER.debug("Refused a per-vehicle request: retry in %d s", retry_after_s)
            return web.json_response(
                {"ok": False, "error": str(limited), "retry_after_s": retry_after_s},
                status=429,
                headers={"Retry-After": str(retry_after_s)},
            )
        except (KeyError, TypeError, ValueError) as error:
            _log_rejected(error)
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        except AutoControlError as refusal:
            # The boundary's own answer to a pause/resume: not admissible (nothing moved), or
            # committed with a failed follow-up. The stable code is the content.
            _LOGGER.debug("Refused a SpotNav webhook action: %s", refusal.code)
            return web.json_response({"ok": False, "error": refusal.code}, status=409)
        except Exception:
            _LOGGER.exception("SpotNav charger command failed")
            return web.json_response({"ok": False, "error": "Charger command failed"}, status=502)

    return async_handle_webhook


def async_register_charger_webhook(hass: HomeAssistant, entry: ChargerConfigEntry) -> None:
    """Register this charger's webhook, and unregister it when the entry unloads or fails to set up."""
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    webhook.async_register(
        hass,
        DOMAIN,
        "SpotNav charging control",
        webhook_id,
        _handler_for(entry),
        local_only=False,
        allowed_methods=("POST",),
    )
    entry.async_on_unload(lambda: webhook.async_unregister(hass, webhook_id))
