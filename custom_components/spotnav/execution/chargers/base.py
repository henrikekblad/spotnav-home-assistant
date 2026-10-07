"""What every charger path shares: the outcome codes, the rate limiter, the two path interfaces
(`StartStopPath`, `CurrentPath`), `AdapterCapabilities` and the small helpers the paths read states with.

Nothing here knows a vendor. A vendor's behaviour lives in its own module beside this one and
registers itself in `registry.py`.
"""

from __future__ import annotations

import asyncio
import logging
import math
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from ..charger_profiles import WritePolicy

_ADAPTER_LOGGER = logging.getLogger(__name__)

# Why a write was not made, or what became of it: stable codes, never prose.
ASSIGN_ASSIGNED: Final = "assigned"
ASSIGN_PROBE_IN_FLIGHT: Final = "probe_in_flight"
ASSIGN_BELOW_MINIMUM: Final = "below_minimum"
ASSIGN_NO_TARGET: Final = "no_connector_target"
ASSIGN_READ_FAILED: Final = "assigned_current_unreadable"
ASSIGN_WRITE_FAILED: Final = "write_failed"
ASSIGN_UNCONFIRMED: Final = "unconfirmed"
#: The charger already carries the value; nothing was sent.
ASSIGN_UNCHANGED: Final = "unchanged"
#: The write policy's minimum interval (or per-minute budget) has not elapsed.
ASSIGN_RATE_LIMITED: Final = "rate_limited"
#: The setting is stored in flash: never written from the regulator loop.
ASSIGN_FLASH_GUARD: Final = "flash_guard"
#: A write while paused would only be stored (Peblar), so none is made.
ASSIGN_IGNORED_WHILE_PAUSED: Final = "ignored_while_paused"
#: The number caps every charger of an installation that has more than one.
ASSIGN_INSTALLATION_SHARED: Final = "installation_shared"
#: Another system writes the same installation-wide limit (Perific balancing a Zaptec installation),
#: so SpotNav starts and stops only.
ASSIGN_EXTERNAL_BALANCER: Final = "external_balancer"
#: This charger has no way to take a current.
ASSIGN_UNSUPPORTED: Final = "unsupported"
#: The number's unit is neither A nor mA, so its value is never assumed to be amperes.
ASSIGN_UNIT_UNKNOWN: Final = "unit_unknown"
#: The entity is unavailable or gone.
ASSIGN_TARGET_UNAVAILABLE: Final = "target_unavailable"
#: The charge point stored the value but needs a reboot before it applies it.
ASSIGN_REBOOT_REQUIRED: Final = "reboot_required"

#: Outcomes in which the charger now carries the requested current.
IN_EFFECT_OUTCOMES: Final = frozenset({ASSIGN_ASSIGNED, ASSIGN_UNCONFIRMED, ASSIGN_UNCHANGED})
#: Outcomes in which a write was refused by policy or capability, before anything was sent.
REFUSED_OUTCOMES: Final = frozenset(
    {
        ASSIGN_RATE_LIMITED,
        ASSIGN_FLASH_GUARD,
        ASSIGN_IGNORED_WHILE_PAUSED,
        ASSIGN_INSTALLATION_SHARED,
        ASSIGN_EXTERNAL_BALANCER,
        ASSIGN_UNSUPPORTED,
        ASSIGN_UNIT_UNKNOWN,
        ASSIGN_TARGET_UNAVAILABLE,
        ASSIGN_BELOW_MINIMUM,
    }
)

# Why a current is being written: a session start (or a manual one), the regulator loop, the
# restore after active control is turned off, a re-send after the charger forgot its limit, or the sun's own
# modulation on a site where the regulator does not write it.
WRITE_SESSION_START: Final = "session_start"
WRITE_REGULATOR: Final = "regulator"
WRITE_RESTORE: Final = "restore"
WRITE_RESEND: Final = "resend"
WRITE_SOLAR: Final = "solar"
#: The writes that modulate a running charge, again and again: each obeys a charger's policy for repeated writes
#: (no flash-stored setting, the Easee's start floor held, the same value not sent twice) and never lands while a
#: stop is on its way.
MODULATION_WRITES: Final = frozenset({WRITE_REGULATOR, WRITE_SOLAR})

_AMPERE_UNITS: Final = {"a": 1.0, "amp": 1.0, "amps": 1.0, "ampere": 1.0, "amperes": 1.0, "ma": 1000.0}


#: Data keys never recorded in the command log, whatever service carries them.
_SECRET_KEYS: Final = frozenset(
    {"token", "access_token", "refresh_token", "api_key", "password", "secret", "webhook_id", "devid", "charge_point_id"}
)

#: The calls a running command makes, collected by `call_service` for the adapter's command log. `None`
#: outside a command (a read, a restore), where nothing is recorded.
_CALL_TAP: ContextVar[list[dict[str, Any]] | None] = ContextVar("spotnav_charger_call_tap", default=None)


#: How long a charger service call may take when no command says otherwise.
DEFAULT_CALL_TIMEOUT_S: Final = 30.0
_CALL_TIMEOUT: ContextVar[float] = ContextVar("spotnav_charger_call_timeout", default=DEFAULT_CALL_TIMEOUT_S)


def start_call_tap(timeout_s: float = DEFAULT_CALL_TIMEOUT_S) -> tuple[list[dict[str, Any]], Any]:
    """Begin collecting the service calls of one command, each given up on after `timeout_s`;
    returns the list and the reset token.
    """
    calls: list[dict[str, Any]] = []
    return calls, (_CALL_TAP.set(calls), _CALL_TIMEOUT.set(timeout_s))


def end_call_tap(token: Any) -> None:
    tap_token, timeout_token = token
    _CALL_TIMEOUT.reset(timeout_token)
    _CALL_TAP.reset(tap_token)


def loggable_data(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """A service call's data for the log: entity and device ids and plain values stay, secrets and the
    OCPP charge point id (`devid`) are replaced.
    """
    return {
        str(key): ("**REDACTED**" if str(key).lower() in _SECRET_KEYS else value)
        for key, value in (data or {}).items()
    }


async def call_service(
    hass: HomeAssistant,
    domain: str,
    service: str,
    data: Mapping[str, Any] | None = None,
    *,
    blocking: bool = True,
    return_response: bool = False,
) -> Any:
    """`hass.services.async_call`, noted in the running command's call list (see `CommandLog`).

    Given up on after the command's timeout (30 s, 45 s for a known cloud integration): a call that
    never answers raises `TimeoutError`, which the command log records as an error, so it can never
    hold a controller's lock for good.
    """
    tap = _CALL_TAP.get()
    if tap is not None:
        tap.append({"service": f"{domain}.{service}", "data": loggable_data(data)})
    async with asyncio.timeout(_CALL_TIMEOUT.get()):
        if return_response:
            return await hass.services.async_call(
                domain, service, data, blocking=blocking, return_response=True
            )
        return await hass.services.async_call(domain, service, data, blocking=blocking)


def _lower(state: State | None) -> str | None:
    """A state's text lower-cased, or `None` for missing, unavailable or unknown."""
    if state is None or not isinstance(state.state, str):
        return None
    if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    return state.state.strip().lower()


def _entity_unavailable(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether a service call on this entity would be skipped: Home Assistant drops an unavailable
    (or missing) target and only logs it, so a command to it would be reported done though nothing
    was sent.
    """
    state = hass.states.get(entity_id)
    return state is None or state.state == STATE_UNAVAILABLE


def _not_executed(entity_id: str) -> bool:
    _ADAPTER_LOGGER.warning(
        "The charger's %s is unavailable, so the command was not sent", entity_id.split(".", 1)[0]
    )
    return False


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _amps_factor(state: State | None) -> float | None:
    """How many of the entity's own units make one ampere (1 for A, 1000 for mA), or `None`."""
    if state is None:
        return None
    unit = state.attributes.get("unit_of_measurement")
    if not unit:
        return None
    return _AMPERE_UNITS.get(str(unit).strip().lower())


class WriteRateLimiter:
    """The policy's time budget for current writes: a minimum interval and a per-minute window.

    Never sleeps: `wait_s()` says how long until a write is allowed, `record()` notes one sent. The
    controller refuses a write that would come early, so the charger is never written faster than its
    policy, whatever asks.
    """

    def __init__(self, policy: WritePolicy, now: Callable[[], datetime] = dt_util.utcnow) -> None:
        self._policy = policy
        self._now = now
        self._last: datetime | None = None
        self._window: deque[datetime] = deque()

    def wait_s(self) -> float:
        now = self._now()
        wait = 0.0
        if self._last is not None and self._policy.min_interval_s > 0:
            wait = max(wait, self._policy.min_interval_s - (now - self._last).total_seconds())
        limit = self._policy.max_writes_per_minute
        if limit is not None:
            while self._window and (now - self._window[0]).total_seconds() >= 60.0:
                self._window.popleft()
            if len(self._window) >= limit:
                wait = max(wait, 60.0 - (now - self._window[0]).total_seconds())
        return max(0.0, wait)

    def record(self) -> None:
        now = self._now()
        self._last = now
        if self._policy.max_writes_per_minute is not None:
            self._window.append(now)

    @property
    def last_write_at(self) -> datetime | None:
        return self._last


# --- start/stop paths ----------------------------------------------------------------------------


def path_description(
    kind: str,
    *,
    entity_ids: tuple[str, ...] = (),
    inverted: bool = False,
    start_option: str | None = None,
    stop_option: str | None = None,
) -> dict[str, Any]:
    """A start/stop path as the entity configuration reports it: the same keys for every kind."""
    return {
        "kind": kind,
        "entity_ids": list(entity_ids),
        "inverted": inverted,
        "start_option": start_option,
        "stop_option": stop_option,
    }


def current_description(kind: str, *, entity_id: str | None = None, service: str | None = None) -> dict[str, Any]:
    """A current path as the entity configuration reports it: the same keys for every kind."""
    return {"kind": kind, "entity_id": entity_id, "service": service}


class StartStopPath(ABC):
    """How a charge is started and stopped, and whether the charger is enabled as commanded."""

    kind: str

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @abstractmethod
    async def async_start(self, amps: int | None = None) -> bool:
        """Send the start; `False` when it was not executed (the entity is unavailable and Home
        Assistant would have skipped the call), so nothing may claim a start that never happened.

        `amps` is the current the charge is requested at, when known: a path whose start is itself
        a current write (ABB's pause by 0 A) needs it; every other ignores it.
        """

    @abstractmethod
    async def async_stop(self) -> bool:
        """Send the stop; `False` when it was not executed, as for `async_start`."""

    @abstractmethod
    def enabled_state(self) -> bool | None:
        """Whether the charger is enabled as commanded: `True` (started), `False` (stopped) or
        `None` when this path cannot tell (a button, a neutral select). Never a guess.
        """

    @abstractmethod
    def entity_ids(self) -> tuple[str, ...]:
        """The entities whose reports change `enabled_state`."""

    def memory(self) -> dict[str, Any]:
        """What this path remembers that a restart must not forget (plain JSON values); none by default."""
        return {}

    def restore_memory(self, memory: dict[str, Any]) -> None:
        """Take back what `memory` gave, after a restart. Storage is untrusted: a wrong kind reads as absent."""
        del memory  # a path that remembers nothing takes nothing back

    @abstractmethod
    def describe(self) -> dict[str, Any]: ...

    def describe_state(self) -> dict[str, Any]:
        """What the path itself remembers or reads that decides what a start does (diagnostics only;
        never an entity id or a secret). `{}` for a path with no state of its own.
        """
        return {}


# --- current paths -------------------------------------------------------------------------------


class CurrentPath(ABC):
    """How a current is set, if it can be."""

    kind: str = "none"
    can_set: bool = False

    def __init__(self, policy: WritePolicy) -> None:
        self.policy = policy
        self.last_written_a: int | None = None

    @abstractmethod
    async def async_set(self, amps: int, *, reason: str, verify: bool = False) -> str:
        """Write `amps` if the policy allows and return the outcome code. Called with the
        controller's `_assign_lock` held.
        """

    def setpoint_a(self) -> int | None:
        """What the charger is set to, as far as it can be known."""
        return self.last_written_a

    def read_back_a(self) -> int | None:
        """The limit the charger itself reports, when it can be read back; `None` otherwise."""
        return None

    def available(self) -> str | None:
        """`None` while this path can write, else the code saying why it cannot right now."""
        return None

    def wait_s(self) -> float:
        return 0.0

    def retry_entity_ids(self) -> tuple[str, ...]:
        """Entities whose coming back is a reason to write a current that found its target
        unavailable at the session start (a number that exists only while a session runs).
        """
        return ()

    def describe(self) -> dict[str, Any]:
        return current_description(self.kind)

    def describe_state(self) -> dict[str, Any]:
        """What this path remembers or reads that decides what a write does (diagnostics only)."""
        return {}


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    """What this adapter supports, now. The plan and the regulator read these, never assume."""

    start_stop: bool
    #: A current can be written at all (a session start, at least).
    set_current: bool
    #: The regulator loop may write the current during a session.
    regulated_current: bool
    reads_charging_state: bool
    reads_measured_current: bool
    reads_energy_register: bool

    def as_dict(self) -> dict[str, bool]:
        return {
            "start_stop": self.start_stop,
            "set_current": self.set_current,
            "regulated_current": self.regulated_current,
            "reads_charging_state": self.reads_charging_state,
            "reads_measured_current": self.reads_measured_current,
            "reads_energy_register": self.reads_energy_register,
        }
