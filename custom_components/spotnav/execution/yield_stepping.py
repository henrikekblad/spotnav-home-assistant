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

import math
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
    # Step 2, one over-ceiling sample while yield stepping is verified and the excess is small:
    # one urgent step back, not the floor. A second consecutive sample takes `passthrough_ceiling`.
    "transient_step_back",
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
    # Credited climb: while the home battery is verified to give way to the car (see
    # `YieldVerdict.battery_verified`) and sun/grid priority is `car_first`, a step may be larger,
    # up to the battery's own charge current per phase and never above `max_step_a`; within
    # `near_target_a` of the request it is back to 1 A. The next step waits `step_gap_s` so the
    # previous one is measured first.
    max_step_a: float = 3.0
    near_target_a: float = 3.0
    credit_min_a: float = 1.0
    step_gap_s: float = 20.0
    # One over-ceiling sample is a transient only while it is no more than `transient_excess_a`
    # above the ceiling and fewer than `transient_limit` transients happened within
    # `transient_window_s`; anything else is the existing hard stop.
    transient_excess_a: float = 4.0
    transient_limit: int = 2
    transient_window_s: float = 120.0
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
    # The home battery's charge current per phase in amps (the smallest across this charger's
    # phases), `None` when there is no usable reading or it is not charging.
    battery_charge_a: float | None = None
    # Whether the site's solar priority lets the car take what the battery gives up (`car_first`).
    credit_allowed: bool = False


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
    # The last confirmed step was met by a matching drop of the battery's charge current, so the
    # battery's charge may be credited as headroom for the car.
    battery_verified: bool = False
    # The battery charge current (A per phase) a larger step was sized from, else `None`.
    battery_credit_a: float | None = None


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

        # The battery's per-phase charge current, kept like the stable/baseline site current.
        self._stable_battery: float | None = None
        self._baseline_battery: float | None = None
        self._battery_verified = False

        # Over-ceiling bookkeeping for the transient rule.
        self._ceiling_hits = 0
        self._transient_times: list[float] = []
        # The size of the last licensed step, the size a transient steps back, and when the next
        # credited step may be licensed.
        self._last_step_a = self._config.step_a
        self._next_step_at = 0.0

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
            self._battery_verified = False
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
            # A step just written whose delivered current has not risen yet is in flight: the
            # baseline must keep the value from before it, or a revert would restore the step.
            in_flight = (
                self._stable_assigned is not None
                and assigned_a > self._stable_assigned
                and any(delivered_a[phase] < cfg.follow_ratio * assigned_a for phase in phases)
            )
            if prev_delivered is None or (steady and not in_flight):
                self._stable_site = dict(site_a)
                self._stable_delivered = dict(delivered_a)
                self._stable_assigned = assigned_a
                self._stable_battery = observation.battery_charge_a

            if self._stable_delivered is not None and any(
                delivered_a[phase] - self._stable_delivered[phase] >= cfg.min_delta_a for phase in phases
            ):
                self._was_confirmed_before_settling = self._state == "confirmed"
                self._state = "settling"
                self._t_step = now
                self._baseline_site = dict(self._stable_site)
                self._baseline_delivered = dict(self._stable_delivered)
                self._baseline_assigned = self._stable_assigned
                self._baseline_battery = self._stable_battery

        # 2. Hard ceiling: checked before everything else; nothing overrides it.
        over_ceiling = [site_a[phase] - cfg.ceiling_a for phase in phases if site_a[phase] >= cfg.ceiling_a]
        if not over_ceiling:
            self._ceiling_hits = 0
        else:
            self._ceiling_hits += 1
            transient = self._transient_step_back(now, assigned_a, max(over_ceiling))
            if transient is not None:
                return transient
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
                    self._battery_verified = False
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
                    rise = max(
                        delivered_a[phase] - self._baseline_delivered[phase] for phase in changed_phases
                    )
                    self._battery_verified = self._battery_gave_way(
                        self._baseline_battery, observation.battery_charge_a, rise
                    )
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
            fresh = self._y_at is not None and (now - self._y_at) <= cfg.ttl_s
            absorbed = self._reference is not None and all(
                site_a[phase] <= self._reference[phase] + cfg.absorb_tol_a for phase in phases
            )

            if self._state == "confirmed" and fresh and absorbed:
                credit_a = (
                    observation.battery_charge_a
                    if self._battery_verified and observation.credit_allowed
                    else None
                )
                if credit_a is None:
                    step_a = cfg.step_a
                    unabsorbed_a = step_a
                else:
                    # The battery gives up what the car takes, so only the part it did not absorb
                    # in the verified step can reach the grid. At least one whole amp.
                    step_a = float(math.floor(min(cfg.max_step_a, credit_a)))
                    if requested_a - assigned_a <= cfg.near_target_a:
                        step_a = min(step_a, 1.0)
                    unabsorbed_a = step_a * (1.0 - (self._y or 0.0))
                if (
                    step_a >= (cfg.step_a if credit_a is None else cfg.credit_min_a)
                    and now >= self._next_step_at
                    and all(site_a[phase] + unabsorbed_a <= cfg.ceiling_a for phase in phases)
                ):
                    target = min(requested_a, assigned_a + step_a)
                    self._last_step_a = step_a
                    if credit_a is not None:
                        self._next_step_at = now + cfg.step_gap_s
                    return self._verdict(
                        now, "write", target, "confirmed_step", False, battery_credit_a=credit_a
                    )

            ceiling_ok = all(site_a[phase] + cfg.step_a <= cfg.ceiling_a for phase in phases)
            stale_confirmed = self._state == "confirmed" and not fresh
            if (self._state == "unknown" or stale_confirmed) and ceiling_ok:
                target = min(requested_a, assigned_a + cfg.step_a)
                self._last_step_a = cfg.step_a
                return self._verdict(now, "write", target, "probe_step", False)

        if step5_verdict is not None:
            return step5_verdict
        return self._verdict(now, "passthrough", None, "passthrough_raw_holds", False)

    def _battery_gave_way(
        self, before_a: float | None, after_a: float | None, rise_a: float
    ) -> bool:
        """Whether the battery's charge current fell by `confirm_y` of what the car's delivered
        current rose by (both per phase), so the grid stayed put because the battery gave way, not
        because something else did. Unknown on either side is not verified.
        """
        if before_a is None or after_a is None or rise_a <= 0.0:
            return False
        return _clamp01((before_a - after_a) / rise_a) >= self._config.confirm_y

    def _transient_step_back(
        self, now: float, assigned_a: float, excess_a: float
    ) -> YieldVerdict | None:
        """One over-ceiling sample while yield stepping is verified: a single urgent step back
        instead of the hard stop. `None` when the hard stop applies: not verified, a second
        consecutive sample, a large excess, too many recent transients, or no room above the floor.
        """
        cfg = self._config
        verified = self._state == "confirmed" or (
            self._state == "settling" and self._was_confirmed_before_settling
        )
        self._transient_times = [t for t in self._transient_times if now - t < cfg.transient_window_s]
        if (
            not verified
            or self._ceiling_hits != 1
            or excess_a > cfg.transient_excess_a
            or len(self._transient_times) >= cfg.transient_limit
            or assigned_a <= cfg.min_current_a
        ):
            return None
        target = max(cfg.min_current_a, assigned_a - max(1.0, self._last_step_a))
        if self._state == "settling":
            baseline = self._baseline_assigned
            if baseline is not None and cfg.min_current_a <= baseline < assigned_a:
                target = min(target, baseline)
            # The step in progress is abandoned; yield stays confirmed as it was.
            self._state = "confirmed"
        self._transient_times.append(now)
        self._next_step_at = now + cfg.settle_s + cfg.step_gap_s
        return self._verdict(now, "write", target, "transient_step_back", True)

    def _enter_backoff(self, now: float) -> None:
        """Start or restart backoff: doubles on each consecutive failure, capped at
        `backoff_max_s`; a confirm resets it (done in `observe`'s settling branch).
        """
        duration = self._next_backoff_s
        self._backoff_until = now + duration
        self._state = "backoff"
        self._battery_verified = False
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
        self._stable_battery = None
        self._baseline_battery = None
        self._battery_verified = False
        self._ceiling_hits = 0
        self._transient_times = []
        self._next_step_at = 0.0
        self._backoff_until = None
        self._next_backoff_s = self._config.backoff_initial_s

    def _verdict(
        self,
        now: float,
        action: YieldAction,
        current_a: float | None,
        reason: YieldReason,
        urgent: bool,
        battery_credit_a: float | None = None,
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
            battery_verified=self._battery_verified,
            battery_credit_a=battery_credit_a,
        )
