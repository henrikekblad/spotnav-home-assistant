"""Best-effort load-balancing decisions: the current a charger would be given.

Pure decision logic on top of the verified measurements in
`site/site_capacity.py`; nothing here calls a Home Assistant service. The
decisions are acted on by `SiteCapacityController._async_apply_active_control`,
which needs `ACTIVE_CONTROL_READY` and the site's `CONF_ACTIVE_CONTROL_ENABLED`
opt-in, and re-checks live readings first (`decision_still_applyable`).

Per phase:

- Increase: capped by the uncredited margin (`measured_margin_a`, |net|), or where the direction
  is known by the worst case on the signed rest of the site (`phase_signed_margin_a`).
- Decrease: the risky direction, since reducing import raises net current
  magnitude when the site nets to export. Allowed only for net import, with
  `delta_w <= help_margin_factor * total` and a run of consistent-sign
  readings. This is an active-power check only; reactive power and desync are
  not modelled.
- Active overload (margin negative) is checked first. Import overload proposes
  a lower current directly; export or unclear overload yields `None`.

Outcomes stay distinct: a legal setpoint (`0` or `>= min_current_a`), a
confident pause (`0.0`, `_legalize_or_pause`), and `None` for "no reliable
action" (never just because a computed value was small).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, Mapping, Sequence

from .site_capacity import (
    allocation_order,
    ChargerRequest,
    DirectPhaseMeasurement,
    PhaseName,
    PhaseUnusableReason,
    PhaseValue,
    round_down_to_step,
    SiteCapacityResult,
)


# Direction of a phase's total signed active power relative to a zero band.
DirectionClass = Literal["net_import", "net_export", "near_zero"]

# Every reason reported for a phase or a whole charger.
RegulatorReason = Literal[
    "derived_mode_required",
    "requested_current_unknown",
    "charger_phase_wiring_unknown",
    "charger_measurement_unusable",
    "phase_measurement_unusable",
    "no_change_requested",
    "increase_within_safe_uncredited_margin",
    "decrease_confirmed_safe",
    "decrease_would_increase_net_current_kept_unchanged",
    "near_zero_crossing_kept_unchanged",
    "insufficient_consecutive_confirmation_kept_unchanged",
    "decrease_margin_exceeded_kept_unchanged",
    "reducing_current_due_to_active_import_overload",
    "active_export_overload_no_reliable_action",
    "active_overload_direction_unclear_no_reliable_action",
    # Ceiling fell between 0 and `min_current_a`: an explicit pause (0.0).
    "paused_safe_current_below_charger_minimum",
    # A sibling on a shared phase was undetermined (only `allocate_regulator_decisions`).
    "shared_phase_blocked_by_another_chargers_undetermined_decision",
]

# Decisions in which the fuse is in danger *now*: a write the charger cannot take in time must then
# stop the charge (`ChargingController.async_apply_regulated_current`), not wait.
MUST_LOWER_REASONS = frozenset({"reducing_current_due_to_active_import_overload"})

# Within this band (W) of zero net power a sign-based prediction is not trusted.
DEFAULT_ZERO_MARGIN_W = 500.0

# Fraction of the theoretical `delta_w <= 2 * total` bound a reduction must stay within.
DEFAULT_HELP_MARGIN_FACTOR = 0.5

# Consecutive consistent readings required to confirm a reduction (guards against desync).
DEFAULT_MIN_CONSECUTIVE_CONFIRMATIONS = 3


@dataclass(frozen=True, slots=True)
class PhaseDecisionBasis:
    """Every measurement one phase's decision was built on, kept for diagnostics."""

    signed_active_power_w: float | None
    signed_active_power_age_s: float | None
    measured_current_a: float | None
    measured_current_age_s: float | None
    requested_current_a: float | None
    direction: DirectionClass | None
    confirmed_direction: DirectionClass | None


@dataclass(frozen=True, slots=True)
class RegulatorDecision:
    """One charger's decision: the proposed current, or `None` when unsupportable."""

    proposed_current_a: float | None
    reason: RegulatorReason
    limiting_phase: PhaseName | None
    basis: Mapping[PhaseName, PhaseDecisionBasis] = field(default_factory=dict)


def classify_direction(signed_power_w: float | None, zero_margin_w: float) -> DirectionClass | None:
    """One phase's direction, or `None` if unknown; `zero_margin_w` is the near-zero band."""
    if signed_power_w is None:
        return None
    if abs(signed_power_w) <= zero_margin_w:
        return "near_zero"
    return "net_import" if signed_power_w > 0 else "net_export"


def confirmed_direction(
    recent_directions: Sequence[DirectionClass | None], min_consecutive: int
) -> DirectionClass | None:
    """The direction shared by the last `min_consecutive` entries, else `None`."""
    if min_consecutive <= 0 or len(recent_directions) < min_consecutive:
        return None
    window = recent_directions[-min_consecutive:]
    first = window[0]
    if first is None:
        return None
    if any(entry != first for entry in window):
        return None
    return first


def _legalize_or_pause(
    proposed: float, min_current_a: float, natural_reason: RegulatorReason
) -> tuple[float, RegulatorReason]:
    """Turn a computed ceiling strictly between 0 and `min_current_a` into a pause."""
    if 0.0 < proposed < min_current_a:
        return 0.0, "paused_safe_current_below_charger_minimum"
    return proposed, natural_reason


def _decide_phase(
    *,
    safe_uncredited_headroom_a: float | None,
    signed_increase_headroom_a: float | None = None,
    signed_active_power_w: float | None,
    signed_active_power_age_s: float | None,
    signed_active_power_reason: PhaseUnusableReason | None,
    voltage_v: float | None,
    requested_current_a: float,
    measured_current: PhaseValue,
    confirmed_dir: DirectionClass | None,
    max_age_s: float,
    zero_margin_w: float,
    help_margin_factor: float,
    min_current_a: float,
) -> tuple[float | None, RegulatorReason]:
    """One phase's decision; see the module docstring for the rules."""
    if (
        measured_current.value is None
        or measured_current.problem is not None
        or measured_current.age_s is None
        or measured_current.age_s > max_age_s
    ):
        return None, "charger_measurement_unusable"
    current = measured_current.value

    if (
        signed_active_power_w is None
        or signed_active_power_reason is not None
        or signed_active_power_age_s is None
        or signed_active_power_age_s > max_age_s
    ):
        return None, "phase_measurement_unusable"
    if safe_uncredited_headroom_a is None:
        return None, "phase_measurement_unusable"

    # Active overload: checked before comparing with the requested current, so a
    # "no change" or decrease is never reported as fine while over the fuse.
    if safe_uncredited_headroom_a < 0.0:
        direction = classify_direction(signed_active_power_w, zero_margin_w)
        if direction == "net_import":
            # Less EV import can only move the total toward zero on the P axis, so
            # no confirmation run is needed; capped below the current draw.
            ceiling = max(0.0, current + safe_uncredited_headroom_a)
            proposed = round_down_to_step(min(requested_current_a, ceiling))
            return _legalize_or_pause(
                proposed, min_current_a, "reducing_current_due_to_active_import_overload"
            )
        if direction == "net_export":
            # Export overload: reducing import would worsen it and an increase is
            # not trusted during an overload. No reliable action, never "keep".
            return None, "active_export_overload_no_reliable_action"
        # Near zero or unclear: same refusal to guess.
        return None, "active_overload_direction_unclear_no_reliable_action"

    if abs(requested_current_a - current) < 1e-9:
        return current, "no_change_requested"

    if requested_current_a > current:
        # Increase, not overloaded: capped by the worst case of the new draw. Where the direction
        # is known that judges the signed rest of the site (export leaves room); otherwise the
        # charger is additive on |net|.
        increase_headroom = (
            safe_uncredited_headroom_a
            if signed_increase_headroom_a is None
            else max(safe_uncredited_headroom_a, signed_increase_headroom_a)
        )
        cap = current + increase_headroom
        proposed = round_down_to_step(min(requested_current_a, cap))
        return _legalize_or_pause(proposed, min_current_a, "increase_within_safe_uncredited_margin")

    # Decrease, not overloaded: the risky direction.
    direction = classify_direction(signed_active_power_w, zero_margin_w)
    if direction == "near_zero":
        return current, "near_zero_crossing_kept_unchanged"
    if direction == "net_export":
        # Net export: any reduction increases the export magnitude.
        return current, "decrease_would_increase_net_current_kept_unchanged"

    # direction == "net_import" from here on.
    if voltage_v is None or voltage_v <= 0:
        return current, "insufficient_consecutive_confirmation_kept_unchanged"
    # Assumes near-unity power factor for the EV load.
    delta_w = voltage_v * (current - requested_current_a)
    if delta_w > 2.0 * signed_active_power_w:
        # Beyond the theoretical bound even before any margin.
        return current, "decrease_would_increase_net_current_kept_unchanged"
    if confirmed_dir != "net_import":
        return current, "insufficient_consecutive_confirmation_kept_unchanged"
    if delta_w > help_margin_factor * signed_active_power_w:
        return current, "decrease_margin_exceeded_kept_unchanged"
    return _legalize_or_pause(requested_current_a, min_current_a, "decrease_confirmed_safe")


ApplyabilityFailureKind = Literal[
    "no_proposal",
    "no_basis",
    "phase_unusable",
    "phase_value_missing",
    "phase_age_unknown",
    "phase_stale",
    "direction_changed",
]


@dataclass(frozen=True, slots=True)
class ApplyabilityFailure:
    """Which check stopped a write; `phase` is `None` for decision-level checks."""

    kind: ApplyabilityFailureKind
    phase: PhaseName | None = None


def applyability_failure(
    *,
    decision: RegulatorDecision,
    signed_active_power_w: Mapping[PhaseName, float | None],
    signed_active_power_age_s: Mapping[PhaseName, float | None],
    signed_active_power_reason: Mapping[PhaseName, PhaseUnusableReason | None],
    max_age_s: float,
    zero_margin_w: float = DEFAULT_ZERO_MARGIN_W,
) -> ApplyabilityFailure | None:
    """The first reason `decision` may not be written now, or `None` if it may."""
    if decision.proposed_current_a is None:
        return ApplyabilityFailure("no_proposal")
    if not decision.basis:
        return ApplyabilityFailure("no_basis")
    for phase, basis in decision.basis.items():
        if signed_active_power_reason.get(phase) is not None:
            return ApplyabilityFailure("phase_unusable", phase)
        value = signed_active_power_w.get(phase)
        if value is None:
            return ApplyabilityFailure("phase_value_missing", phase)
        age_s = signed_active_power_age_s.get(phase)
        if age_s is None:
            return ApplyabilityFailure("phase_age_unknown", phase)
        if age_s > max_age_s:
            return ApplyabilityFailure("phase_stale", phase)
        if classify_direction(value, zero_margin_w) != basis.direction:
            return ApplyabilityFailure("direction_changed", phase)
    return None


def decision_still_applyable(
    *,
    decision: RegulatorDecision,
    signed_active_power_w: Mapping[PhaseName, float | None],
    signed_active_power_age_s: Mapping[PhaseName, float | None],
    signed_active_power_reason: Mapping[PhaseName, PhaseUnusableReason | None],
    max_age_s: float,
    zero_margin_w: float = DEFAULT_ZERO_MARGIN_W,
) -> bool:
    """Whether `decision` may still be written to a charger now.

    The site can swing between decision and write, so every phase in
    `decision.basis` must still be fresh, usable and classified in the same
    direction. A `None` proposal or empty `basis` is never applyable. Pure: the
    caller passes re-read measurements.
    """
    return (
        applyability_failure(
            decision=decision,
            signed_active_power_w=signed_active_power_w,
            signed_active_power_age_s=signed_active_power_age_s,
            signed_active_power_reason=signed_active_power_reason,
            max_age_s=max_age_s,
            zero_margin_w=zero_margin_w,
        )
        is None
    )


def _reduction_overloads_another_phase(
    *,
    site_result: SiteCapacityResult,
    request: ChargerRequest,
    phases: Sequence[PhaseName],
    proposed: float,
    voltage_by_phase: Mapping[PhaseName, float | None],
) -> bool:
    """Whether lowering the charger to `proposed` would push a phase that nets to export over the
    ceiling. Per phase, with `Ip` the signed active current and `Iq` the rest of the measured
    magnitude, the new current is `sqrt((Ip - drop)^2 + Iq^2)`. Unknown inputs count as overload."""
    measured_source = request.measured_current_a
    if measured_source is None:
        return True
    for phase in phases:
        measured = measured_source.get(phase).value
        drop = max(0.0, (measured or 0.0) - proposed)
        if drop <= 0.0:
            continue
        power = site_result.phase_signed_active_power_w.get(phase)
        magnitude = site_result.measured_phase_current_a.get(phase)
        margin = site_result.measured_margin_a.get(phase)
        voltage = voltage_by_phase.get(phase)
        if power is None or magnitude is None or margin is None or voltage is None or voltage <= 0:
            return True
        ceiling = magnitude + margin
        active = max(-magnitude, min(magnitude, power / voltage))
        if active >= 0.0:
            continue  # a net import only falls when the charger is lowered
        reactive_sq = max(0.0, magnitude * magnitude - active * active)
        if (active - drop) ** 2 + reactive_sq > ceiling * ceiling + 1e-9:
            return True
    return False


def decide_charging_current(
    *,
    site_result: SiteCapacityResult,
    request: ChargerRequest,
    voltage_by_phase: Mapping[PhaseName, float | None],
    confirmed_direction_by_phase: Mapping[PhaseName, DirectionClass | None],
    max_age_s: float,
    zero_margin_w: float = DEFAULT_ZERO_MARGIN_W,
    help_margin_factor: float = DEFAULT_HELP_MARGIN_FACTOR,
    headroom_by_phase: Mapping[PhaseName, float | None] | None = None,
    signed_headroom_by_phase: Mapping[PhaseName, float | None] | None = None,
) -> RegulatorDecision:
    """One charger's decision, combining every phase it draws from.

    Derived mode only. The most restrictive phase wins: an increase takes the
    smallest per-phase value, a decrease the largest (a charger cannot reduce
    one phase alone). Both pick an existing, already legal per-phase value.
    """
    if site_result.measurement_mode != "derived_phase_current":
        return RegulatorDecision(None, "derived_mode_required", None, {})
    if request.requested_current_a is None:
        return RegulatorDecision(None, "requested_current_unknown", None, {})
    phases = request.phases_used()
    if phases is None:
        return RegulatorDecision(None, "charger_phase_wiring_unknown", None, {})
    if request.measured_current_a is None:
        return RegulatorDecision(None, "charger_measurement_unusable", None, {})

    per_phase: dict[PhaseName, tuple[float | None, RegulatorReason]] = {}
    overloaded_phases: set[PhaseName] = set()
    basis: dict[PhaseName, PhaseDecisionBasis] = {}
    for phase in phases:
        measured = request.measured_current_a.get(phase)
        signed_w = site_result.phase_signed_active_power_w.get(phase)
        signed_age = site_result.phase_signed_active_power_age_s.get(phase)
        signed_reason = site_result.phase_signed_active_power_reason.get(phase)
        # Site-wide margin by default, or the remaining headroom passed in by
        # `allocate_regulator_decisions`.
        headroom = (
            headroom_by_phase.get(phase)
            if headroom_by_phase is not None
            else site_result.measured_margin_a.get(phase)
        )
        signed_headroom = (
            signed_headroom_by_phase.get(phase)
            if signed_headroom_by_phase is not None
            else site_result.phase_signed_margin_a.get(phase)
        )
        voltage = voltage_by_phase.get(phase)
        confirmed = confirmed_direction_by_phase.get(phase)

        value, reason = _decide_phase(
            safe_uncredited_headroom_a=headroom,
            signed_increase_headroom_a=signed_headroom,
            signed_active_power_w=signed_w,
            signed_active_power_age_s=signed_age,
            signed_active_power_reason=signed_reason,
            voltage_v=voltage,
            requested_current_a=request.requested_current_a,
            measured_current=measured,
            confirmed_dir=confirmed,
            max_age_s=max_age_s,
            zero_margin_w=zero_margin_w,
            help_margin_factor=help_margin_factor,
            min_current_a=request.min_current_a,
        )
        per_phase[phase] = (value, reason)
        if headroom is not None and headroom < 0.0:
            overloaded_phases.add(phase)
        basis[phase] = PhaseDecisionBasis(
            signed_active_power_w=signed_w,
            signed_active_power_age_s=signed_age,
            measured_current_a=measured.value,
            measured_current_age_s=measured.age_s,
            requested_current_a=request.requested_current_a,
            direction=classify_direction(signed_w, zero_margin_w),
            confirmed_direction=confirmed,
        )

    for phase in phases:
        value, reason = per_phase[phase]
        if value is None:
            return RegulatorDecision(None, reason, phase, basis)

    if overloaded_phases:
        # The fuse is in danger on at least one phase: the most restrictive phase wins, whatever
        # the others say (a hold or no-change on a healthy phase must not hide the overload).
        limiting_phase, (proposed, reason) = min(per_phase.items(), key=lambda kv: kv[1][0])
        if _reduction_overloads_another_phase(
            site_result=site_result,
            request=request,
            phases=phases,
            proposed=proposed,
            voltage_by_phase=voltage_by_phase,
        ):
            return RegulatorDecision(None, "active_overload_direction_unclear_no_reliable_action", limiting_phase, basis)
        return RegulatorDecision(proposed, reason, limiting_phase, basis)

    first_measured = request.measured_current_a.get(phases[0]).value or 0.0
    is_increase = request.requested_current_a > first_measured

    if is_increase:
        limiting_phase, (proposed, reason) = min(per_phase.items(), key=lambda kv: kv[1][0])
    else:
        limiting_phase, (proposed, reason) = max(per_phase.items(), key=lambda kv: kv[1][0])

    return RegulatorDecision(proposed, reason, limiting_phase, basis)


def _raises_on_credit(
    decision: RegulatorDecision,
    request: ChargerRequest,
    own: DirectPhaseMeasurement | None,
    phases: Sequence[PhaseName],
    tolerance_a: float,
) -> bool:
    """Whether `decision` goes above the credited draw on a phase where the request's reading is a credit (more
    than the charger's own reading `own`) by more than `tolerance_a`."""
    proposed = decision.proposed_current_a
    credited = request.measured_current_a
    if proposed is None or credited is None:
        return False
    for phase in phases:
        value = credited.get(phase).value
        own_value = None if own is None else own.get(phase).value
        if value is None or (own_value is not None and value <= own_value):
            continue
        if proposed > value + tolerance_a:
            return True
    return False


def allocate_regulator_decisions(
    *,
    site_result: SiteCapacityResult,
    requests: Sequence[ChargerRequest],
    voltage_by_phase: Mapping[PhaseName, float | None],
    confirmed_direction_by_phase: Mapping[PhaseName, DirectionClass | None],
    max_age_s: float,
    zero_margin_w: float = DEFAULT_ZERO_MARGIN_W,
    help_margin_factor: float = DEFAULT_HELP_MARGIN_FACTOR,
    own_measured_current_a: Mapping[str, DirectPhaseMeasurement | None] | None = None,
    credit_raise_tolerance_a: float = 1.0,
) -> dict[str, RegulatorDecision]:
    """One decision per charger, computed sequentially so chargers sharing a phase
    are never offered the same headroom.

    Chargers are processed in `allocation_order` (priority, then the site's charger order). A per-phase `remaining`
    headroom starts at `site_result.measured_margin_a` and shrinks by
    `proposed - measured` after each decision. A `None` decision blocks its
    phases for the rest of the cycle; later chargers sharing one are refused.

    `own_measured_current_a` is each charger's own reading where its request carries a credited one (a start the
    site meter shows before the charger does). A credited reading may keep a charger from a pause or a lowering,
    never raise it: a decision that goes more than `credit_raise_tolerance_a` above the credited draw on a credited
    phase rests on the credit for an increase, and the decision on the charger's own reading is taken instead.
    """
    remaining: dict[PhaseName, float | None] = dict(site_result.measured_margin_a)
    remaining_signed: dict[PhaseName, float | None] = dict(site_result.phase_signed_margin_a)
    blocked_phases: set[PhaseName] = set()
    decisions: dict[str, RegulatorDecision] = {}

    for request in allocation_order(requests):
        phases_used = request.phases_used()
        if phases_used is not None and blocked_phases.intersection(phases_used):
            decisions[request.charger_entry_id] = RegulatorDecision(
                None,
                "shared_phase_blocked_by_another_chargers_undetermined_decision",
                None,
                {},
            )
            continue

        decision = decide_charging_current(
            site_result=site_result,
            request=request,
            voltage_by_phase=voltage_by_phase,
            confirmed_direction_by_phase=confirmed_direction_by_phase,
            max_age_s=max_age_s,
            zero_margin_w=zero_margin_w,
            help_margin_factor=help_margin_factor,
            headroom_by_phase=remaining,
            signed_headroom_by_phase=remaining_signed,
        )
        own = (own_measured_current_a or {}).get(request.charger_entry_id, request.measured_current_a)
        if (
            own is not request.measured_current_a
            and phases_used is not None
            and _raises_on_credit(decision, request, own, phases_used, credit_raise_tolerance_a)
        ):
            request = replace(request, measured_current_a=own)
            decision = decide_charging_current(
                site_result=site_result,
                request=request,
                voltage_by_phase=voltage_by_phase,
                confirmed_direction_by_phase=confirmed_direction_by_phase,
                max_age_s=max_age_s,
                zero_margin_w=zero_margin_w,
                help_margin_factor=help_margin_factor,
                headroom_by_phase=remaining,
                signed_headroom_by_phase=remaining_signed,
            )
        decisions[request.charger_entry_id] = decision

        if phases_used is None:
            continue

        if decision.proposed_current_a is None:
            blocked_phases.update(phases_used)
            continue

        # Explicit `None` check rather than `assert`, so it holds under `python -O`.
        measured_source = request.measured_current_a
        if measured_source is None:
            blocked_phases.update(phases_used)
            continue

        for phase in phases_used:
            measured = measured_source.get(phase).value
            if remaining[phase] is not None and measured is not None:
                remaining[phase] = remaining[phase] - (decision.proposed_current_a - measured)
                if remaining_signed[phase] is not None:
                    remaining_signed[phase] = remaining_signed[phase] - (
                        decision.proposed_current_a - measured
                    )

    return decisions
