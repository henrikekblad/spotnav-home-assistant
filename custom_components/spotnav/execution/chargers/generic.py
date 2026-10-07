"""The vendor-neutral paths: a switch, a select option or a button pair to start and stop, a
number to set the current, and no current at all.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State

from ...const import CURRENT_CONTROL_NUMBER, DEFAULT_MIN_CURRENT_A
from ..charger_profiles import PATH_BUTTONS, PATH_SELECT, PATH_SWITCH, WritePolicy
from .base import (
    _amps_factor,
    _entity_unavailable,
    _finite,
    _lower,
    _not_executed,
    ASSIGN_ASSIGNED,
    ASSIGN_BELOW_MINIMUM,
    ASSIGN_EXTERNAL_BALANCER,
    ASSIGN_FLASH_GUARD,
    ASSIGN_IGNORED_WHILE_PAUSED,
    ASSIGN_INSTALLATION_SHARED,
    ASSIGN_RATE_LIMITED,
    ASSIGN_TARGET_UNAVAILABLE,
    ASSIGN_UNCHANGED,
    ASSIGN_UNCONFIRMED,
    ASSIGN_UNIT_UNKNOWN,
    ASSIGN_UNSUPPORTED,
    ASSIGN_WRITE_FAILED,
    call_service,
    current_description,
    CurrentPath,
    path_description,
    StartStopPath,
    MODULATION_WRITES,
    WriteRateLimiter,
)
from .registry import (
    ChargerContext,
    external_balancer,
    installation_check,
    register_current,
    register_start_stop,
)

_ADAPTER_LOGGER = logging.getLogger(__name__)

class SwitchPath(StartStopPath):
    """`switch.turn_on` starts, `switch.turn_off` stops; inverted for a switch that means paused."""

    kind = PATH_SWITCH

    def __init__(self, hass: HomeAssistant, entity_id: str, *, inverted: bool = False) -> None:
        super().__init__(hass)
        self.entity_id = entity_id
        self.inverted = inverted

    async def _switch(self, service: str) -> bool:
        if _entity_unavailable(self.hass, self.entity_id):
            return _not_executed(self.entity_id)
        await call_service(
            self.hass,
            "switch", service, {"entity_id": self.entity_id}, blocking=True
        )
        return True

    async def async_start(self, amps: int | None = None) -> bool:
        return await self._switch("turn_off" if self.inverted else "turn_on")

    async def async_stop(self) -> bool:
        return await self._switch("turn_on" if self.inverted else "turn_off")

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

    async def _select(self, option: str) -> bool:
        if _entity_unavailable(self.hass, self.entity_id):
            return _not_executed(self.entity_id)
        await call_service(
            self.hass,
            "select", "select_option", {"entity_id": self.entity_id, "option": option}, blocking=True
        )
        return True

    async def async_start(self, amps: int | None = None) -> bool:
        return await self._select(self.start_option)

    async def async_stop(self) -> bool:
        return await self._select(self.stop_option)

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

    async def _press(self, entity_id: str) -> bool:
        if _entity_unavailable(self.hass, entity_id):
            return _not_executed(entity_id)
        await call_service(self.hass, "button", "press", {"entity_id": entity_id}, blocking=True)
        return True

    async def async_start(self, amps: int | None = None) -> bool:
        return await self._press(self.start_entity_id)

    async def async_stop(self) -> bool:
        return await self._press(self.stop_entity_id)

    def enabled_state(self) -> None:
        return None

    def entity_ids(self) -> tuple[str, ...]:
        return ()

    def describe(self) -> dict[str, Any]:
        return path_description(self.kind, entity_ids=(self.start_entity_id, self.stop_entity_id))


class NoCurrent(CurrentPath):
    """A charger whose current SpotNav cannot set (start and stop only)."""

    def __init__(self) -> None:
        super().__init__(WritePolicy())

    async def async_set(self, amps: int, *, reason: str, verify: bool = False) -> str:
        return ASSIGN_UNSUPPORTED


class NumberCurrent(CurrentPath):
    """`number.set_value` on the charger's current-limit number, under the write policy.

    Rounds down to the entity's step, never raises a value to satisfy its minimum (a request below it
    is refused), clamps to its maximum, and converts mA. Refuses, in this order: below the floor, an
    entity that is gone or has no ampere unit (`unknown` counts as gone, except for a session-bound
    number, which reads `unknown` until its first write), a value it already carries (nothing sent), a flash
    setting asked by the regulator, a charger that would ignore the write while paused, a number that
    caps several chargers, and an interval or budget not yet elapsed.

    A number whose state is not what the charger applies (`WritePolicy.state_is_not_setpoint`,
    OpenEVSE) is never skipped for "already showing the target": the regulator skips a write only when
    it is the very value last written, a session start or a re-send always writes, and the setpoint is
    what was last written.
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
        balanced_elsewhere: Callable[[], bool] | None = None,
        disconnected_values: tuple[str, ...] = (),
    ) -> None:
        super().__init__(policy)
        self.hass = hass
        self.entity_id = entity_id
        self._enabled = enabled
        self._limiter = limiter or WriteRateLimiter(policy)
        self._single_charger = single_charger
        self._balanced_elsewhere = balanced_elsewhere
        self._disconnected = tuple(value.lower() for value in disconnected_values)

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
        if self.policy.state_is_not_setpoint:
            return self.last_written_a
        state = self.hass.states.get(self.entity_id)
        factor = _amps_factor(state)
        if state is None or factor is None:
            return self.last_written_a
        present = self._present_amps(state, factor)
        if present is None:
            return self.last_written_a
        return int(math.floor(present + 1e-9))

    def available(self) -> str | None:
        if self.policy.installation_wide and self._balanced_elsewhere is not None and self._balanced_elsewhere():
            return ASSIGN_EXTERNAL_BALANCER
        if self.policy.installation_wide and (
            self._single_charger is None or not self._single_charger()
        ):
            return ASSIGN_INSTALLATION_SHARED
        return None

    def wait_s(self) -> float:
        return self._limiter.wait_s()

    def retry_entity_ids(self) -> tuple[str, ...]:
        return (self.entity_id,) if self.policy.session_bound else ()

    def needs_resend(self, old_status: str | None, new_status: str | None) -> bool:
        """Whether a status change is a plug-in, after which a charger that forgets its limit with
        the session (`WritePolicy.resend_after_plug_in`) is told the current again.
        """
        if not self.policy.resend_after_plug_in or not self._disconnected:
            return False
        was_away = old_status is None or old_status.lower() in (*self._disconnected, STATE_UNAVAILABLE, STATE_UNKNOWN, "")
        now_there = new_status is not None and new_status.lower() not in (
            *self._disconnected,
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
            "",
        )
        if was_away and now_there:
            self.last_written_a = None  # the charger dropped the claim with the session
            return True
        return False

    async def async_set(self, amps: int, *, reason: str, verify: bool = False) -> str:
        if amps < DEFAULT_MIN_CURRENT_A:
            return ASSIGN_BELOW_MINIMUM
        state = self.hass.states.get(self.entity_id)
        if state is None or state.state == STATE_UNAVAILABLE:
            return ASSIGN_TARGET_UNAVAILABLE
        if state.state == STATE_UNKNOWN and not self.policy.session_bound:
            return ASSIGN_TARGET_UNAVAILABLE
        factor = _amps_factor(state)
        if factor is None:
            return ASSIGN_UNIT_UNKNOWN
        native = self._native_value(state, amps, factor)
        if native is None:
            return ASSIGN_BELOW_MINIMUM
        present = _finite(state.state)
        if self.policy.state_is_not_setpoint:
            # What the number shows is not what is applied: only a repeat of our own last write is
            # skipped, and only from the regulator.
            if (
                reason in MODULATION_WRITES
                and self.last_written_a is not None
                and self.last_written_a == int(math.floor(native / factor + 1e-9))
            ):
                return ASSIGN_UNCHANGED
        elif present is not None and abs(present - native) < 1e-9:
            return ASSIGN_UNCHANGED
        if reason in MODULATION_WRITES and not self.policy.regulator_writes:
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
            await call_service(
                self.hass,
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


def _switch_path(context: ChargerContext) -> StartStopPath:
    """The default path: the charge-control switch, which any unknown or incomplete path falls back to."""
    raw = context.raw_path
    is_switch = raw.get("kind") == PATH_SWITCH
    return SwitchPath(
        context.hass,
        str(raw.get("entity_id") or context.charge_control) if is_switch else context.charge_control,
        inverted=bool(raw.get("inverted")) if is_switch else False,
    )


def _select_path(context: ChargerContext) -> StartStopPath | None:
    raw = context.raw_path
    if not (raw.get("entity_id") and raw.get("start_option") and raw.get("stop_option")):
        return None
    return SelectPath(
        context.hass,
        str(raw["entity_id"]),
        start_option=str(raw["start_option"]),
        stop_option=str(raw["stop_option"]),
    )


def _button_path(context: ChargerContext) -> StartStopPath | None:
    raw = context.raw_path
    if not (raw.get("start_entity_id") and raw.get("stop_entity_id")):
        return None
    return ButtonPath(context.hass, str(raw["start_entity_id"]), str(raw["stop_entity_id"]))


def _number_current(context: ChargerContext) -> CurrentPath | None:
    current_limit = context.current_limit
    if not current_limit:
        return None
    check = installation_check()
    single_charger = None
    if context.policy.installation_wide and check is not None:
        hass = context.hass

        def single_charger() -> bool:
            return check(hass, current_limit)

    profile = context.profile
    balanced_elsewhere = None
    if context.policy.installation_wide:
        hass_for_balancer = context.hass

        def balanced_elsewhere() -> bool:
            return external_balancer(hass_for_balancer) is not None

    return NumberCurrent(
        context.hass,
        current_limit,
        context.policy,
        enabled=context.enabled_for_writes,
        limiter=context.limiter,
        single_charger=single_charger,
        balanced_elsewhere=balanced_elsewhere,
        disconnected_values=profile.disconnected_values if profile is not None else (),
    )


register_start_stop(PATH_SWITCH, _switch_path)
register_start_stop(PATH_SELECT, _select_path)
register_start_stop(PATH_BUTTONS, _button_path)
register_current(CURRENT_CONTROL_NUMBER, _number_current)
