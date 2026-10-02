"""Webasto Next (Modbus): a session start needs the command register to change value.

Register 5006 takes 1 (start a session) and 2 (cancel it), and the manual says a session is started
only when the value changes: a second 1 after a session that ended by itself starts nothing
(`tomwellnitz/Webasto-Next-Modbus`, register spec in the repository). The integration's start button
writes 1 and its stop button writes 2 and neither writes 0, and no entity writes 0 either. So the value
is made to change by pressing the stop button first whenever the last command SpotNav sent was a start
(or nothing is known, after a restart), and then the start button.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from ..charger_profiles import PATH_BUTTONS, PATH_BUTTONS_TOGGLE
from .base import path_description, StartStopPath
from .generic import ButtonPath
from .registry import ChargerContext, register_start_stop


class TogglingButtonPath(ButtonPath):
    """A start button that is pressed after a stop press unless the last command was a stop."""

    kind = PATH_BUTTONS_TOGGLE

    def __init__(self, hass: HomeAssistant, start_entity_id: str, stop_entity_id: str) -> None:
        super().__init__(hass, start_entity_id, stop_entity_id)
        self._last: str | None = None

    async def async_start(self, amps: int | None = None) -> bool:
        if self._last != "stop":
            # The register may still hold 1; a second 1 would start nothing, so make it change.
            if not await self._press(self.stop_entity_id):
                return False
        started = await self._press(self.start_entity_id)
        if started:
            self._last = "start"
        return started

    async def async_stop(self) -> bool:
        stopped = await super().async_stop()
        if stopped:
            self._last = "stop"
        return stopped

    def describe(self) -> dict[str, Any]:
        return path_description(PATH_BUTTONS, entity_ids=(self.start_entity_id, self.stop_entity_id))


def _toggling_button_path(context: ChargerContext) -> StartStopPath | None:
    raw = context.raw_path
    if not (raw.get("start_entity_id") and raw.get("stop_entity_id")):
        return None
    return TogglingButtonPath(context.hass, str(raw["start_entity_id"]), str(raw["stop_entity_id"]))


register_start_stop(PATH_BUTTONS_TOGGLE, _toggling_button_path)
