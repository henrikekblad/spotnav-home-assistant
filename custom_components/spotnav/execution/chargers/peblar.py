"""Peblar: pauses are counted against the vendor's relay-protection budget.

The charge switch is the REST API's current limit (0 mA pauses, the last known limit resumes). Peblar's
OpenAPI says pausing must not happen more than three times in ten minutes (rolling window), to protect
the relays of the charger and the vehicle. A Stop beyond that budget does not pause: it lowers the
current limit to the 6 A floor, so the charge keeps running at the least the pilot allows, and the
next Stop after the window has room pauses again. A Stop while already paused counts for nothing.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...const import DEFAULT_MIN_CURRENT_A
from ..charger_profiles import PATH_SWITCH, PATH_SWITCH_BUDGET
from .base import _entity_unavailable, _not_executed, call_service, path_description, StartStopPath
from .generic import SwitchPath
from .registry import ChargerContext, register_start_stop

_LOGGER = logging.getLogger(__name__)

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

    async def async_stop(self) -> bool:
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
            "The charger allows %s pauses in ten minutes, so the charge is held at the floor instead",
            self._budget,
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
        return True

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
