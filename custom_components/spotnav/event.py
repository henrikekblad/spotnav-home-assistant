"""The charger's event entity: what happened, for automations."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Final

from homeassistant.components.event import EventEntity
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_ENTRY_TYPE, ENTRY_TYPE_SITE
from .entity import AutoSurface, SpotNavChargingEntity
from .execution.charger_events import (
    CHARGER_EVENT_TYPES,
    ChargerEventTracker,
    ChargerFacts,
    EVENT_CHARGE_FINISHED,
    EVENT_CHARGE_STARTED,
)
from .execution.controller import ChargingController
from .planning.auto_controller import AutoSnapshot
from .planning.auto_settings import AutoSettings
from .runtime import ChargerConfigEntry, domain_data
from .sessions.model import ChargeSession
from .sessions.store import SessionStore
from .sessions.summary import cost_figures
from .vehicles.soc_estimate import read_energy_register_kwh

#: Without the session as it stood when the charge began, a finished charge's session is the open one, or one
#: that closed this recently (the notifier's rule for a completed charge).
_SESSION_RECENT_S: Final = 600.0


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
    finished charge delivered and what it cost, the plan's times and current, the departure that is at risk).

    A finished charge's cost is its session's, priced as the session sensors price it, for the energy delivered
    since this charge began (a short pause continues the session, so the session as it stood at the start is
    kept and subtracted). The session recorder is brought up to the stop first, so the figures are what the
    session holds at that moment; energy the charger reports after the stop is in the session, not the event.
    """

    _attr_translation_key = "charger_events"
    _attr_event_types = list(CHARGER_EVENT_TYPES)

    def __init__(self, entry: ChargerConfigEntry, controller: ChargingController, auto: AutoSurface) -> None:
        super().__init__(entry, controller)
        self._attr_unique_id = f"{entry.entry_id}_charger_events"
        self._auto = auto
        self._tracker = ChargerEventTracker()
        self._snapshot: AutoSnapshot | None = None
        #: The charge's session as it stood when the charge began (a copy), `None` until known.
        self._run_from: ChargeSession | None = None

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
        events = self._tracker.observe(self._facts())
        charge_events = any(kind in (EVENT_CHARGE_STARTED, EVENT_CHARGE_FINISHED) for kind, _ in events)
        if charge_events or (self._tracker.charging and self._run_from is None):
            self._sessions_up_to_now()
        for event_type, attributes in events:
            if event_type == EVENT_CHARGE_STARTED:
                self._run_from = self._open_session_copy()
            elif event_type == EVENT_CHARGE_FINISHED:
                attributes = {**attributes, **self._charge_cost()}
                self._run_from = None
            self._trigger_event(event_type, attributes)
        if self._tracker.charging and self._run_from is None:
            # A charge already going at the start, or before the recorder was there: it counts from now.
            self._run_from = self._open_session_copy()
        self.async_write_ha_state()

    def _session_store(self) -> SessionStore | None:
        return domain_data(self.hass).session_store

    @callback
    def _sessions_up_to_now(self) -> None:
        """Let the session recorder take this instant's reading first (it would a moment later)."""
        recorder = self._entry.runtime_data.sessions
        if recorder is not None:
            recorder.evaluate()

    def _open_session_copy(self) -> ChargeSession | None:
        store = self._session_store()
        session = None if store is None else store.open_raw(self._entry.entry_id)
        # A copy: the recorder accrues into its own object.
        return None if session is None else replace(session)

    def _charge_cost(self) -> dict[str, Any]:
        """`cost`, `currency`, `average_price` and `unpriced_kwh` of the charge that just finished."""
        store = self._session_store()
        if store is None:
            return cost_figures(None)
        session = finished_session(store, self._entry.entry_id, self._run_from)
        if session is None:
            return cost_figures(None)
        return cost_figures(store.priced(session.since(self._run_from)))

    def _facts(self) -> ChargerFacts:
        return charger_facts(self.hass, self.controller, self._snapshot, self._auto.settings(self._entry.entry_id))


def finished_session(store: SessionStore, charger_id: str, run_from: ChargeSession | None) -> ChargeSession | None:
    """The session of the charge that just finished, as stored: the one it began in, open or closed; without
    that, the open one, else one that closed in the last ten minutes."""
    opened = store.open_raw(charger_id)
    closed = store.closed_raw(charger_id)
    if run_from is not None:
        if opened is not None and opened.id == run_from.id:
            return opened
        return next((item for item in reversed(closed) if item.id == run_from.id), None)
    if opened is not None:
        return opened
    if not closed:
        return None
    last = max(closed, key=lambda item: item.start)
    if last.end is None or (dt_util.utcnow() - last.end).total_seconds() > _SESSION_RECENT_S:
        return None
    return last


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
        charging=controller.adapter.charging_observation(),
        connected=controller.adapter.vehicle_connected(),
        plan_key=plan_key_of(plan),
        plan=plan_attributes,
        at_risk=at_risk,
        at_risk_info=at_risk_info,
        register_kwh=read_energy_register_kwh(hass, controller.energy_register_entity_id),
    )
