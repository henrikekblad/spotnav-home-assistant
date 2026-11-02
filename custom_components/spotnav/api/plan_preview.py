"""`spotnav/plan_preview`: what a charger's plan would be if a settings body were saved, with nothing saved.

The card's Plan dialog asks this while a person edits it, so the dialog can show the plan before Save. The request
is the one `spotnav/update_settings` takes, without `expected_revision`: `{api_version, charger_id, settings}`.
The body is judged by the same code as a write (`decode_settings`, `body_mutator`, the non-administrator rule,
the charger's current range, the departure date's guard and the store's final validation), and the plan is the
one the controller's own calculation makes on the record a write would store (`AutoPlannerController.async_preview`).

Nothing happens: no settings or revision are written, no plan is installed or proposal saved, no appointment is
armed, the charger and the car are asked nothing, no listener, notice or event hears of it. A Save afterwards
calculates the same plan at the same moment.

Who may ask: whoever may Save that body (an administrator, or under `users_change_plan` any signed-in user for the
plan's fields). At most `RATE_PER_S` requests a second per connection and `MAX_IN_FLIGHT` being calculated at once
across all of them (`spotnav_rate_limited`), and a body of at most `MAX_BODY_BYTES` (`spotnav_body_too_large`).

The charger webhook's `plan_preview {"settings"}` (`async_webhook_plan_preview`) is the same for the paired app, which
counts as an administrator: the same checks, with one rate window per charger webhook in place of a connection's, the
same calculations-at-once limit shared with the card, and what the app leaves out of a body kept as its Save keeps it.

The answer: `{api_version, ok, error, preview}`. A body a Save would refuse is `ok: false` with the Save's own code
(`invalid_amps`, `invalid_departure`, ...) and `preview: null`. Otherwise `preview` is
`{state, reason, plan, status, strip}` (`encode_preview`).
"""

from __future__ import annotations

from collections import deque
import logging
import time
from typing import Any, Callable, Final

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.json import json_bytes
from homeassistant.util import dt as dt_util

from ..planning.auto_controller import AutoPlannerController, AutoSnapshot
from ..planning.auto_settings import AutoSettings, AutoSettingsError, STRATEGY_SOLAR
from ..planning.status_compose import compose_status, ProposalFacts, StatusFacts
from ..pricing.price_source import entry_source_for
from ..runtime import domain_data, preview_for
from ..util import aware_iso, finite_number
from .common import (
    ERROR_CHARGER_UNLOADED,
    ERROR_NOT_ADMIN,
    ERROR_UNSUPPORTED_VERSION,
    is_admin,
    lookup_charger,
    send_unsupported_version,
)
from .dashboard import planning_facts
from .settings import (
    body_mutator,
    decode_settings,
    ERROR_SETTINGS_UNAVAILABLE,
    plan_only_mutator,
    PriceRoute,
    refuse_amps_above_charger_range,
    SETTINGS_API_VERSION,
    settings_version_of,
    SettingsRefusal,
    strategy_of,
)

_LOGGER = logging.getLogger(__name__)

#: Requests a second one connection may make; the card asks once ~300 ms after the last change.
RATE_PER_S: Final = 5
#: Previews calculated at once across every connection; one more is `spotnav_rate_limited`.
MAX_IN_FLIGHT: Final = 4
#: The largest body judged, as JSON; a whole settings body is far smaller.
MAX_BODY_BYTES: Final = 16_384

ERROR_RATE_LIMITED: Final = "spotnav_rate_limited"
ERROR_BODY_TOO_LARGE: Final = "spotnav_body_too_large"

#: Where each open connection's recent request times are kept (dropped when it closes).
_RATE_DATA: Final = "spotnav_plan_preview_rate"
_RATE_KEY: Final = "spotnav_plan_preview_rate"
#: Each charger webhook's recent request times.
_WEBHOOK_RATE_DATA: Final = "spotnav_plan_preview_webhook_rate"
#: How many previews are being calculated now, installation-wide.
_IN_FLIGHT_DATA: Final = "spotnav_plan_preview_in_flight"

#: The states a plan comes with.
_PLANNED_STATES: Final = frozenset({"proposal_ready", "proposal_unpriced"})


def async_preview_judged(
    hass: HomeAssistant,
    entry_id: Any,
    replacement: Any,
    *,
    plan_only: bool,
    from_app: bool = False,
    reads_auto_periods: bool = True,
) -> tuple[AutoPlannerController, AutoSettings]:
    """The controller and the record a write of `replacement` would store now, or the write's own refusal.

    `SettingsRefusal` for the charger or the caller (as `async_update_settings` refuses them), `AutoSettingsError`
    for the body. Nothing is written. `from_app` and `reads_auto_periods` are the webhook Save's (`api/webhook.py`),
    so the app's preview keeps what its Save keeps.
    """
    _entry, failure = lookup_charger(hass, entry_id)
    if failure is not None:
        raise SettingsRefusal(failure)
    store = domain_data(hass).auto_store
    if store is None:
        raise SettingsRefusal(ERROR_SETTINGS_UNAVAILABLE)
    if plan_only and not store.settings(entry_id).users_change_plan:
        raise SettingsRefusal(ERROR_NOT_ADMIN)
    decoded = decode_settings(replacement)
    source = entry_source_for(hass, entry_id)
    route = None if source is None else PriceRoute(source, decoded)
    if route is not None and plan_only and route.overrides is not None:
        raise SettingsRefusal(ERROR_NOT_ADMIN)
    mutate: Callable[[AutoSettings], AutoSettings] = body_mutator(
        hass, entry_id, replacement, decoded, from_app=from_app, reads_auto_periods=reads_auto_periods
    )
    if route is not None:
        # As a write: the price entry's area and its row stay the charger's own as stored, so a body that echoes
        # them as read moves nothing of the charger's (and a user who may plan is not refused for them).
        mutate = route.mutator(hass, mutate)
    if plan_only:
        mutate = plan_only_mutator(mutate)
    refuse_amps_above_charger_range(hass, entry_id, decoded)
    controller = preview_for(hass, entry_id)
    if controller is None:
        raise SettingsRefusal(ERROR_CHARGER_UNLOADED)
    return controller, controller.proposed_settings(mutate)


def _plan(snapshot: AutoSnapshot) -> dict[str, Any] | None:
    """The plan a Save would make, as the card's plan line reads it; `None` when it makes none."""
    proposal = snapshot.proposal
    if snapshot.state not in _PLANNED_STATES or proposal is None or not proposal.has_plan or not proposal.periods:
        return None
    shown = proposal.shown_periods
    cost = finite_number(proposal.estimated_cost)
    currency = snapshot.currency
    return {
        "periods": [{"start": aware_iso(start), "end": aware_iso(end)} for start, end in shown],
        "planned_kwh": finite_number(proposal.delivered_kwh),
        "requested_kwh": finite_number(proposal.requested_kwh),
        "power_kw": finite_number(proposal.power_kw),
        "cost": None if cost is None or currency is None or proposal.unpriced else {"value": cost, "currency": currency},
        "finish": aware_iso(shown[-1][1]),
        "unpriced": proposal.unpriced,
        "short_of_deadline": proposal.short_of_deadline,
    }


def _strip(snapshot: AutoSnapshot) -> dict[str, Any] | None:
    """The hours the plan was chosen from, quarter by quarter, with each quarter's price (the minor unit, fees
    included, as the plan priced it) and whether it is planned; `None` without a plan."""
    proposal = snapshot.proposal
    if _plan(snapshot) is None or proposal is None:
        return None
    rows = proposal.horizon
    shown = proposal.shown_periods
    start = rows[0].utc_start if rows else shown[0][0]
    end = rows[-1].utc_end if rows else shown[-1][1]
    if snapshot.departure_at is not None and snapshot.departure_at > end:
        end = snapshot.departure_at
    return {
        "start": aware_iso(start),
        "end": aware_iso(end),
        "quarters": [
            {
                "start": aware_iso(row.utc_start),
                "price": finite_number(row.effective_minor_per_kwh),
                "planned": row.in_proposal,
            }
            for row in rows
        ],
    }


def _status(snapshot: AutoSnapshot, settings: AutoSettings) -> dict[str, Any]:
    """The plan's own status lines, composed as the dashboard's are, from the preview's planning alone: what the
    charger is doing now (charging, a pause, the site) is the dashboard's, not the preview's."""
    strategy = strategy_of(settings)
    if strategy == STRATEGY_SOLAR:
        # The sun decides, not a plan: nothing to preview.
        return {"tone": "normal", "lines": []}
    proposal = snapshot.proposal
    planned = _plan(snapshot)
    facts = StatusFacts(
        now=snapshot.calculated_at or dt_util.utcnow(),
        has_settings=True,
        strategy=strategy,
        departure_enabled=settings.departure_enabled,
        planning=planning_facts(snapshot),
        prices_unpriced=snapshot.unpriced,
        proposal=None
        if planned is None or proposal is None
        else ProposalFacts(
            periods=tuple(proposal.shown_periods),
            planned_kwh=planned["planned_kwh"],
            requested_kwh=planned["requested_kwh"],
            cost=None if planned["cost"] is None else planned["cost"]["value"],
            currency=None if planned["cost"] is None else planned["cost"]["currency"],
            distance_mil=finite_number(proposal.distance_mil),
            unpriced=proposal.unpriced,
            short_of_deadline=proposal.short_of_deadline,
            now_cost=finite_number(proposal.now_cost),
            need_met_at=proposal.need_met_at,
        ),
    )
    return compose_status(facts)


def encode_preview(snapshot: AutoSnapshot, settings: AutoSettings) -> dict[str, Any]:
    """`{state, reason, plan, status, strip}`: the snapshot's planning state and reason; the plan (`null` when
    none: then `status` says why, in the dashboard's status codes); the status lines the plan comes with (a
    departure it does not make, prices it waits for, ...); and the strip the card draws under it."""
    return {
        "state": snapshot.state,
        "reason": snapshot.reason,
        "plan": _plan(snapshot),
        "status": _status(snapshot, settings),
        "strip": _strip(snapshot),
    }


def preview_answer(error: str | None, preview: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"api_version": SETTINGS_API_VERSION, "ok": error is None, "error": error, "preview": preview}


async def async_plan_preview(
    hass: HomeAssistant,
    entry_id: Any,
    replacement: Any,
    *,
    plan_only: bool,
    from_app: bool = False,
    reads_auto_periods: bool = True,
) -> dict[str, Any]:
    """The whole answer for one body (see the module); `SettingsRefusal` for the charger or the caller."""
    try:
        controller, settings = async_preview_judged(
            hass,
            entry_id,
            replacement,
            plan_only=plan_only,
            from_app=from_app,
            reads_auto_periods=reads_auto_periods,
        )
    except AutoSettingsError as refusal:
        return preview_answer(refusal.code)
    snapshot = await controller.async_preview(settings)
    return preview_answer(None, encode_preview(snapshot, settings))


def _take(window: deque[float]) -> bool:
    """Whether one more request fits in this caller's last second (`RATE_PER_S`); counted when it does."""
    now = time.monotonic()
    while window and now - window[0] >= 1.0:
        window.popleft()
    if len(window) >= RATE_PER_S:
        return False
    window.append(now)
    return True


def _allowed(hass: HomeAssistant, connection: websocket_api.ActiveConnection) -> bool:
    """Whether this connection may ask once more now (`RATE_PER_S` a second); the times go when it closes."""
    windows: dict[int, deque[float]] = hass.data.setdefault(_RATE_DATA, {})
    key = id(connection)
    window = windows.get(key)
    if window is None:
        window = windows[key] = deque()
        connection.subscriptions[_RATE_KEY] = lambda: windows.pop(key, None)
    return _take(window)


def _webhook_allowed(hass: HomeAssistant, entry_id: str) -> bool:
    """Whether this charger's webhook may ask once more now: the webhook is the app's one caller, as a connection is
    the card's (one window per charger, as many as there are chargers)."""
    windows: dict[str, deque[float]] = hass.data.setdefault(_WEBHOOK_RATE_DATA, {})
    return _take(windows.setdefault(entry_id, deque()))


async def async_webhook_plan_preview(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any], *, reads_auto_periods: bool
) -> tuple[int, dict[str, Any]]:
    """`plan_preview {"settings"}` over the charger webhook, for its own charger: the command's checks in its order
    (version, rate, body cap, the calculations at once) and its core, as an administrator and with the webhook
    Save's handling of what the app leaves out. `(http_status, body)`: 200 with the command's envelope for a
    preview, 400 with it (`preview: null`) for a body a Save refuses; a refusal before the body is judged is
    `{"ok": false, "error"}`, 429 for `spotnav_rate_limited` (with `retry_after_s`), else 400."""
    if settings_version_of(payload.get("api_version", SETTINGS_API_VERSION)) is None:
        return 400, {"ok": False, "error": ERROR_UNSUPPORTED_VERSION}
    if not _webhook_allowed(hass, entry.entry_id):
        return 429, {"ok": False, "error": ERROR_RATE_LIMITED, "retry_after_s": 1}
    if len(json_bytes(payload.get("settings"))) > MAX_BODY_BYTES:
        return 400, {"ok": False, "error": ERROR_BODY_TOO_LARGE}
    if hass.data.get(_IN_FLIGHT_DATA, 0) >= MAX_IN_FLIGHT:
        return 429, {"ok": False, "error": ERROR_RATE_LIMITED, "retry_after_s": 1}
    hass.data[_IN_FLIGHT_DATA] = hass.data.get(_IN_FLIGHT_DATA, 0) + 1
    try:
        answer = await async_plan_preview(
            hass,
            entry.entry_id,
            payload.get("settings"),
            plan_only=False,
            from_app=True,
            reads_auto_periods=reads_auto_periods,
        )
    except SettingsRefusal as refusal:
        return 400, {"ok": False, "error": refusal.code}
    finally:
        hass.data[_IN_FLIGHT_DATA] -= 1
    return (200 if answer["ok"] else 400), answer


@websocket_api.websocket_command(
    {
        "type": "spotnav/plan_preview",
        # Version is judged in the handler so a wrong one gets the settings contract's stable code.
        "api_version": object,
        "charger_id": str,
        "settings": object,
    }
)
@websocket_api.async_response
async def websocket_plan_preview(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """What a Save of `settings` would plan, with nothing saved (see the module)."""
    if settings_version_of(msg.get("api_version")) is None:
        send_unsupported_version(connection, msg, SETTINGS_API_VERSION)
        return
    if not _allowed(hass, connection):
        connection.send_error(msg["id"], ERROR_RATE_LIMITED, "Too many plan previews; try again in a moment")
        return
    if len(json_bytes(msg.get("settings"))) > MAX_BODY_BYTES:
        connection.send_error(msg["id"], ERROR_BODY_TOO_LARGE, "That settings body is too large")
        return
    if hass.data.get(_IN_FLIGHT_DATA, 0) >= MAX_IN_FLIGHT:
        connection.send_error(msg["id"], ERROR_RATE_LIMITED, "Too many plan previews; try again in a moment")
        return
    hass.data[_IN_FLIGHT_DATA] = hass.data.get(_IN_FLIGHT_DATA, 0) + 1
    try:
        answer = await async_plan_preview(
            hass, msg.get("charger_id"), msg.get("settings"), plan_only=not is_admin(connection)
        )
    except SettingsRefusal as refusal:
        message = "Only an administrator may change that" if refusal.code == ERROR_NOT_ADMIN else "That charger is not available"
        connection.send_error(msg["id"], refusal.code, message)
        return
    finally:
        hass.data[_IN_FLIGHT_DATA] -= 1
    connection.send_result(msg["id"], answer)


@callback
def async_setup_plan_preview_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_plan_preview)
