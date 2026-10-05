"""The charger's event entity: what happened, for automations."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENTRY_TYPE, ENTRY_TYPE_SITE
from .entity import AutoSurface, SpotNavChargingEntity
from .execution.charger_events import CHARGER_EVENT_TYPES, ChargerEventTracker, ChargerFacts
from .execution.controller import ChargingController
from .planning.auto_controller import AutoSnapshot
from .planning.auto_settings import AutoSettings
from .runtime import ChargerConfigEntry
from .vehicles.soc_estimate import read_energy_register_kwh


async def async_setup_entry(
    hass: HomeAssistant, entry: ChargerConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        return
    async_add_entities([ChargerEventsEntity(entry, entry.runtime_data.controller, AutoSurface.resolve(hass, entry.entry_id))])


class ChargerEventsEntity(SpotNavChargingEntity, EventEntity):
    """`charge_started`, `charge_finished`, `plugged_in`, `unplugged`, `plan_installed` and `plan_at_risk`.

    One entity with six event types: an automation triggers on the entity and, when it cares which, on
    the `event_type` attribute. The event's own attributes carry what is useful to know (the energy a
    finished charge delivered, the plan's times and current, the departure that is at risk).
    """

    _attr_translation_key = "charger_events"
    _attr_event_types = list(CHARGER_EVENT_TYPES)

    def __init__(self, entry: ChargerConfigEntry, controller: ChargingController, auto: AutoSurface) -> None:
        super().__init__(entry, controller)
        self._attr_unique_id = f"{entry.entry_id}_charger_events"
        self._auto = auto
        self._tracker = ChargerEventTracker()
        self._snapshot: AutoSnapshot | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._observe()
        self.async_on_remove(self.controller.add_listener(self._observe))
        self.async_on_remove(self.controller.add_charge_state_listener(self._observe))
        if self._auto.preview is not None:
            self.async_on_remove(self._auto.preview.add_listener(self._on_snapshot))

    @callback
    def _on_snapshot(self, snapshot: AutoSnapshot) -> None:
        self._snapshot = snapshot
        self._observe()

    @callback
    def _observe(self) -> None:
        for event_type, attributes in self._tracker.observe(self._facts()):
            self._trigger_event(event_type, attributes)
        self.async_write_ha_state()

    def _facts(self) -> ChargerFacts:
        return charger_facts(self.hass, self.controller, self._snapshot, self._auto.settings(self._entry.entry_id))


def plan_key_of(plan: Any) -> str | None:
    """What identifies an installed plan: a change of it is a new plan."""
    if plan is None:
        return None
    return plan.auto_identity or f"{plan.start}|{plan.end}|{plan.amps}|{plan.periods}"


def charger_facts(
    hass: HomeAssistant,
    controller: ChargingController,
    snapshot: AutoSnapshot | None,
    settings: AutoSettings | None,
) -> ChargerFacts:
    """One observation of a charger for `ChargerEventTracker`; the notifier reads the same."""
    plan = controller.plan
    plan_attributes: dict[str, Any] = {}
    if plan is not None:
        plan_attributes = {
            "start": plan.start,
            "end": plan.end,
            "periods": plan.periods,
            "amps": plan.amps,
            "energy_kwh": plan.energy_kwh,
            "automatic": plan.auto_owned,
        }
    # At risk: nothing fits before the departure, or the plan is the best effort that cannot meet it.
    at_risk = snapshot is not None and (
        snapshot.reason == "deadline_too_short"
        or (snapshot.proposal is not None and snapshot.proposal.short_of_deadline)
    )
    at_risk_info: dict[str, Any] = {}
    if at_risk and settings is not None:
        at_risk_info = {
            "departure_time": settings.departure.strftime("%H:%M"),
            "requested_kwh": settings.requested_kwh,
        }
    return ChargerFacts(
        charging=bool(controller.charging),
        connected=controller.adapter.vehicle_connected(),
        plan_key=plan_key_of(plan),
        plan=plan_attributes,
        at_risk=at_risk,
        at_risk_info=at_risk_info,
        register_kwh=read_energy_register_kwh(hass, controller.energy_register_entity_id),
    )
