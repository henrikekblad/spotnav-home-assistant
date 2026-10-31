"""NexBlue: the `charging` switch, whose state is assumed for a while after each command.

Home Assistant 2026.10 gives the core `nexblue` integration a `charging` switch (unique id `<serial>_charging`).
Its commands go to NexBlue's cloud, and its state is the cloud status, polled once a minute: on while the
status is charging, waiting for available power or waiting for the car. After a command the switch shows the
commanded state (`assumed_state` in its attributes) until a refresh confirms it, or for at most 20 s; the
integration refreshes 3 s and 20 s after the command.

So while the switch says its state is assumed it is not read as the truth: whether the charger is enabled is
then read from the status sensor, by the switch's own rule, and stays unknown while that sensor says nothing.
Once confirmed the switch is read as any other.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from homeassistant.const import ATTR_ASSUMED_STATE
from homeassistant.core import HomeAssistant

from ..charger_profiles import PATH_SWITCH, PATH_SWITCH_ASSUMED
from .base import _entity_ready, path_description, StartStopPath
from .generic import SwitchPath
from .registry import ChargerContext, register_start_stop

#: The statuses the integration's switch reads as on (`ACTIVE_CHARGING_STATES`: charging, waiting for
#: available power, waiting for the car), as its status sensor words them.
ACTIVE_STATUSES: Final = frozenset({"charging", "lb_waiting", "ev_waiting"})


class AssumedStateSwitchPath(SwitchPath):
    """A switch whose state, while assumed, is read from the charger's status instead."""

    kind = PATH_SWITCH_ASSUMED

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        *,
        status: Callable[[], str | None],
        status_entity_id: str | None,
    ) -> None:
        super().__init__(hass, entity_id)
        self._status = status
        self._status_entity_id = status_entity_id

    def assumed(self) -> bool:
        """Whether the switch shows a commanded state the charger has not confirmed yet."""
        state = self.hass.states.get(self.entity_id)
        return state is not None and state.attributes.get(ATTR_ASSUMED_STATE) is True

    def enabled_state(self) -> bool | None:
        if not self.assumed():
            return super().enabled_state()
        status = self._status() if self._status_entity_id else None
        if status is None:
            return None
        return status in ACTIVE_STATUSES

    def entity_ids(self) -> tuple[str, ...]:
        if self._status_entity_id:
            return (self.entity_id, self._status_entity_id)
        return (self.entity_id,)

    def ready(self) -> bool:
        # Only the switch is commanded; the status sensor is read, never written.
        return _entity_ready(self.hass, self.entity_id)

    def describe_state(self) -> dict[str, Any]:
        return {"assumed_state": self.assumed()}

    def describe(self) -> dict[str, Any]:
        # To a reader it is the switch it is built on.
        return path_description(PATH_SWITCH, entity_ids=(self.entity_id,), inverted=self.inverted)


def _assumed_switch_path(context: ChargerContext) -> StartStopPath | None:
    entity_id = context.raw_path.get("entity_id")
    if not entity_id:
        return None
    return AssumedStateSwitchPath(
        context.hass,
        str(entity_id),
        status=context.status,
        status_entity_id=context.status_entity_id,
    )


register_start_stop(PATH_SWITCH_ASSUMED, _assumed_switch_path)
