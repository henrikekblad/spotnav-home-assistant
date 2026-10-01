"""The per-charger steps of the site flows, shared by the create and options flows.

After `site_details` each associated charger gets its own `site_charger_wiring` step (static field keys,
so every field has a translated label, and the charger's name in the description). A charger that chose
a manual measured-current source continues to `charger_manual_source` (one entity with per-phase
attributes) or `charger_manual_entities` (three entities, one per phase) before the next charger.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.data_entry_flow import FlowResult

from ..const import CONF_MEASURED_CURRENT_SOURCE
from .labels import MANUAL_CHOICE, MANUAL_ENTITIES_CHOICE
from .measured_source import (
    async_manual_source_step,
    charger_device_id,
    charger_device_scope,
    manual_entities_step,
)
from .site_form import charger_wiring_schema, PendingSiteDetails

_LOGGER = logging.getLogger(__name__)


class ChargerWiringSteps:
    """Mixin for a flow handler that has `hass`, `_pending_site`, `_wiring_queue` and
    `_manual_measured_sources`, and implements `_stored_wiring` and `_finish_pending_site`."""

    hass: Any
    _pending_site: PendingSiteDetails | None
    _wiring_queue: list[str]
    _manual_measured_sources: dict[str, dict[str, Any]]

    def _init_charger_wiring(self) -> None:
        self._pending_site = None
        self._wiring_queue = []
        self._manual_measured_sources = {}

    def _stored_wiring(self, charger_entry_id: str) -> dict[str, Any]:
        """The wiring already stored for one charger (nothing for a brand-new site)."""
        return {}

    async def _finish_pending_site(self) -> FlowResult:
        raise NotImplementedError

    async def _begin_charger_wiring(self, pending: PendingSiteDetails) -> FlowResult:
        """Walk every charger of `pending` through its wiring step, then save."""
        self._pending_site = pending
        self._wiring_queue = list(pending.charger_entry_ids)
        self._manual_measured_sources = {}
        pending.charger_inputs.clear()
        return await self.async_step_site_charger_wiring()

    async def _next_charger(self) -> FlowResult:
        self._wiring_queue.pop(0)
        return await self.async_step_site_charger_wiring()

    async def async_step_site_charger_wiring(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """One charger's phase wiring and measured-current source, for the first charger still queued."""
        pending = self._pending_site
        if pending is None:
            return self.async_abort(reason="no_pending_site")  # type: ignore[attr-defined]
        if not self._wiring_queue:
            return await self._finish_pending_site()
        charger_entry_id = self._wiring_queue[0]
        if user_input is not None:
            pending.charger_inputs[charger_entry_id] = dict(user_input)
            choice = user_input.get("measured_source")
            if choice in (MANUAL_CHOICE, MANUAL_ENTITIES_CHOICE):
                if charger_device_id(self.hass, charger_entry_id) is None:
                    # Skip rather than block, leaving the stored source untouched.
                    _LOGGER.warning(
                        "Skipping the manual measured-current entry for charger %s: "
                        "its own device could not be resolved",
                        charger_entry_id,
                    )
                    return await self._next_charger()
                if choice == MANUAL_CHOICE:
                    return await self.async_step_charger_manual_source()
                return await self.async_step_charger_manual_entities()
            return await self._next_charger()
        entry = self.hass.config_entries.async_get_entry(charger_entry_id)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="site_charger_wiring",
            data_schema=charger_wiring_schema(
                self.hass,
                charger_entry_id,
                existing=self._stored_wiring(charger_entry_id),
                candidates=pending.charger_candidates.get(charger_entry_id),
            ),
            description_placeholders={"charger": entry.title if entry is not None else charger_entry_id},
        )

    async def async_step_charger_manual_source(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect the manually entered measured-current source of the charger being wired: one entity
        plus its per-phase attribute names."""
        charger_entry_id = self._wiring_queue[0]
        outcome = await async_manual_source_step(
            self.hass,
            scope=charger_device_scope(self.hass, charger_entry_id),
            stored_source=self._stored_wiring(charger_entry_id).get(CONF_MEASURED_CURRENT_SOURCE),
            user_input=user_input,
        )
        if outcome.source is None:
            return self.async_show_form(  # type: ignore[attr-defined]
                step_id="charger_manual_source",
                data_schema=outcome.schema,
                errors=outcome.errors,
                description_placeholders=outcome.description_placeholders,
            )
        self._manual_measured_sources[charger_entry_id] = outcome.source
        return await self._next_charger()

    async def async_step_charger_manual_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect the measured current of the charger being wired as three entities, one per phase."""
        charger_entry_id = self._wiring_queue[0]
        outcome = manual_entities_step(
            self.hass,
            scope=charger_device_scope(self.hass, charger_entry_id),
            stored_source=self._stored_wiring(charger_entry_id).get(CONF_MEASURED_CURRENT_SOURCE),
            user_input=user_input,
        )
        if outcome.source is None:
            return self.async_show_form(  # type: ignore[attr-defined]
                step_id="charger_manual_entities",
                data_schema=outcome.schema,
                errors=outcome.errors,
            )
        self._manual_measured_sources[charger_entry_id] = outcome.source
        return await self._next_charger()
