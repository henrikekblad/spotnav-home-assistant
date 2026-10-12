"""Schedule, connection and site-capacity sensors for SpotNav charging control."""

import json
import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry, ConfigEntryChange, SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfEnergy, UnitOfPower
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
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
from .execution.solar_execution import site_solar_surplus
from .setup_hints import add_charger_hint
from .execution.power_energy import fresh_power_w, integrated_energy_unique_id, PowerIntegrator
from .runtime import controller_for
from .sessions.sensors import session_entities
from .vehicles.choices import flow_language
from .site.site_capacity import site_activity
from .site.site_capacity_controller import SiteCapacityController
from .site.solar_surplus import SiteSurplus
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
        entities: list[Any] = [SiteStateEntity(entry, site_controller), SiteSolarSurplusEntity(entry, site_controller)]
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


def rounded_amps(value: float | None) -> float | None:
    """A current as an attribute shows it: to 0.1 A, so float jitter is no change of state."""
    return None if value is None else round(float(value), 1)


class _BuiltOnChangeSiteSensor(SpotNavSiteEntity, SensorEntity):
    """A site sensor whose state and attributes are built when the site changes, not when they are read.

    Home Assistant times the read of a state and its attributes (and warns above 0.4 s); building them in
    the site's change callback keeps that read a lookup, and a write Home Assistant makes on its own
    reuses what was built.

    The site changes with every meter reading (several a second on some meters), so a site change is
    written only when the state or the attributes differ from what was written last: every change of an
    attribute is a new attributes row in the recorder. The attributes therefore carry nothing that moves
    with each reading (no ages, no raw per-phase readings); that detail is in the site's diagnostics.
    """

    _written: tuple[Any, dict[str, Any] | None] | None = None

    async def async_added_to_hass(self) -> None:
        self._rebuild()
        self.async_on_remove(self.controller.add_listener(self._on_site_changed))

    @callback
    def _on_site_changed(self) -> None:
        if self._rebuild():
            self.async_write_ha_state()

    def _rebuild(self) -> bool:
        """Build the state and attributes; whether they differ from the last ones built."""
        built = (self._build_value(), self._build_attributes())
        if built == self._written:
            return False
        self._written = built
        self._attr_native_value, self._attr_extra_state_attributes = built
        return True

    def _build_value(self) -> Any:
        raise NotImplementedError

    def _build_attributes(self) -> dict[str, Any] | None:
        raise NotImplementedError


class SiteStateEntity(_BuiltOnChangeSiteSensor):
    """The site capacity controller's own state.

    One named state instead of several booleans, with the few facts a person reads it for as attributes
    (never a webhook ID or other secret). The per-phase readings, their ages, the headroom figures and the
    per-charger solar, hybrid, yield-stepping, battery-probe and resume detail are in the site's
    diagnostics (`diagnostics._site_diagnostics`), not here: they move with every reading. So does the
    limiting phase where the phases carry about the same load (each phase's report hands it on).
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _attr_translation_key = "site_state"

    # Kept on the entity but out of the recorder's history: settings and capability facts, and the
    # localized hint, which say nothing about a moment.
    _unrecorded_attributes = frozenset(
        {
            "next_step",
            "measurement_mode",
            "current_estimated",
            "membership_conflicts",
            "site_measurement_health",
            "blocking_reasons",
            "active_load_balancing_available",
            "active_load_balancing_enabled",
            "solar_capable",
            "solar_reason",
            "solar_priority",
        }
    )

    def __init__(self, entry: ConfigEntry, controller: SiteCapacityController) -> None:
        super().__init__(entry, controller)
        self._attr_unique_id = f"{entry.entry_id}_site_state"

    def _build_value(self) -> str:
        return self.controller.result.state

    def _site_members(self) -> list[str]:
        return list(self.controller.config.get(CONF_CHARGER_ENTRY_IDS) or [])

    def _build_attributes(self) -> dict[str, Any]:
        result = self.controller.result
        snapshot = self.controller.evaluation.capability
        attributes: dict[str, Any] = {}
        if not any(controller_for(self.hass, charger_id) for charger_id in self._site_members()):
            # A site with no charger says how to add one, where its own device page shows it.
            attributes["next_step"] = add_charger_hint(flow_language(self.hass))
        return {
            **attributes,
            "reason": result.reason,
            # The state in a person's words: `measuring`, `balancing` (active load balancing on) or
            # `not_measuring`; the state itself stays `observing` when healthy.
            "site_activity": site_activity(result.state, self.controller.active_control_enabled),
            "measurement_mode": result.measurement_mode,
            # `guarded` while a phase is trusted only as confirmed unchanged (see `classify_phase_liveness`).
            "confidence": result.confidence,
            # True when a phase's current is estimated from active power alone (`|P| / (U x 0.9)`).
            "current_estimated": result.current_estimated,
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
            # Solar surplus priority: `car_first` or `battery_first`, present even when never stored.
            "solar_priority": self.controller.config.get(
                CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY
            ),
        }


#: The site's solar surplus is written at once when it moves at least this much (watts) from what it shows ...
SURPLUS_STEP_W = 50.0
#: ... and a smaller change once this long (seconds) after the last write, so a meter reporting twice a second
#: does not put a row in the recorder for every reading.
SURPLUS_INTERVAL_S = 10.0
#: Never two writes of a number closer together than this (seconds): a house load swinging by a few hundred
#: watts would otherwise pass the step on every reading.
SURPLUS_MIN_GAP_S = 5.0


def surplus_write_delay(
    shown: dict[str, Any] | None, new: dict[str, Any] | None, elapsed_s: float | None
) -> float | None:
    """How long (seconds) until `new` is written over `shown`, `None` when nothing changed. `shown`/`new` are
    the state and attributes (`None` for unknown); `elapsed_s` is the time since the last write.

    Unknown to a number and back, or another priority, at once; a move of `SURPLUS_STEP_W` after
    `SURPLUS_MIN_GAP_S`; any other change after `SURPLUS_INTERVAL_S`.
    """
    if shown == new:
        return None
    if shown is None or new is None or elapsed_s is None or shown["priority"] != new["priority"]:
        return 0.0
    if abs(new["surplus_w"] - shown["surplus_w"]) >= SURPLUS_STEP_W:
        return max(0.0, SURPLUS_MIN_GAP_S - elapsed_s)
    return max(0.0, SURPLUS_INTERVAL_S - elapsed_s)


def _surplus_shown(surplus: SiteSurplus | None) -> dict[str, Any] | None:
    if surplus is None:
        return None
    return {
        "surplus_w": round(surplus.surplus_w),
        "export_w": round(surplus.export_w),
        "battery_w": round(surplus.battery_w),
        "car_w": round(surplus.car_w),
        "priority": surplus.priority_effective,
    }


class SiteSolarSurplusEntity(SpotNavSiteEntity, SensorEntity):
    """What a car could take from the sun at the site now (`site_solar_surplus`), on the basis solar charges
    on: export, a charging battery under `car_first`, and what the site's cars already draw; a discharging
    battery never. `unknown` without a usable grid reading. Writes are throttled (`surplus_write_delay`)."""

    _attr_translation_key = "solar_surplus"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    def __init__(self, entry: ConfigEntry, controller: SiteCapacityController) -> None:
        super().__init__(entry, controller)
        self._attr_unique_id = f"{entry.entry_id}_solar_surplus"
        self._shown: dict[str, Any] | None = None
        self._written_at: float | None = None
        self._flush_cancel: Callable[[], None] | None = None
        self._flush_due: float | None = None
        # Replaceable in tests.
        self._now: Callable[[], float] = time.monotonic

    async def async_added_to_hass(self) -> None:
        self._shown = _surplus_shown(site_solar_surplus(self.hass, self.controller))
        self._written_at = self._now()
        self.async_on_remove(self.controller.add_listener(self._on_site_changed))
        self.async_on_remove(self._cancel_flush)

    @property
    def native_value(self) -> float | None:
        return None if self._shown is None else self._shown["surplus_w"]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._shown is None:
            return None
        return {key: value for key, value in self._shown.items() if key != "surplus_w"}

    @callback
    def _cancel_flush(self) -> None:
        if self._flush_cancel is not None:
            self._flush_cancel()
            self._flush_cancel = None
        self._flush_due = None

    @callback
    def _on_site_changed(self) -> None:
        if self.controller.rerendering:
            # A solar re-render carries no new reading.
            return
        self._consider()

    @callback
    def _flush(self, _now: Any) -> None:
        self._flush_cancel = None
        self._flush_due = None
        self._consider()

    @callback
    def _consider(self) -> None:
        new = _surplus_shown(site_solar_surplus(self.hass, self.controller))
        now = self._now()
        elapsed_s = None if self._written_at is None else now - self._written_at
        delay = surplus_write_delay(self._shown, new, elapsed_s)
        if delay is None:
            self._cancel_flush()
            return
        if delay > 0:
            due = now + delay
            if self._flush_cancel is None or self._flush_due is None or due < self._flush_due:
                self._cancel_flush()
                self._flush_due = due
                self._flush_cancel = async_call_later(self.hass, delay, self._flush)
            return
        self._cancel_flush()
        self._shown = new
        self._written_at = now
        self.async_write_ha_state()


class ChargerProposedCurrentEntity(_BuiltOnChangeSiteSensor):
    """One associated charger's proposed current from the site controller.

    Observation and comparison only; see `site/site_capacity_controller.py` for whether and when
    a proposal is applied.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _attr_translation_key = "charger_proposed_current"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    # Fixed per charger, or what the regulator only computes for comparison.
    _unrecorded_attributes = frozenset(
        {
            "charger_entry_id",
            "measured_charger_current_health",
            "regulator_proposed_current_a",
            "regulator_reason",
        }
    )

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

    def _build_value(self) -> float | None:
        allocation = self._allocation()
        return rounded_amps(allocation.proposed_current_a) if allocation else None

    def _build_attributes(self) -> dict[str, Any] | None:
        allocation = self._allocation()
        if allocation is None:
            return None
        charger_controller = controller_for(self.controller.hass, self._charger_entry_id)
        setpoint_current_a = (
            None if charger_controller is None else charger_controller.setpoint_current_a
        )
        capability = self.controller.evaluation.capability.charger_measurement.get(self._charger_entry_id)
        regulator_decision = self.controller.regulator_decisions.get(self._charger_entry_id)
        # The charger's measured current per phase moves with every reading; it is the charger's own
        # sensor, and the site's diagnostics (`history_60min`, the regulator's basis) carry it. So does the
        # limiting phase on a site whose phases carry about the same load, and is in the diagnostics'
        # `result.allocations` and `regulator`.
        return {
            "charger_entry_id": allocation.charger_entry_id,
            # Requested and setpoint values stay explicitly distinct.
            "requested_current_a": rounded_amps(allocation.requested_current_a),
            "setpoint_current_a": rounded_amps(setpoint_current_a),
            "measured_charger_current_health": capability.health if capability else "not_configured",
            "state": allocation.state,
            "reason": allocation.reason,
            # Best-effort load balancing (regulator): a separate diagnostic computation
            # from "proposed_current_a"/"state"/"reason" above, never sent to the charger.
            "regulator_proposed_current_a": (
                rounded_amps(regulator_decision.proposed_current_a) if regulator_decision else None
            ),
            "regulator_reason": regulator_decision.reason if regulator_decision else None,
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
