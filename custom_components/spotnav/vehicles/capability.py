"""A structured capability/health snapshot for one site.

Summarises a `SiteCapacityResult` (plus a little config context) into what
currently works, what doesn't and why, to back a status entity and diagnostics.

`load_balancing` keeps two separate fields: `active_available` (the site *could*
be actively controlled now) and `active_enabled` (the installation opted in).
Neither enables or performs control; both come from the facts
`SiteCapacityController`'s apply path requires before it may write.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from ..site.site_capacity import (
    ACTIVE_CONTROL_READY,
    calculate_site_capacity,
    ChargerRequest,
    Confidence,
    MeasurementMode,
    SiteCalculationConfig,
)


HealthState = Literal[
    "not_configured",
    "configured_missing",
    "stale",
    "invalid",
    "inconsistent",
    "healthy",
]


@dataclass(frozen=True, slots=True)
class MeasurementHealth:
    """The site's measurement health, evaluated as if the site were enabled."""

    configured: bool
    mode: MeasurementMode | None
    health: HealthState
    confidence: Confidence
    age_s: float | None


@dataclass(frozen=True, slots=True)
class ChargerMeasurementHealth:
    """One associated charger's own optional measured-current health."""

    configured: bool
    health: HealthState
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class LoadBalancingCapability:
    calculation_available: bool
    calculation_enabled: bool
    has_unknown_active_request: bool
    blocking_reasons: tuple[str, ...]
    # Defaults are False: a capability nobody reported is not announced.
    active_available: bool = False
    active_enabled: bool = False


@dataclass(frozen=True, slots=True)
class SiteCapabilitySnapshot:
    """The full capability ladder for one site."""

    planning_available: bool
    charger_control_available: bool
    current_control_available: bool
    site_measurement: MeasurementHealth
    charger_measurement: Mapping[str, ChargerMeasurementHealth]
    load_balancing: LoadBalancingCapability


def build_capability_snapshot(
    *,
    config: SiteCalculationConfig,
    requests: Sequence[ChargerRequest],
    charger_has_charge_control: Mapping[str, bool],
    charger_has_current_control: Mapping[str, bool],
    membership_conflict_charger_ids: Sequence[str],
    charger_is_commandable: Mapping[str, bool] | None = None,
    active_control_enabled: bool = False,
) -> SiteCapabilitySnapshot:
    """Build the snapshot. `requests` is what would be passed to `calculate_site_capacity` now.

    Measurement health and `active_available` are evaluated as if the site were
    enabled, so a user can see a setup would work before switching it on;
    `calculation_enabled` / `active_enabled` report the real toggles.

    `charger_is_commandable` is the exact predicate the apply path gates each
    write on (`current_control` is `CURRENT_CONTROL_CHANGE_CONFIGURATION`), not
    derived from `charger_has_current_control`: a charger with a current entity
    but no opt-in is never written to. `None` means "no commandable charger".
    """
    probe_config = replace(config, enabled=True)
    probe_result = calculate_site_capacity(probe_config, requests)

    site_measurement = _site_measurement_health(config, probe_result)
    charger_measurement = {
        request.charger_entry_id: _charger_measurement_health(request, config.max_age_s)
        for request in requests
    }
    has_unknown_active_request = any(
        allocation.state == "requested_current_unknown" for allocation in probe_result.allocations
    )

    blocking: list[str] = []
    if site_measurement.health != "healthy":
        blocking.append(f"site_measurement_{site_measurement.health}")
    if membership_conflict_charger_ids:
        blocking.append("duplicate_membership")

    # Mirrors exactly what `SiteCapacityController._async_apply_active_control`
    # requires before writing: the global ready flag, healthy site measurement,
    # no membership conflict and at least one commandable charger. The site's own
    # opt-in is `active_enabled` and deliberately not part of this.
    commandable_chargers = charger_is_commandable or {}
    active_available = (
        ACTIVE_CONTROL_READY
        and site_measurement.health == "healthy"
        and not membership_conflict_charger_ids
        and any(commandable_chargers.values())
    )

    load_balancing = LoadBalancingCapability(
        calculation_available=site_measurement.health == "healthy" and not membership_conflict_charger_ids,
        calculation_enabled=config.enabled,
        has_unknown_active_request=has_unknown_active_request,
        blocking_reasons=tuple(blocking),
        active_available=active_available,
        active_enabled=active_control_enabled,
    )

    return SiteCapabilitySnapshot(
        planning_available=True,
        charger_control_available=all(charger_has_charge_control.values())
        if charger_has_charge_control
        else False,
        current_control_available=any(charger_has_current_control.values()),
        site_measurement=site_measurement,
        charger_measurement=charger_measurement,
        load_balancing=load_balancing,
    )


def _site_measurement_health(config: SiteCalculationConfig, probe_result) -> MeasurementHealth:
    ages = [age for age in probe_result.phase_age_s.values() if age is not None]
    max_age = max(ages) if ages else None

    if config.measurement_mode == "unavailable" or config.main_fuse_a is None or config.main_fuse_a <= 0:
        return MeasurementHealth(
            configured=False, mode=None, health="not_configured", confidence="none", age_s=None
        )

    state = probe_result.state
    if state == "missing_measurements":
        health: HealthState = "configured_missing"
    elif state == "stale_measurements":
        health = "stale"
    elif state == "invalid_measurements":
        health = "invalid"
    elif state == "observing":
        health = "healthy"
    else:  # pragma: no cover - defensive; enabled=True rules out "disabled"
        health = "invalid"

    return MeasurementHealth(
        configured=True,
        mode=config.measurement_mode,
        health=health,
        confidence=probe_result.confidence,
        age_s=max_age,
    )


def _charger_measurement_health(request: ChargerRequest, max_age_s: float) -> ChargerMeasurementHealth:
    if request.measured_current_a is None:
        return ChargerMeasurementHealth(configured=False, health="not_configured", confidence="none")
    phases_used = request.phases_used()
    if phases_used is None:
        return ChargerMeasurementHealth(configured=True, health="invalid", confidence="none")
    values = [request.measured_current_a.get(phase) for phase in phases_used]
    if any(v.problem == "invalid" for v in values):
        return ChargerMeasurementHealth(configured=True, health="invalid", confidence="low")
    if any(v.value is None for v in values):
        return ChargerMeasurementHealth(configured=True, health="configured_missing", confidence="none")
    if any(v.age_s is None or v.age_s > max_age_s for v in values):
        return ChargerMeasurementHealth(configured=True, health="stale", confidence="low")
    return ChargerMeasurementHealth(configured=True, health="healthy", confidence="high")
