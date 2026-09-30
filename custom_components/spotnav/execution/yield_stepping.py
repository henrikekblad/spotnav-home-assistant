"""Yield-verified stepping: find out from the site's own readings whether a load on the site
gives way to the charger, and if so use that instead of ignoring it.

`site/site_capacity.py` measures headroom against the fuse. Where a house battery holds the grid
current at a setpoint slightly above the fuse, that headroom is always negative, so the overload
branch fires forever and the car sits at its floor. Instead of judging *where* the site current is,
this module judges whether a proposed change *moves* it, using only the two readings the integration
already has, before and after one step.

Two traps the code exists to avoid:

1. Never derive a setpoint from delivered current. A car delivers less than assigned (about 89 %,
   about 98 % at the floor). Every setpoint proposed here, a step or a revert, comes from
   `assigned_a` and the `stable_assigned`/`baseline_assigned` recorded from it. Delivered current
   only detects that a step happened and measures the response.
2. "Hold" means "do not write". A `hold` verdict carries `current_a is None`; writing the measured
   current back would itself be an unintended reduction.

The floor is not a plateau: a raw pause below `min_current_a` with `assigned_a` already at or below
it is never written, so `raw` and `assigned_a` would never converge. It is treated as a hold
(`held_at_floor_raw_pause`) so a probe can be considered; a pause above the floor still passes
through.

Pure Python: no Home Assistant import, I/O, asyncio or clock. Time arrives through
`YieldObservation.now`, so tests can replay observations. It decides one thing: whether the raw,
already-safe decision should be overridden, and how, never beyond what the raw decision's own
evidence supports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from ..site.site_capacity import PhaseName


# Per-charger state.
YieldState = Literal["unknown", "settling", "confirmed", "backoff"]

# What a verdict tells the caller to do; only `"write"` carries `YieldVerdict.current_a`.
YieldAction = Literal["write", "hold", "passthrough"]

# Every reason a verdict can carry, one per rule in `observe`'s ordered steps, so a log
# line always says which rule decided.
YieldReason = Literal[
    # Step 1: a needed reading, `assigned_a`, or the raw proposal was `None`.
    "passthrough_no_basis",
    # Step 2: the hard ceiling, which nothing overrides.
    "passthrough_ceiling",
    # Step 3, still inside the settle window.
    "settling_hold",
    # Step 3 expiry, step absorbed (`y >= confirm_y`). A hold: the stepped assignment is
    # not touched again this tick; a further step waits for a later observation.
    "confirmed_after_settling",
    # Step 3 expiry, step not absorbed (`y < confirm_y`): urgent revert to the pre-step
    # value, and `backoff` begins.
    "revert_not_absorbed",
    # Step 4: real headroom exists on the raw decision's own evidence.
    "passthrough_raw_increase",
    # Step 5, confirmed and flat: the site sits where the yielding load holds it.
    "held_at_yield_setpoint",
    # Step 5, at the floor with a raw pause that cannot be carried out (below
    # `min_current_a`, nothing left to reduce); only changes what step 6 may do.
    "held_at_floor_raw_pause",
    # Step 5, otherwise, with a reference on record: the site rose above it, so the raw
    # reduction goes through at once.
    "passthrough_site_rising",
    # Step 5, otherwise, with no reference recorded: an ordinary passthrough.
    "passthrough_raw_reduce",
    # Step 6, confirmed and fresh and flat: a licensed step.
    "confirmed_step",
    # Step 6, unknown or confirmed-but-stale: an unverified probe.
    "probe_step",
    "passthrough_not_drawing",
    "passthrough_car_not_using_assignment",
    # Step 6 licensed nothing and step 5 did not hold (raw held at `assigned_a`, or a step
    # was blocked by `backoff` or `requested_a` already being met).
    "passthrough_raw_holds",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class YieldConfig:
    """Every tunable, with its default.

    `ceiling_a` has no default: it is a property of the site, so the caller supplies it. `min_current_a`
    is the charger's configured minimum; a raw pause at the floor is recognised as not a carryable
    reduction by using it.
    """

    step_a: float = 2.0
    min_delta_a: float = 1.5
    steady_a: float = 0.3
    settle_s: float = 30.0
    confirm_y: float = 0.7
    ttl_s: float = 300.0
    absorb_tol_a: float = 0.5
    session_floor_a: float = 1.0
    # A step is licensed only while the car demonstrably uses the current it has: delivered at
    # least this fraction of assigned on every phase (normally 0.89, 0.98 at the floor).
    follow_ratio: float = 0.8
    backoff_initial_s: float = 600.0
    backoff_max_s: float = 3600.0
    min_current_a: float = 6.0
    ceiling_a: float


@dataclass(frozen=True, slots=True)
class YieldObservation:
    """One snapshot of the two readings this layer may use.

    `site_current_a` and `delivered_current_a` are keyed by phase; a missing or `None` entry means "no
    basis". `now` is seconds from the caller's clock; this module never reads one.
    """

    now: float
    site_current_a: Mapping[PhaseName, float | None]
    delivered_current_a: Mapping[PhaseName, float | None]
    phases: tuple[PhaseName, ...]


@dataclass(frozen=True, slots=True)
class YieldVerdict:
    """What to do about one charger this observation, and why.

    `current_a` is set only for `"write"`; a `"hold"` carries `None`. `y` and `y_age_s` are the current
    estimate and its age (`None` before any confirmed step). `reference_a` is the per-phase site current
    the yielding load was last seen to hold, empty until the first confirm.
    """

    action: YieldAction
    current_a: float | None
    reason: YieldReason
    urgent: bool
    state: YieldState
    y: float | None
    y_age_s: float | None
    reference_a: Mapping[PhaseName, float]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


class YieldStepper:
    """One instance per charger: the state machine fed one `YieldObservation` at a time."""

    def __init__(self, config: YieldConfig) -> None:
        self._config = config

        self._state: YieldState = "unknown"

        # Most recent steady (non-ramping) reading: the baseline the next step is detected against.
        self._stable_site: dict[PhaseName, float] | None = None
        self._stable_delivered: dict[PhaseName, float] | None = None
        self._stable_assigned: float | None = None

        # Previous observation's delivered current, to decide whether this one is steady enough
        # to update the stable baseline (which a ramp in progress must not overwrite).
        self._prev_delivered: dict[PhaseName, float] | None = None

        # Baseline of the step being settled, copied when it was detected so a later ramp
        # cannot move it.
        self._t_step: float | None = None
        self._baseline_site: dict[PhaseName, float] | None = None
        self._baseline_delivered: dict[PhaseName, float] | None = None
        self._baseline_assigned: float | None = None
        # Whether settling interrupted "confirmed"; needed to restore it if the car did not follow.
        self._was_confirmed_before_settling = False

        self._y: float | None = None
        self._y_at: float | None = None
        self._reference: dict[PhaseName, float] | None = None

        self._backoff_until: float | None = None
        # Duration of the next backoff: doubles on each consecutive failure up to
        # `backoff_max_s`, reset by a confirm.
        self._next_backoff_s = self._config.backoff_initial_s

    def observe(
        self,
        raw_proposed_current_a: float | None,
        requested_a: float,
        assigned_a: float | None,
        observation: YieldObservation,
    ) -> YieldVerdict:
        """Update state from one observation and return its verdict, evaluating the rules in order.

        `raw_proposed_current_a` is the regulator's proposal (may be `None`); `requested_a` is the
        plan's requested current, always known and never a reason to pass through; `assigned_a` is the last
        written value, `None` before a session's first write.
        """
        cfg = self._config
        now = observation.now
        phases = observation.phases
        site = observation.site_current_a
        delivered = observation.delivered_current_a

        # 1. No basis: never act on data the raw decision could not use.
        if (
            raw_proposed_current_a is None
            or assigned_a is None
            or any(site.get(phase) is None or delivered.get(phase) is None for phase in phases)
        ):
            return self._verdict(now, "passthrough", None, "passthrough_no_basis", False)

        site_a = {phase: site[phase] for phase in phases}
        delivered_a = {phase: delivered[phase] for phase in phases}

        # Session reset: the car stopped drawing, so nothing from the last session applies.
        if all(delivered_a[phase] < cfg.session_floor_a for phase in phases):
            # A car that is not drawing can confirm nothing, so no step may be licensed.
            self._reset_session()
            return self._verdict(now, "passthrough", None, "passthrough_not_drawing", False)

        # An elapsed backoff is over: probing restarts from "unknown", not from confirmed
        # data a revert already disproved.
        if (
            self._state == "backoff"
            and self._backoff_until is not None
            and now >= self._backoff_until
        ):
            self._state = "unknown"
            self._y = None
            self._y_at = None
            self._reference = None

        # Stable baseline and step detection, only while not settling: a ramp must not
        # overwrite the pre-ramp baseline nor be redetected against a moved one.
        prev_delivered = self._prev_delivered
        self._prev_delivered = dict(delivered_a)
        if self._state != "settling":
            steady = prev_delivered is not None and all(
                abs(delivered_a[phase] - prev_delivered.get(phase, delivered_a[phase])) < cfg.steady_a
                for phase in phases
            )
            if prev_delivered is None or steady:
                self._stable_site = dict(site_a)
                self._stable_delivered = dict(delivered_a)
                self._stable_assigned = assigned_a

            if self._stable_delivered is not None and any(
                delivered_a[phase] - self._stable_delivered[phase] >= cfg.min_delta_a for phase in phases
            ):
                self._was_confirmed_before_settling = self._state == "confirmed"
                self._state = "settling"
                self._t_step = now
                self._baseline_site = dict(self._stable_site)
                self._baseline_delivered = dict(self._stable_delivered)
                self._baseline_assigned = self._stable_assigned

        # 2. Hard ceiling: checked before everything else; nothing overrides it.
        if any(site_a[phase] >= cfg.ceiling_a for phase in phases):
            if self._state in ("settling", "confirmed"):
                self._enter_backoff(now)
            urgent = raw_proposed_current_a < assigned_a
            return self._verdict(now, "passthrough", None, "passthrough_ceiling", urgent)

        # 3. Settling: hold until `settle_s` elapses, then measure.
        if self._state == "settling":
            assert self._t_step is not None
            if now - self._t_step < cfg.settle_s:
                return self._verdict(now, "hold", None, "settling_hold", False)

            assert self._baseline_delivered is not None
            changed_phases = [
                phase
                for phase in phases
                if delivered_a[phase] - self._baseline_delivered[phase] >= cfg.min_delta_a
            ]
            if not changed_phases:
                # The car did not follow (not a failure): return to the pre-settling state and
                # continue as if it had never begun.
                if self._was_confirmed_before_settling:
                    self._state = "confirmed"
                else:
                    self._state = "unknown"
                    self._y = None
                    self._y_at = None
                    self._reference = None
            else:
                assert self._baseline_site is not None
                y_values = [
                    _clamp01(
                        1.0
                        - (site_a[phase] - self._baseline_site[phase])
                        / (delivered_a[phase] - self._baseline_delivered[phase])
                    )
                    for phase in changed_phases
                ]
                y = min(y_values)
                if y >= cfg.confirm_y:
                    self._state = "confirmed"
                    self._y = y
                    self._y_at = now
                    self._reference = dict(site_a)
                    self._next_backoff_s = cfg.backoff_initial_s
                    # A hold: a further step waits for a later observation.
                    return self._verdict(now, "hold", None, "confirmed_after_settling", False)
                else:
                    revert_to = self._baseline_assigned
                    self._enter_backoff(now)
                    return self._verdict(
                        now, "write", revert_to, "revert_not_absorbed", True
                    )

        # 4. Raw wants to go up: real headroom on its own evidence, so it wins.
        if raw_proposed_current_a > assigned_a:
            return self._verdict(now, "passthrough", None, "passthrough_raw_increase", False)

        # --- 5. Raw wants to reduce or pause.
        step5_verdict: YieldVerdict | None = None
        if raw_proposed_current_a < assigned_a:
            floor_pause = raw_proposed_current_a < cfg.min_current_a and assigned_a <= cfg.min_current_a
            if floor_pause:
                # A pause that cannot legally be carried out (below the charger minimum) with
                # nothing left to reduce is not a real "wants to reduce" signal; hold the previous
                # assignment and let step 6 consider a probe.
                step5_verdict = self._verdict(
                    now, "hold", None, "held_at_floor_raw_pause", False
                )
            elif self._state == "confirmed" and all(
                site_a[phase] <= self._reference[phase] + cfg.absorb_tol_a for phase in phases
            ):
                step5_verdict = self._verdict(
                    now, "hold", None, "held_at_yield_setpoint", False
                )
            else:
                reason: YieldReason = (
                    "passthrough_site_rising" if self._reference is not None else "passthrough_raw_reduce"
                )
                return self._verdict(now, "passthrough", None, reason, True)

        # 6. Raw holds, or step 5 held: consider one step up. Reached only when raw equals
        # `assigned_a` or step 5 held.
        following = all(
            delivered_a[phase] >= cfg.follow_ratio * assigned_a for phase in phases
        )
        if assigned_a < requested_a and self._state != "backoff" and not following:
            if step5_verdict is not None:
                return step5_verdict
            return self._verdict(
                now, "passthrough", None, "passthrough_car_not_using_assignment", False
            )
        if assigned_a < requested_a and self._state != "backoff":
            target = min(requested_a, assigned_a + cfg.step_a)
            ceiling_ok = all(site_a[phase] + cfg.step_a <= cfg.ceiling_a for phase in phases)
            fresh = self._y_at is not None and (now - self._y_at) <= cfg.ttl_s
            absorbed = self._reference is not None and all(
                site_a[phase] <= self._reference[phase] + cfg.absorb_tol_a for phase in phases
            )

            if self._state == "confirmed" and fresh and absorbed and ceiling_ok:
                return self._verdict(now, "write", target, "confirmed_step", False)

            stale_confirmed = self._state == "confirmed" and not fresh
            if (self._state == "unknown" or stale_confirmed) and ceiling_ok:
                return self._verdict(now, "write", target, "probe_step", False)

        if step5_verdict is not None:
            return step5_verdict
        return self._verdict(now, "passthrough", None, "passthrough_raw_holds", False)

    def _enter_backoff(self, now: float) -> None:
        """Start or restart backoff: doubles on each consecutive failure, capped at
        `backoff_max_s`; a confirm resets it (done in `observe`'s settling branch).
        """
        duration = self._next_backoff_s
        self._backoff_until = now + duration
        self._state = "backoff"
        self._next_backoff_s = min(duration * 2.0, self._config.backoff_max_s)

    def _reset_session(self) -> None:
        """The car stopped drawing: discard everything, including a step in progress."""
        self._state = "unknown"
        self._stable_site = None
        self._stable_delivered = None
        self._stable_assigned = None
        self._prev_delivered = None
        self._t_step = None
        self._baseline_site = None
        self._baseline_delivered = None
        self._baseline_assigned = None
        self._was_confirmed_before_settling = False
        self._y = None
        self._y_at = None
        self._reference = None
        self._backoff_until = None
        self._next_backoff_s = self._config.backoff_initial_s

    def _verdict(
        self,
        now: float,
        action: YieldAction,
        current_a: float | None,
        reason: YieldReason,
        urgent: bool,
    ) -> YieldVerdict:
        y_age_s = None if self._y_at is None else now - self._y_at
        return YieldVerdict(
            action=action,
            current_a=current_a,
            reason=reason,
            urgent=urgent,
            state=self._state,
            y=self._y,
            y_age_s=y_age_s,
            reference_a=dict(self._reference) if self._reference is not None else {},
        )
