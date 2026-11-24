"""Instant notifications for the paired app: an empty wake-up through the SpotNav relay.

The app (opt-in) registers its Firebase token at the relay and gets an opaque `push_ref` back, which it
gives Home Assistant: an app from this release with `phone_notifications` (`api/notification_phones.py`), one from
before with `push_register` on each charger. The reference is its phone's (`phone_store.py`). When an event the phone
wants happens at a charger it follows, and the phone's route is its app (`phones.route_of`: the app shows that kind of
notice), `POST /v1/push/wake` carries only the `push_ref`; the relay sends Firebase an empty `{"t": "wake"}` and the app
runs its own check against this Home Assistant, as it does every 15 minutes. Nothing about the charge leaves Home
Assistant. A phone whose app is woken gets no Companion notification for the same notice.

A reference the relay answers is no longer registered (404 `unknown_ref`, 400 `invalid_ref`) is cleared from its phone,
which then falls to its Companion service from the next notice on. A transient failure is not made up for: the app's
own check every 15 minutes still finds what happened. The ref is a credential of sorts and is never logged nor put in
diagnostics.

Sending (per charger, `ChargerPush`): one request round at a time to every app-routed phone of the charger, no
retries; the same event is not woken for again within `REPEAT_S` and no more than `HOURLY_LIMIT` wake-ups go out an
hour (the relay limits as well). The site's notices wake their phones once each (`async_wake_refs`) under the site
notice's own limits.

The question which car is plugged in (`vehicle_identify`) has its own rules, since the app posts its own
notification for it: every new question wakes (`async_question`), and so does its end, however it ends
(`async_question_settled`), so the app can take that notification down. Neither is held back by the repeat
rule nor the hourly limit, though both count towards it; a question wakes at most twice, once asked and once
settled, and a plug-in asks at most once. Either one arriving while a wake-up is under way sends one more
after it, since the app's check under way may have read the question as it was.
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
from .settings import DEFAULT_EVENTS, EVENT_VEHICLE_IDENTIFY, NOTIFICATION_EVENTS

_LOGGER = logging.getLogger(__name__)

#: The deployed SpotNav relay: since 2.0 it carries push only (no prices). Injected so tests never reach it.
DEFAULT_BASE_URL: Final = "https://spotnav.sensnology.se"
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
#: At most this many wake-ups per charger in an hour (a limit of its own, apart from the notifier's `HOURLY_LIMIT`).
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


class ChargerPush:
    """One charger's wake-ups: to the paired apps of the phones that follow it (`phone_store.py`)."""

    def __init__(self, hass: HomeAssistant, entry_id: str, *, base_url: str = DEFAULT_BASE_URL) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._url = f"{base_url.rstrip('/')}{WAKE_PATH}"
        self._last_sent: dict[str, datetime] = {}
        self._sent_times: deque[datetime] = deque()
        self._in_flight = False
        #: A question which car is plugged in is open, and a wake-up is owed after the one under way.
        self._question_open = False
        self._follow_up = False
        self._last_result: str | None = None
        self._last_at: datetime | None = None

    async def async_load(self) -> None:
        """Nothing of its own to load: the phones hold the references (`phone_store.py`)."""

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The charger is gone for good: forget the registration the release before kept for it."""
        await Store(hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}").async_remove()

    def _phones(self) -> Any:
        from .phone_store import phone_store  # noqa: PLC0415 - the store imports this module's neighbours

        return phone_store(self._hass)

    def _refs(self, event: str) -> tuple[str, ...]:
        phones = self._phones()
        return () if phones is None else phones.recipients(event, charger=self._entry_id)[1]

    @property
    def registration(self) -> PushRegistration | None:
        """The first app that can be woken for this charger, as the release before kept one (its charger events)."""
        phones = self._phones()
        found = () if phones is None else phones.app_phones_of(self._entry_id)
        if not found or found[0].app is None or found[0].app.push_ref is None:
            return None
        phone = found[0]
        return PushRegistration(
            push_ref=phone.app.push_ref, events=tuple(event for event in phone.events if event in NOTIFICATION_EVENTS)
        )

    async def async_register(self, registration: PushRegistration | None) -> None:
        """An app from before this release registered on this charger (`push_register`), or cleared it with `None`."""
        self._last_sent.clear()
        phones = self._phones()
        if phones is None:
            return
        if registration is None:
            await phones.async_push_register(self._entry_id, None, ())
        else:
            await phones.async_push_register(self._entry_id, registration.push_ref, registration.events)

    def diagnostics(self) -> dict[str, Any]:
        """Whether an app can be woken for this charger and how the last wake-up went; never a ref."""
        phones = self._phones()
        found = () if phones is None else phones.app_phones_of(self._entry_id)
        wanted = {event for phone in found for event in phone.events}
        return {
            "registered": bool(found),
            "events": [event for event in NOTIFICATION_EVENTS if event in wanted] if found else None,
            "last_wake_result": self._last_result,
            "last_wake_at": None if self._last_at is None else self._last_at.isoformat(),
        }

    @callback
    def async_event(self, event: str, now: datetime, woken: set[str] | None = None) -> bool:
        """An event happened: wake the apps of this charger's phones that want it, if the limits allow. `woken`:
        references already woken for the same notice (left out here, and the ones woken added).

        Returns whether an app is being woken for it (or already is, for another event).
        """
        refs = tuple(ref for ref in self._refs(event) if woken is None or ref not in woken)
        if not refs:
            return False
        if self._in_flight:
            # The app's check reads every event at once; a wake-up under way covers this one.
            _LOGGER.debug("SpotNav charger %s: a wake-up is already under way", self._entry_id)
            return True
        last = self._last_sent.get(event)
        if last is not None and (now - last).total_seconds() < REPEAT_S:
            _LOGGER.debug("SpotNav charger %s: %s not woken for again so soon", self._entry_id, event)
            return False
        while self._sent_times and (now - self._sent_times[0]).total_seconds() >= 3600:
            self._sent_times.popleft()
        if len(self._sent_times) >= HOURLY_LIMIT:
            _LOGGER.debug("SpotNav charger %s: hourly wake-up limit reached", self._entry_id)
            return False
        self._last_sent[event] = now
        if woken is not None:
            woken.update(refs)
        self._start_wake(refs, now)
        return True

    @callback
    def async_question(self, now: datetime) -> bool:
        """A new question which car is plugged in: wake the apps that want `vehicle_identify`, whatever the repeat
        rule and the hourly limit. Returns whether an app is being woken for it."""
        self._question_open = True
        return self._question_wake(now)

    @callback
    def async_question_settled(self, now: datetime) -> bool:
        """The open question ended (answered anywhere, decided, swiped, unanswered, unplugged): wake the apps so they
        take their notification down, once per question. Returns whether an app is being woken."""
        if not self._question_open:
            return False
        self._question_open = False
        return self._question_wake(now)

    def _question_wake(self, now: datetime) -> bool:
        refs = self._refs(EVENT_VEHICLE_IDENTIFY)
        if not refs:
            return False
        if self._in_flight:
            self._follow_up = True
            return True
        self._start_wake(refs, now)
        return True

    def _start_wake(self, refs: tuple[str, ...], now: datetime) -> None:
        self._sent_times.append(now)
        self._in_flight = True
        self._hass.async_create_task(self._async_wake(refs), eager_start=True)

    async def _async_wake(self, refs: tuple[str, ...]) -> None:
        try:
            for ref in refs:
                await self._async_send(ref, KIND_WAKE)
        finally:
            self._in_flight = False
        if self._follow_up:
            self._follow_up = False
            again = self._refs(EVENT_VEHICLE_IDENTIFY)
            if again:
                self._start_wake(again, dt_util.utcnow())

    async def async_send_test(self) -> str | None:
        """Send this charger's apps a test push now (`spotnav.send_test_notification`): the result (the first that
        did not go, else `sent`), `None` with no app to wake. Counted against the hourly limit but not refused by it
        nor by the repeat rule: a person asked for it."""
        phones = self._phones()
        found = () if phones is None else phones.app_phones_of(self._entry_id)
        refs = tuple(phone.app.push_ref for phone in found if phone.app is not None and phone.app.push_ref is not None)
        if not refs:
            return None
        self._sent_times.append(dt_util.utcnow())
        results = [await self._async_send(ref, KIND_TEST) for ref in refs]
        return next((result for result in results if result != RESULT_SENT), RESULT_SENT)

    async def _async_send(self, push_ref: str, kind: str) -> str:
        result = await self._async_post(push_ref, kind)
        self._last_result = result
        self._last_at = dt_util.utcnow()
        if result == RESULT_UNKNOWN_REF:
            phones = self._phones()
            if phones is not None:
                _LOGGER.info(
                    "SpotNav charger %s: an app's instant notifications are no longer registered; dropped",
                    self._entry_id,
                )
                # The phone falls to its Companion service, if it has one, from the next notice on.
                await phones.async_drop_ref(push_ref)
        elif result != RESULT_SENT:
            _LOGGER.debug("SpotNav charger %s: %s push not sent: %s", self._entry_id, kind, result)
        return result

    async def _async_post(self, push_ref: str, kind: str) -> str:
        return await async_post(self._hass, self._url, push_ref, kind)


async def async_post(hass: HomeAssistant, url: str, push_ref: str, kind: str = KIND_WAKE) -> str:
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
            async with async_get_clientsession(hass).post(url, json=body) as response:
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


async def async_wake_refs(hass: HomeAssistant, refs: tuple[str, ...], *, base_url: str = DEFAULT_BASE_URL) -> None:
    """Wake these apps once each for a site's notice (the site notice keeps its own limits); a reference the relay no
    longer knows is dropped from its phone."""
    from .phone_store import phone_store  # noqa: PLC0415 - the store imports this module's neighbours

    url = f"{base_url.rstrip('/')}{WAKE_PATH}"
    for ref in refs:
        result = await async_post(hass, url, ref)
        if result == RESULT_UNKNOWN_REF:
            phones = phone_store(hass)
            if phones is not None:
                await phones.async_drop_ref(ref)
        elif result != RESULT_SENT:
            _LOGGER.debug("SpotNav: a site notice's wake-up was not sent: %s", result)
