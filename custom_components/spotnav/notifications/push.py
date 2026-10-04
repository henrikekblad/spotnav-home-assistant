"""Instant notifications for the paired app: an empty wake-up through the SpotNav relay.

The app (opt-in) registers its Firebase token at the relay and gets an opaque `push_ref` back, which it
gives this charger over the webhook (`push_register`) with the events it wants. When one of those events
happens here, `POST /v1/push/wake` carries only the `push_ref`; the relay sends Firebase an empty
`{"t": "wake"}` and the app runs its own check against this Home Assistant, as it does every 15 minutes.
Nothing about the charge leaves Home Assistant.

Per charger, like the notification settings: stored in the charger's own store, cleared by the app (a
`null` ref) or when the charger entry is removed, and dropped when the relay answers that the token is
no longer registered (404). Independent of the Companion app's notify targets. The ref is a credential
of sorts and is never logged nor put in diagnostics.

Sending: one request at a time, no retries; the same event is not woken for again within `REPEAT_S` and
no more than `HOURLY_LIMIT` wake-ups go out an hour (the relay limits as well).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from aiohttp import ClientError
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..pricing.price_repository import DEFAULT_BASE_URL
from .settings import DEFAULT_EVENTS, NOTIFICATION_EVENTS

_LOGGER = logging.getLogger(__name__)

#: The relay's wake-up endpoint and its contract version.
WAKE_PATH: Final = "/v1/push/wake"
WAKE_VERSION: Final = 1
#: What the app is woken for: an event (it runs its check) or a person's test (`send_test_notification`).
KIND_WAKE: Final = "wake"
KIND_TEST: Final = "test"
#: One request, bounded; never retried.
WAKE_TIMEOUT_S: Final = 10.0
#: The same event for the same charger is not woken for again within this long (as notifications).
REPEAT_S: Final = 15 * 60.0
#: At most this many wake-ups per charger in an hour.
HOURLY_LIMIT: Final = 12
#: A `push_ref` as the relay makes one: base64url text (padding allowed), bounded.
MAX_REF_LENGTH: Final = 512
_REF = re.compile(r"^[A-Za-z0-9_-]{16,512}={0,2}$")

_STORE_VERSION: Final = 1
_STORE_KEY_PREFIX: Final = f"{DOMAIN}.push"

#: The stable refusal code of a `push_register` body that cannot be stored.
ERROR_INVALID_PUSH: Final = "invalid_push_register"

#: What the last wake-up came to, as diagnostics show it.
RESULT_SENT: Final = "sent"
RESULT_UNKNOWN_REF: Final = "unknown_ref"
RESULT_RATE_LIMITED: Final = "rate_limited"
RESULT_PUSH_DISABLED: Final = "push_disabled"
RESULT_TIMEOUT: Final = "timeout"
RESULT_NETWORK: Final = "network"
#: The relay's 400 for a ref it cannot open (e.g. after its push key rotated).
_INVALID_REF: Final = "invalid_ref"


class PushRegisterError(ValueError):
    """A `push_register` body that cannot be stored; the message is for the log only."""


@dataclass(frozen=True, slots=True)
class PushRegistration:
    """The app's wake-up address at the relay and the events it wants to be woken for."""

    push_ref: str
    events: tuple[str, ...] = DEFAULT_EVENTS

    def as_dict(self) -> dict[str, Any]:
        return {"push_ref": self.push_ref, "events": list(self.events)}


def _events_of(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise PushRegisterError("events must be a list")
    for event in raw:
        if event not in NOTIFICATION_EVENTS:
            raise PushRegisterError("an event must be one of the notification events")
    if len(set(raw)) != len(raw):
        raise PushRegisterError("an event must not repeat")
    return tuple(event for event in NOTIFICATION_EVENTS if event in raw)


def parse_push_register(payload: dict[str, Any]) -> PushRegistration | None:
    """The registration a webhook body asks for, `None` to clear, or a refusal.

    `push_ref` is required: base64url text of at most `MAX_REF_LENGTH` characters, or `null`. `events`
    is optional (the default events the app's own check uses) and ignored with a `null` ref.
    """
    if "push_ref" not in payload:
        raise PushRegisterError("push_ref is required")
    ref = payload["push_ref"]
    if ref is None:
        return None
    if not isinstance(ref, str) or len(ref) > MAX_REF_LENGTH or not _REF.match(ref):
        raise PushRegisterError("push_ref must be short base64url text")
    events = _events_of(payload["events"]) if "events" in payload else DEFAULT_EVENTS
    return PushRegistration(push_ref=ref, events=events)


def _stored(raw: Any) -> PushRegistration | None:
    """A stored record, or `None` for anything that is not one (never a failed setup)."""
    if not isinstance(raw, dict):
        return None
    try:
        return parse_push_register(raw)
    except PushRegisterError:
        return None


class ChargerPush:
    """One charger's wake-up registration and the sending of wake-ups to it."""

    def __init__(self, hass: HomeAssistant, entry_id: str, *, base_url: str = DEFAULT_BASE_URL) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._url = f"{base_url.rstrip('/')}{WAKE_PATH}"
        self._store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}")
        self._registration: PushRegistration | None = None
        self._last_sent: dict[str, datetime] = {}
        self._sent_times: deque[datetime] = deque()
        self._in_flight = False
        self._last_result: str | None = None
        self._last_at: datetime | None = None

    async def async_load(self) -> None:
        self._registration = _stored(await self._store.async_load())

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The charger is gone for good: forget its registration."""
        await Store(hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}").async_remove()

    @property
    def registration(self) -> PushRegistration | None:
        return self._registration

    async def async_register(self, registration: PushRegistration | None) -> None:
        """Store the app's registration, or clear it with `None`."""
        self._registration = registration
        self._last_sent.clear()
        if registration is None:
            await self._store.async_remove()
        else:
            await self._store.async_save(registration.as_dict())

    def diagnostics(self) -> dict[str, Any]:
        """Whether a phone is registered and how the last wake-up went; never the ref."""
        registration = self._registration
        return {
            "registered": registration is not None,
            "events": None if registration is None else list(registration.events),
            "last_wake_result": self._last_result,
            "last_wake_at": None if self._last_at is None else self._last_at.isoformat(),
        }

    @callback
    def async_event(self, event: str, now: datetime) -> None:
        """An event happened: wake the app if it asked for this one and the limits allow."""
        registration = self._registration
        if registration is None or event not in registration.events:
            return
        if self._in_flight:
            # The app's check reads every event at once; a wake-up under way covers this one.
            _LOGGER.debug("SpotNav charger %s: a wake-up is already under way", self._entry_id)
            return
        last = self._last_sent.get(event)
        if last is not None and (now - last).total_seconds() < REPEAT_S:
            _LOGGER.debug("SpotNav charger %s: %s not woken for again so soon", self._entry_id, event)
            return
        while self._sent_times and (now - self._sent_times[0]).total_seconds() >= 3600:
            self._sent_times.popleft()
        if len(self._sent_times) >= HOURLY_LIMIT:
            _LOGGER.debug("SpotNav charger %s: hourly wake-up limit reached", self._entry_id)
            return
        self._last_sent[event] = now
        self._sent_times.append(now)
        self._in_flight = True
        self._hass.async_create_task(self._async_wake(registration.push_ref), eager_start=True)

    async def _async_wake(self, push_ref: str) -> None:
        try:
            await self._async_send(push_ref, KIND_WAKE)
        finally:
            self._in_flight = False

    async def async_send_test(self) -> str | None:
        """Send the app a test push now (`spotnav.send_test_notification`): the result, `None` with no
        registration. Counted against the hourly limit but not refused by it nor by the repeat rule: a
        person asked for it."""
        registration = self._registration
        if registration is None:
            return None
        self._sent_times.append(dt_util.utcnow())
        return await self._async_send(registration.push_ref, KIND_TEST)

    async def _async_send(self, push_ref: str, kind: str) -> str:
        result = await self._async_post(push_ref, kind)
        self._last_result = result
        self._last_at = dt_util.utcnow()
        if result == RESULT_UNKNOWN_REF:
            registration = self._registration
            if registration is not None and registration.push_ref == push_ref:
                _LOGGER.info(
                    "SpotNav charger %s: the app's instant notifications are no longer registered; dropped",
                    self._entry_id,
                )
                await self.async_register(None)
        elif result != RESULT_SENT:
            _LOGGER.debug("SpotNav charger %s: %s push not sent: %s", self._entry_id, kind, result)
        return result

    async def _async_post(self, push_ref: str, kind: str) -> str:
        """One request to the relay; the result code, never the ref.

        A 404 drops the registration only when the relay says the token is unknown (`unknown_ref`), not
        for any 404 a proxy might answer; a 400 `invalid_ref` (a ref sealed under a rotated key) drops it
        too.
        """
        body: dict[str, Any] = {"v": WAKE_VERSION, "push_ref": push_ref}
        if kind != KIND_WAKE:
            body["kind"] = kind
        error: Any = None
        try:
            async with asyncio.timeout(WAKE_TIMEOUT_S):
                async with async_get_clientsession(self._hass).post(self._url, json=body) as response:
                    status = response.status
                    if status in (400, 404):
                        try:
                            answer = await response.json(content_type=None)
                        except ValueError:
                            answer = None
                        error = answer.get("error") if isinstance(answer, dict) else None
        except TimeoutError:
            return RESULT_TIMEOUT
        except ClientError:
            return RESULT_NETWORK
        if status == 200:
            return RESULT_SENT
        if (status == 404 and error == RESULT_UNKNOWN_REF) or (status == 400 and error == _INVALID_REF):
            # Unregistered at Firebase, or a ref the relay can no longer open (its push key rotated):
            # it will never work again, so it is dropped and the app registers anew.
            return RESULT_UNKNOWN_REF
        if status == 429:
            return RESULT_RATE_LIMITED
        if status == 503:
            return RESULT_PUSH_DISABLED
        return f"http_{status}"
