"""Characterisation of `SiteCapacityController._async_apply_to_charger`: seeded scenarios, each one call of the function
with every collaborator it calls answering from the scenario, and the whole sequence of calls it makes (the awaited
command paths with their arguments, the sync checks it consults, the outcome lines it logs), its return value and the
charger's damper after it, digested and compared with the digests recorded from the code as it stood before the
function was split into a decide part and a write part. A pure refactor keeps every digest; a change of behaviour shows
which chunk of cases moved.

What is drawn: the charger (none, not commandable, switch-only, commandable; paused by balancing, a phase switch
settling), a running battery probe, a lost measurement, the decision's reason and proposal, a failed applyability
check, what is on the charger after a pause or an overload, the resume and the probe claiming the charger, the step
guard's rule, yield stepping on or off and its verdict, the battery's band, the damper's last write, deadband and dwell,
the margin, active control turned off before the write, and the result of a must-lower. The real damper decides, so
its deadband, dwell and protection band are met as they are.

The resume and the battery probe are awaited in the middle of the function; their stand-ins sometimes change what is
read after them (the charger's pause, the damper's last write, yield stepping, active control, the site closing, the
charger's minimum, its margin, and the damper kept for the charger replaced by another), so a read moved across one of
those awaits changes the record. Each outcome line carries the damper as it is when the line is logged.

A second set (`_exact_case`) draws from values exact in binary placed on the thresholds the function compares against:
a proposal equal to what was written (no lowering due), a last write at exactly the charger's minimum, a proposal at the
minimum, a guarded proposal and a stepper's current equal to what was written, a change of exactly the deadband, a
margin of exactly the protection band, and setpoints on half amps (rounded to even).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import random
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.spotnav.const import CONF_ACTIVE_CONTROL_ENABLED, CONF_PHASE_WIRING, CONF_YIELD_STEPPING_ENABLED
from custom_components.spotnav.execution.yield_stepping import YieldVerdict
from custom_components.spotnav.site import site_capacity_controller as scc
from custom_components.spotnav.site.regulator import ApplyabilityFailure, PhaseDecisionBasis, RegulatorDecision
from custom_components.spotnav.site.regulator_damping import RegulatorDamper
from custom_components.spotnav.site.site_capacity import PHASES
from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController
from custom_components.spotnav.site.step_guard import GuardResult

CID = "charger_a"
CASES = 6000
EXACT_CASES = 3000
CHUNK = 250

_REASONS = (
    "no_change_requested",
    "increase_within_room",
    "reducing_current_due_to_active_import_overload",
    "reducing_current_due_to_active_import_overload",
    "paused_safe_current_below_charger_minimum",
    scc.REASON_MAGNITUDE_LOWERING,
    scc.REASON_MAGNITUDE_RAISE,
    "phase_direction_unknown",
    "phase_direction_unknown_at_charge_start",
    scc.REASON_CHARGER_MEASUREMENT_UNUSABLE,
    scc.REASON_HELD_STILL_NO_RAISE,
    scc.REASON_IMPORT_TARGET_HELD,
    scc.REASON_IMPORT_TARGET_PAUSED,
)
_YIELD_REASONS = (
    "settling_hold",
    "confirmed_after_settling",
    "held_at_yield_setpoint",
    "probe_step",
    "confirmed_step",
    "revert_not_absorbed",
    "down_step_absorbed_by_battery",
    "passthrough_raw_reduce",
    "passthrough_raw_holds",
    "passthrough_ceiling",
    "passthrough_no_basis",
)
_FAILURE_KINDS = ("no_basis", "phase_unusable", "phase_stale", "direction_changed")
_GUARD_RULES = ("held_same_reading", "trimmed_overshoot", "up_margin", "edge")
_T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


class _Charger:
    """The charger controller as `_async_apply_to_charger` itself reads it; everything else goes to stand-ins."""

    def __init__(self, *, commandable: bool, switch_only: bool, paused: bool, settling: bool | None) -> None:
        self.is_commandable = commandable
        self.is_switch_only = switch_only
        self.paused_by_balancing = paused
        if settling is not None:
            self.phase_switch_settling = settling

    def __repr__(self) -> str:
        return f"charger(paused={self.paused_by_balancing})"


def _d(decision: RegulatorDecision) -> str:
    return f"D({decision.reason},{decision.proposed_current_a},{decision.overload},{decision.limiting_phase},{decision.pause_if_switched_pause_fails})"


def _dm(damper: RegulatorDamper) -> str:
    since = None if damper._pending_since is None else (damper._pending_since - _T0).total_seconds()
    return f"M({damper._written},{damper._pending},{since})"


class _Scenario:
    def __init__(self, rng: random.Random, exact: bool) -> None:
        self.rng = rng
        self.exact = exact
        self.events: list[str] = []
        # Seconds after `_T0` on the damper's clock.
        self.clock = 0.0

    def ev(self, *parts: Any) -> None:
        self.events.append("|".join(str(part) for part in parts))

    def chance(self, p: float) -> bool:
        return self.rng.random() < p


def _amps(rng: random.Random) -> float:
    return rng.choice([0.0, 5.0, 5.5, 6.0, 6.5, 7.0, 8.0, 9.5, 10.0, 12.0, 15.5, 16.0, 16.5, 20.0, 32.0])


def _near(rng: random.Random, base: float | None, deadband: float) -> float:
    if base is None:
        return _amps(rng)
    return max(0.0, base + rng.choice([0.0, 0.0, 0.5, -0.5, 1.0, -1.0, deadband, -deadband, 2.5, -4.0]))


def _verdict(sc: _Scenario, current: float | None) -> YieldVerdict:
    rng = sc.rng
    action = rng.choice(["hold", "write", "write", "passthrough", "passthrough"])
    return YieldVerdict(
        action=action,
        current_a=current if action == "write" else (None if rng.random() < 0.8 else current),
        reason=rng.choice(_YIELD_REASONS),
        urgent=sc.chance(0.3),
        state="unknown",
        y=None,
        y_age_s=None,
        reference_a={},
        battery_credit_a=rng.choice([None, None, 2.0]),
        reaches_request=sc.chance(0.2),
        yield_verified=sc.chance(0.3),
        battery_share_step=sc.chance(0.15),
    )


def _install(controller: SiteCapacityController, sc: _Scenario, charger: Any, state: dict[str, Any]) -> None:
    rng = sc.rng
    deadband = state["deadband"]
    allowed = list(state["allowed"])

    def active_control_allowed() -> bool:
        value = allowed.pop(0) if allowed else True
        sc.ev("allowed", value)
        return value

    def measurement_lost_due(cid: str, fresh: Any) -> ApplyabilityFailure | None:
        sc.ev("lost?", cid, fresh.tag)
        return state["lost"]

    def draws_above_minimum(cid: str, cc: Any) -> bool:
        sc.ev("draws_above_min?", cid, cc)
        return state["draws_above_min"]

    def forget_pause(damper: RegulatorDamper, cc: Any, previous: float | None, minimum: float, proposed: float) -> Any:
        sc.ev("forget_pause", _dm(damper), cc, previous, minimum, proposed)
        if sc.chance(0.15):
            return None if previous is None else max(0.0, previous - 1.0)
        return previous

    def overload_from_charger(cid: str, cc: Any, decision: Any, damper: Any, previous: Any, minimum: float) -> Any:
        sc.ev("overload_from_charger", _d(decision), _dm(damper), previous, minimum)
        r = rng.random()
        if r < 0.5:
            return None
        if r < 0.7 and previous is not None:
            return previous
        return _near(rng, previous, deadband)

    def car_draws(cid: str, decision: Any) -> bool:
        value = sc.chance(0.4)
        sc.ev("car_draws", _d(decision), value)
        return value

    def battery_takes_the_fuse() -> bool:
        value = sc.chance(0.5)
        sc.ev("battery_takes_fuse", value)
        return value

    def phase_read_at(fresh: Any, phase: Any) -> float | None:
        sc.ev("phase_read_at", phase)
        return state["site_read_at"]

    def phases_read_at(fresh: Any) -> dict[str, float | None]:
        sc.ev("phases_read_at")
        return {phase: state["site_read_at"] for phase in PHASES}

    class _Guard:
        def guard(
            self,
            *,
            now: float,
            proposed_a: float,
            previous_a: float | None,
            requested_a: Any,
            margin_a: Any,
            charger_read_at: Any,
            site_read_at: Any,
            min_current_a: float,
        ) -> GuardResult:
            sc.ev(
                "guard",
                proposed_a,
                previous_a,
                requested_a,
                margin_a,
                charger_read_at is None,
                site_read_at,
                min_current_a,
            )
            mode = rng.choice(["none", "none", "same", "hold", "lower", "raise", "trim"])
            if mode == "none":
                return GuardResult(proposed_a)
            rule = rng.choice(_GUARD_RULES)
            if mode == "same":
                return GuardResult(proposed_a, rule)
            if mode == "hold":
                return GuardResult(previous_a if previous_a is not None else proposed_a, rule)
            if mode == "lower":
                return GuardResult(max(0.0, proposed_a - rng.choice([0.5, 1.0, 2.0])), rule)
            if mode == "raise":
                return GuardResult(proposed_a + rng.choice([0.5, 1.0, 2.0]), rule)
            base = previous_a if previous_a is not None else proposed_a
            return GuardResult((proposed_a + base) / 2.0, rule)

    guard = _Guard()

    def step_guard_for(cid: str) -> Any:
        sc.ev("step_guard_for", cid)
        return guard

    def consult(*, charger_entry_id: str, decision: Any, fresh: Any, assigned_a: Any) -> YieldVerdict | None:
        sc.ev("consult", charger_entry_id, _d(decision), fresh.tag, assigned_a)
        if sc.chance(0.1):
            return None
        base = decision.proposed_current_a if decision.proposed_current_a is not None else assigned_a
        if sc.exact and assigned_a is not None and sc.chance(0.4):
            return _verdict(sc, assigned_a)
        return _verdict(sc, _near(rng, base, deadband))

    def record_verdict(cid: str, verdict: Any) -> None:
        sc.ev("record_verdict", verdict)

    def log_transition(cid: str, verdict: Any, fresh: Any) -> None:
        sc.ev("log_transition", verdict, fresh.tag)

    def under_target(cid: str, decision: Any, verdict: Any, previous: Any) -> Any:
        sc.ev("under_target", _d(decision), verdict, previous)
        if verdict is not None and sc.chance(0.15):
            return _verdict(sc, previous)
        return verdict

    def lowering_held_by_battery(cid: str, cc: Any, decision: Any, fresh: Any, previous: Any, target: Any) -> bool:
        value = sc.chance(0.3)
        sc.ev("lowering_held_by_battery", cc, _d(decision), previous, target, value)
        return value

    def battery_holds_grid_at_limit(decision: Any, fresh: Any, cc: Any, cid: str, previous: Any) -> bool:
        value = sc.chance(0.4)
        sc.ev("battery_holds_grid", _d(decision), cc, previous, value)
        return value

    def restores_target_lowering(cid: str, proposed: Any, previous: Any) -> bool:
        value = sc.chance(0.2)
        sc.ev("restores_target", proposed, previous, value)
        return value

    def log_outcome(
        cid: str,
        decision: Any,
        *,
        outcome: str,
        detail: Any = None,
        detail_phase: Any = None,
        setpoint: Any = None,
        previous_setpoint: Any = None,
        yield_verdict: Any = None,
    ) -> None:
        sc.ev(
            "OUTCOME",
            cid,
            _d(decision),
            outcome,
            detail,
            detail_phase,
            setpoint,
            previous_setpoint,
            yield_verdict,
            _dm(state["damper"]),
        )

    def applyability(**kwargs: Any) -> Any:
        sc.ev("applyability", _d(kwargs["decision"]), *(kwargs[key] for key in sorted(kwargs) if key != "decision"))
        return state["failure"]

    def side_effect(name: str) -> None:
        """What another task could have changed while the resume or the probe was awaited."""
        r = rng.random()
        damper = controller._dampers[CID]
        if r < 0.1:
            charger.paused_by_balancing = not charger.paused_by_balancing
            sc.ev(name, "side:paused", charger.paused_by_balancing)
        elif r < 0.2:
            damper.record_write(_amps(rng))
            sc.ev(name, "side:written", _dm(damper))
        elif r < 0.25:
            controller.config[CONF_YIELD_STEPPING_ENABLED] = not controller.config[CONF_YIELD_STEPPING_ENABLED]
            sc.ev(name, "side:yield", controller.config[CONF_YIELD_STEPPING_ENABLED])
        elif r < 0.3:
            allowed.insert(0, False)
            sc.ev(name, "side:off")
        elif r < 0.35:
            controller._closed = True
            sc.ev(name, "side:closed")
        elif r < 0.42:
            wiring = controller.config[CONF_PHASE_WIRING][CID]
            wiring["min_current_a"] = rng.choice([5.0, 6.0, 8.0, 10.0])
            sc.ev(name, "side:minimum", wiring["min_current_a"])
        elif r < 0.48:
            # The step goes on with the damper it began with, whatever the site keeps for the charger by then.
            other = state["new_damper"]()
            other.record_write(_amps(rng))
            controller._dampers[CID] = other
            sc.ev(name, "side:damper", _dm(other))
        elif r < 0.53:
            state["margins"][CID] = rng.choice([None, -1.0, 0.0, 1.0, 3.0])
            sc.ev(name, "side:margin", state["margins"][CID])

    async def switch_only(cid: str, cc: Any, decision: Any, fresh: Any, max_age_s: float) -> None:
        sc.ev("AWAIT switch_only", cid, cc, _d(decision), fresh.tag, max_age_s)

    async def probe_pass(cid: str, cc: Any, decision: Any, fresh: Any, probe: Any) -> None:
        sc.ev("AWAIT probe_pass", cid, cc, _d(decision), fresh.tag, probe.tag)

    async def step_to_minimum(cid: str, cc: Any, decision: Any, lost: Any) -> None:
        sc.ev("AWAIT step_to_minimum", cid, cc, _d(decision), lost)

    async def resume(cid: str, cc: Any, decision: Any, fresh: Any, damper: Any, previous: Any) -> bool:
        value = sc.chance(state["resume_p"])
        sc.ev("AWAIT resume", cid, cc, _d(decision), fresh.tag, _dm(damper), previous, value)
        side_effect("resume")
        return value

    async def start_probe(cid: str, cc: Any, decision: Any, fresh: Any, damper: Any, previous: Any) -> bool:
        value = sc.chance(state["probe_p"])
        sc.ev("AWAIT start_probe", cid, cc, _d(decision), fresh.tag, _dm(damper), previous, value)
        side_effect("probe")
        return value

    async def magnitude_lowering(cid: str, cc: Any, decision: Any, fresh: Any, max_age_s: float) -> None:
        sc.ev("AWAIT magnitude_lowering", cid, cc, _d(decision), fresh.tag, max_age_s)

    async def magnitude_raise(cid: str, cc: Any, decision: Any, fresh: Any, max_age_s: float, margin: Any) -> None:
        sc.ev("AWAIT magnitude_raise", cid, cc, _d(decision), fresh.tag, max_age_s, margin)

    async def must_lower(
        cid: str,
        cc: Any,
        decision: Any,
        damper: Any,
        target_a: Any,
        previous: Any,
        margin: Any,
        verdict: Any,
        site_read_at: Any,
        *,
        written_a: Any,
        fresh: Any,
        phases_read_at: Any,
    ) -> bool:
        value = sc.chance(0.8)
        sc.ev(
            "AWAIT must_lower",
            cid,
            cc,
            _d(decision),
            _dm(damper),
            target_a,
            previous,
            margin,
            verdict,
            site_read_at,
            written_a,
            fresh.tag,
            sorted(phases_read_at.items()),
            value,
        )
        return value

    async def write_regulated(
        cid: str,
        cc: Any,
        decision: Any,
        damper: Any,
        damping: Any,
        setpoint: int,
        previous: Any,
        verdict: Any,
        *,
        site_read_at: Any = None,
    ) -> Any:
        sc.ev(
            "AWAIT write_regulated",
            cid,
            cc,
            _d(decision),
            _dm(damper),
            damping,
            type(setpoint).__name__,
            setpoint,
            previous,
            verdict,
            site_read_at,
        )
        return None

    stubs = {
        "_active_control_allowed": active_control_allowed,
        "_measurement_lost_due": measurement_lost_due,
        "_draws_above_minimum": draws_above_minimum,
        "_forget_pause_when_charging": forget_pause,
        "_overload_from_charger": overload_from_charger,
        "_car_draws": car_draws,
        "_battery_takes_the_fuse": battery_takes_the_fuse,
        "_phase_read_at": phase_read_at,
        "_phases_read_at": phases_read_at,
        "_step_guard_for": step_guard_for,
        "_consult_yield_stepper": consult,
        "_record_yield_stepping_verdict": record_verdict,
        "_log_yield_stepping_transition": log_transition,
        "_yield_verdict_under_target": under_target,
        "_lowering_held_by_battery": lowering_held_by_battery,
        "_battery_holds_grid_at_limit": battery_holds_grid_at_limit,
        "_restores_target_lowering": restores_target_lowering,
        "_log_active_control_outcome": log_outcome,
        "_async_apply_to_switch_only": switch_only,
        "_async_battery_probe_pass": probe_pass,
        "_async_step_to_minimum": step_to_minimum,
        "_async_maybe_resume_paused_charge": resume,
        "_async_maybe_start_battery_probe": start_probe,
        "_async_apply_magnitude_lowering": magnitude_lowering,
        "_async_apply_magnitude_raise": magnitude_raise,
        "_async_apply_must_lower": must_lower,
        "_async_write_regulated": write_regulated,
    }
    for name, stub in stubs.items():
        setattr(controller, name, stub)
    controller._test_applyability = applyability


def _decision(rng: random.Random, reason: str, proposed: float | None) -> RegulatorDecision:
    basis = {
        phase: PhaseDecisionBasis(
            signed_active_power_w=4646.0,
            signed_active_power_age_s=1.0,
            measured_current_a=5.9,
            measured_current_age_s=rng.choice([None, 1.0]),
            requested_current_a=rng.choice([None, 10.0, 16.0]),
            direction="net_import",
            confirmed_direction="net_import",
        )
        for phase in PHASES
    }
    return RegulatorDecision(
        proposed_current_a=proposed,
        reason=reason,
        limiting_phase=rng.choice(["L1", "L2", None]),
        basis=basis,
        overload=reason == "paused_safe_current_below_charger_minimum" and rng.random() < 0.7,
        pause_if_switched_pause_fails=rng.random() < 0.1,
    )


def _draw_state(sc: _Scenario) -> tuple[dict[str, Any], Any, RegulatorDecision, RegulatorDamper, float]:
    rng = sc.rng
    exact = sc.exact
    minimum = rng.choice([6.0, 6.0, 8.0])
    deadband = rng.choice([0.0, 1.0, 2.0])
    dwell = rng.choice([0.0, 60.0, 600.0])
    if exact:
        previous = rng.choice([None, minimum, minimum, minimum - 0.5, minimum + 0.5, 10.0, 16.0])
        if previous is None:
            proposed: float | None = rng.choice([minimum, minimum, minimum + 0.5, minimum - 0.5, 16.0])
        else:
            proposed = rng.choice(
                [
                    previous,
                    previous,
                    minimum,
                    previous + deadband,
                    previous - deadband,
                    previous + 0.5,
                    previous - 0.5,
                    previous + 1.5,
                ]
            )
        margin = rng.choice([None, 1.0, 1.0, 0.5, 1.5, -1.0])
    else:
        previous = None if rng.random() < 0.2 else _amps(rng)
        proposed = _near(rng, previous, deadband)
        margin = rng.choice([None, -6.0, -1.0, 0.0, 1.0, 1.5, 3.0])
    if rng.random() < 0.08:
        proposed = None
    reason = rng.choice(_REASONS)
    decision = _decision(rng, reason, proposed)
    kind = rng.choices(["none", "not_commandable", "switch_only", "commandable"], [1, 1, 2, 30])[0]
    charger: Any = None
    if kind != "none":
        charger = _Charger(
            commandable=kind == "commandable",
            switch_only=kind == "switch_only",
            paused=rng.random() < 0.3,
            settling=rng.choices([None, False, True], [2, 10, 1])[0],
        )

    def new_damper() -> RegulatorDamper:
        return RegulatorDamper(deadband_a=deadband, dwell_s=dwell, now=lambda: _T0 + timedelta(seconds=sc.clock))

    damper = new_damper()
    if previous is not None:
        damper.record_write(previous)
    if rng.random() < 0.3:
        # A candidate already pending for a while.
        damper._pending = _near(rng, previous, deadband)
        damper._pending_since = _T0 - timedelta(seconds=rng.choice([0.0, 30.0, 60.0, 600.0]))
    state = {
        "deadband": deadband,
        "allowed": [rng.random() > 0.05, rng.random() > 0.05],
        "lost": None if rng.random() < 0.85 else ApplyabilityFailure(rng.choice(_FAILURE_KINDS), "L2"),
        "draws_above_min": rng.random() < 0.5,
        "failure": None
        if rng.random() < 0.85
        else ApplyabilityFailure(rng.choice(_FAILURE_KINDS), rng.choice([None, "L1"])),
        "site_read_at": rng.choice([None, 1000.0, 1003.0]),
        "resume_p": rng.choice([0.0, 0.0, 0.3]),
        "probe_p": rng.choice([0.0, 0.0, 0.3]),
        "probe": rng.choices([None, False, True], [90, 4, 6])[0],
        "closed": rng.random() < 0.03,
        "yield": rng.random() < 0.6,
        "minimum": minimum,
        "margin": margin,
        "margins": {CID: margin, "other": 9.0},
        "damper": damper,
        "new_damper": new_damper,
    }
    sc.clock = rng.choice([0.0, 0.0, 59.0, 60.0, 600.0])
    return state, charger, decision, damper, margin


def _controller(state: dict[str, Any], damper: RegulatorDamper) -> SiteCapacityController:
    controller = SiteCapacityController.__new__(SiteCapacityController)
    controller.hass = SimpleNamespace()
    controller.entry_id = "site"
    controller.config = {
        CONF_ACTIVE_CONTROL_ENABLED: True,
        CONF_YIELD_STEPPING_ENABLED: state["yield"],
        CONF_PHASE_WIRING: {CID: {"phases": 3, "phase": None, "min_current_a": state["minimum"]}},
    }
    controller._dampers = {CID: damper}
    controller._battery_probes = (
        {} if state["probe"] is None else {CID: SimpleNamespace(probing=state["probe"], tag=f"probe{state['probe']}")}
    )
    controller._closed = state["closed"]
    return controller


async def _run(seed: int, exact: bool, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    rng = random.Random(seed * 2 + (1 if exact else 0))
    sc = _Scenario(rng, exact)
    state, charger, decision, damper, margin = _draw_state(sc)
    controller = _controller(state, damper)
    _install(controller, sc, charger, state)
    monkeypatch.setattr(scc, "controller_for", lambda hass, cid: charger)
    monkeypatch.setattr(scc, "applyability_failure", controller._test_applyability)
    monkeypatch.setattr(
        scc,
        "_LOGGER",
        SimpleNamespace(
            debug=lambda msg, *args, **kw: sc.ev("LOG debug", msg % args),
            info=lambda msg, *args, **kw: sc.ev("LOG info", msg % args),
            warning=lambda msg, *args, **kw: sc.ev("LOG warning", msg % args),
            exception=lambda msg, *args, **kw: sc.ev("LOG exception", msg % args),
        ),
    )
    fresh = SimpleNamespace(
        tag="fresh",
        phase_signed_active_power_w="spw",
        phase_signed_active_power_age_s="spa",
        phase_signed_active_power_reason="spr",
    )
    sc.ev("START", _d(decision), _dm(damper), state["margin"], charger)
    result = await controller._async_apply_to_charger(CID, decision, fresh, 30.0, state["margins"])
    sc.ev("RESULT", result, _dm(damper), _dm(controller._dampers[CID]), charger, sorted(controller._dampers))
    return sc.events


async def _digests(count: int, exact: bool, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], set[str]]:
    digests: list[str] = []
    reached: set[str] = set()
    for start in range(0, count, CHUNK):
        h = hashlib.sha256()
        for seed in range(start, start + CHUNK):
            for line in await _run(seed, exact, monkeypatch):
                if line.startswith(("AWAIT", "OUTCOME", "RESULT")):
                    reached.add(line.split("|")[0] + ("|" + line.split("|")[4] if line.startswith("OUTCOME") else ""))
                h.update(line.encode())
                h.update(b"\n")
        digests.append(h.hexdigest()[:16])
    return digests, reached


# Recorded from `_async_apply_to_charger` before it was split into a decide part and a write part.
EXPECTED = [
    "725f5add8635b90c",
    "69d2b04b6d7f9265",
    "7322f7c25b91c4a5",
    "3a34f3e484bfd115",
    "9e5faa3a4753af6d",
    "24bc2521a6a18a5d",
    "c969b98ae4822565",
    "2c4c7dca4867bdef",
    "a91cacd048ebde7a",
    "01508662d3417295",
    "e94cd58e240c0017",
    "4f469bdcd56f02ad",
    "52f2b06904aa80a7",
    "943011d16efdd3db",
    "930d77ef538c72e0",
    "7f174532295c2bd7",
    "a69d302bb7540a74",
    "71f2bead0c9c983c",
    "d35262d6225092f6",
    "747bcbb4f6ebea6c",
    "f7183e19aeb2fba3",
    "6563472a4802bccf",
    "af0341549287ba68",
    "45d7bf2c5f4ec8d2",
]
EXPECTED_EXACT = [
    "e718b864a889efce",
    "ce4cd2688cd36c53",
    "b9313fdeed289a78",
    "6c2b8bf651939624",
    "9af68a66eaa2fb2c",
    "1dc4dac317218a21",
    "f20b690c2904ad99",
    "110e8cd0c497cee9",
    "65480b99dce829be",
    "55eec3f5c3aaeac2",
    "05eecc5b230101e7",
    "24af91e8d733a4c1",
]


async def test_apply_to_charger_matches_recorded_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    digests, reached = await _digests(CASES, False, monkeypatch)
    assert digests == EXPECTED, digests
    # Every path is reached: each awaited command and each kind of held outcome.
    for path in (
        "AWAIT switch_only",
        "AWAIT probe_pass",
        "AWAIT step_to_minimum",
        "AWAIT resume",
        "AWAIT start_probe",
        "AWAIT magnitude_lowering",
        "AWAIT magnitude_raise",
        "AWAIT must_lower",
        "AWAIT write_regulated",
        "OUTCOME|charger_not_commandable",
        "OUTCOME|charger_not_loaded",
        "OUTCOME|phase_switch_settling",
        "OUTCOME|paused_by_balancing",
        "OUTCOME|held_battery_at_limit",
    ):
        assert path in reached, (path, sorted(reached))
    assert any(path.startswith("OUTCOME|yield_stepping_") for path in reached)
    assert any(path.startswith("OUTCOME|damping_") for path in reached)


async def test_apply_to_charger_exact_thresholds_match_recorded_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    digests, _ = await _digests(EXACT_CASES, True, monkeypatch)
    assert digests == EXPECTED_EXACT, digests
