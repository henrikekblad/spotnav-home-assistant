"""The pairing handshake: a code a person compares, approved here.

The app asks to be paired, shows a six-digit code it generated and polls. This module holds the
pending request and the approval a person gives in Home Assistant. The endpoint is
`POST /api/webhook/spotnav_pairing` with a fixed, public webhook id, which is safe only because
it grants nothing on its own:

* a request only creates a pending record, useless without a person tapping Approve;
* the register is in memory only, so no stale approval survives a restart;
* an approval is short-lived: once delivered it can be fetched again for a minute, only so a lost
  response does not waste it, and then a replayed poll gets `expired`;
* the code, request id, webhook id and any approved payload are never logged.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import HomeAssistant

from ..const import DOMAIN
from ..runtime import domain_data
from ..vehicles.charger_inventory import charger_pairing_payload, webhook_base_url


_LOGGER = logging.getLogger(__name__)

#: The fixed, public webhook id the whole handshake is posted to.
PAIRING_WEBHOOK_ID = "spotnav_pairing"

#: How long a request stays open; matches the app's own deadline.
PAIRING_TIMEOUT_S = 300.0

#: Maximum requests awaiting a decision. Enough for a household, too few for a stranger to
#: achieve anything (each needs a person to read a code and tap Approve), and a cap on prompts.
MAX_PENDING_REQUESTS = 8

#: How long a delivered verdict can be fetched again by the same request id, so a response lost in
#: transit (a VPN, a flaky link) does not waste the approval.
REDELIVERY_WINDOW_S = 60.0

#: 32 bytes of urlsafe randomness, never derived from the code.
REQUEST_ID_BYTES = 32

#: The code shape the app generates: six digits, compared by a person.
CODE_LENGTH = 6


@dataclass(frozen=True, slots=True)
class PendingPairing:
    """One request waiting for a person: the code to compare, and who asked."""

    request_id: str
    code: str
    device: str
    created_at: float


class PairingRegister:
    """The pending pairing requests of one instance, in memory only.

    A request is open, then approved or denied (not yet delivered), then delivered, then gone:
    a delivered verdict stays fetchable for [REDELIVERY_WINDOW_S] after its first delivery. The
    clock is injected for tests.
    """

    def __init__(
        self,
        now: Callable[[], float] = time.monotonic,
        timeout_s: float = PAIRING_TIMEOUT_S,
        limit: int = MAX_PENDING_REQUESTS,
        redelivery_s: float = REDELIVERY_WINDOW_S,
    ) -> None:
        self._now = now
        self._timeout_s = timeout_s
        self._limit = limit
        self._redelivery_s = redelivery_s
        self._open: dict[str, PendingPairing] = {}
        self._decided: dict[str, str] = {}
        # request id -> (verdict, record, first delivered at)
        self._delivered: dict[str, tuple[str, PendingPairing, float]] = {}

    def open(self, code: str, device: str) -> PendingPairing | None:
        """Record a request, or `None` when too many are already waiting."""
        self._prune()
        if len(self._open) >= self._limit:
            return None
        record = PendingPairing(
            request_id=secrets.token_urlsafe(REQUEST_ID_BYTES),
            code=code,
            device=device,
            created_at=self._now(),
        )
        self._open[record.request_id] = record
        return record

    def describe(self, request_id: str) -> PendingPairing | None:
        """What this request is, for the screen that shows it. Read-only."""
        self._prune()
        return self._open.get(request_id)

    def approve(self, request_id: str) -> bool:
        """Record a person's approval. `False` when there is nothing to approve."""
        return self._decide(request_id, "approved")

    def deny(self, request_id: str) -> bool:
        """Record a person's refusal. `False` when there is nothing to refuse."""
        return self._decide(request_id, "denied")

    def take(self, request_id: str) -> tuple[str, PendingPairing | None]:
        """The verdict for [request_id].

        `pending` until a person decides; then `approved` or `denied`, repeated for the same id for
        [REDELIVERY_WINDOW_S] after the first delivery. Unknown, timed-out and long-delivered all
        answer `expired`, so the id cannot be probed.
        """
        self._prune()
        verdict = self._decided.pop(request_id, None)
        if verdict is not None:
            record = self._open.pop(request_id, None)
            if record is not None:
                self._delivered[request_id] = (verdict, record, self._now())
            return verdict, record
        delivered = self._delivered.get(request_id)
        if delivered is not None:
            return delivered[0], delivered[1]
        record = self._open.get(request_id)
        return ("pending", record) if record is not None else ("expired", None)

    def pending_count(self) -> int:
        """How many requests are awaiting a decision right now."""
        self._prune()
        return len(self._open)

    def _decide(self, request_id: str, verdict: str) -> bool:
        self._prune()
        if request_id not in self._open:
            return False
        self._decided[request_id] = verdict
        return True

    def _prune(self) -> None:
        """Drop everything that has run out of time, rather than let it pile up."""
        cutoff = self._now() - self._timeout_s
        stale = [rid for rid, record in self._open.items() if record.created_at <= cutoff]
        for request_id in stale:
            self._open.pop(request_id, None)
            self._decided.pop(request_id, None)
        expired = [
            rid
            for rid, (_verdict, _record, at) in self._delivered.items()
            if at <= self._now() - self._redelivery_s
        ]
        for request_id in expired:
            self._delivered.pop(request_id, None)


def instance_base_url(hass: HomeAssistant) -> str:
    """This instance's own base URL, derived from the always-registered pairing webhook, so it also
    works with no chargers at all.
    """
    return webhook_base_url(hass, PAIRING_WEBHOOK_ID)[0]


async def async_handle_pairing_webhook(
    hass: HomeAssistant, _webhook_id: str, request: web.Request
) -> web.Response:
    """The one endpoint of the handshake, refusing exactly as the charger one does."""
    try:
        payload: dict[str, Any] = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Payload must be a JSON object")
        if payload.get("version") != 1:
            raise ValueError("Unsupported payload version")
        action = payload.get("action")
        if action == "request":
            return _async_request(hass, payload)
        if action == "poll":
            return _async_poll(hass, payload)
        raise ValueError("Unsupported action")
    except (KeyError, TypeError, ValueError) as error:
        # The message only, never the payload: request ids and codes must not reach a log.
        _LOGGER.warning("Rejected SpotNav pairing request: %s", error)
        return web.json_response({"ok": False, "error": str(error)}, status=400)


def _async_request(hass: HomeAssistant, payload: dict[str, Any]) -> web.Response:
    """A phone asking to be paired: record it, and put it in front of a person."""
    code = payload.get("code")
    if not isinstance(code, str) or not _is_code(code):
        raise ValueError("A six-digit code is required")
    device = payload.get("device")
    # Cosmetic: a missing model must not refuse an otherwise valid attempt.
    device_label = device.strip() if isinstance(device, str) and device.strip() else "(unknown device)"

    register = domain_data(hass).pairing
    record = register.open(code, device_label)
    if record is None:
        _LOGGER.warning("Refused a SpotNav pairing request: too many awaiting approval")
        raise ValueError("Too many pairing requests are awaiting approval")

    # Surfaced in Settings -> Devices & Services; only the request id travels, the code stays in the register.
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_INTEGRATION_DISCOVERY,
                # Name the card after the phone and its code, which is meant to be compared.
                "title_placeholders": {"device": device_label, "code": code},
            },
            data={"request_id": record.request_id},
        )
    )
    return web.json_response({"ok": True, "request_id": record.request_id})


def _async_poll(hass: HomeAssistant, payload: dict[str, Any]) -> web.Response:
    """The phone asking what happened. One of four exact answers, never more."""
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("A request id is required")

    # Asked before the verdict is taken, so that "approved" is never delivered with an unusable
    # address. `async_generate_url` raises
    # while the instance URL is unknown (e.g. just after a restart); staying "pending" lets the
    # next poll succeed.
    base_url = instance_base_url(hass)
    if not base_url:
        return web.json_response({"status": "pending"})

    verdict, _record = domain_data(hass).pairing.take(request_id)
    if verdict == "pending":
        return web.json_response({"status": "pending"})
    if verdict == "denied":
        return web.json_response({"status": "denied"})
    if verdict == "approved":
        # Built now so a charger added in between is included; the pairing payload is the only
        # place a webhook id crosses to the app.
        return web.json_response(
            {
                "status": "approved",
                "url": base_url,
                "chargers": charger_pairing_payload(hass),
            }
        )
    return web.json_response({"status": "expired"})


def _is_code(code: str) -> bool:
    """Exactly six digits: what the app generates, and what a person compares."""
    return len(code) == CODE_LENGTH and code.isascii() and code.isdigit()


def async_register_pairing(hass: HomeAssistant) -> None:
    """Register the pairing endpoint and its register, once per instance.

    Not in `async_setup_entry` (the id is fixed and public), and never unregistered: an endpoint
    that vanishes when the last entry goes would be missing after the next one is added, and it
    grants nothing without an approval.
    """
    domain_data(hass).pairing = PairingRegister()
    webhook.async_register(
        hass,
        DOMAIN,
        "SpotNav pairing",
        PAIRING_WEBHOOK_ID,
        async_handle_pairing_webhook,
        local_only=False,
        allowed_methods=("POST",),
    )
