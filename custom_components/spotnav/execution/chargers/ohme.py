"""Ohme: a charge waiting for approval is approved before max charge is selected.

With "require approval" on, a plug-in leaves the charger in `pending_approval`, where the charge-mode
select is unavailable (the integration offers it only while a mode exists) and a selection would be
dropped by Home Assistant. The integration has an `approve` button for exactly that state and no
resume call (its select maps `paused` to a stop request and `max_charge` to a max-charge request), so
Start presses the approve button first when the status says approval is pending.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..charger_profiles import entity_matches_keys, PATH_SELECT, PATH_SELECT_APPROVE
from .base import _entity_unavailable, path_description, StartStopPath
from .generic import SelectPath
from .registry import ChargerContext, register_start_stop

_PENDING_APPROVAL = "pending_approval"
_APPROVE_KEYS = ("approve",)


def approve_button(hass: HomeAssistant, select_entity_id: str) -> str | None:
    """The approve button of the same charger as the charge-mode select, or `None`."""
    registry = er.async_get(hass)
    select = registry.async_get(select_entity_id)
    if select is None or select.device_id is None:
        return None
    for entry in er.async_entries_for_device(registry, select.device_id):
        if (
            entry.domain == "button"
            and entry.platform == select.platform
            and entity_matches_keys(
                translation_key=entry.translation_key, unique_id=entry.unique_id, keys=_APPROVE_KEYS
            )
        ):
            return entry.entity_id
    return None


class ApprovingSelectPath(SelectPath):
    """A select whose Start approves a pending charge first."""

    kind = PATH_SELECT_APPROVE

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        *,
        start_option: str,
        stop_option: str,
        status: Callable[[], str | None],
    ) -> None:
        super().__init__(hass, entity_id, start_option=start_option, stop_option=stop_option)
        self._status = status

    async def async_start(self, amps: int | None = None) -> bool:
        if self._status() == _PENDING_APPROVAL:
            button = approve_button(self.hass, self.entity_id)
            if button is not None and not _entity_unavailable(self.hass, button):
                await self.hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
                # The approval is the start; the mode select comes back once the charger has a mode.
                if not _entity_unavailable(self.hass, self.entity_id):
                    await self._select(self.start_option)
                return True
        return await super().async_start()

    def describe(self) -> dict[str, Any]:
        # To a reader it is the select it is built on.
        return path_description(
            PATH_SELECT,
            entity_ids=(self.entity_id,),
            start_option=self.start_option,
            stop_option=self.stop_option,
        )


def _approving_select_path(context: ChargerContext) -> StartStopPath | None:
    raw = context.raw_path
    if not (raw.get("entity_id") and raw.get("start_option") and raw.get("stop_option")):
        return None
    return ApprovingSelectPath(
        context.hass,
        str(raw["entity_id"]),
        start_option=str(raw["start_option"]),
        stop_option=str(raw["stop_option"]),
        status=context.status,
    )


register_start_stop(PATH_SELECT_APPROVE, _approving_select_path)
