"""Home Assistant diagnostics for SpotNav charging control.

Redacts the webhook ID and never includes a webhook/pairing URL (derived from it), so a dump is
safe to attach to a public bug report. Everything else (entity IDs, reason codes, measurement
ages, controller state) is included in full.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ENTRY_TYPE, CONF_OCPP_CHARGE_POINT_ID, CONF_WEBHOOK_ID, ENTRY_TYPE_SITE
from .pricing.price_repository import catalogue_summary, day_summary, index_summary
from .runtime import controller_for, domain_data, executor_for, preview_for, site_controller_for
from .site.regulator import RegulatorDecision
from .site.site_capacity import ChargerAllocation, SiteCapacityResult
from .vehicles.capability import SiteCapabilitySnapshot


_LOGGER = logging.getLogger(__name__)

TO_REDACT = {CONF_WEBHOOK_ID, CONF_OCPP_CHARGE_POINT_ID, "charge_point_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one SpotNav config entry."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        return _site_diagnostics(hass, entry)
    return _charger_diagnostics(hass, entry)


def _charger_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    controller = controller_for(hass, entry.entry_id)
    resolved = controller.resolve_current() if controller else None
    return {
        "entry_type": "charger",
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "price_data": _price_data(hass),
        "auto_price": _auto_price(hass, entry),
        "controller": None
        if controller is None
        else {
            "charge_control": controller.charge_control,
            "current_limit": controller.current_limit,
            "charging": controller.charging,
            "requested_current_a": controller.requested_current_a,
            "setpoint_current_a": controller.setpoint_current_a,
            "resolved_current": None
            if resolved is None
            else {"amps": resolved.amps, "source": resolved.source},
            # OCPP identity and role facts: which connector a write would go to, whether a unique
            # target exists, whether the station ceiling and session limit were found. Role facts,
            # not entity ids, so the redaction above stays the only place that knows what is private.
            "ocpp": controller.ocpp_control_facts(),
            # How the charger is driven and under which write policy (`execution/charger_adapter.py`):
            # kinds and facts only, never an entity id.
            "adapter": {
                "platform": controller.adapter.platform,
                "start_stop": controller.adapter.path.describe()["kind"],
                "current": controller.adapter.current.kind,
                "current_enabled": controller.adapter.current_enabled,
                "policy": controller.adapter.policy.as_dict(),
                "capabilities": controller.adapter.capabilities.as_dict(),
            },
            "plan": asdict(controller.plan) if controller.plan else None,
        },
    }


def _site_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    controller = site_controller_for(hass, entry.entry_id)
    return {
        "entry_type": "site",
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "price_data": _price_data(hass),
        "result": None if controller is None else _result_to_dict(controller.result),
        "capability": None
        if controller is None
        else _capability_to_dict(controller.capability_snapshot),
        "membership_conflicts": []
        if controller is None
        else [
            {
                "charger_entry_id": conflict.charger_entry_id,
                "conflicting_site_entry_id": conflict.conflicting_site_entry_id,
                "conflicting_site_title": conflict.conflicting_site_title,
            }
            for conflict in controller.membership_conflicts
        ],
        # Best-effort load balancing (regulator): a separate, diagnostic-only computation
        # from "result" above, never sent to a charger.
        "regulator": {}
        if controller is None
        else {
            charger_entry_id: _regulator_decision_to_dict(decision)
            for charger_entry_id, decision in controller.regulator_decisions.items()
        },
    }


def _regulator_decision_to_dict(decision: RegulatorDecision) -> dict[str, Any]:
    return {
        "proposed_current_a": decision.proposed_current_a,
        "reason": decision.reason,
        "limiting_phase": decision.limiting_phase,
        "basis": {phase: asdict(entry) for phase, entry in decision.basis.items()},
    }


def _result_to_dict(result: SiteCapacityResult) -> dict[str, Any]:
    return {
        "state": result.state,
        "reason": result.reason,
        "measurement_mode": result.measurement_mode,
        "confidence": result.confidence,
        "measured_phase_current_a": dict(result.measured_phase_current_a),
        "phase_age_s": dict(result.phase_age_s),
        # `phase_liveness` decides whether a value is accepted for the headroom calculation
        # (`fresh` and `confirmed_unchanged` are; a `confirmed_unchanged` phase comes off headroom
        # by `CONFIRMED_UNCHANGED_MARGIN_A`). See `classify_phase_liveness` in site/site_capacity.py.
        "phase_report_age_s": dict(result.phase_report_age_s),
        "phase_liveness": dict(result.phase_liveness),
        # Diagnostic only, never a charging current: one phase's signed active power in watts
        # (derived mode only; `None` in direct mode or when the phase is unusable, with
        # `phase_signed_active_power_reason` saying why). See `_phase_signed_active_power`.
        "phase_signed_active_power_w": dict(result.phase_signed_active_power_w),
        "phase_signed_active_power_age_s": dict(result.phase_signed_active_power_age_s),
        "phase_signed_active_power_reason": dict(result.phase_signed_active_power_reason),
        # How each phase's current was obtained (`measured`, `apparent`, `reactive` or `estimated`);
        # `current_estimated` is true when any phase is estimated from active power alone, which
        # assumes `estimated_power_factor` (see `estimate_current_from_power`).
        "phase_current_basis": dict(result.phase_current_basis),
        "current_estimated": result.current_estimated,
        "estimated_power_factor": result.estimated_power_factor,
        # Three distinct headroom numbers (see site/site_capacity.py). Only `phase_headroom_a`
        # feeds `allocations`; the other two are diagnostic.
        "phase_headroom_a": dict(result.phase_headroom_a),
        "measured_margin_a": dict(result.measured_margin_a),
        "calculated_headroom_after_ev_credit_a": dict(result.calculated_headroom_after_ev_credit_a),
        "estimated_headroom_if_battery_yields_a": dict(result.estimated_headroom_if_battery_yields_a),
        "battery_yield_basis": result.battery_yield_basis,
        "limiting_phase": result.limiting_phase,
        "max_age_s": result.max_age_s,
        "allocations": [_allocation_to_dict(allocation) for allocation in result.allocations],
    }


def _price_data(hass: HomeAssistant) -> dict[str, Any]:
    """The shared price manager and repository at dump time, compactly.

    Both are domain-scoped singletons, so every entry's dump describes the same one. Redaction is
    by construction: subscriber counts but no owner ids or tokens, the repository's own day
    summaries but no interval arrays, and no response body, listener, webhook id or exception text.
    Never raises; every lifecycle state yields an honest section.
    """
    manager = domain_data(hass).price_refresh
    repository = domain_data(hass).price_repository
    if manager is None and repository is None:
        return {"available": False, "reason": "not_set_up"}

    data: dict[str, Any] = {"available": True}
    if manager is not None:
        snapshot = manager.manager_snapshot()
        data["running"] = snapshot.running
        data["shutdown"] = snapshot.shutdown
        data["subscribed_areas"] = len(snapshot.areas)
        data["areas"] = [
            {
                "area": area.area_id,
                "subscribers": area.subscriber_count,
                "state": area.state,
                "reason": area.reason,
                "today": area.today.isoformat(),
                "tomorrow": area.tomorrow.isoformat(),
                # Three-valued: with no current index authority the answer is `None` (unknown),
                # never inferred by negating "waiting".
                "today_authority": area.today_authority,
                "tomorrow_authority": area.tomorrow_authority,
                "tomorrow_published": _tomorrow_published(area),
                "today_state": area.today_state,
                "tomorrow_state": area.tomorrow_state,
                "index_state": area.index_state,
                "index_revision": area.index_revision,
                "currency": None if area.catalogue is None else area.catalogue.currency,
                "major_unit": None if area.catalogue is None else area.catalogue.major_unit,
                "minor_unit": None if area.catalogue is None else area.catalogue.minor_unit,
                "timezone": None if area.catalogue is None else area.catalogue.tz,
                "fetched_at": _iso(area.fetched_at),
                "attempted_at": _iso(area.attempted_at),
                "next_attempt": _iso(area.next_attempt),
                "today_data": day_summary(area.today_snapshot),
                "tomorrow_data": day_summary(area.tomorrow_snapshot),
            }
            for area in snapshot.areas
        ]
        data["catalogue"] = catalogue_summary(snapshot.catalogue)
        data["index"] = index_summary(snapshot.index)
    if repository is not None and manager is None:
        data["catalogue"] = catalogue_summary(repository.catalogue_snapshot())
        data["index"] = index_summary(repository.index_snapshot())
    return data


def _auto_price(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """The Auto preview state for one entry, compactly and never raising.

    Answers "why is this charger not charging by itself" without leaking anything: it reads a frozen
    snapshot and copies fields by name (no intervals, documents, webhook id, pairing URL, entity or
    owner ids, exception text). A proposal restored from the store is marked `historical`. `live`
    says whether a preview controller is registered. An unexpected failure becomes
    `available: False` with a stable reason.
    """
    try:
        return _auto_price_section(hass, entry)
    except Exception as err:  # noqa: BLE001 - a dump must never fail the diagnostics call
        _LOGGER.warning("Auto diagnostics could not be produced: %s", type(err).__name__)
        return {"available": False, "reason": "unavailable"}


def _auto_price_section(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """The Auto preview state for one entry, from the snapshot and the store only."""
    controller = preview_for(hass, entry.entry_id)
    store = domain_data(hass).auto_store
    if controller is None and store is None:
        return {"available": False, "reason": "not_set_up"}
    # `live` distinguishes "no preview loaded right now" from "has one"; state and reason only
    # exist while a controller is registered.
    data: dict[str, Any] = {"available": True, "live": controller is not None}
    # Execution facts when a boundary is registered: an opaque identity, the two revisions behind
    # it, a pause flag and a stable error code; never the plan, a price document or a slot series.
    executor = executor_for(hass, entry.entry_id)
    if executor is not None:
        applied = executor.applied
        pending = executor.pending
        data.update(
            execution=executor.execution_state(),
            paused=executor.paused,
            applied_identity=None if applied is None else applied.identity,
            applied_settings_revision=None if applied is None else applied.settings_revision,
            applied_price_identity=None if applied is None else applied.price_identity,
            pending_identity=None if pending is None else pending.identity,
            pending_attempt=executor.pending_attempt,
            execution_error=executor.last_error,
        )

    if controller is not None:
        snapshot = controller.snapshot()
        data.update(
            state=snapshot.state,
            reason=snapshot.reason,
            settings_revision=snapshot.settings_revision,
            generation=snapshot.generation,
            area=snapshot.area_id,
            currency=snapshot.currency,
            major_unit=snapshot.major_unit,
            minor_unit=snapshot.minor_unit,
            timezone=snapshot.timezone,
            missing=list(snapshot.missing),
            price_state=snapshot.price_state,
            price_identity=snapshot.price_identity,
            today=snapshot.today,
            tomorrow=snapshot.tomorrow,
            calculated_at=_iso(snapshot.calculated_at),
            priced_slots=snapshot.priced_slots,
            unpriced_slots=snapshot.unpriced_slots,
            unpriced=snapshot.unpriced,
            applied=snapshot.applied,
            error_code=snapshot.last_error_code,
            proposal=_proposal_summary(
                snapshot.proposal, historical=False, applied=snapshot.applied
            ),
        )
    if store is not None:
        stored = store.proposal(entry.entry_id)
        if controller is None or data.get("proposal") is None:
            data["settings_revision"] = store.settings(entry.entry_id).revision
            data["proposal"] = (
                None if stored is None else {
                    "historical": True,
                    "applied": False,
                    "calculated_at": _iso(stored.calculated_at),
                    "state": stored.state,
                    "reason": stored.reason,
                    "settings_revision": stored.settings_revision,
                    "area": stored.area_id,
                    "slots_needed": stored.slots_needed,
                    "priced_slots": stored.priced_slots,
                    "unpriced_slots": stored.unpriced_slots,
                    "unpriced": stored.unpriced,
                    "delivered_kwh": stored.delivered_kwh,
                    "estimated_cost": stored.estimated_cost,
                    "distance_mil": stored.distance_mil,
                    "periods": [
                        [start, end]
                        for start, end in zip(stored.period_starts, stored.period_ends)
                    ],
                    "price_identity": stored.price_identity,
                }
            )
    return data


def _proposal_summary(
    proposal: Any, *, historical: bool, applied: bool = False
) -> dict[str, Any] | None:
    """A plan summarised for a dump: counts, money and period bounds, never the slots.

    `applied` is passed in so the summary and the `applied` beside it cannot disagree; a restored
    proposal keeps the default.
    """
    if proposal is None:
        return None
    return {
        "historical": historical,
        "applied": applied,
        "in_process": not historical,
        "slots_needed": proposal.slots_needed,
        "priced_slots": proposal.priced_slots,
        "unpriced_slots": proposal.unpriced_slots,
        "unpriced": proposal.unpriced,
        "power_kw": proposal.power_kw,
        "requested_kwh": proposal.requested_kwh,
        "delivered_kwh": proposal.delivered_kwh,
        "distance_mil": proposal.distance_mil,
        "estimated_cost": proposal.estimated_cost,
        "periods": [
            [_iso(start), _iso(end)] for start, end in proposal.periods
        ],
    }


def _tomorrow_published(area: Any) -> bool | None:
    """Tomorrow's authority as `True`, `False` or `None` (nobody currently saying)."""
    if area.tomorrow_authority == "listed":
        return True
    if area.tomorrow_authority == "not_listed":
        return False
    return None


def _iso(moment: Any) -> str | None:
    return None if moment is None else moment.isoformat()


def _allocation_to_dict(allocation: ChargerAllocation) -> dict[str, Any]:
    return {
        "charger_entry_id": allocation.charger_entry_id,
        "requested_current_a": allocation.requested_current_a,
        "proposed_current_a": allocation.proposed_current_a,
        "limiting_phase": allocation.limiting_phase,
        "state": allocation.state,
        "reason": allocation.reason,
    }


def _capability_to_dict(snapshot: SiteCapabilitySnapshot) -> dict[str, Any]:
    return {
        "planning_available": snapshot.planning_available,
        "charger_control_available": snapshot.charger_control_available,
        "current_control_available": snapshot.current_control_available,
        "site_measurement": asdict(snapshot.site_measurement),
        "charger_measurement": {
            charger_entry_id: asdict(health)
            for charger_entry_id, health in snapshot.charger_measurement.items()
        },
        "load_balancing": asdict(snapshot.load_balancing),
    }
