"""Pure, Home-Assistant-independent site capacity / load-balancing math.

From a snapshot of configuration and measurements this computes what a
site-wide load balancer would recommend for each associated SpotNav charger
(`proposed_current_a`). It never calls a Home Assistant service:
`site/site_capacity_controller.py` reads entity states, normalizes them into the
types here and publishes the result.

- The grid meter sits upstream of every load, so a phase's `measured_total_a`
  already includes each charger's draw. `measured_current_a` (per charger) is
  that draw and must be genuinely measured, never a setpoint; `None` credits
  nothing. Credits exceeding the site total beyond `MEASUREMENT_TOLERANCE_A`
  give `invalid_measurements` rather than being clamped.
- `site_headroom_per_phase` is already the full ceiling for all chargers on a
  phase; `allocate_chargers` only subtracts each charger's *proposed* current, to
  reserve it for the next charger on that phase.
- `measured_margin_a` (`main_fuse_a - safety_margin_a - measured_phase_current_a`,
  no crediting) always equals `phase_headroom_a`, which `allocations` use; a
  phase accepted on a `confirmed_unchanged` reading loses
  `CONFIRMED_UNCHANGED_MARGIN_A`. `calculated_headroom_after_ev_credit_a` and
  `estimated_headroom_if_battery_yields_a` are diagnostic only.

Why crediting stays diagnostic:
- Direct mode subtracts scalar magnitudes of phasors, which understates other
  load unless both share a phase angle.
- Derived mode computes `sqrt(P^2 + Q^2) / V`, so subtracting amps double-counts
  the charger. `_derived_other_load_per_phase` subtracts an assumed
  unity-power-factor `P` instead, yet a reactive house load (P = 0 W,
  Q = -1000 var) with an EV at cos(phi) = 0.95 still gives about 0.7 A of other
  load against a true 4.35 A.
- Every current is an unsigned magnitude. If the rest of the site exports 28 A
  while an EV imports 10 A the meter reads 18 A and crediting gives 8 A of other
  load against a true 28 A. Even `measured_margin_a` assumes one import
  direction, so no output here is a control directive.

A charger that draws more than the net reading on a phase is normal on a solar
site (the rest of the site exports). It is therefore never a reason to refuse the
reading. The fuse is judged on |net| (`measured_margin_a`, valid in every case,
since |net + d| <= |net| + d). Where the direction is known (derived mode: signed
active power) the rest of the site is taken signed (`rest = net - charger`, may be
negative) and `phase_signed_margin_a` gives the room for the charger's worst case
|rest + proposed|. A magnitude-only source assumes the worst, the charger being
additive on |site|, and only warns (`charger_reading_exceeds_site_phases`) when the
charger reads more than the site, which can mean miswiring.

The only path that may command a charger is
`SiteCapacityController._async_apply_active_control` (see `ACTIVE_CONTROL_READY`).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast, Literal

from ..const import (
    CHARGER_PRIORITY_FIRST,
    CHARGER_PRIORITY_LAST,
    CHARGER_PRIORITY_NORMAL,
    DEFAULT_CHARGER_PRIORITY,
)


PhaseName = Literal["L1", "L2", "L3"]
PHASES: tuple[PhaseName, ...] = ("L1", "L2", "L3")

# Prerequisites for commanding chargers: a verified grid-meter sign convention
# (every source was the inverter's own), a signed power source (derived mode
# only; direct-mode CT readings are unsigned) and a regulation model for both
# directions (`site/regulator.py`, best-effort). Commands happen only in
# `SiteCapacityController._async_apply_active_control`, which also needs the
# site's `CONF_ACTIVE_CONTROL_ENABLED` option.
ACTIVE_CONTROL_READY = True

MeasurementMode = Literal["direct_phase_current", "derived_phase_current", "unavailable"]

# Site-level: whether configuration and measurements are healthy enough to compute.
SiteState = Literal[
    "disabled",
    "not_configured",
    "missing_measurements",
    "stale_measurements",
    "invalid_measurements",
    "observing",
]

# Per-charger outcome, meaningful once the site is "observing".
# "requested_current_unknown" also covers a charger blocked by another one on a
# shared phase. "not_requesting": the charger asks for 0 A (it is not charging) where
# that used to read "below_minimum_current"; added after the others, so a reader that
# knows only those reads it as a state it does not know.
ChargerState = Literal[
    "invalid_measurements",
    "requested_current_unknown",
    "capacity_available",
    "capacity_limited",
    "below_minimum_current",
    "not_requesting",
]

ControllerState = SiteState | ChargerState

# What the site is doing, in words a person reads (`site_activity`): `observing` is the healthy state, in
# which the site measures and, with active load balancing on, also balances. Additive beside the state,
# whose value stays as it is.
SiteActivity = Literal["measuring", "balancing", "not_measuring"]
SITE_ACTIVITIES: tuple[SiteActivity, ...] = ("measuring", "balancing", "not_measuring")


def site_activity(state: str, active_control_enabled: bool) -> SiteActivity:
    """`measuring` or `balancing` (active load balancing on) while the site is `observing`, else `not_measuring`."""
    if state != "observing":
        return "not_measuring"
    return "balancing" if active_control_enabled else "measuring"

Confidence = Literal["high", "low", "guarded", "none"]
# "guarded": a phase was accepted on a `confirmed_unchanged` reading. "none": no
# proposal was made.

# Why a phase's value has not changed (see `classify_phase_liveness`); the first
# two are accepted for headroom, the last two block.
PhaseLiveness = Literal["fresh", "confirmed_unchanged", "unconfirmed_stale", "no_recent_report"]

# Why a per-phase diagnostic is `None`: "missing"/"invalid" mirror `PhaseProblem`;
# "stale" is checked separately against `max_age_s` at the point of use.
PhaseUnusableReason = Literal["missing", "invalid", "stale"]

# Commonly the AC EVSE floor; overridable per charger (see `ChargerRequest`).
DEFAULT_MIN_CURRENT_A = 6.0

_PRIORITY_RANK = {CHARGER_PRIORITY_FIRST: 0, CHARGER_PRIORITY_NORMAL: 1, CHARGER_PRIORITY_LAST: 2}

# Allowance for rounding/sensor noise when validating summed charger credits.
MEASUREMENT_TOLERANCE_A = 0.5

# Headroom discount for a phase accepted on a `confirmed_unchanged` reading. Fixed
# and nonzero so it never equals `fresh`.
CONFIRMED_UNCHANGED_MARGIN_A = 1.0

REASON_DISABLED = "site_disabled"
REASON_NOT_CONFIGURED = "site_not_configured"
REASON_MISSING = "required_phase_measurement_missing"
REASON_STALE = "phase_measurement_stale"
REASON_INVALID = "phase_measurement_invalid"
REASON_OBSERVING = "observing"
REASON_UNKNOWN_PHASE = "single_phase_charger_wiring_unknown"
REASON_REQUESTED_UNKNOWN = "charger_requested_current_unknown"
REASON_BLOCKED_BY_UNKNOWN_SIBLING = "shared_phase_blocked_by_another_chargers_unknown_request"
REASON_CAPACITY_AVAILABLE = "requested_current_fits_within_headroom"
REASON_CAPACITY_LIMITED = "requested_current_reduced_to_available_headroom"
REASON_BELOW_MINIMUM = "available_headroom_below_charger_minimum_current"
REASON_NO_CURRENT_REQUESTED = "no_current_requested"


# Why a `PhaseValue` is `None`: absent, or present but unusable.
PhaseProblem = Literal["missing", "invalid"]


@dataclass(frozen=True, slots=True)
class PhaseValue:
    """One phase's already-normalized measurement, or the lack of one.

    `problem` is `None` exactly when `value` is not. `age_s` is time since the
    value last changed (`None` means unknown, treated as stale); `report_age_s` is
    time since HA last heard from the entity, and only feeds
    `classify_phase_liveness`.
    """

    value: float | None
    age_s: float | None = None
    problem: PhaseProblem | None = None
    report_age_s: float | None = None


@dataclass(frozen=True, slots=True)
class DirectPhaseMeasurement:
    """Directly measured grid current per phase, already normalized to A."""

    l1: PhaseValue
    l2: PhaseValue
    l3: PhaseValue

    def get(self, phase: PhaseName) -> PhaseValue:
        return {"L1": self.l1, "L2": self.l2, "L3": self.l3}[phase]


# How a derived phase's current was obtained, most to least exact:
# `measured` (the meter's own current, as |I|), `apparent` (S / U), `reactive`
# (sqrt(P^2 + Q^2) / U) and `estimated` (|P| / (U x `ESTIMATED_POWER_FACTOR`),
# used only when the meter gives nothing better).
CurrentBasis = Literal["measured", "apparent", "reactive", "estimated"]

# The power factor the estimate assumes. With PF >= this value the estimate is
# never below the true current; below it the estimate understates the current
# (see `estimate_current_from_power`).
ESTIMATED_POWER_FACTOR = 0.9


@dataclass(frozen=True, slots=True)
class DerivedPhaseInput:
    """One phase's readings for derived mode.

    `active_power_w` (signed, import positive) and `voltage_v` are required. The
    rest are optional, `None` meaning "not configured": `reactive_power_var`,
    `apparent_power_va` and `current_a` (already |I|). A configured optional input
    that is unusable makes the phase unusable rather than silently falling back to
    a cruder basis; only a phase with none of them configured is estimated.
    """

    active_power_w: PhaseValue
    reactive_power_var: PhaseValue | None
    voltage_v: PhaseValue
    apparent_power_va: PhaseValue | None = None
    current_a: PhaseValue | None = None


@dataclass(frozen=True, slots=True)
class DerivedCurrent:
    """One derived phase's current, how it was obtained, and its freshness inputs."""

    value: float | None
    basis: CurrentBasis | None
    age_s: float | None
    report_age_s: float | None
    problem: PhaseProblem | None


@dataclass(frozen=True, slots=True)
class DerivedPhaseMeasurement:
    l1: DerivedPhaseInput
    l2: DerivedPhaseInput
    l3: DerivedPhaseInput

    def get(self, phase: PhaseName) -> DerivedPhaseInput:
        return {"L1": self.l1, "L2": self.l2, "L3": self.l3}[phase]


@dataclass(frozen=True, slots=True)
class ChargerRequest:
    """One SpotNav-controlled charger's request and known wiring.

    `phase` must be set for a single-phase charger (`None` means unknown wiring
    and no recommendation). `measured_current_a` is the charger's own measured
    current, never a setpoint; unusable readings credit nothing and a
    three-phase charger is all-or-nothing. `requested_current_a` of `None` is
    unknown, unlike `0.0`.
    """

    charger_entry_id: str
    requested_current_a: float | None
    phases: Literal[1, 3]
    phase: PhaseName | None = None
    min_current_a: float = DEFAULT_MIN_CURRENT_A
    measured_current_a: DirectPhaseMeasurement | None = None
    #: "first", "normal" or "last": where this charger stands in the allocation order.
    priority: str = DEFAULT_CHARGER_PRIORITY
    #: The charger's place in the site's own charger list (the order they joined), which settles
    #: chargers of one priority; equal places fall back to the entry id, so the order never depends
    #: on how the requests are listed.
    order: int = 0

    def phases_used(self) -> tuple[PhaseName, ...] | None:
        """The phases this request draws from, or `None` if that's unknown."""
        if self.phases == 3:
            return PHASES
        if self.phase is None:
            return None
        return (self.phase,)


@dataclass(frozen=True, slots=True)
class ChargerAllocation:
    """One charger's outcome: an observation, never a control directive (see the
    module docstring), including when `proposed_current_a` is below the request."""

    charger_entry_id: str
    requested_current_a: float | None
    proposed_current_a: float | None
    limiting_phase: PhaseName | None
    state: ControllerState
    reason: str


# Which basis produced `SiteCapacityResult.estimated_headroom_if_battery_yields_a`
# (see `estimated_headroom_if_battery_yields`).
BatteryPhaseBasis = Literal["measured_per_phase", "assumed_equal_split", "unknown"]


@dataclass(frozen=True, slots=True)
class BatteryYieldEstimate:
    """Optional diagnostic input describing a home battery presently charging.

    `aggregate_charge_power_w` is signed (positive means charging).
    `per_phase_charge_current_a` is an unsigned magnitude, so it is only used with
    a fresh `direction_confirmation_power_w`.
    """

    per_phase_charge_current_a: DirectPhaseMeasurement | None = None
    direction_confirmation_power_w: PhaseValue | None = None
    aggregate_charge_power_w: PhaseValue | None = None


@dataclass(frozen=True, slots=True)
class SiteCalculationConfig:
    """Site-level configuration for one calculation. Home-Assistant-independent."""

    enabled: bool
    main_fuse_a: float | None
    safety_margin_a: float
    measurement_mode: MeasurementMode
    max_age_s: float
    direct: DirectPhaseMeasurement | None = None
    derived: DerivedPhaseMeasurement | None = None
    battery: BatteryYieldEstimate | None = None


@dataclass(frozen=True, slots=True)
class SiteCapacityResult:
    """The full result of one site capacity calculation."""

    state: ControllerState
    reason: str
    measurement_mode: MeasurementMode
    confidence: Confidence
    measured_phase_current_a: Mapping[PhaseName, float | None]
    phase_age_s: Mapping[PhaseName, float | None]
    # What `allocations` use; equal to `measured_margin_a` in every mode.
    phase_headroom_a: Mapping[PhaseName, float | None]
    limiting_phase: PhaseName | None
    max_age_s: float
    allocations: tuple[ChargerAllocation, ...]
    # Raw margin with no charger credit; currently equal to `phase_headroom_a`.
    measured_margin_a: Mapping[PhaseName, float | None] = field(
        default_factory=lambda: {phase: None for phase in PHASES}
    )
    # Headroom with each charger's measured draw credited back; diagnostic only.
    calculated_headroom_after_ev_credit_a: Mapping[PhaseName, float | None] = field(
        default_factory=lambda: {phase: None for phase in PHASES}
    )
    estimated_headroom_if_battery_yields_a: Mapping[PhaseName, float | None] = field(
        default_factory=lambda: {phase: None for phase in PHASES}
    )
    battery_yield_basis: BatteryPhaseBasis = "unknown"
    # Diagnostic only: how long since HA heard anything from the source.
    phase_report_age_s: Mapping[PhaseName, float | None] = field(
        default_factory=lambda: {phase: None for phase in PHASES}
    )
    # Diagnostic only; `None` when the value is missing/invalid.
    phase_liveness: Mapping[PhaseName, PhaseLiveness | None] = field(
        default_factory=lambda: {phase: None for phase in PHASES}
    )
    # Diagnostic only: signed active power in watts (negative = exporting), derived
    # mode only; `None` when not usable (see `_phase_signed_active_power`).
    phase_signed_active_power_w: Mapping[PhaseName, float | None] = field(
        default_factory=lambda: {phase: None for phase in PHASES}
    )
    phase_signed_active_power_age_s: Mapping[PhaseName, float | None] = field(
        default_factory=lambda: {phase: None for phase in PHASES}
    )
    phase_signed_active_power_reason: Mapping[PhaseName, PhaseUnusableReason | None] = field(
        default_factory=lambda: {phase: None for phase in PHASES}
    )
    # Diagnostic only: as above but never dropped for raw age; pair with
    # `phase_liveness`.
    phase_signed_active_power_diagnostic_w: Mapping[PhaseName, float | None] = field(
        default_factory=lambda: {phase: None for phase in PHASES}
    )
    # Diagnostic only: voltage, `None` in direct mode or when missing/invalid.
    phase_voltage_v: Mapping[PhaseName, float | None] = field(
        default_factory=lambda: {phase: None for phase in PHASES}
    )
    # How each phase's current was obtained (`CurrentBasis`); `None` for an unusable phase.
    phase_current_basis: Mapping[PhaseName, CurrentBasis | None] = field(
        default_factory=lambda: {phase: None for phase in PHASES}
    )
    # True when any phase's current is an estimate from power alone
    # (`estimate_current_from_power`); `estimated_power_factor` is the assumption behind it.
    current_estimated: bool = False
    estimated_power_factor: float | None = None
    # Room per phase for a charger's increase in the worst case |rest + proposed| <= ceiling,
    # `rest` being the site without the charger taken signed. Only where the direction is known
    # (derived mode); `None` elsewhere, where `measured_margin_a` (|net|, additive) applies.
    phase_signed_margin_a: Mapping[PhaseName, float | None] = field(
        default_factory=lambda: {phase: None for phase in PHASES}
    )
    # Phases where a magnitude-only source has the charger reading more than the site reading,
    # possible miswiring; diagnostic only, never a reason to refuse the reading.
    charger_reading_exceeds_site_phases: tuple[PhaseName, ...] = ()


# Accepted ampere spellings, shared via `normalized_ampere_unit` with
# `vehicles/discovery.py` and `site/measurement_source.py`.
_AMPERE_UNIT_ALIASES: dict[str, str] = {
    "a": "A",
    "amp": "A",
    "amps": "A",
    "ampere": "A",
    "amperes": "A",
    "ma": "mA",
    "milliamp": "mA",
    "milliamps": "mA",
}


def normalized_ampere_unit(unit: object) -> str | None:
    """The canonical spelling (`"A"` or `"mA"`) of `unit`, or `None`."""
    if not isinstance(unit, str):
        return None
    return _AMPERE_UNIT_ALIASES.get(unit.strip().lower())


def classify_current(
    raw: object, unit: str | None, *, signed: bool = False
) -> tuple[float | None, PhaseProblem | None]:
    """A raw sensor value + unit as amperes, plus why it failed if it did.

    An explicit "A"/"mA" unit is required (never assumed). Absent values are
    `"missing"`; non-numeric, non-finite, negative or unit-less ones `"invalid"`.
    `signed=True` is for a source known to report export as a negative current
    (HomeWizard, Huawei, Fronius ...): the fuse carries |I| either way, so the
    magnitude is taken instead of rejecting the reading. Without the flag a
    negative value stays `"invalid"`, since an unknown sign convention is a fault.
    """
    if _is_absent(raw):
        return None, "missing"
    value = _as_finite_float(raw)
    if value is not None and signed:
        value = abs(value)
    if value is None or value < 0:
        return None, "invalid"
    canonical = normalized_ampere_unit(unit)
    if canonical is None:
        return None, "invalid"
    if canonical == "mA":
        return value / 1000.0, None
    return value, None


def normalize_power(raw: object, unit: str | None) -> float | None:
    """A raw power value + unit as watts/VAR, or `None`. See `classify_power`."""
    value, _problem = classify_power(raw, unit)
    return value


def classify_power(raw: object, unit: str | None) -> tuple[float | None, PhaseProblem | None]:
    """A raw power value + unit as watts/VAR, plus why it failed if it did. Power
    may be negative; an explicit unit is required."""
    if _is_absent(raw):
        return None, "missing"
    value = _as_finite_float(raw)
    if value is None:
        return None, "invalid"
    if not unit:
        return None, "invalid"
    normalized_unit = unit.strip().lower()
    if normalized_unit in ("w", "var"):
        return value, None
    if normalized_unit in ("kw", "kvar"):
        return value * 1000.0, None
    return None, "invalid"


def normalize_voltage(raw: object, unit: str | None) -> float | None:
    """A raw voltage value + unit as volts, or `None`. See `classify_voltage`."""
    value, _problem = classify_voltage(raw, unit)
    return value


def classify_voltage(raw: object, unit: str | None) -> tuple[float | None, PhaseProblem | None]:
    """A raw voltage value + unit as volts, plus why it failed if it did. Zero,
    negative or unit-less values are `"invalid"`."""
    if _is_absent(raw):
        return None, "missing"
    value = _as_finite_float(raw)
    if value is None or value <= 0:
        return None, "invalid"
    if not unit:
        return None, "invalid"
    normalized_unit = unit.strip().lower()
    if normalized_unit in ("v", "volt", "volts"):
        return value, None
    if normalized_unit in ("kv",):
        return value * 1000.0, None
    return None, "invalid"


def _is_absent(raw: object) -> bool:
    return raw is None or (
        isinstance(raw, str) and raw.strip().lower() in ("unknown", "unavailable", "none", "")
    )


def _as_finite_float(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None  # bool is a subclass of int; never read as a measurement
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _combine_problems(*problems: PhaseProblem | None) -> PhaseProblem | None:
    """The most severe of several problems: `"invalid"` beats `"missing"`."""
    present = [p for p in problems if p is not None]
    if not present:
        return None
    return "invalid" if "invalid" in present else "missing"


def apparent_current_from_derived(
    active_power_w: float | None,
    reactive_power_var: float | None,
    voltage_v: float | None,
) -> float | None:
    """`sqrt(P^2 + Q^2) / V`, or `None` if any input is missing/invalid."""
    if active_power_w is None or reactive_power_var is None or voltage_v is None:
        return None
    if voltage_v <= 0:
        return None
    apparent_va = math.hypot(active_power_w, reactive_power_var)
    return apparent_va / voltage_v


def estimate_current_from_power(
    active_power_w: float | None,
    voltage_v: float | None,
    power_factor: float = ESTIMATED_POWER_FACTOR,
) -> float | None:
    """`|P| / (V x power_factor)`: the current of a phase known only by its active power.

    The true current is `|P| / (V x PF)`, so with the assumed `power_factor` no greater than the
    true PF the estimate is no lower than the truth. At the default 0.9 that holds for every
    PF >= 0.9; for a lower PF the estimate understates the current by the ratio
    `PF / power_factor` (see `estimate_covers_power_factor`).
    """
    if active_power_w is None or voltage_v is None or voltage_v <= 0:
        return None
    if not 0 < power_factor <= 1:
        raise ValueError("power_factor must be in (0, 1]")
    return abs(active_power_w) / (voltage_v * power_factor)


def estimate_covers_power_factor(
    true_power_factor: float, assumed_power_factor: float = ESTIMATED_POWER_FACTOR
) -> bool:
    """Whether an estimate made with `assumed_power_factor` is at least the true current for a
    load of `true_power_factor`; `False` below the assumed value, where it understates.
    """
    return true_power_factor >= assumed_power_factor


def classify_apparent_power(
    raw: object, unit: str | None
) -> tuple[float | None, PhaseProblem | None]:
    """A raw apparent-power value + unit as VA, plus why it failed if it did. Negative values are
    `"invalid"` (apparent power is a magnitude); an explicit unit is required."""
    if _is_absent(raw):
        return None, "missing"
    value = _as_finite_float(raw)
    if value is None or value < 0 or not unit:
        return None, "invalid"
    normalized_unit = unit.strip().lower()
    if normalized_unit == "va":
        return value, None
    if normalized_unit == "kva":
        return value * 1000.0, None
    return None, "invalid"


def derived_phase_current(derived_input: DerivedPhaseInput) -> DerivedCurrent:
    """One phase's current for the fuse check in derived mode, and how it was obtained.

    Active power and voltage must both be usable. Then, in order: the meter's own current, S / U
    from apparent power, sqrt(P^2 + Q^2) / U from reactive power, and, only when none of those
    three is configured, the conservative `estimate_current_from_power`. A configured input that
    cannot be used never falls through to a cruder basis: the phase is then missing or invalid.
    """
    power, voltage = derived_input.active_power_w, derived_input.voltage_v
    configured: list[tuple[CurrentBasis, PhaseValue]] = [
        (basis, phase_value)
        for basis, phase_value in (
            ("measured", derived_input.current_a),
            ("apparent", derived_input.apparent_power_va),
            ("reactive", derived_input.reactive_power_var),
        )
        if phase_value is not None
    ]

    def freshness(inputs: Sequence[PhaseValue]) -> tuple[float | None, float | None]:
        # Oldest input sets freshness; one unknown age makes it unknown.
        ages = [value.age_s for value in inputs]
        report_ages = [value.report_age_s for value in inputs]
        return (
            None if any(a is None for a in ages) else max(cast(list[float], ages)),
            None if any(a is None for a in report_ages) else max(cast(list[float], report_ages)),
        )

    base_problem = _combine_problems(power.problem, voltage.problem)
    if base_problem is not None or power.value is None or voltage.value is None:
        problem = (
            _combine_problems(
                power.problem, voltage.problem, *(pv.problem for _b, pv in configured)
            )
            or "missing"
        )
        age_s, report_age_s = freshness([power, voltage, *(pv for _b, pv in configured)])
        return DerivedCurrent(None, None, age_s, report_age_s, problem)

    for basis, phase_value in configured:
        if phase_value.value is None or phase_value.problem is not None:
            continue
        if basis == "measured":
            value: float | None = phase_value.value
        elif basis == "apparent":
            value = phase_value.value / voltage.value
        else:
            value = apparent_current_from_derived(power.value, phase_value.value, voltage.value)
        if value is None:
            continue
        age_s, report_age_s = freshness([power, voltage, phase_value])
        return DerivedCurrent(value, basis, age_s, report_age_s, None)

    if configured:
        age_s, report_age_s = freshness([power, voltage, *(pv for _b, pv in configured)])
        problem = _combine_problems(*(pv.problem for _b, pv in configured)) or "missing"
        return DerivedCurrent(None, None, age_s, report_age_s, problem)

    age_s, report_age_s = freshness([power, voltage])
    return DerivedCurrent(
        estimate_current_from_power(power.value, voltage.value),
        "estimated",
        age_s,
        report_age_s,
        None,
    )


def round_down_to_step(value: float, step: float = 1.0) -> float:
    """Round down to a multiple of `step`, never up into headroom not available."""
    if step <= 0:
        raise ValueError("step must be positive")
    return math.floor(value / step) * step


def _credited_current_per_phase(
    measured_total_a: Mapping[PhaseName, float],
    requests: Sequence[ChargerRequest],
    max_age_s: float,
    tolerance_a: float = MEASUREMENT_TOLERANCE_A,
) -> tuple[dict[PhaseName, float], frozenset[PhaseName]]:
    """Validated, measured charger current per phase.

    Returns `(credited, inconsistent_phases)`. Credits are summed per phase and
    validated once against that phase's total plus `tolerance_a`, so the result is
    order-independent; an over-large claim is flagged and credited as `0.0`.
    """
    credited: dict[PhaseName, float] = {phase: 0.0 for phase in PHASES}
    for request in requests:
        phases_used = request.phases_used()
        if phases_used is None:
            continue
        sanitized = _all_or_nothing_measured_current(request, phases_used, max_age_s)
        if sanitized is None:
            continue
        for phase in phases_used:
            credited[phase] += sanitized.get(phase).value

    inconsistent: set[PhaseName] = set()
    for phase in PHASES:
        if credited[phase] > measured_total_a[phase] + tolerance_a:
            inconsistent.add(phase)
    return credited, frozenset(inconsistent)


def other_load_per_phase(
    measured_total_a: Mapping[PhaseName, float],
    requests: Sequence[ChargerRequest],
    max_age_s: float,
    tolerance_a: float = MEASUREMENT_TOLERANCE_A,
) -> tuple[dict[PhaseName, float], frozenset[PhaseName]]:
    """Other load per phase: the measured total minus each charger's measured
    current, credited at most once. Direct-mode version; an inconsistent phase
    keeps its uncredited total."""
    credited, inconsistent = _credited_current_per_phase(
        measured_total_a, requests, max_age_s, tolerance_a
    )
    other = dict(measured_total_a)
    for phase in PHASES:
        if phase in inconsistent:
            continue
        other[phase] = max(0.0, measured_total_a[phase] - credited[phase])
    return other, inconsistent


def _derived_other_load_per_phase(
    derived: DerivedPhaseMeasurement,
    apparent_total_a: Mapping[PhaseName, float],
    requests: Sequence[ChargerRequest],
    max_age_s: float,
    tolerance_a: float = MEASUREMENT_TOLERANCE_A,
) -> tuple[dict[PhaseName, float], frozenset[PhaseName]]:
    """Derived mode's `other_load_per_phase`: the credit is applied in the P/Q
    domain at an assumed unity power factor (see the module docstring). Only a phase
    whose current came from power (`reactive` or `estimated`) is credited that way;
    one read from the meter's own current or apparent power keeps its total."""
    credited, inconsistent = _credited_current_per_phase(
        apparent_total_a, requests, max_age_s, tolerance_a
    )
    other: dict[PhaseName, float] = dict(apparent_total_a)
    for phase in PHASES:
        if phase in inconsistent or credited[phase] <= 0.0:
            continue
        derived_input = derived.get(phase)
        voltage = cast(float, derived_input.voltage_v.value)
        assumed_ev_active_power_w = credited[phase] * voltage
        adjusted_active_power_w = cast(float, derived_input.active_power_w.value) - assumed_ev_active_power_w
        basis = derived_phase_current(derived_input).basis
        if basis == "reactive":
            corrected = apparent_current_from_derived(
                adjusted_active_power_w,
                cast(PhaseValue, derived_input.reactive_power_var).value,
                voltage,
            )
        elif basis == "estimated":
            corrected = estimate_current_from_power(adjusted_active_power_w, voltage)
        else:
            corrected = None
        other[phase] = max(0.0, corrected) if corrected is not None else apparent_total_a[phase]
    return other, inconsistent


def _is_usable(phase_value: PhaseValue, max_age_s: float) -> bool:
    """Present, valid and fresh; an unknown age is never treated as fresh."""
    return (
        phase_value.value is not None
        and phase_value.problem is None
        and phase_value.age_s is not None
        and phase_value.age_s <= max_age_s
    )


def _liveness_allows_headroom(liveness: PhaseLiveness, age_s: float | None) -> bool:
    """Whether a phase with this liveness may contribute headroom: `fresh`, or
    `confirmed_unchanged` with a known value age (unknown age is never fresh)."""
    if liveness == "fresh":
        return True
    if liveness == "confirmed_unchanged":
        return age_s is not None
    return False


def _confirmed_unchanged_phases(
    liveness: Mapping[PhaseName, PhaseLiveness | None],
) -> frozenset[PhaseName]:
    """The phases accepted on a confirmed-unchanged reading (discounted)."""
    return frozenset(
        phase for phase, value in liveness.items() if value == "confirmed_unchanged"
    )


def headroom_with_liveness_margin(
    headroom_a: Mapping[PhaseName, float],
    uncertain_phases: Collection[PhaseName],
    margin_a: float,
) -> dict[PhaseName, float]:
    """[headroom_a] with [margin_a] taken off every phase in [uncertain_phases].

    Only ever subtracts, so an uncertain phase never yields more than a fresh one.
    """
    discount = abs(margin_a)
    return {
        phase: value - (discount if phase in uncertain_phases else 0.0)
        for phase, value in headroom_a.items()
    }


def classify_phase_liveness(
    age_s: float | None, report_age_s: float | None, max_age_s: float
) -> PhaseLiveness:
    """Grade why a phase's value looks stale.

    `report_age_s` (last state write) adds evidence a flat sensor is alive but is
    not proof: an integration can re-serve a cached value from a dead link. So
    `confirmed_unchanged` is only used at a discount.

    - `age_s <= max_age_s`: `"fresh"`.
    - `report_age_s is None`: `"unconfirmed_stale"`.
    - `report_age_s <= max_age_s`: `"confirmed_unchanged"`.
    - Otherwise `"no_recent_report"`.
    """
    if age_s is not None and age_s <= max_age_s:
        return "fresh"
    if report_age_s is None:
        return "unconfirmed_stale"
    if report_age_s <= max_age_s:
        return "confirmed_unchanged"
    return "no_recent_report"


def _all_or_nothing_measured_current(
    request: ChargerRequest, phases_used: tuple[PhaseName, ...], max_age_s: float
) -> DirectPhaseMeasurement | None:
    """This charger's `measured_current_a`, or `None` if any of its phases is
    unusable (crediting only the good phases would understate its draw)."""
    if request.measured_current_a is None:
        return None
    if not all(_is_usable(request.measured_current_a.get(phase), max_age_s) for phase in phases_used):
        return None
    return request.measured_current_a


def site_headroom_per_phase(
    main_fuse_a: float,
    safety_margin_a: float,
    other_load_a: Mapping[PhaseName, float],
) -> dict[PhaseName, float]:
    """The full ceiling for chargers on each phase: `main_fuse_a -
    safety_margin_a - other_load_a`. Not to be reduced again per charger."""
    ceiling = main_fuse_a - safety_margin_a
    return {phase: ceiling - other_load_a[phase] for phase in PHASES}


def estimated_headroom_if_battery_yields(
    headroom_a: Mapping[PhaseName, float | None],
    battery: BatteryYieldEstimate | None,
    voltage_v: Mapping[PhaseName, float | None] | None,
    max_age_s: float,
) -> tuple[dict[PhaseName, float | None], BatteryPhaseBasis]:
    """`headroom_a` plus the battery's charging current, if it stopped now.

    Returns `(estimate, basis)`: `"measured_per_phase"` when the direction
    confirmation and every per-phase current are fresh, positive and valid;
    `"assumed_equal_split"` when only the aggregate power is usable (needs
    `voltage_v`); else `"unknown"` (all `None`).
    """
    if battery is None:
        return {phase: None for phase in PHASES}, "unknown"

    def _added_to_headroom(phase: PhaseName, extra_a: float) -> float | None:
        base = headroom_a.get(phase)
        return None if base is None else base + extra_a

    def _fresh_and_positive(value: PhaseValue) -> bool:
        return (
            value.value is not None
            and value.value > 0.0
            and value.problem is None
            and value.age_s is not None
            and value.age_s <= max_age_s
        )

    per_phase = battery.per_phase_charge_current_a
    confirmation = battery.direction_confirmation_power_w
    if (
        confirmation is not None
        and _fresh_and_positive(confirmation)
        and per_phase is not None
        and all(_fresh_and_positive(per_phase.get(phase)) for phase in PHASES)
    ):
        return (
            {phase: _added_to_headroom(phase, per_phase.get(phase).value) for phase in PHASES},
            "measured_per_phase",
        )

    aggregate = battery.aggregate_charge_power_w
    if aggregate is not None and _fresh_and_positive(aggregate) and voltage_v is not None:
        share_w = aggregate.value / len(PHASES)
        estimate: dict[PhaseName, float | None] = {}
        any_computed = False
        for phase in PHASES:
            voltage = voltage_v.get(phase)
            if voltage is None or voltage <= 0:
                estimate[phase] = None
                continue
            estimate[phase] = _added_to_headroom(phase, share_w / voltage)
            any_computed = True
        if any_computed:
            return estimate, "assumed_equal_split"

    return {phase: None for phase in PHASES}, "unknown"


def _limiting_phase(
    headroom_a: Mapping[PhaseName, float], phases: Sequence[PhaseName]
) -> PhaseName:
    return min(phases, key=lambda phase: headroom_a[phase])


def allocation_order(requests: Sequence[ChargerRequest]) -> list[ChargerRequest]:
    """The order a site serves its chargers in: "first" before "normal" before "last".

    Chargers of one priority are taken in the site's own charger order (`order`, the order they
    joined), not by their random entry ids; only equal places fall back to the id.
    """
    return sorted(
        requests,
        key=lambda request: charger_order_key(request.priority, request.order, request.charger_entry_id),
    )


def charger_order_key(priority: str, order: int, charger_entry_id: str) -> tuple[int, int, str]:
    """Where a charger stands in its site's order: "first" before "normal" before "last" (an unknown
    priority counts as "normal"), then the site's own charger order, then the entry id. Shared by the
    capacity allocation and the solar surplus split, so both serve chargers in the same order.
    """
    return (_PRIORITY_RANK.get(priority, 1), order, charger_entry_id)


def allocate_chargers(
    phase_headroom_a: Mapping[PhaseName, float],
    requests: Sequence[ChargerRequest],
) -> dict[str, ChargerAllocation]:
    """Split each phase's headroom across every charger that uses it.

    Chargers are taken in `allocation_order` (priority, then the site's charger order), each reserving its
    proposal. An unknown `requested_current_a` blocks every charger sharing one of
    its phases, since it draws an unquantified amount.
    """
    phases_blocked_by_unknown_request: set[PhaseName] = set()
    for request in requests:
        if request.requested_current_a is None:
            phases_used = request.phases_used()
            if phases_used is not None:
                phases_blocked_by_unknown_request.update(phases_used)

    remaining = dict(phase_headroom_a)
    results: dict[str, ChargerAllocation] = {}
    for request in allocation_order(requests):
        phases_used = request.phases_used()
        if phases_used is None:
            results[request.charger_entry_id] = ChargerAllocation(
                charger_entry_id=request.charger_entry_id,
                requested_current_a=request.requested_current_a,
                proposed_current_a=None,
                limiting_phase=None,
                state="invalid_measurements",
                reason=REASON_UNKNOWN_PHASE,
            )
            continue

        if request.requested_current_a is None:
            results[request.charger_entry_id] = ChargerAllocation(
                charger_entry_id=request.charger_entry_id,
                requested_current_a=None,
                proposed_current_a=None,
                limiting_phase=None,
                state="requested_current_unknown",
                reason=REASON_REQUESTED_UNKNOWN,
            )
            continue

        if phases_blocked_by_unknown_request.intersection(phases_used):
            results[request.charger_entry_id] = ChargerAllocation(
                charger_entry_id=request.charger_entry_id,
                requested_current_a=request.requested_current_a,
                proposed_current_a=None,
                limiting_phase=None,
                state="requested_current_unknown",
                reason=REASON_BLOCKED_BY_UNKNOWN_SIBLING,
            )
            continue

        available = min(remaining[phase] for phase in phases_used)
        capped = min(available, request.requested_current_a)
        floored = max(round_down_to_step(capped), 0.0)
        limiting = _limiting_phase(remaining, phases_used)

        if request.requested_current_a <= 0.0 and floored < request.min_current_a:
            # Asking for nothing is not charging, not a headroom too small for the minimum.
            proposed = 0.0
            state: ChargerState = "not_requesting"
            reason = REASON_NO_CURRENT_REQUESTED
            limiting = None
        elif floored < request.min_current_a:
            proposed = 0.0
            state = "below_minimum_current"
            reason = REASON_BELOW_MINIMUM
        elif floored + 1e-9 < request.requested_current_a:
            proposed = floored
            state = "capacity_limited"
            reason = REASON_CAPACITY_LIMITED
        else:
            proposed = floored
            state = "capacity_available"
            reason = REASON_CAPACITY_AVAILABLE
            limiting = None

        for phase in phases_used:
            remaining[phase] -= proposed

        results[request.charger_entry_id] = ChargerAllocation(
            charger_entry_id=request.charger_entry_id,
            requested_current_a=request.requested_current_a,
            proposed_current_a=proposed,
            limiting_phase=limiting,
            state=state,
            reason=reason,
        )
    return results


def calculate_site_capacity(
    config: SiteCalculationConfig, requests: Sequence[ChargerRequest]
) -> SiteCapacityResult:
    """Compute one full, advisory site-capacity snapshot. Never calls a service."""
    empty_currents: dict[PhaseName, float | None] = {phase: None for phase in PHASES}
    empty_ages: dict[PhaseName, float | None] = {phase: None for phase in PHASES}
    empty_headroom: dict[PhaseName, float | None] = {phase: None for phase in PHASES}
    empty_liveness: dict[PhaseName, PhaseLiveness | None] = {phase: None for phase in PHASES}

    if not config.enabled:
        return _short_circuit(
            "disabled",
            REASON_DISABLED,
            config,
            requests,
            empty_currents,
            empty_ages,
            empty_headroom,
            report_ages=empty_ages,
            liveness=empty_liveness,
        )

    if (
        config.measurement_mode == "unavailable"
        or config.main_fuse_a is None
        or config.main_fuse_a <= 0
    ):
        return _short_circuit(
            "not_configured",
            REASON_NOT_CONFIGURED,
            config,
            requests,
            empty_currents,
            empty_ages,
            empty_headroom,
            report_ages=empty_ages,
            liveness=empty_liveness,
        )

    measured, ages, report_ages, liveness, problem, bases = _resolve_measurements(config)
    # A site that proposes nothing keeps the mode's confidence; "guarded" only
    # describes a proposal made from a flat reading.
    mode_confidence: Confidence = "high" if config.measurement_mode == "direct_phase_current" else "low"

    if problem is not None:
        return _short_circuit(
            problem,
            _REASON_FOR_PROBLEM[problem],
            config,
            requests,
            measured,
            ages,
            empty_headroom,
            mode_confidence,
            report_ages=report_ages,
            liveness=liveness,
            bases=bases,
        )

    # Every phase is usable here, so every value is a real float.
    confirmed_measured = cast(dict[PhaseName, float], measured)
    # Phases accepted on a re-reported unchanged value: usable, at a discount.
    uncertain_phases = _confirmed_unchanged_phases(liveness)
    confidence: Confidence = "guarded" if uncertain_phases else mode_confidence

    # Uncredited margin, the value `allocations` use.
    measured_margin = headroom_with_liveness_margin(
        site_headroom_per_phase(config.main_fuse_a, config.safety_margin_a, confirmed_measured),
        uncertain_phases,
        CONFIRMED_UNCHANGED_MARGIN_A,
    )

    # The credited variants are still computed in both modes: an implausible
    # credit stays an `invalid_measurements` signal, and the result feeds only the
    # diagnostic `calculated_headroom_after_ev_credit_a`.
    if config.measurement_mode == "direct_phase_current":
        other_load, inconsistent_phases = other_load_per_phase(
            confirmed_measured, requests, config.max_age_s
        )
    else:
        other_load, inconsistent_phases = _derived_other_load_per_phase(
            cast(DerivedPhaseMeasurement, config.derived), confirmed_measured, requests, config.max_age_s
        )
    credited_headroom = headroom_with_liveness_margin(
        site_headroom_per_phase(config.main_fuse_a, config.safety_margin_a, other_load),
        uncertain_phases,
        CONFIRMED_UNCHANGED_MARGIN_A,
    )
    headroom = measured_margin
    signed_margin = _signed_margin_per_phase(config, confirmed_measured, uncertain_phases)
    # A phase whose credited charger reading exceeds its site reading is normal when the rest of
    # the site exports and the direction is known. Without a known direction the charger is
    # treated as additive (`measured_margin`); the odd reading is only reported.
    exceeds = tuple(
        phase for phase in PHASES if phase in inconsistent_phases and signed_margin[phase] is None
    )
    allocations = allocate_chargers(headroom, requests)

    limiting = _limiting_phase(headroom, PHASES)
    ordered_allocations = tuple(allocations[request.charger_entry_id] for request in requests)
    battery_estimate, battery_basis = estimated_headroom_if_battery_yields(
        headroom, config.battery, _derived_voltage_by_phase(config), config.max_age_s
    )
    signed_power, signed_power_age, signed_power_reason, signed_power_diagnostic = (
        _derived_signed_active_power_by_phase(config)
    )
    voltage_diagnostic = _derived_voltage_diagnostic_by_phase(config)

    return SiteCapacityResult(
        state="observing",
        reason=REASON_OBSERVING,
        measurement_mode=config.measurement_mode,
        confidence=confidence,
        measured_phase_current_a=measured,
        phase_age_s=ages,
        phase_headroom_a=cast(dict[PhaseName, float | None], headroom),
        limiting_phase=limiting,
        max_age_s=config.max_age_s,
        allocations=ordered_allocations,
        measured_margin_a=cast(dict[PhaseName, float | None], measured_margin),
        calculated_headroom_after_ev_credit_a=cast(dict[PhaseName, float | None], credited_headroom),
        estimated_headroom_if_battery_yields_a=battery_estimate,
        battery_yield_basis=battery_basis,
        phase_report_age_s=report_ages,
        phase_liveness=liveness,
        phase_signed_active_power_w=signed_power,
        phase_signed_active_power_age_s=signed_power_age,
        phase_signed_active_power_reason=signed_power_reason,
        phase_signed_active_power_diagnostic_w=signed_power_diagnostic,
        phase_voltage_v=voltage_diagnostic,
        phase_current_basis=dict(bases),
        current_estimated=_any_estimated(bases),
        estimated_power_factor=ESTIMATED_POWER_FACTOR if _any_estimated(bases) else None,
        phase_signed_margin_a=cast(dict[PhaseName, float | None], signed_margin),
        charger_reading_exceeds_site_phases=exceeds,
    )


def _signed_margin_per_phase(
    config: SiteCalculationConfig,
    magnitude_a: Mapping[PhaseName, float],
    uncertain_phases: Collection[PhaseName],
) -> dict[PhaseName, float | None]:
    """Per phase, the room for the charger's increase judged on the signed rest of the site.

    With `I` the phase's |net| current, `Ip = P / U` its signed active part (clamped to `I`) and
    `Iq = sqrt(I^2 - Ip^2)` the rest, a charger of `c` A (unity power factor) leaves
    `|rest + proposed|` of `sqrt((Ip - c + proposed)^2 + Iq^2)`. Staying under the ceiling `C`
    allows `proposed - c <= sqrt(C^2 - Iq^2) - Ip`, which is this margin. It equals
    `C - I` on net import without reactive load, and is larger when the site exports. `None`
    where the direction is unknown (direct mode) or the voltage is unusable.
    """
    result: dict[PhaseName, float | None] = {phase: None for phase in PHASES}
    if (
        config.measurement_mode != "derived_phase_current"
        or config.derived is None
        or config.main_fuse_a is None
    ):
        return result
    ceiling = config.main_fuse_a - config.safety_margin_a
    for phase in PHASES:
        derived_input = config.derived.get(phase)
        power = derived_input.active_power_w.value
        voltage = derived_input.voltage_v.value
        if power is None or voltage is None or voltage <= 0 or ceiling <= 0:
            continue
        magnitude = magnitude_a[phase]
        active = max(-magnitude, min(magnitude, power / voltage))
        reactive_sq = max(0.0, magnitude * magnitude - active * active)
        margin = math.sqrt(max(0.0, ceiling * ceiling - reactive_sq)) - active
        if phase in uncertain_phases:
            margin -= CONFIRMED_UNCHANGED_MARGIN_A
        result[phase] = margin
    return result


def _any_estimated(bases: Mapping[PhaseName, CurrentBasis | None] | None) -> bool:
    return bases is not None and any(basis == "estimated" for basis in bases.values())


_REASON_FOR_PROBLEM: dict[SiteState, str] = {
    "missing_measurements": REASON_MISSING,
    "stale_measurements": REASON_STALE,
    "invalid_measurements": REASON_INVALID,
}


def _short_circuit(
    state: SiteState,
    reason: str,
    config: SiteCalculationConfig,
    requests: Sequence[ChargerRequest],
    measured: Mapping[PhaseName, float | None],
    ages: Mapping[PhaseName, float | None],
    headroom: Mapping[PhaseName, float | None],
    confidence: Confidence = "none",
    *,
    report_ages: Mapping[PhaseName, float | None] | None = None,
    liveness: Mapping[PhaseName, PhaseLiveness | None] | None = None,
    bases: Mapping[PhaseName, CurrentBasis | None] | None = None,
) -> SiteCapacityResult:
    """Every charger inherits the same site-level problem; `report_ages`/`liveness`
    default to all unknown."""
    empty = {phase: None for phase in PHASES}
    allocations = tuple(
        ChargerAllocation(
            charger_entry_id=request.charger_entry_id,
            requested_current_a=request.requested_current_a,
            proposed_current_a=None,
            limiting_phase=None,
            state=state,
            reason=reason,
        )
        for request in requests
    )
    battery_estimate, battery_basis = estimated_headroom_if_battery_yields(
        headroom, config.battery, _derived_voltage_by_phase(config), config.max_age_s
    )
    signed_power, signed_power_age, signed_power_reason, signed_power_diagnostic = (
        _derived_signed_active_power_by_phase(config)
    )
    voltage_diagnostic = _derived_voltage_diagnostic_by_phase(config)
    return SiteCapacityResult(
        state=state,
        reason=reason,
        measurement_mode=config.measurement_mode,
        confidence=confidence,
        measured_phase_current_a=measured,
        phase_age_s=ages,
        phase_headroom_a=headroom,
        limiting_phase=None,
        max_age_s=config.max_age_s,
        allocations=allocations,
        measured_margin_a=headroom,
        calculated_headroom_after_ev_credit_a=headroom,
        estimated_headroom_if_battery_yields_a=battery_estimate,
        battery_yield_basis=battery_basis,
        phase_report_age_s=report_ages if report_ages is not None else empty,
        phase_liveness=liveness if liveness is not None else empty,
        phase_signed_active_power_w=signed_power,
        phase_signed_active_power_age_s=signed_power_age,
        phase_signed_active_power_reason=signed_power_reason,
        phase_signed_active_power_diagnostic_w=signed_power_diagnostic,
        phase_voltage_v=voltage_diagnostic,
        phase_current_basis=dict(bases) if bases is not None else dict(empty),
        current_estimated=_any_estimated(bases),
        estimated_power_factor=ESTIMATED_POWER_FACTOR if _any_estimated(bases) else None,
    )


def _derived_voltage_by_phase(config: SiteCalculationConfig) -> dict[PhaseName, float | None] | None:
    """Each phase's voltage from `config.derived`, or `None` in direct mode."""
    if config.measurement_mode != "derived_phase_current" or config.derived is None:
        return None
    return {phase: config.derived.get(phase).voltage_v.value for phase in PHASES}


def _phase_signed_active_power(
    phase_value: PhaseValue | None, max_age_s: float
) -> tuple[float | None, float | None, PhaseUnusableReason | None]:
    """One phase's signed active power in watts, its raw age, and (when unusable)
    why. Mirrors `_is_usable`, including the `.problem` check."""
    if phase_value is None:
        return None, None, "missing"
    if phase_value.value is None or phase_value.problem is not None:
        reason: PhaseUnusableReason = (
            cast(PhaseUnusableReason, phase_value.problem)
            if phase_value.problem is not None
            else "missing"
        )
        return None, phase_value.age_s, reason
    if phase_value.age_s is None or phase_value.age_s > max_age_s:
        return None, phase_value.age_s, "stale"
    return phase_value.value, phase_value.age_s, None


def _phase_diagnostic_value(phase_value: PhaseValue | None) -> float | None:
    """One phase's raw reading, `None` only for "missing" or "invalid", never for
    age."""
    if phase_value is None or phase_value.value is None or phase_value.problem is not None:
        return None
    return phase_value.value


def _derived_signed_active_power_by_phase(
    config: SiteCalculationConfig,
) -> tuple[
    dict[PhaseName, float | None],
    dict[PhaseName, float | None],
    dict[PhaseName, PhaseUnusableReason | None],
    dict[PhaseName, float | None],
]:
    """Signed power `values`/`ages`/`reasons`/`diagnostic` per phase; all `None`
    in direct mode."""
    empty: dict[PhaseName, float | None] = {phase: None for phase in PHASES}
    empty_reason: dict[PhaseName, PhaseUnusableReason | None] = {phase: None for phase in PHASES}
    if config.measurement_mode != "derived_phase_current" or config.derived is None:
        return dict(empty), dict(empty), dict(empty_reason), dict(empty)

    values: dict[PhaseName, float | None] = {}
    ages: dict[PhaseName, float | None] = {}
    reasons: dict[PhaseName, PhaseUnusableReason | None] = {}
    diagnostic: dict[PhaseName, float | None] = {}
    for phase in PHASES:
        derived_input = config.derived.get(phase)
        power = derived_input.active_power_w if derived_input is not None else None
        value, age_s, reason = _phase_signed_active_power(power, config.max_age_s)
        values[phase] = value
        ages[phase] = age_s
        reasons[phase] = reason
        diagnostic[phase] = _phase_diagnostic_value(power)
    return values, ages, reasons, diagnostic


def _derived_voltage_diagnostic_by_phase(config: SiteCalculationConfig) -> dict[PhaseName, float | None]:
    """`phase_voltage_v` per phase, age-independent; all `None` in direct mode."""
    empty: dict[PhaseName, float | None] = {phase: None for phase in PHASES}
    if config.measurement_mode != "derived_phase_current" or config.derived is None:
        return dict(empty)
    return {
        phase: _phase_diagnostic_value(config.derived.get(phase).voltage_v) for phase in PHASES
    }


def _resolve_measurements(
    config: SiteCalculationConfig,
) -> tuple[
    dict[PhaseName, float | None],
    dict[PhaseName, float | None],
    dict[PhaseName, float | None],
    dict[PhaseName, PhaseLiveness | None],
    SiteState | None,
    dict[PhaseName, CurrentBasis | None],
]:
    """Normalize every phase's value for the configured mode.

    Returns `(values, ages, report_ages, liveness, problem, bases)`, where `problem` is
    the single site-level state, by priority invalid, stale, then missing, and `bases`
    says how each usable phase's current was obtained.
    """
    values: dict[PhaseName, float | None] = {}
    ages: dict[PhaseName, float | None] = {}
    report_ages: dict[PhaseName, float | None] = {}
    liveness: dict[PhaseName, PhaseLiveness | None] = {}
    problems: set[SiteState] = set()
    bases: dict[PhaseName, CurrentBasis | None] = {phase: None for phase in PHASES}

    for phase in PHASES:
        if config.measurement_mode == "direct_phase_current":
            phase_value = config.direct.get(phase) if config.direct is not None else None
            if phase_value is None:
                value, age_s, report_age_s, phase_problem = None, None, None, "missing"
            else:
                value, age_s, report_age_s, phase_problem = (
                    phase_value.value,
                    phase_value.age_s,
                    phase_value.report_age_s,
                    phase_value.problem,
                )
                bases[phase] = "measured" if phase_value.value is not None else None
        else:
            derived_input = config.derived.get(phase) if config.derived is not None else None
            if derived_input is None:
                value, age_s, report_age_s, phase_problem = None, None, None, "missing"
            else:
                derived_current = derived_phase_current(derived_input)
                value = derived_current.value
                age_s = derived_current.age_s
                report_age_s = derived_current.report_age_s
                phase_problem = derived_current.problem
                bases[phase] = derived_current.basis

        values[phase] = value
        ages[phase] = age_s
        report_ages[phase] = report_age_s

        if value is None:
            problems.add("invalid_measurements" if phase_problem == "invalid" else "missing_measurements")
            liveness[phase] = None
            continue
        # `fresh` and `confirmed_unchanged` are accepted; see `_liveness_allows_headroom`.
        liveness_value = classify_phase_liveness(age_s, report_age_s, config.max_age_s)
        liveness[phase] = liveness_value
        if not _liveness_allows_headroom(liveness_value, age_s):
            problems.add("stale_measurements")

    if "invalid_measurements" in problems:
        return values, ages, report_ages, liveness, "invalid_measurements", bases
    if "stale_measurements" in problems:
        return values, ages, report_ages, liveness, "stale_measurements", bases
    if "missing_measurements" in problems:
        return values, ages, report_ages, liveness, "missing_measurements", bases
    return values, ages, report_ages, liveness, None, bases
