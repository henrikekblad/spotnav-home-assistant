"""SmartEVSE: Start puts back the mode the person had, not Normal.

The charger's mode select is its own regulation (Normal, Solar, Smart) and its stop (Off, Pause) at
once. Selecting Normal to start replaces the underlying mode, so a person on Smart or Solar would lose
it at the first Stop and Start (`dingo35/ha-SmartEVSEv3` and the firmware's `setMode`). The path
remembers the option that was selected when SpotNav stopped it and selects that one again, and Normal
only when nothing is remembered (after a restart of Home Assistant) or the charger no longer offers it
(Smart and Solar are withdrawn while no mains meter is configured).
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from ..charger_profiles import PATH_SELECT, PATH_SELECT_RESTORE
from .base import _lower, path_description, StartStopPath
from .generic import SelectPath
from .registry import ChargerContext, register_start_stop

#: The select's own words for a stopped charge besides the configured stop option.
_PAUSED_OPTIONS = frozenset({"off", "pause"})


class RestoringSelectPath(SelectPath):
    """A select whose Start selects the mode that was active before the last Stop."""

    kind = PATH_SELECT_RESTORE

    def __init__(self, hass: HomeAssistant, entity_id: str, *, start_option: str, stop_option: str) -> None:
        super().__init__(hass, entity_id, start_option=start_option, stop_option=stop_option)
        self._resume_option: str | None = None

    def _paused(self, text: str) -> bool:
        return text == self.stop_option.lower() or text in _PAUSED_OPTIONS

    async def async_stop(self) -> bool:
        state = self.hass.states.get(self.entity_id)
        text = _lower(state)
        stopped = await super().async_stop()
        if stopped and state is not None and text is not None and not self._paused(text):
            self._resume_option = str(state.state)
        return stopped

    def _start_target(self) -> str:
        remembered = self._resume_option
        state = self.hass.states.get(self.entity_id)
        options = [str(option).lower() for option in (state.attributes.get("options") or [])] if state else []
        if remembered is not None and remembered.lower() in options:
            return remembered
        return self.start_option

    async def async_start(self, amps: int | None = None) -> bool:
        return await self._select(self._start_target())

    def enabled_state(self) -> bool | None:
        """Any mode but a stopped one means the charger is enabled (Smart and Solar included)."""
        text = _lower(self.hass.states.get(self.entity_id))
        if text is None:
            return None
        return not self._paused(text)

    def describe_state(self) -> dict[str, Any]:
        return {"resume_option": self._resume_option, "start_target": self._start_target()}

    def describe(self) -> dict[str, Any]:
        # To a reader it is the select it is built on.
        return path_description(
            PATH_SELECT,
            entity_ids=(self.entity_id,),
            start_option=self.start_option,
            stop_option=self.stop_option,
        )


def _restoring_select_path(context: ChargerContext) -> StartStopPath | None:
    raw = context.raw_path
    if not (raw.get("entity_id") and raw.get("start_option") and raw.get("stop_option")):
        return None
    return RestoringSelectPath(
        context.hass,
        str(raw["entity_id"]),
        start_option=str(raw["start_option"]),
        stop_option=str(raw["stop_option"]),
    )


register_start_stop(PATH_SELECT_RESTORE, _restoring_select_path)
