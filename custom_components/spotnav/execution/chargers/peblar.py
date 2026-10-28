"""Peblar: pauses are counted against the vendor's relay-protection budget.

The charge switch is the REST API's current limit (0 mA pauses, the last known limit resumes). Peblar's
OpenAPI says pausing must not happen more than three times in ten minutes (rolling window), to protect
the relays of the charger and the vehicle. A Stop beyond that budget does not pause: it lowers the
current limit to the 6 A floor, so the charge keeps running at the least the pilot allows, and the
next Stop after the window has room pauses again. Such a Stop answers `STOP_HELD_AT_FLOOR`, never a plain
`True`: the controller does not take it for a stop, and load balancing counts the 6 A still drawn. A Stop
while already paused counts for nothing.

Phases: the core integration's `force_single_phase` switch (on = one phase, off = three), which it creates only
when the charger allows forcing one phase and has more than one phase connected. It is found on the charger's
own device by its translation key or unique id (`<serial>_force_single_phase`); without it the charger has no
`PhaseSwitch`.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final, Literal

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ...const import DEFAULT_MIN_CURRENT_A
from ..charger_profiles import entity_matches_keys, PATH_SWITCH, PATH_SWITCH_BUDGET
from .base import (
    _entity_unavailable,
    _lower,
    _not_executed,
    call_service,
    path_description,
    PhaseSwitch,
    PhaseSwitchResult,
    PHASES_INVALID,
    PHASES_TARGET_UNAVAILABLE,
    StartStopPath,
    STOP_HELD_AT_FLOOR,
)
from .generic import SwitchPath
from .registry import ChargerContext, register_phase_switch, register_start_stop

_LOGGER = logging.getLogger(__name__)

PEBLAR_DOMAIN: Final = "peblar"
_FORCE_SINGLE_PHASE_KEYS: Final = ("force_single_phase",)

PAUSE_WINDOW_S = 600.0


class PauseBudgetSwitchPath(SwitchPath):
    """The charge switch, with at most `budget` pauses in any `PAUSE_WINDOW_S`."""

    kind = PATH_SWITCH_BUDGET

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        *,
        budget: int,
        floor_entity_id: str | None,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        super().__init__(hass, entity_id)
        self._budget = budget
        self._floor_entity_id = floor_entity_id
        self._now = now
        self._pauses: deque[datetime] = deque()

    def _pauses_in_window(self) -> int:
        now = self._now()
        while self._pauses and (now - self._pauses[0]).total_seconds() >= PAUSE_WINDOW_S:
            self._pauses.popleft()
        return len(self._pauses)

    async def async_stop(self) -> bool | str:
        state = self.hass.states.get(self.entity_id)
        if state is not None and state.state != STATE_ON:
            return await super().async_stop()  # already paused: nothing is counted
        if self._pauses_in_window() < self._budget:
            executed = await super().async_stop()
            if executed:
                self._pauses.append(self._now())
            return executed
        # Out of pauses: hold at the floor instead of pausing once more.
        _LOGGER.warning(
            "The charger allows %s pauses in ten minutes, so the charge is not paused but held at %s A",
            self._budget,
            int(DEFAULT_MIN_CURRENT_A),
        )
        if self._floor_entity_id is None or _entity_unavailable(self.hass, self._floor_entity_id):
            return _not_executed(self._floor_entity_id or self.entity_id)
        await call_service(
            self.hass,
            "number",
            "set_value",
            {"entity_id": self._floor_entity_id, "value": int(DEFAULT_MIN_CURRENT_A)},
            blocking=True,
        )
        return STOP_HELD_AT_FLOOR

    def describe_state(self) -> dict[str, Any]:
        return {"pauses_in_window": self._pauses_in_window(), "pause_budget": self._budget}

    def describe(self) -> dict[str, Any]:
        # To a reader it is the switch it is built on.
        return path_description(PATH_SWITCH, entity_ids=(self.entity_id,), inverted=self.inverted)


def _budget_switch_path(context: ChargerContext) -> StartStopPath | None:
    entity_id = context.raw_path.get("entity_id")
    if not entity_id:
        return None
    return PauseBudgetSwitchPath(
        context.hass,
        str(entity_id),
        budget=context.policy.max_pauses_per_10min or 3,
        floor_entity_id=context.current_limit,
        now=context.now,
    )


register_start_stop(PATH_SWITCH_BUDGET, _budget_switch_path)


class ForceSinglePhaseSwitch(PhaseSwitch):
    """Peblar's force-single-phase switch: on charges on one phase, off on the phases connected (three)."""

    async def async_set_phases(self, phases: Literal[1, 3]) -> PhaseSwitchResult:
        if phases not in (1, 3):
            return PhaseSwitchResult(False, PHASES_INVALID)
        if _entity_unavailable(self.hass, self.entity_id):
            _not_executed(self.entity_id)
            return PhaseSwitchResult(False, PHASES_TARGET_UNAVAILABLE)
        service = "turn_on" if phases == 1 else "turn_off"
        await call_service(self.hass, "switch", service, {"entity_id": self.entity_id}, blocking=True)
        return PhaseSwitchResult(True)

    def phases_now(self) -> int | None:
        state = _lower(self.hass.states.get(self.entity_id))
        if state == STATE_ON:
            return 1
        if state == STATE_OFF:
            return 3
        return None


def force_single_phase_entity(hass: HomeAssistant, *entity_ids: str | None) -> str | None:
    """The force-single-phase switch on the device of the first of `entity_ids` that is a Peblar entity, or `None`.

    Found by what the entity is (its translation key or unique id), never by its name; a disabled one is not found.
    """
    registry = er.async_get(hass)
    for entity_id in entity_ids:
        own = registry.async_get(entity_id) if entity_id else None
        if own is None or own.platform != PEBLAR_DOMAIN or own.device_id is None:
            continue
        for entry in er.async_entries_for_device(registry, own.device_id):
            if (
                entry.domain == "switch"
                and entry.platform == PEBLAR_DOMAIN
                and entity_matches_keys(
                    translation_key=entry.translation_key, unique_id=entry.unique_id, keys=_FORCE_SINGLE_PHASE_KEYS
                )
            ):
                return entry.entity_id
        return None
    return None


def _force_single_phase(context: ChargerContext) -> PhaseSwitch | None:
    entity_id = force_single_phase_entity(
        context.hass, context.charge_control, context.status_entity_id, context.current_limit
    )
    return None if entity_id is None else ForceSinglePhaseSwitch(context.hass, entity_id)


register_phase_switch(PEBLAR_DOMAIN, _force_single_phase)
