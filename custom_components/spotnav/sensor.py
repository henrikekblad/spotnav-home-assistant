"""Schedule, connection and site-capacity sensors for SpotNav charging control."""

import json
import math
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry, ConfigEntryChange, SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfEnergy
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CHARGER_ENTRY_IDS,
    CONF_ENTRY_TYPE,
    CONF_SOLAR_PRIORITY,
    CONF_WEBHOOK_ID,
    DEFAULT_MAX_AGE_S,
    DEFAULT_SOLAR_PRIORITY,
    DOMAIN,
    ENTRY_TYPE_SITE,
)
from .entity import AutoSurface, SpotNavAutoEntity, SpotNavChargingEntity, SpotNavSiteEntity
from .execution.controller import ChargingController
from .setup_hints import add_charger_hint
from .execution.power_energy import fresh_power_w, integrated_energy_unique_id, PowerIntegrator
from .runtime import controller_for
from .sessions.sensors import session_entities
from .vehicles.choices import flow_language
from .site.site_capacity_controller import SiteCapacityController
from .vehicles.charger_inventory import (
    charger_entries,
    charger_pairing_payload,
    site_entry,
    webhook_base_url,
)


#: How often a smart plug's power is sampled between its own reports.
INTEGRATION_TICK_S = 30


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        site_controller = entry.runtime_data.controller
        entities: list[Any] = [SiteStateEntity(entry, site_controller)]
        if entry.entry_id == instance_owner_entry_id(hass):
            entities.append(InstanceConnectionEntity(hass, entry))
        for charger_entry_id in site_controller.config.get(CONF_CHARGER_ENTRY_IDS) or []:
            charger_entry = hass.config_entries.async_get_entry(charger_entry_id)
            charger_title = charger_entry.title if charger_entry else charger_entry_id
            entities.append(
                ChargerProposedCurrentEntity(entry, site_controller, charger_entry_id, charger_title)
            )
        async_add_entities(entities)
        return

    controller = entry.runtime_data.controller
    entities = [
        ConnectionEntity(hass, entry, controller),
        PlanTimeEntity(entry, controller, "start"),
        PlanTimeEntity(entry, controller, "end"),
    ]
    entities.extend(integrated_energy_entities(hass, entry, controller))
    entities.extend(session_entities(hass, entry, controller))
    entities.extend(auto_entities(entry, controller, AutoSurface.resolve(hass, entry.entry_id)))
    if entry.entry_id == instance_owner_entry_id(hass):
        entities.append(InstanceConnectionEntity(hass, entry))
    async_add_entities(entities)


def integrated_energy_entities(
    hass: HomeAssistant, entry: ConfigEntry, controller: ChargingController
) -> list[Any]:
    """The energy SpotNav integrates from a smart plug's power sensor, only while it stands in for an
    energy register (a power sensor and no register of the person's own); otherwise a leftover one is
    removed from the registry.
    """
    if controller.power_entity_id is not None and controller.energy_from_power:
        return [IntegratedEnergySensor(hass, entry, controller)]
    registry = er.async_get(hass)
    leftover = registry.async_get_entity_id("sensor", DOMAIN, integrated_energy_unique_id(entry.entry_id))
    if leftover is not None:
        registry.async_remove(leftover)
    return []


def auto_entities(
    entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
) -> list[Any]:
    """The Auto observation and settings surface for one charger.

    Created for every charger entry even when Auto or the price layer is missing, so "Auto is not
    set up" stays distinguishable from "no such concept"; they report unavailable instead.
    """
    return [
        AutoPlanStateSensor(entry, controller, auto),
        AutoExecutionStateSensor(entry, controller, auto),
        AutoNextStartSensor(entry, controller, auto),
        AutoNextEndSensor(entry, controller, auto),
        AutoPeriodsSensor(entry, controller, auto),
        AutoCostSensor(entry, controller, auto),
        AutoEnergySensor(entry, controller, auto),
        AutoSettingsRevisionSensor(entry, controller, auto),
    ]


def instance_owner_entry_id(hass: HomeAssistant) -> str | None:
    """Which config entry carries the instance-wide pairing entity: the site entry when one exists,
    otherwise the first charger (creation order).
    """
    site = site_entry(hass)
    if site is not None:
        return site.entry_id
    first_charger = next(iter(charger_entries(hass)), None)
    return first_charger.entry_id if first_charger else None


def instance_pairing_uri(base_url: str, chargers: list[dict[str, str]]) -> str:
    """The whole instance as one pairing value: the URL once, every charger.

    One query parameter, `chargers`, holding JSON: unlike separator-joined forms it cannot be
    confused by a charger name with commas, ampersands or non-ASCII characters. `urlencode`
    percent-encodes it and `json.dumps` keeps it ASCII. `pairing_uri` keeps the per-charger form.
    """
    return "spotnav://home-assistant?" + urlencode(
        {
            "url": base_url,
            "chargers": json.dumps(chargers, separators=(",", ":")),
        }
    )


class IntegratedEnergySensor(SpotNavChargingEntity, RestoreEntity, SensorEntity):
    """Energy from a smart plug's power sensor: a cumulative kWh counter SpotNav integrates itself.

    Trapezoid over fresh samples (`power_energy.PowerIntegrator`); an interval longer than the maximum
    measurement age is not integrated, and the total survives a restart. It is the charger's energy
    register for delivered-energy accounting and the stopping estimate.
    """

    _attr_translation_key = "integrated_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller: ChargingController) -> None:
        super().__init__(entry, controller)
        self._hass = hass
        self._attr_unique_id = integrated_energy_unique_id(entry.entry_id)
        self._integrator = PowerIntegrator(DEFAULT_MAX_AGE_S)

    @property
    def native_value(self) -> float:
        return round(self._integrator.total_kwh, 6)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            try:
                restored = float(last.state)
            except (TypeError, ValueError):
                restored = 0.0
            if math.isfinite(restored) and restored > 0:
                self._integrator.total_kwh = restored
        self.controller.set_integrated_energy_entity(self.entity_id)
        power_entity = self.controller.power_entity_id
        if power_entity is not None:
            self.async_on_remove(
                async_track_state_change_event(self._hass, [power_entity], self._on_power_changed)
            )
            self.async_on_remove(
                async_track_time_interval(
                    self._hass, self._on_tick, timedelta(seconds=INTEGRATION_TICK_S)
                )
            )
        self._take_sample()

    @callback
    def _on_power_changed(self, _event: Any) -> None:
        self._take_sample()

    @callback
    def _on_tick(self, _now: datetime) -> None:
        self._take_sample()

    @callback
    def _take_sample(self) -> None:
        """One sample now: the sensor's value only while it is fresh, else a gap."""
        now = dt_util.utcnow()
        self._integrator.sample(
            now,
            fresh_power_w(self._hass, self.controller.power_entity_id, now, self._integrator.max_age_s),
        )
        self.async_write_ha_state()


class ConnectionEntity(SpotNavChargingEntity, SensorEntity):
    """Expose pairing data to the Home Assistant owner."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _attr_translation_key = "connection"
    _attr_native_value = "ready"

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, controller: ChargingController
    ) -> None:
        super().__init__(entry, controller)
        self._hass = hass
        self._attr_unique_id = f"{entry.entry_id}_connection"

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        webhook_id = self._entry.data[CONF_WEBHOOK_ID]
        base_url, webhook_url = webhook_base_url(self._hass, webhook_id)
        pairing_query = urlencode({"url": base_url, "webhook": webhook_id})
        return {
            "home_assistant_url": base_url,
            "webhook_id": webhook_id,
            "webhook_url": webhook_url,
            "pairing_uri": f"spotnav://home-assistant?{pairing_query}",
        }


class InstanceConnectionEntity(SensorEntity):
    """Every charger on this instance in one pairing value.

    One base URL plus each charger's own webhook id, so one value provisions all of them and each
    webhook stays scoped to a single charger. It lives on the site entry's device (or the first
    charger's) and is named "App pairing". The value is an attribute, not the state, because states
    are limited to 255 characters. `webhook_id` and pairing URIs stay out of logs and diagnostics.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _attr_has_entity_name = True
    _attr_translation_key = "instance_connection"
    _attr_native_value = "ready"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._attr_unique_id = f"{entry.entry_id}_instance_connection"
        # Identifiers only: restating name/manufacturer/model would overwrite the owning entry's device.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        # A charger appearing or disappearing may happen in another config entry, so listen for
        # entries changing rather than for a plan.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._entry_changed
            )
        )

    @callback
    def _entry_changed(self, change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if entry.domain == DOMAIN:
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        chargers = charger_pairing_payload(self.hass)
        # Any charger's webhook yields the same base URL; no chargers means no URL.
        base_url = webhook_base_url(self.hass, chargers[0]["webhook"])[0] if chargers else ""
        return {
            "home_assistant_url": base_url,
            "pairing_uri": instance_pairing_uri(base_url, chargers),
            # Names and ids only, no webhook ids (those are in the value above).
            "chargers": [
                {"id": charger["id"], "name": charger["name"]} for charger in chargers
            ],
        }


class PlanTimeEntity(SpotNavChargingEntity, SensorEntity):
    """Start or end of the current charging plan."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, point: str
    ) -> None:
        super().__init__(entry, controller)
        self._point = point
        self._attr_unique_id = f"{entry.entry_id}_{point}"
        self._attr_translation_key = f"plan_{point}"

    @property
    def native_value(self) -> datetime | None:
        if self.controller.plan is None:
            return None
        return (
            self.controller.plan.start_time
            if self._point == "start"
            else self.controller.plan.end_time
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the complete, non-secret charging plan for dashboards."""
        plan = self.controller.plan
        if plan is None:
            return None
        periods = plan.periods or [{"start": plan.start, "end": plan.end}]
        return {
            "periods": periods,
            "amps": plan.amps,
            "phases": plan.phases,
            "power_kw": plan.power_kw,
            "energy_kwh": plan.energy_kwh,
            "price_area": plan.price_area,
            "unpriced": plan.unpriced,
        }


class SiteStateEntity(SpotNavSiteEntity, SensorEntity):
    """The site capacity controller's own state.

    One named state instead of several booleans. Raw per-phase detail (never a webhook ID or other
    secret) is exposed only as attributes, like `ConnectionEntity`.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _attr_translation_key = "site_state"

    def __init__(self, entry: ConfigEntry, controller: SiteCapacityController) -> None:
        super().__init__(entry, controller)
        self._attr_unique_id = f"{entry.entry_id}_site_state"

    @property
    def native_value(self) -> str:
        return self.controller.result.state

    def _site_members(self) -> list[str]:
        return list(self.controller.config.get(CONF_CHARGER_ENTRY_IDS) or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self.controller.result
        snapshot = self.controller.evaluation.capability
        attributes: dict[str, Any] = {}
        if not any(controller_for(self.hass, charger_id) for charger_id in self._site_members()):
            # A site with no charger says how to add one, where its own device page shows it.
            attributes["next_step"] = add_charger_hint(flow_language(self.hass))
        return {
            **attributes,
            "reason": result.reason,
            "measurement_mode": result.measurement_mode,
            "confidence": result.confidence,
            "limiting_phase": result.limiting_phase,
            "max_age_s": result.max_age_s,
            # The site's own measured total, never a requested/setpoint/estimated value.
            "measured_site_current_a": dict(result.measured_phase_current_a),
            "phase_age_s": dict(result.phase_age_s),
            # `phase_liveness` decides whether a value is accepted for the proposals below
            # (`fresh` and `confirmed_unchanged` are; a `confirmed_unchanged` phase comes off
            # headroom by `CONFIRMED_UNCHANGED_MARGIN_A` and `confidence` reports "guarded").
            # See `classify_phase_liveness`. `phase_report_age_s` is the raw number behind it
            # (`last_reported`, not `last_updated`).
            "phase_report_age_s": dict(result.phase_report_age_s),
            "phase_liveness": dict(result.phase_liveness),
            # Diagnostic only, never a charging current: one phase's signed active power in watts
            # (negative = exporting, same convention as `grid_active_power`), derived mode only;
            # `None` when the phase is unusable, with `phase_signed_active_power_reason` saying why.
            "phase_signed_active_power_w": dict(result.phase_signed_active_power_w),
            "phase_signed_active_power_age_s": dict(result.phase_signed_active_power_age_s),
            "phase_signed_active_power_reason": dict(result.phase_signed_active_power_reason),
            # How each phase's current was obtained: `measured` (the meter's own current, as |I|),
            # `apparent` (S / U), `reactive` (sqrt(P^2 + Q^2) / U) or `estimated`
            # (|P| / (U x `estimated_power_factor`)). An estimate is never lower than the true
            # current for a power factor of at least that assumption and understates below it, so
            # `current_estimated` is stated wherever the current is shown.
            "phase_current_basis": dict(result.phase_current_basis),
            "current_estimated": result.current_estimated,
            "estimated_power_factor": result.estimated_power_factor,
            # Four distinct headroom numbers; never conflate them (see site/site_capacity.py).
            # 1. "measured_margin_a": the raw margin at the main fuse, crediting no charger's
            #    own current.
            "measured_margin_a": dict(result.measured_margin_a),
            # 2. "phase_headroom_a": what the proposals below were computed from; always identical
            #    to (1) in every mode.
            "phase_headroom_a": dict(result.phase_headroom_a),
            # 3. "calculated_headroom_after_ev_credit_a": headroom if a charger's own measured
            #    draw were credited back. Diagnostic only, not proven safe to rely on (see the
            #    "Scalar current subtraction" section of site/site_capacity.py).
            "calculated_headroom_after_ev_credit_a": dict(
                result.calculated_headroom_after_ev_credit_a
            ),
            # 4. "estimated_headroom_if_battery_yields_a": diagnostic only, never a safe limit:
            #    extra headroom if a home battery stopped charging now, layered on (2).
            # `battery_yield_basis` says how far to trust it: "measured_per_phase",
            # "assumed_equal_split" (a whole-site aggregate divided evenly, an explicit
            # assumption) or "unknown" (every phase `None`, never guessed).
            "estimated_headroom_if_battery_yields_a": dict(
                result.estimated_headroom_if_battery_yields_a
            ),
            "battery_yield_basis": result.battery_yield_basis,
            # Diagnostic; see SiteCapacityController's class doc.
            "membership_conflicts": [
                {
                    "charger_entry_id": conflict.charger_entry_id,
                    "conflicting_site_entry_id": conflict.conflicting_site_entry_id,
                    "conflicting_site_title": conflict.conflicting_site_title,
                }
                for conflict in self.controller.membership_conflicts
            ],
            # Compact capability/health snapshot (see vehicles/capability.py); full detail is in diagnostics.
            "site_measurement_health": snapshot.site_measurement.health,
            "site_measurement_configured": snapshot.site_measurement.configured,
            "charger_control_available": snapshot.charger_control_available,
            "current_control_available": snapshot.current_control_available,
            "calculation_available": snapshot.load_balancing.calculation_available,
            "has_unknown_active_request": snapshot.load_balancing.has_unknown_active_request,
            "blocking_reasons": list(snapshot.load_balancing.blocking_reasons),
            # `available`: this site could be actively controlled now (healthy measurement, no
            # membership conflict, a charger that opted into the write path). `enabled`: this
            # installation turned it on. Reported separately; neither alone says whether active
            # load balancing is running. See vehicles/capability.py.
            "active_load_balancing_available": snapshot.load_balancing.active_available,
            "active_load_balancing_enabled": snapshot.load_balancing.active_enabled,
            # Whether solar and hybrid can run on this site's measurement, and the stable reason when
            # not (a direct site needs the meter's total grid power).
            "solar_capable": snapshot.solar.capable,
            "solar_reason": snapshot.solar.reason,
            # The meter's total grid power, its age and the export derived from it.
            "grid_power": self.controller.evaluation.grid_power,
            # Yield-verified stepping (see `site_capacity_controller.yield_stepping_snapshot`): one
            # entry per associated charger, present even while the site option is off.
            "yield_stepping": self.controller.yield_stepping_snapshot,
            # The battery-on-the-fuse probe (`site/battery_probe.py`): one entry per associated
            # charger with its state and last outcome.
            "battery_probe": self.controller.battery_probe_snapshot,
            # The resumes of a charge load balancing paused: how many in ten minutes, and the back-off left after
            # too many (`site_capacity_controller.RESUME_LIMIT`).
            "balancing_resume": self.controller.balancing_resume_snapshot(),
            # Solar surplus priority: `car_first` or `battery_first`, present even when never stored.
            "solar_priority": self.controller.config.get(
                CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY
            ),
            # Solar surplus execution: one entry per associated charger, present even when its
            # strategy is not `solar`, mirroring `yield_stepping`.
            "solar_surplus": self.controller.solar_surplus_snapshot,
            # Hybrid diagnostics: one entry per associated charger, mirroring `solar_surplus`.
            "hybrid": self.controller.hybrid_snapshot,
        }


class ChargerProposedCurrentEntity(SpotNavSiteEntity, SensorEntity):
    """One associated charger's proposed current from the site controller.

    Observation and comparison only; see `site/site_capacity_controller.py` for whether and when
    a proposal is applied.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _attr_translation_key = "charger_proposed_current"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(
        self,
        entry: ConfigEntry,
        controller: SiteCapacityController,
        charger_entry_id: str,
        charger_title: str,
    ) -> None:
        super().__init__(entry, controller)
        self._charger_entry_id = charger_entry_id
        self._attr_unique_id = f"{entry.entry_id}_proposed_{charger_entry_id}"
        self._attr_translation_placeholders = {"charger": charger_title}

    def _allocation(self) -> Any | None:
        for allocation in self.controller.result.allocations:
            if allocation.charger_entry_id == self._charger_entry_id:
                return allocation
        return None

    @property
    def native_value(self) -> float | None:
        allocation = self._allocation()
        return allocation.proposed_current_a if allocation else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        allocation = self._allocation()
        if allocation is None:
            return None
        charger_controller = controller_for(self.controller.hass, self._charger_entry_id)
        setpoint_current_a = (
            None if charger_controller is None else charger_controller.setpoint_current_a
        )
        evaluation = self.controller.evaluation
        measured = evaluation.charger_measured_current.get(self._charger_entry_id)
        capability = evaluation.capability.charger_measurement.get(self._charger_entry_id)
        regulator_decision = self.controller.regulator_decisions.get(self._charger_entry_id)
        return {
            "charger_entry_id": allocation.charger_entry_id,
            # Requested, setpoint and measured values stay explicitly distinct.
            "requested_current_a": allocation.requested_current_a,
            "setpoint_current_a": setpoint_current_a,
            "measured_charger_current_a": None
            if measured is None
            else {
                "L1": measured.l1.value,
                "L2": measured.l2.value,
                "L3": measured.l3.value,
            },
            "measured_charger_current_health": capability.health if capability else "not_configured",
            "state": allocation.state,
            "reason": allocation.reason,
            "limiting_phase": allocation.limiting_phase,
            # Best-effort load balancing (regulator): a separate diagnostic computation
            # from "proposed_current_a"/"state"/"reason" above, never sent to the charger.
            "regulator_proposed_current_a": (
                regulator_decision.proposed_current_a if regulator_decision else None
            ),
            "regulator_reason": regulator_decision.reason if regulator_decision else None,
            "regulator_limiting_phase": (
                regulator_decision.limiting_phase if regulator_decision else None
            ),
        }

class AutoProposalSensor(SpotNavAutoEntity, SensorEntity):
    """Base for the sensors that describe the latest proposal.

    A proposal is not necessarily the plan on the charger: a proposal waiting at a window boundary
    reports `applied: false` while `applied_identity` names the plan in force.
    """

    def proposal(self):
        snapshot = self.snapshot
        return None if snapshot is None else snapshot.proposal

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self.snapshot
        return {
            "applied": None if snapshot is None else snapshot.applied,
            "applied_identity": None if snapshot is None else snapshot.applied_identity,
            "pending_identity": None if snapshot is None else snapshot.pending_identity,
        }


class AutoPlanStateSensor(SpotNavAutoEntity, SensorEntity):
    """The Auto calculation's own state, with the compact facts that explain it.

    The native state is the stable proposal state, so automations can trigger on it. No price
    interval array, relay document or identifier appears here.
    """

    _attr_translation_key = "auto_plan_state"

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="auto_plan_state")

    @property
    def native_value(self) -> str | None:
        snapshot = self.snapshot
        return None if snapshot is None else snapshot.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self.snapshot
        if snapshot is None:
            return {"reason": None, "missing": [], "historical": None}
        return {
            "reason": snapshot.reason,
            "missing": list(snapshot.missing),
            "area_id": snapshot.area_id,
            "currency": snapshot.currency,
            "major_unit": snapshot.major_unit,
            "minor_unit": snapshot.minor_unit,
            "price_state": snapshot.price_state,
            "price_identity": snapshot.price_identity,
            "priced_slots": snapshot.priced_slots,
            "unpriced_slots": snapshot.unpriced_slots,
            "unpriced": snapshot.unpriced,
            "calculated_at": snapshot.calculated_at,
            "price_wait": snapshot.price_wait,
            "publication_at": snapshot.publication_at,
            # `historical`: restored from storage rather than calculated by this process.
            "historical": not snapshot.in_process,
            "applied": snapshot.applied,
            "last_error_code": snapshot.last_error_code,
        }


class AutoExecutionStateSensor(SpotNavAutoEntity, SensorEntity):
    """What the charger is doing about the proposal: the stable execution fact.

    `proposal_ready`: a plan exists; `scheduled`: installed; `apply_pending`: waiting for a window
    boundary; `paused` / `execution_error`: nothing applied.
    """

    _attr_translation_key = "auto_execution_state"

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="auto_execution_state")

    @property
    def native_value(self) -> str | None:
        snapshot = self.snapshot
        return None if snapshot is None else snapshot.execution

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self.snapshot
        executor = self._auto.executor
        settings = self.settings
        return {
            # A bounded pause that has run out suspends nothing. Without a boundary there is no
            # clock to ask, so the stored fact stands.
            "paused": None
            if settings is None
            else (settings.execution_paused if executor is None else executor.paused),
            "applied_identity": None if snapshot is None else snapshot.applied_identity,
            "pending_identity": None if snapshot is None else snapshot.pending_identity,
            "pending_attempt": None if executor is None else executor.pending_attempt,
            "execution_error": None if executor is None else executor.last_error,
        }

#: Bound on the period pairs an attribute carries (the planner's own cap is eight).
MAX_PROPOSAL_PERIODS = 8


class AutoNextStartSensor(AutoProposalSensor):
    """When the next (or currently open) period starts, as a timestamp."""

    _attr_translation_key = "auto_next_start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="auto_next_start")

    @property
    def native_value(self) -> datetime | None:
        return auto_next_period(self.proposal())[0]


class AutoNextEndSensor(AutoProposalSensor):
    """When that same period ends, as a timestamp: the pair the dashboard needs."""

    _attr_translation_key = "auto_next_end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="auto_next_end")

    @property
    def native_value(self) -> datetime | None:
        return auto_next_period(self.proposal())[1]


def auto_next_period(proposal) -> tuple[datetime | None, datetime | None]:
    """The first period that is not already over, or a pair of `None`s, read against the wall clock."""
    if proposal is None:
        return (None, None)
    now = dt_util.utcnow()
    for start, end in proposal.periods:
        if end > now:
            return (start, end)
    return (None, None)


class AutoPeriodsSensor(AutoProposalSensor):
    """How many charging periods the latest proposal uses, and their bounded start/end pairs."""

    _attr_translation_key = "auto_periods"

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="auto_periods")

    @property
    def native_value(self) -> int | None:
        proposal = self.proposal()
        return None if proposal is None else len(proposal.periods)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        proposal = self.proposal()
        attributes = dict(super().extra_state_attributes)
        attributes["periods"] = [
            {"start": start.isoformat(), "end": end.isoformat()}
            for start, end in (() if proposal is None else proposal.periods[:MAX_PROPOSAL_PERIODS])
        ]
        attributes["unpriced"] = None if proposal is None else proposal.unpriced
        return attributes


class AutoCostSensor(AutoProposalSensor):
    """What the latest proposal is estimated to cost, in the market's own currency."""

    _attr_translation_key = "auto_cost"
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="auto_cost")

    @property
    def native_unit_of_measurement(self) -> str | None:
        """The ISO currency, the only monetary identity there is (`kr` alone would not tell SEK from NOK)."""
        proposal = self.proposal()
        return None if proposal is None else proposal.currency

    @property
    def native_value(self) -> float | None:
        proposal = self.proposal()
        return None if proposal is None else proposal.estimated_cost


class AutoEnergySensor(AutoProposalSensor):
    """How much energy the latest proposal plans to deliver."""

    _attr_translation_key = "auto_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="auto_energy")

    @property
    def native_value(self) -> float | None:
        proposal = self.proposal()
        return None if proposal is None else proposal.delivered_kwh


class AutoSettingsRevisionSensor(SpotNavAutoEntity, SensorEntity):
    """The revision of the stored Auto settings: a compare-and-set witness."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _attr_translation_key = "auto_settings_revision"

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="auto_settings_revision")

    @property
    def native_value(self) -> int | None:
        settings = self.settings
        return None if settings is None else settings.revision
