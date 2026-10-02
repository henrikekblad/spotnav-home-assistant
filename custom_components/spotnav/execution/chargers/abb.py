"""ABB Terra AC: a pause is 0 A written to the current limit, not the stop button.

The stop button writes the Modbus session command that ends the session and leaves the charger asking
for a new badge. ABB's own way to pause is a current limit under 6 A: the session enters its pause
state and, when the limit is set to 6 A or more again, resumes (Modbus manual v1.11, register 4100h;
the integration's own text for the number: "Set to 0 A to pause charging without ending the session").
So Stop writes 0 to the limit and Start writes the current the charge is requested at. The start
button is pressed only while the charger waits for authorization (state B1).
"""

from __future__ import annotations

import math
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from ...const import DEFAULT_MIN_CURRENT_A
from ..charger_profiles import PATH_NUMBER_PAUSE
from .base import _entity_unavailable, _finite, _not_executed, call_service, path_description, StartStopPath
from .registry import ChargerContext, register_start_stop


#: The charging-state text of a charger that has a vehicle and waits to be authorized to start.
_AWAITING_AUTHORIZATION = "state b1"


class NumberPausePath(StartStopPath):
    """Stop writes 0 A to the current number, Start writes the requested current."""

    kind = PATH_NUMBER_PAUSE

    def __init__(
        self,
        hass: HomeAssistant,
        number_entity_id: str,
        start_entity_id: str | None,
        *,
        status=lambda: None,
    ) -> None:
        super().__init__(hass)
        self.number_entity_id = number_entity_id
        self.start_entity_id = start_entity_id
        self._status = status
        #: The limit the charge ran at when it was last paused, to resume at when no current is asked.
        self._resume_a: int | None = None

    async def _write(self, value: int) -> bool:
        if _entity_unavailable(self.hass, self.number_entity_id):
            return _not_executed(self.number_entity_id)
        await call_service(
            self.hass,
            "number", "set_value", {"entity_id": self.number_entity_id, "value": value}, blocking=True
        )
        return True

    def _limit(self) -> float | None:
        state = self.hass.states.get(self.number_entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return _finite(state.state)

    async def async_stop(self) -> bool:
        limit = self._limit()
        if limit is not None and limit >= DEFAULT_MIN_CURRENT_A:
            self._resume_a = int(math.floor(limit))
        return await self._write(0)

    def _start_current(self, amps: int | None) -> int:
        state = self.hass.states.get(self.number_entity_id)
        maximum = _finite(state.attributes.get("max")) if state is not None else None
        wanted = amps if amps is not None and amps >= DEFAULT_MIN_CURRENT_A else self._resume_a
        value = int(wanted) if wanted is not None else int(DEFAULT_MIN_CURRENT_A)
        value = max(value, int(DEFAULT_MIN_CURRENT_A))
        if maximum is not None and maximum >= DEFAULT_MIN_CURRENT_A:
            value = min(value, int(maximum))
        return value

    async def async_start(self, amps: int | None = None) -> bool:
        status = self._status() or ""
        if (
            self.start_entity_id
            and status.startswith(_AWAITING_AUTHORIZATION)
            and not _entity_unavailable(self.hass, self.start_entity_id)
        ):
            await call_service(
                self.hass,
                "button", "press", {"entity_id": self.start_entity_id}, blocking=True
            )
        return await self._write(self._start_current(amps))

    def enabled_state(self) -> bool | None:
        limit = self._limit()
        if limit is None:
            return None
        return limit >= DEFAULT_MIN_CURRENT_A

    def entity_ids(self) -> tuple[str, ...]:
        return (self.number_entity_id,)

    def describe_state(self) -> dict[str, Any]:
        return {
            "limit_a": self._limit(),
            "resume_a": self._resume_a,
            "awaiting_authorization": (self._status() or "").startswith(_AWAITING_AUTHORIZATION),
        }

    def describe(self) -> dict[str, Any]:
        ids = ((self.start_entity_id,) if self.start_entity_id else ()) + (self.number_entity_id,)
        return path_description(self.kind, entity_ids=ids)


def _number_pause_path(context: ChargerContext) -> StartStopPath | None:
    raw = context.raw_path
    number = raw.get("number_entity_id")
    if not number:
        return None
    return NumberPausePath(
        context.hass,
        str(number),
        str(raw["start_entity_id"]) if raw.get("start_entity_id") else None,
        status=context.status,
    )


register_start_stop(PATH_NUMBER_PAUSE, _number_pause_path)
