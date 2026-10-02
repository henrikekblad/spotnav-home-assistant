"""Easee: its own `action_command` service starts and stops, `set_charger_dynamic_limit` sets the
current, and the charger's statuses, pause and plug-in behaviour need the handling below.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ...const import CURRENT_CONTROL_EASEE, DEFAULT_MIN_CURRENT_A
from ..charger_profiles import PATH_EASEE, WritePolicy
from .base import (
    _finite,
    ASSIGN_ASSIGNED,
    ASSIGN_BELOW_MINIMUM,
    ASSIGN_IGNORED_WHILE_PAUSED,
    ASSIGN_RATE_LIMITED,
    ASSIGN_UNCHANGED,
    ASSIGN_UNCONFIRMED,
    ASSIGN_WRITE_FAILED,
    current_description,
    CurrentPath,
    path_description,
    StartStopPath,
    WRITE_REGULATOR,
    WRITE_RESEND,
    WRITE_SESSION_START,
    WriteRateLimiter,
)
from .registry import ChargerContext, register_current, register_start_stop

_ADAPTER_LOGGER = logging.getLogger(__name__)

_EASEE_DOMAIN: Final = "easee"
#: Easee's dynamic limit, sent with no expiry: if Home Assistant goes away the charger keeps the
#: last (lower) limit, the safe side for a fuse. The charger clears it on plug-in and reboot itself.
EASEE_TIME_TO_LIVE_MIN: Final = 0
#: How long to wait before reading an Easee limit back to confirm it (its push is asynchronous).
EASEE_READBACK_DELAY_S: Final = 2.0
#: A read-back that still disagrees after this long says the service silently did nothing.
EASEE_CONFIRM_AFTER_S: Final = 20.0
#: Easee statuses in which no cable is connected, so a move out of one is a plug-in.
_EASEE_DISCONNECTED: Final = frozenset({"disconnected", "offline", STATE_UNAVAILABLE, STATE_UNKNOWN, ""})
#: Easee statuses in which a `start` (authorize) is what the charger waits for.
_EASEE_AWAITING_AUTHORIZATION: Final = frozenset({"awaiting_authorization", "authenticating"})
#: The status in which an authorized charger that was not paused by us waits for a start, and in which
#: the status sensor's `config_authorizationRequired` attribute says a `start` is still owed.
_EASEE_AWAITING_START: Final = "awaiting_start"
_EASEE_AUTHORIZATION_ATTRIBUTE: Final = "config_authorizationrequired"
#: The disabled-by-default diagnostic sensor that carries the dynamic limit (easee_hass
#: `dynamic_charger_limit`); its state is the only read-back there is.
EASEE_LIMIT_SENSOR_KEY: Final = "dynamic_charger_limit"
#: After a charge was started or re-sent at the floor, the regulator may not take it lower for this
#: long: a fast 7 A to 6 A drop aborts charges on slow cars (evcc#33963).
EASEE_START_HOLD_S: Final = 60.0


class EaseeCommandPath(StartStopPath):
    """`easee.action_command`: `pause` and `resume` start and stop a charge, `start` only authorizes.

    Easee's own labels are "Authorize (start) charging" and "Deauthorize (stop) charging": they
    matter only while the charger waits for an authorization. On a charger that does not need one,
    `start` does nothing visible, which is why a Start sent that way was never acknowledged. What
    actually holds a charge is `pause_charging` (it keeps the authorization and limits the dynamic
    charger current to 0) and `resume_charging` (it lifts that again). Easee's `is_enabled` switch is
    a stored charger setting and is never written.

    The path remembers that it has paused the charger (`paused`): the status alone cannot say, a
    paused charger reports `awaiting_start` exactly as one that was never started does. A dynamic
    limit written meanwhile would raise the paused charger's current and resume it behind our back.
    After a restart the memory is gone: a dynamic limit that reads back as 0 A (when the limit sensor
    is enabled) is a pause all the same.
    """

    kind = PATH_EASEE

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        *,
        limiter: WriteRateLimiter | None = None,
        status: Callable[[], str | None] = lambda: None,
        authorization_required: Callable[[], bool | None] = lambda: None,
        read_back: Callable[[], int | None] = lambda: None,
    ) -> None:
        super().__init__(hass)
        self.device_id = device_id
        self._limiter = limiter
        self._status = status
        self._authorization_required = authorization_required
        self._read_back = read_back
        #: `True` after a pause of ours, `False` after a resume, `None` when nothing is known (a
        #: restart, a plug-in).
        self.paused: bool | None = None

    def is_paused(self) -> bool:
        """Whether a dynamic limit must not be written now: the charger was paused by us and
        nothing since has said otherwise. Unknown counts as not paused, except that a limit the
        charger reads back as 0 A is a pause (after a restart, before the first `resume`): after a
        plug-in the charger sits in `awaiting_start` and is owed its limit
        (`EaseeDynamicLimit.needs_resend`).
        """
        status = self._status()
        if status is not None:
            if status in _EASEE_DISCONNECTED:
                self.paused = None  # a plug-in clears Easee's dynamic limit and with it the pause
                return False
            if status == "charging":
                self.paused = False  # someone resumed it; what the charger does is the truth
        if self.paused is None and self._read_back() == 0:
            return True
        return bool(self.paused)

    def forget_pause(self) -> None:
        """A plug-in cleared the charger's limit and our pause with it: nothing is paused now, whatever
        a stale read-back of the old limit still says.
        """
        self.paused = False

    def _start_owed(self) -> bool:
        """Whether a `start` (authorize) must precede the `resume`: the charger says it waits for an
        authorization, or the status sensor says authorization is required and the charger sits in
        `awaiting_start` without a pause of ours to explain it.
        """
        status = self._status()
        if status in _EASEE_AWAITING_AUTHORIZATION:
            return True
        return (
            status == _EASEE_AWAITING_START
            and self.paused is not True
            and self._authorization_required() is True
        )

    async def _command(self, action: str) -> None:
        # Counted against the settings budget, never refused by it: a stop is a safety action.
        if self._limiter is not None:
            self._limiter.record()
        await self.hass.services.async_call(
            _EASEE_DOMAIN,
            "action_command",
            {"device_id": self.device_id, "action_command": action},
            blocking=True,
        )

    async def async_start(self) -> bool:
        if self._start_owed():
            await self._command("start")
        await self._command("resume")
        self.paused = False
        return True

    async def async_stop(self) -> bool:
        # Never `stop`: deauthorizing would make the next Start wait for an authorization again.
        await self._command("pause")
        self.paused = True
        return True

    def enabled_state(self) -> None:
        return None

    def entity_ids(self) -> tuple[str, ...]:
        return ()

    def describe(self) -> dict[str, Any]:
        return path_description(self.kind)


def _easee_authorization_required(hass: HomeAssistant, status_entity_id: str | None) -> bool | None:
    """The status sensor's `config.authorizationRequired` attribute, or `None` when it has none."""
    state = hass.states.get(status_entity_id) if status_entity_id else None
    if state is None:
        return None
    for key, value in state.attributes.items():
        if str(key).lower() == _EASEE_AUTHORIZATION_ATTRIBUTE and isinstance(value, bool):
            return value
    return None


class EaseeDynamicLimit(CurrentPath):
    """`easee.set_charger_dynamic_limit`, re-sent after plug-in and reboot, never the max-limit
    services (those are flash).

    Easee's services log and return instead of raising, and skip a call whose value equals the one
    they cached, so a write is read back from the state of the charger's `dynamic_charger_limit`
    sensor (diagnostic, disabled by default; the status sensor has no such attribute): a value that
    still disagrees after `EASEE_CONFIRM_AFTER_S` marks the cache suspect, and the next write first
    sends one amp lower (the safe direction, never below the start minimum) so the service cannot
    skip it. Without that sensor nothing can be read back: a write is then unverifiable, never
    failed, and the regulator loop does not send the same value again and again.

    A charge starts, and a limit is re-sent after a plug-in, at `min_start_a` or more (7 A; the
    firmware delays a 6 A start by about five minutes). The regulator may still go down to 6 A, but
    not within `EASEE_START_HOLD_S` of such a write.
    """

    kind = "service"
    can_set = True

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        policy: WritePolicy,
        *,
        status_entity_id: str | None,
        limiter: WriteRateLimiter,
        now: Callable[[], datetime] = dt_util.utcnow,
        paused: Callable[[], bool] = lambda: False,
        on_plug_in: Callable[[], None] = lambda: None,
        min_start_a: float = DEFAULT_MIN_CURRENT_A,
    ) -> None:
        super().__init__(policy)
        self._min_start_a = max(DEFAULT_MIN_CURRENT_A, min_start_a)
        self._floor_written_at: datetime | None = None
        self._paused = paused
        self._on_plug_in = on_plug_in
        self.hass = hass
        self.device_id = device_id
        self.status_entity_id = status_entity_id
        self._limiter = limiter
        self._now = now
        self._written_at: datetime | None = None
        self._suspect = False

    def limit_entity_id(self) -> str | None:
        """The charger's enabled `dynamic_charger_limit` sensor, or `None` when it has none (it is
        disabled by default). Found on the charger's device by translation key or unique-id suffix.
        """
        registry = er.async_get(self.hass)
        for entry in er.async_entries_for_device(registry, self.device_id):
            if entry.domain != "sensor" or entry.platform != _EASEE_DOMAIN:
                continue
            if (entry.translation_key or "").lower() == EASEE_LIMIT_SENSOR_KEY or str(
                entry.unique_id
            ).lower().endswith(f"_{EASEE_LIMIT_SENSOR_KEY}"):
                return entry.entity_id
        return None

    def read_back_a(self) -> int | None:
        """The dynamic limit the charger reports, from the state of its limit sensor; `None` when
        there is no enabled sensor or it reads nothing (unavailable, unknown).
        """
        entity_id = self.limit_entity_id()
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        number = _finite(state.state) if state is not None else None
        return None if number is None else int(math.floor(number + 1e-9))

    def setpoint_a(self) -> int | None:
        read = self.read_back_a()
        return read if read is not None else self.last_written_a

    def wait_s(self) -> float:
        return self._limiter.wait_s()

    def _note_confirmation(self) -> None:
        """Mark the cache suspect when an old write is still not what the charger reports."""
        if self.last_written_a is None or self._written_at is None:
            return
        read = self.read_back_a()
        if read is None:
            return
        old = (self._now() - self._written_at).total_seconds() >= EASEE_CONFIRM_AFTER_S
        self._suspect = old and read != self.last_written_a

    async def _send(self, amps: int) -> bool:
        self._limiter.record()
        try:
            await self.hass.services.async_call(
                _EASEE_DOMAIN,
                "set_charger_dynamic_limit",
                {
                    "device_id": self.device_id,
                    "current": amps,
                    "time_to_live": EASEE_TIME_TO_LIVE_MIN,
                },
                blocking=True,
            )
        except Exception:
            _ADAPTER_LOGGER.warning(
                "Could not set the charger's current through Easee; "
                "the request is recorded but not applied"
            )
            return False
        return True

    async def async_set(self, amps: int, *, reason: str, verify: bool = False) -> str:
        if amps < DEFAULT_MIN_CURRENT_A:
            return ASSIGN_BELOW_MINIMUM
        if self._paused():
            # A limit above 0 would lift the pause; the Start that follows sends it again.
            return ASSIGN_IGNORED_WHILE_PAUSED
        floor = math.ceil(self._min_start_a - 1e-9)
        if reason in (WRITE_SESSION_START, WRITE_RESEND):
            amps = max(amps, floor)  # never below the start minimum; never lowered either
        elif reason == WRITE_REGULATOR and amps < floor and self._holding_start_floor():
            # Held, not applied: the next pass asks again. A fuse that cannot wait is a stop.
            return ASSIGN_RATE_LIMITED
        self._note_confirmation()
        read = self.read_back_a()
        if reason != WRITE_RESEND and read == amps:
            # Only what the charger itself reports counts: a remembered write may have been cleared
            # by a plug-in or a reboot since.
            return ASSIGN_UNCHANGED
        if read is None and reason == WRITE_REGULATOR and self.last_written_a == amps:
            # Nothing can be read back, so the regulator loop must not spend the write budget on
            # the same value every pass; a start, a restore or a plug-in resend still sends it.
            return ASSIGN_UNCHANGED
        # Two calls in one write when the cache is suspect, so the budget must cover both. The amp
        # lower is never below the start minimum: a 6 A write first would bring the delay back.
        needed = (
            2
            if (self._suspect or reason == WRITE_RESEND) and amps - 1 >= max(DEFAULT_MIN_CURRENT_A, floor)
            else 1
        )
        if self._limiter.wait_s() > 0 or not self._budget_allows(needed):
            return ASSIGN_RATE_LIMITED
        if needed == 2 and not await self._send(amps - 1):
            return ASSIGN_WRITE_FAILED
        if not await self._send(amps):
            return ASSIGN_WRITE_FAILED
        self.last_written_a = amps
        self._written_at = self._now()
        self._suspect = False
        if reason in (WRITE_SESSION_START, WRITE_RESEND) and amps <= floor:
            self._floor_written_at = self._written_at
        if verify and self.limit_entity_id() is not None:
            await asyncio.sleep(EASEE_READBACK_DELAY_S)
            read = self.read_back_a()
            # A sensor that reads nothing cannot confirm and cannot deny: unverifiable is not failed.
            if read is not None and read != amps:
                return ASSIGN_UNCONFIRMED
        return ASSIGN_ASSIGNED

    def _holding_start_floor(self) -> bool:
        written = self._floor_written_at
        return written is not None and (self._now() - written).total_seconds() < EASEE_START_HOLD_S

    def _budget_allows(self, count: int) -> bool:
        limit = self.policy.max_writes_per_minute
        if limit is None:
            return True
        window = self._limiter._window  # noqa: SLF001 - the limiter is this path's own collaborator
        now = self._now()
        recent = sum(1 for moment in window if (now - moment).total_seconds() < 60.0)
        return recent + count <= limit

    def needs_resend(self, old_status: str | None, new_status: str | None) -> bool:
        """Whether a status change is a plug-in or a reboot, after which the limit is gone."""
        if not self.policy.resend_after_plug_in:
            return False
        old = (old_status or "").strip().lower()
        new = (new_status or "").strip().lower()
        plugged_in = old in _EASEE_DISCONNECTED and new not in _EASEE_DISCONNECTED
        if plugged_in:
            self._on_plug_in()  # the charger cleared its limit, and our pause with it
        return plugged_in

    def describe(self) -> dict[str, Any]:
        return current_description(self.kind, service=f"{_EASEE_DOMAIN}.set_charger_dynamic_limit")


def _easee_path(context: ChargerContext) -> StartStopPath | None:
    device_id = context.raw_path.get("device_id")
    if not device_id:
        return None
    hass = context.hass
    return EaseeCommandPath(
        hass,
        str(device_id),
        limiter=context.limiter,
        status=context.status,
        authorization_required=lambda: _easee_authorization_required(hass, context.status_entity_id),
        read_back=context.read_back,
    )


def _easee_current(context: ChargerContext) -> CurrentPath | None:
    path = context.path
    if not isinstance(path, EaseeCommandPath):
        return None
    return EaseeDynamicLimit(
        context.hass,
        path.device_id,
        context.policy,
        status_entity_id=context.status_entity_id,
        limiter=context.limiter,
        now=context.now,
        paused=path.is_paused,
        on_plug_in=path.forget_pause,
        min_start_a=(context.profile.min_start_current_a if context.profile is not None else None)
        or DEFAULT_MIN_CURRENT_A,
    )


register_start_stop(PATH_EASEE, _easee_path)
register_current(CURRENT_CONTROL_EASEE, _easee_current)
