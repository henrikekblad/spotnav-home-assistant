"""The charger adapter: one small surface over every way SpotNav can drive a charger.

`ChargerAdapter` answers what the controller needs and nothing else: start, stop, set a current,
whether the charger is charging, what current it measures, its energy register, and what it supports
(`AdapterCapabilities`) under which write policy (`charger_profiles.WritePolicy`). It is built from
two interchangeable parts plus read-only facts:

* a **start/stop path**: a switch (optionally inverted, V2C), a select option (go-e, myenergi,
  OpenEVSE), a button pair (Lektrico, Wattpilot) or Easee's `action_command` service;
* a **current path**: none, OCPP's `ChangeConfiguration` on `AssignedCurrent` (the original and still
  the only one for an OCPP charger, moved here unchanged), `number.set_value` under the write policy,
  or Easee's `set_charger_dynamic_limit`;
* read-only facts: a status sensor and the values that mean charging, measured-current sensors (A or
  mA) and the energy register.

The controller keeps its locks, deadband, dwell and yield stepping; this module only decides whether
one write may go out *now* and sends it. Nothing here waits for a rate limit: a write the policy does
not allow is refused with a stable code and the controller decides what that means for the fuse
(`ChargingController.async_apply_regulated_current`). Every write is reported as written only after
the service call returned.
"""

from __future__ import annotations

import asyncio
import logging
import math
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from ..const import DEFAULT_MIN_CURRENT_A
from ..vehicles.ocpp_identity import OcppConnectorTarget
from ..vehicles.soc_estimate import read_energy_register_kwh
from .charger_profiles import (
    OCPP_NUMBER_POLICY,
    OCPP_POLICY,
    PATH_BUTTONS,
    PATH_EASEE,
    PATH_SELECT,
    PATH_SWITCH,
    WritePolicy,
)

# The OCPP logs keep the controller's logger: they are the same messages they always were.
_LOGGER = logging.getLogger("custom_components.spotnav.execution.controller")
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
#: This charger has no way to take a current.
ASSIGN_UNSUPPORTED: Final = "unsupported"
#: The number's unit is neither A nor mA, so its value is never assumed to be amperes.
ASSIGN_UNIT_UNKNOWN: Final = "unit_unknown"
#: The entity is unavailable or gone.
ASSIGN_TARGET_UNAVAILABLE: Final = "target_unavailable"

#: Outcomes in which the charger now carries the requested current.
IN_EFFECT_OUTCOMES: Final = frozenset({ASSIGN_ASSIGNED, ASSIGN_UNCONFIRMED, ASSIGN_UNCHANGED})
#: Outcomes in which a write was refused by policy or capability, before anything was sent.
REFUSED_OUTCOMES: Final = frozenset(
    {
        ASSIGN_RATE_LIMITED,
        ASSIGN_FLASH_GUARD,
        ASSIGN_IGNORED_WHILE_PAUSED,
        ASSIGN_INSTALLATION_SHARED,
        ASSIGN_UNSUPPORTED,
        ASSIGN_UNIT_UNKNOWN,
        ASSIGN_TARGET_UNAVAILABLE,
        ASSIGN_BELOW_MINIMUM,
    }
)

# Why a current is being written: a session start (or a manual one), the regulator loop, the
# restore after active control is turned off, or a re-send after the charger forgot its limit.
WRITE_SESSION_START: Final = "session_start"
WRITE_REGULATOR: Final = "regulator"
WRITE_RESTORE: Final = "restore"
WRITE_RESEND: Final = "resend"

_AMPERE_UNITS: Final = {"a": 1.0, "amp": 1.0, "amps": 1.0, "ampere": 1.0, "amperes": 1.0, "ma": 1000.0}

_OCPP_DOMAIN = "ocpp"
_OCPP_GET_CONFIGURATION_SERVICE = "get_configuration"
_OCPP_CONFIGURE_SERVICE = "configure"
_OCPP_ASSIGNED_CURRENT_KEY = "AssignedCurrent"

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


def _lower(state: State | None) -> str | None:
    """A state's text lower-cased, or `None` for missing, unavailable or unknown."""
    if state is None or not isinstance(state.state, str):
        return None
    if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    return state.state.strip().lower()


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
    async def async_start(self) -> None: ...

    @abstractmethod
    async def async_stop(self) -> None: ...

    @abstractmethod
    def enabled_state(self) -> bool | None:
        """Whether the charger is enabled as commanded: `True` (started), `False` (stopped) or
        `None` when this path cannot tell (a button, a neutral select). Never a guess.
        """

    @abstractmethod
    def entity_ids(self) -> tuple[str, ...]:
        """The entities whose reports change `enabled_state`."""

    @abstractmethod
    def describe(self) -> dict[str, Any]: ...


class SwitchPath(StartStopPath):
    """`switch.turn_on` starts, `switch.turn_off` stops; inverted for a switch that means paused."""

    kind = PATH_SWITCH

    def __init__(self, hass: HomeAssistant, entity_id: str, *, inverted: bool = False) -> None:
        super().__init__(hass)
        self.entity_id = entity_id
        self.inverted = inverted

    async def async_start(self) -> None:
        await self.hass.services.async_call(
            "switch",
            "turn_off" if self.inverted else "turn_on",
            {"entity_id": self.entity_id},
            blocking=True,
        )

    async def async_stop(self) -> None:
        await self.hass.services.async_call(
            "switch",
            "turn_on" if self.inverted else "turn_off",
            {"entity_id": self.entity_id},
            blocking=True,
        )

    def enabled_state(self) -> bool:
        state = self.hass.states.get(self.entity_id)
        if state is None:
            return False
        return state.state == (STATE_OFF if self.inverted else STATE_ON)

    def entity_ids(self) -> tuple[str, ...]:
        return (self.entity_id,)

    def describe(self) -> dict[str, Any]:
        return path_description(self.kind, entity_ids=(self.entity_id,), inverted=self.inverted)


class SelectPath(StartStopPath):
    """`select.select_option` with the option that starts and the one that stops."""

    kind = PATH_SELECT

    def __init__(self, hass: HomeAssistant, entity_id: str, *, start_option: str, stop_option: str) -> None:
        super().__init__(hass)
        self.entity_id = entity_id
        self.start_option = start_option
        self.stop_option = stop_option

    async def _select(self, option: str) -> None:
        await self.hass.services.async_call(
            "select", "select_option", {"entity_id": self.entity_id, "option": option}, blocking=True
        )

    async def async_start(self) -> None:
        await self._select(self.start_option)

    async def async_stop(self) -> None:
        await self._select(self.stop_option)

    def enabled_state(self) -> bool | None:
        state = self.hass.states.get(self.entity_id)
        text = _lower(state)
        if text is None:
            return None
        if text == self.start_option.lower():
            return True
        if text == self.stop_option.lower():
            return False
        return None

    def entity_ids(self) -> tuple[str, ...]:
        return (self.entity_id,)

    def describe(self) -> dict[str, Any]:
        return path_description(
            self.kind,
            entity_ids=(self.entity_id,),
            start_option=self.start_option,
            stop_option=self.stop_option,
        )


class ButtonPath(StartStopPath):
    """`button.press` on a start button and on a stop button."""

    kind = PATH_BUTTONS

    def __init__(self, hass: HomeAssistant, start_entity_id: str, stop_entity_id: str) -> None:
        super().__init__(hass)
        self.start_entity_id = start_entity_id
        self.stop_entity_id = stop_entity_id

    async def async_start(self) -> None:
        await self.hass.services.async_call(
            "button", "press", {"entity_id": self.start_entity_id}, blocking=True
        )

    async def async_stop(self) -> None:
        await self.hass.services.async_call(
            "button", "press", {"entity_id": self.stop_entity_id}, blocking=True
        )

    def enabled_state(self) -> None:
        return None

    def entity_ids(self) -> tuple[str, ...]:
        return ()

    def describe(self) -> dict[str, Any]:
        return path_description(self.kind, entity_ids=(self.start_entity_id, self.stop_entity_id))


class EaseeCommandPath(StartStopPath):
    """`easee.action_command` start and stop. Easee has no switch that means charging, and the
    `is_enabled` switch is a stored charger setting that is never written.
    """

    kind = PATH_EASEE

    def __init__(
        self, hass: HomeAssistant, device_id: str, *, limiter: WriteRateLimiter | None = None
    ) -> None:
        super().__init__(hass)
        self.device_id = device_id
        self._limiter = limiter

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

    async def async_start(self) -> None:
        await self._command("start")

    async def async_stop(self) -> None:
        await self._command("stop")

    def enabled_state(self) -> None:
        return None

    def entity_ids(self) -> tuple[str, ...]:
        return ()

    def describe(self) -> dict[str, Any]:
        return path_description(self.kind)


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

    def available(self) -> str | None:
        """`None` while this path can write, else the code saying why it cannot right now."""
        return None

    def wait_s(self) -> float:
        return 0.0

    def describe(self) -> dict[str, Any]:
        return current_description(self.kind)


class NoCurrent(CurrentPath):
    """A charger whose current SpotNav cannot set (start and stop only)."""

    def __init__(self) -> None:
        super().__init__(WritePolicy())

    async def async_set(self, amps: int, *, reason: str, verify: bool = False) -> str:
        return ASSIGN_UNSUPPORTED


def rewrite_assigned_current(current: str, connector: int, amps: int) -> str:
    """`current` with one connector's assignment replaced, or a `ValueError`.

    `AssignedCurrent` is a comma-separated list of `<connector>.<amps>` pairs that the OCPP integration
    validates strictly, so a reading not of that shape is refused before anything is sent. The list is
    rebuilt in ascending connector order (so it compares with what was read); an unlisted connector is
    added and every other entry keeps its value.
    """
    if connector < 1:
        raise ValueError("A connector to assign must be a positive number")
    if amps < 1:
        raise ValueError("A current to assign must be a positive number")
    entries: dict[int, int] = {}
    for part in current.split(","):
        connector_text, dot, amps_text = part.partition(".")
        if not dot or not connector_text.isdigit() or not amps_text.isdigit():
            raise ValueError("AssignedCurrent is not a list this integration can read")
        other_connector = int(connector_text)
        if other_connector < 1:
            raise ValueError("AssignedCurrent is not a list this integration can read")
        entries[other_connector] = int(amps_text)
    entries[connector] = amps
    return ",".join(
        f"{number}.{value}" for number, value in sorted(entries.items())
    )

def assigned_amps_for_connector(current: str, connector: int) -> int | None:
    """The amps `AssignedCurrent` carries for one connector, or `None`.

    `None` for a string not exactly the `<connector>.<amps>` list, or a connector with no entry. Used
    to confirm a write by reading it back, never to guess.
    """
    found: int | None = None
    for part in current.split(","):
        connector_text, dot, amps_text = part.partition(".")
        if not dot or not connector_text.isdigit() or not amps_text.isdigit():
            return None
        if int(connector_text) == connector:
            found = int(amps_text)
    return found


async def read_assigned_current_value(hass: HomeAssistant, devid: str) -> str | None:
    """The charge point's raw `AssignedCurrent` answer, or `None` when it answered without a value.

    Unlike `OcppAssignedCurrent.async_read_assigned_current`, a call that could not be made or that
    failed raises, so a caller can tell "no answer" from "answered without the key". Read only.
    """
    configuration = await hass.services.async_call(
        _OCPP_DOMAIN,
        _OCPP_GET_CONFIGURATION_SERVICE,
        {"devid": devid, "ocpp_key": _OCPP_ASSIGNED_CURRENT_KEY},
        blocking=True,
        return_response=True,
    )
    value = configuration.get("value") if isinstance(configuration, dict) else None
    return value if isinstance(value, str) else None


class OcppAssignedCurrent(CurrentPath):
    """OCPP `ChangeConfiguration` on `AssignedCurrent`: read, rewrite one connector's entry, write.

    The original path, unchanged: the same two service calls with the same payloads in the same order
    and the same log lines, only moved out of the controller. It has no policy of its own.
    """

    kind = "ocpp"
    can_set = True

    def __init__(self, hass: HomeAssistant, target: Callable[[], OcppConnectorTarget | None]) -> None:
        super().__init__(OCPP_POLICY)
        self.hass = hass
        self._target = target

    async def async_set(self, amps: int, *, reason: str, verify: bool = False) -> str:
        if amps < DEFAULT_MIN_CURRENT_A:
            # Nothing to send, and nothing failed: below the floor there is no valid pilot current
            # to assign, so the value cannot be expressed and no service call is attempted.
            _LOGGER.debug(
                "Not assigning %sA: below the %sA floor there is no valid pilot current, "
                "so nothing was sent",
                amps,
                DEFAULT_MIN_CURRENT_A,
            )
            return ASSIGN_BELOW_MINIMUM
        # One gate, about identity rather than any slider: without a unique connector target there
        # is no connector to command, so the request is recorded without being applied.
        if self._target() is None:
            _LOGGER.warning(
                "This charger has no unique OCPP connector target, so its current cannot be set "
                "through ChangeConfiguration"
            )
            return ASSIGN_NO_TARGET
        read = await self.async_read_assigned_current()
        if read is None:
            _LOGGER.warning(
                "Could not set the charger's current through ChangeConfiguration; "
                "the request is recorded but not applied"
            )
            return ASSIGN_READ_FAILED
        devid, connector, current = read
        try:
            # Raises for a string this integration cannot read; same answer as a failed read: do not write.
            assigned = rewrite_assigned_current(current, connector, amps)
        except Exception:
            _LOGGER.warning(
                "Could not set the charger's current through ChangeConfiguration; "
                "the request is recorded but not applied"
            )
            return ASSIGN_READ_FAILED
        if not await self.async_write_assigned_current(devid, assigned):
            # Deliberately every failure: a missing, unreachable or refusing charger integration is
            # one situation here, the current was not assigned.
            _LOGGER.warning(
                "Could not set the charger's current through ChangeConfiguration; "
                "the request is recorded but not applied"
            )
            return ASSIGN_WRITE_FAILED
        if verify:
            confirmed = await self.async_read_assigned_current()
            if confirmed is None or assigned_amps_for_connector(confirmed[2], connector) != amps:
                return ASSIGN_UNCONFIRMED
        self.last_written_a = amps
        return ASSIGN_ASSIGNED

    async def async_read_assigned_current(self) -> tuple[str, int, str] | None:
        """Read the charger's `AssignedCurrent` slot."""
        target = self._target()
        if target is None:
            return None
        devid, connector = target.devid, target.connector_id
        try:
            configuration = await self.hass.services.async_call(
                _OCPP_DOMAIN,
                _OCPP_GET_CONFIGURATION_SERVICE,
                {"devid": devid, "ocpp_key": _OCPP_ASSIGNED_CURRENT_KEY},
                blocking=True,
                return_response=True,
            )
        except Exception:
            return None
        current = configuration.get("value") if isinstance(configuration, dict) else None
        if not isinstance(current, str) or not current:
            return None
        return devid, connector, current

    async def async_write_assigned_current(self, devid: str, value: str) -> bool:
        """Write the slot verbatim, reporting whether it went. Writes nothing else."""
        try:
            await self.hass.services.async_call(
                _OCPP_DOMAIN,
                _OCPP_CONFIGURE_SERVICE,
                {
                    "devid": devid,
                    "ocpp_key": _OCPP_ASSIGNED_CURRENT_KEY,
                    "value": value,
                },
                blocking=True,
            )
        except Exception:
            return False
        return True

    def available(self) -> str | None:
        return ASSIGN_NO_TARGET if self._target() is None else None

    def describe(self) -> dict[str, Any]:
        return current_description(self.kind, service=f"{_OCPP_DOMAIN}.{_OCPP_CONFIGURE_SERVICE}")


def single_charger_installation(hass: HomeAssistant, number_entity_id: str) -> bool:
    """Whether the installation a limit number belongs to has exactly one charger.

    Zaptec's number sits on the Installation device and caps every charger under it. The chargers are
    the devices that name it as `via_device`; an installation with none that can be found, or several,
    is not single, so the write is refused rather than lowering someone else's charger.
    """
    entity = er.async_get(hass).async_get(number_entity_id)
    if entity is None or entity.device_id is None:
        return False
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    children = [
        device
        for device in devices.devices
        if device.via_device_id == entity.device_id
        and any(
            candidate.platform == entity.platform
            for candidate in er.async_entries_for_device(registry, device.id)
        )
    ]
    return len(children) == 1


class NumberCurrent(CurrentPath):
    """`number.set_value` on the charger's current-limit number, under the write policy.

    Rounds down to the entity's step, never raises a value to satisfy its minimum (a request below it
    is refused), clamps to its maximum, and converts mA. Refuses, in this order: below the floor, an
    entity that is gone or has no ampere unit, a value it already carries (nothing sent), a flash
    setting asked by the regulator, a charger that would ignore the write while paused, a number that
    caps several chargers, and an interval or budget not yet elapsed.
    """

    kind = "number"
    can_set = True

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        policy: WritePolicy,
        *,
        enabled: Callable[[], bool | None],
        limiter: WriteRateLimiter | None = None,
        single_charger: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(policy)
        self.hass = hass
        self.entity_id = entity_id
        self._enabled = enabled
        self._limiter = limiter or WriteRateLimiter(policy)
        self._single_charger = single_charger

    def _native_value(self, state: State, amps: int, factor: float) -> float | None:
        native = amps * factor
        step = _finite(state.attributes.get("step"))
        if step is not None and step > 0:
            native = math.floor(native / step + 1e-9) * step
            decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
            native = round(native, decimals + 1)
        minimum = _finite(state.attributes.get("min"))
        maximum = _finite(state.attributes.get("max"))
        if minimum is not None and native < minimum:
            return None
        if maximum is not None and native > maximum:
            native = maximum
        return native

    def _present_amps(self, state: State, factor: float) -> float | None:
        value = _finite(state.state)
        return None if value is None else value / factor

    def setpoint_a(self) -> int | None:
        state = self.hass.states.get(self.entity_id)
        factor = _amps_factor(state)
        if state is None or factor is None:
            return self.last_written_a
        present = self._present_amps(state, factor)
        if present is None:
            return self.last_written_a
        return int(math.floor(present + 1e-9))

    def available(self) -> str | None:
        if self.policy.installation_wide and (
            self._single_charger is None or not self._single_charger()
        ):
            return ASSIGN_INSTALLATION_SHARED
        return None

    def wait_s(self) -> float:
        return self._limiter.wait_s()

    async def async_set(self, amps: int, *, reason: str, verify: bool = False) -> str:
        if amps < DEFAULT_MIN_CURRENT_A:
            return ASSIGN_BELOW_MINIMUM
        state = self.hass.states.get(self.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return ASSIGN_TARGET_UNAVAILABLE
        factor = _amps_factor(state)
        if factor is None:
            return ASSIGN_UNIT_UNKNOWN
        native = self._native_value(state, amps, factor)
        if native is None:
            return ASSIGN_BELOW_MINIMUM
        present = _finite(state.state)
        if present is not None and abs(present - native) < 1e-9:
            return ASSIGN_UNCHANGED
        if reason == WRITE_REGULATOR and not self.policy.regulator_writes:
            return ASSIGN_FLASH_GUARD
        if self.policy.ignored_while_paused and self._enabled() is False:
            return ASSIGN_IGNORED_WHILE_PAUSED
        unavailable = self.available()
        if unavailable is not None:
            return unavailable
        if self._limiter.wait_s() > 0:
            return ASSIGN_RATE_LIMITED
        value: float | int = int(native) if float(native).is_integer() else native
        # Recorded before the call: a call that raised may still have reached the charger, and the
        # budget counts attempts.
        self._limiter.record()
        try:
            await self.hass.services.async_call(
                "number", "set_value", {"entity_id": self.entity_id, "value": value}, blocking=True
            )
        except Exception:
            _ADAPTER_LOGGER.warning(
                "Could not set the charger's current through its number; "
                "the request is recorded but not applied"
            )
            return ASSIGN_WRITE_FAILED
        self.last_written_a = int(math.floor(native / factor + 1e-9))
        if verify:
            after = self.hass.states.get(self.entity_id)
            read = None if after is None else _finite(after.state)
            if read is None or abs(read - native) > 1e-9:
                return ASSIGN_UNCONFIRMED
        return ASSIGN_ASSIGNED

    def describe(self) -> dict[str, Any]:
        return current_description(self.kind, entity_id=self.entity_id, service="number.set_value")


class EaseeDynamicLimit(CurrentPath):
    """`easee.set_charger_dynamic_limit`, re-sent after plug-in and reboot, never the max-limit
    services (those are flash).

    Easee's services log and return instead of raising, and skip a call whose value equals the one
    they cached, so a write is read back from the status sensor's attributes: a value that still
    disagrees after `EASEE_CONFIRM_AFTER_S` marks the cache suspect, and the next write first sends
    one amp lower (the safe direction) so the service cannot skip it.
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
    ) -> None:
        super().__init__(policy)
        self.hass = hass
        self.device_id = device_id
        self.status_entity_id = status_entity_id
        self._limiter = limiter
        self._now = now
        self._written_at: datetime | None = None
        self._suspect = False

    def read_back_a(self) -> int | None:
        """The dynamic limit the charger reports, from the status sensor's attributes."""
        if not self.status_entity_id:
            return None
        state = self.hass.states.get(self.status_entity_id)
        if state is None:
            return None
        for key, value in state.attributes.items():
            if "dynamicchargercurrent" in str(key).replace("_", "").lower():
                number = _finite(value)
                if number is not None:
                    return int(math.floor(number + 1e-9))
        return None

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
        self._note_confirmation()
        read = self.read_back_a()
        if reason != WRITE_RESEND and read == amps:
            # Only what the charger itself reports counts: a remembered write may have been cleared
            # by a plug-in or a reboot since.
            return ASSIGN_UNCHANGED
        # Two calls in one write when the cache is suspect, so the budget must cover both.
        needed = 2 if (self._suspect or reason == WRITE_RESEND) and amps > DEFAULT_MIN_CURRENT_A else 1
        if self._limiter.wait_s() > 0 or not self._budget_allows(needed):
            return ASSIGN_RATE_LIMITED
        if needed == 2 and not await self._send(amps - 1):
            return ASSIGN_WRITE_FAILED
        if not await self._send(amps):
            return ASSIGN_WRITE_FAILED
        self.last_written_a = amps
        self._written_at = self._now()
        self._suspect = False
        if verify:
            await asyncio.sleep(EASEE_READBACK_DELAY_S)
            if self.read_back_a() != amps:
                return ASSIGN_UNCONFIRMED
        return ASSIGN_ASSIGNED

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
        return old in _EASEE_DISCONNECTED and new not in _EASEE_DISCONNECTED

    def describe(self) -> dict[str, Any]:
        return current_description(self.kind, service=f"{_EASEE_DOMAIN}.set_charger_dynamic_limit")


# --- the adapter ---------------------------------------------------------------------------------


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


#: After a Start, how long a charger that has not yet reported itself enabled is still treated as
#: started for the one rule that depends on it (a current is not written while paused). Its report
#: lags the command; a start that really failed leaves the value stored, not lost.
START_GRACE_S: Final = 30.0

#: The status values the progress check reads, in OCPP's own spelling (`charge_progress.py`).
PROGRESS_CHARGING: Final = "Charging"
PROGRESS_SUSPENDED_EV: Final = "SuspendedEV"


class ChargerAdapter:
    """One charger as the controller drives it. See the module docstring."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        platform: str | None,
        path: StartStopPath,
        current: CurrentPath,
        policy: WritePolicy,
        status_entity_id: str | None = None,
        charging_values: tuple[str, ...] = (),
        vehicle_idle_values: tuple[str, ...] = (),
        current_entity_ids: tuple[str, ...] = (),
        energy_entity_id: str | None = None,
        current_enabled: bool = False,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        self.hass = hass
        self._now = now
        self._started_at: datetime | None = None
        #: The person's explicit opt-in to writing a current (`CONF_CURRENT_CONTROL`); a path that
        #: could write is still never used without it.
        self.current_enabled = current_enabled
        self.platform = platform
        self.path = path
        self.current = current
        self.policy = policy
        self.status_entity_id = status_entity_id
        self._charging_values = tuple(value.lower() for value in charging_values)
        self._idle_values = tuple(value.lower() for value in vehicle_idle_values)
        self.current_entity_ids = current_entity_ids
        self.energy_entity_id = energy_entity_id

    # -- commands

    async def async_start(self) -> None:
        self._started_at = self._now()
        await self.path.async_start()

    async def async_stop(self) -> None:
        self._started_at = None
        await self.path.async_stop()

    async def async_set_current(self, amps: int, *, reason: str, verify: bool = False) -> str:
        return await self.current.async_set(amps, reason=reason, verify=verify)

    # -- reads

    def enabled_state(self) -> bool | None:
        return self.path.enabled_state()

    def enabled_for_writes(self) -> bool | None:
        """`enabled_state`, except that a charger just started counts as enabled for
        `START_GRACE_S` while it has not yet said so.
        """
        state = self.path.enabled_state()
        if state is False and self._started_at is not None:
            if (self._now() - self._started_at).total_seconds() < START_GRACE_S:
                return True
        return state

    def charging_state(self) -> bool:
        """Whether the charger is charging: the status sensor when one is configured and readable,
        otherwise the control (the switch is on, the option is the start one).
        """
        status = self._status()
        if status is not None:
            return status in self._charging_values
        return self.path.enabled_state() is True

    def _status(self) -> str | None:
        if not self.status_entity_id:
            return None
        return _lower(self.hass.states.get(self.status_entity_id))

    def progress_status(self) -> str | None:
        """The status in the progress check's vocabulary: `Charging`, `SuspendedEV` (connected, the
        vehicle asks for no current) or the raw text; `None` with no readable status sensor.
        """
        status = self._status()
        if status is None:
            return None
        if status in self._charging_values:
            return PROGRESS_CHARGING
        if status in self._idle_values:
            return PROGRESS_SUSPENDED_EV
        return status

    def measured_current_a(self) -> float | None:
        """The highest phase current measured, in A, or `None` (none configured or unreadable)."""
        values: list[float] = []
        for entity_id in self.current_entity_ids:
            state = self.hass.states.get(entity_id)
            value = _finite(state.state) if state is not None else None
            factor = _amps_factor(state)
            if value is None or factor is None:
                continue
            values.append(value / factor)
        return max(values) if values else None

    def energy_register_kwh(self) -> float | None:
        return read_energy_register_kwh(self.hass, self.energy_entity_id)

    def state_entity_ids(self) -> tuple[str, ...]:
        entity_ids = list(self.path.entity_ids())
        if self.status_entity_id:
            entity_ids.append(self.status_entity_id)
        return tuple(entity_ids)

    # -- what it supports

    @property
    def capabilities(self) -> AdapterCapabilities:
        can_set = self.current.can_set and self.current_enabled
        return AdapterCapabilities(
            start_stop=True,
            set_current=can_set and self.current.available() is None,
            regulated_current=(
                can_set and self.policy.regulator_writes and self.current.available() is None
            ),
            reads_charging_state=bool(self.status_entity_id),
            reads_measured_current=bool(self.current_entity_ids),
            reads_energy_register=bool(self.energy_entity_id),
        )

    @property
    def is_ocpp(self) -> bool:
        return isinstance(self.current, OcppAssignedCurrent)

    def describe(self) -> dict[str, Any]:
        """The control path and its write policy, as the entity configuration reports them."""
        return {
            "platform": self.platform,
            "start_stop": self.path.describe(),
            "current": self.current.describe(),
            "charging_state": {
                "source": "status" if self.status_entity_id else "control",
                "entity_id": self.status_entity_id,
            },
            "policy": self.policy.as_dict(),
            "capabilities": self.capabilities.as_dict(),
        }


def build_adapter(
    hass: HomeAssistant,
    config: dict[str, Any],
    *,
    ocpp_target: Callable[[], OcppConnectorTarget | None],
    energy_entity_id: str | None,
    now: Callable[[], datetime] = dt_util.utcnow,
) -> ChargerAdapter:
    """The adapter a charger's stored configuration describes.

    A charger without the detected-charger keys gets exactly what it always had: its charge-control
    switch as the start/stop path and the charging state, and (only when it opted in) OCPP's
    `ChangeConfiguration` for the current. `CONF_CURRENT_CONTROL` stays the explicit opt-in for every
    path that writes a current; the profile only says what such a write is allowed to do.
    """
    from ..const import (
        CONF_CHARGE_CONTROL,
        CONF_CHARGER_CURRENT_ENTITIES,
        CONF_CHARGER_PLATFORM,
        CONF_CHARGING_STATE,
        CONF_CONTROL_PATH,
        CONF_CURRENT_CONTROL,
        CONF_CURRENT_LIMIT,
        CONF_MODE,
        CURRENT_CONTROL_CHANGE_CONFIGURATION,
        CURRENT_CONTROL_EASEE,
        CURRENT_CONTROL_NUMBER,
        MODE_OCPP,
    )
    from .charger_profiles import policy_for, profile_for

    platform = config.get(CONF_CHARGER_PLATFORM) or None
    charge_control: str = config[CONF_CHARGE_CONTROL]
    control = config.get(CONF_CURRENT_CONTROL) or ""
    current_limit = config.get(CONF_CURRENT_LIMIT) or None
    raw_path = config.get(CONF_CONTROL_PATH)
    raw_path = raw_path if isinstance(raw_path, dict) else {}
    kind = raw_path.get("kind")

    if control == CURRENT_CONTROL_NUMBER and config.get(CONF_MODE) == MODE_OCPP:
        policy = OCPP_NUMBER_POLICY
    elif control == CURRENT_CONTROL_CHANGE_CONFIGURATION or config.get(CONF_MODE) == MODE_OCPP:
        policy = OCPP_POLICY
    else:
        policy = policy_for(platform)
    limiter = WriteRateLimiter(policy, now)

    path: StartStopPath
    if kind == PATH_SELECT and raw_path.get("entity_id") and raw_path.get("start_option") and raw_path.get("stop_option"):
        path = SelectPath(
            hass,
            str(raw_path["entity_id"]),
            start_option=str(raw_path["start_option"]),
            stop_option=str(raw_path["stop_option"]),
        )
    elif kind == PATH_BUTTONS and raw_path.get("start_entity_id") and raw_path.get("stop_entity_id"):
        path = ButtonPath(hass, str(raw_path["start_entity_id"]), str(raw_path["stop_entity_id"]))
    elif kind == PATH_EASEE and raw_path.get("device_id"):
        path = EaseeCommandPath(hass, str(raw_path["device_id"]), limiter=limiter)
    else:
        path = SwitchPath(
            hass,
            str(raw_path.get("entity_id") or charge_control) if kind == PATH_SWITCH else charge_control,
            inverted=bool(raw_path.get("inverted")) if kind == PATH_SWITCH else False,
        )

    raw_state = config.get(CONF_CHARGING_STATE)
    raw_state = raw_state if isinstance(raw_state, dict) else {}
    status_entity_id = raw_state.get("entity_id") or None
    profile = profile_for(platform)
    charging_values = tuple(str(v) for v in (raw_state.get("charging_values") or ()))
    idle_values = tuple(profile.vehicle_idle_values) if profile is not None else ()

    holder: dict[str, ChargerAdapter] = {}
    current: CurrentPath = NoCurrent()
    if control == CURRENT_CONTROL_CHANGE_CONFIGURATION or (
        config.get(CONF_MODE) == MODE_OCPP and not control
    ):
        # An OCPP charger that has not opted in still has its path (callers gate on the opt-in).
        current = OcppAssignedCurrent(hass, ocpp_target)
    elif control == CURRENT_CONTROL_NUMBER and current_limit:
        current = NumberCurrent(
            hass,
            current_limit,
            policy,
            enabled=lambda: holder["adapter"].enabled_for_writes(),
            limiter=limiter,
            single_charger=(lambda: single_charger_installation(hass, current_limit))
            if policy.installation_wide
            else None,
        )
    elif control == CURRENT_CONTROL_EASEE and isinstance(path, EaseeCommandPath):
        current = EaseeDynamicLimit(
            hass,
            path.device_id,
            policy,
            status_entity_id=status_entity_id,
            limiter=limiter,
            now=now,
        )

    current_entities = config.get(CONF_CHARGER_CURRENT_ENTITIES)
    adapter = ChargerAdapter(
        hass,
        platform=platform,
        path=path,
        current=current,
        policy=policy,
        status_entity_id=status_entity_id,
        charging_values=charging_values,
        vehicle_idle_values=idle_values,
        current_entity_ids=tuple(current_entities) if isinstance(current_entities, (list, tuple)) else (),
        energy_entity_id=energy_entity_id,
        current_enabled=bool(control),
        now=now,
    )
    holder["adapter"] = adapter
    return adapter
