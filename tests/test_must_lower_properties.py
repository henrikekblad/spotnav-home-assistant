"""Property: an import overload's lowering is never held by a gate that is not about safety.

For random states of every such gate (a membership conflict, a slow meter, a resume or battery probe about to
start, the step guard, the yield stepper's verdict, the band of a battery at the limit, the damper's deadband,
dwell and margin, a battery filling the fuse), one apply pass on a `reducing_current_due_to_active_import_overload`
decision ends, whenever a lowering is due (nothing written yet, or the proposal below what was written), in exactly
one must-lower write of a lower current: the charger's own write path stops the charge when it cannot take that in
time (`ChargingController.async_apply_regulated_current`). When no lowering is due, nothing higher is written.

What still may hold it is about safety and is not drawn here: the fresh readings re-checked (`applyability_failure`,
taken as passing), a battery probe that is running, and a battery whose yield is verified on the site below the
yield ceiling (the stepper's own judgement; `tests/test_battery_fuse.py`).

The gate states are drawn by Hypothesis (reproducibly) and replayed through one site, as `test_core_properties.py`
does for the ownership core; a failure names the state it failed on.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, HealthCheck, settings, strategies as st
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_YIELD_STEPPING_ENABLED
from custom_components.spotnav.execution.controller import REGULATED_STOPPED, REGULATED_WROTE, RegulatedWrite
from custom_components.spotnav.execution.yield_stepping import YieldVerdict
from custom_components.spotnav.site import site_capacity_controller as scc
from custom_components.spotnav.site.regulator_damping import RegulatorDamper
from custom_components.spotnav.site.step_guard import GuardResult

from .test_yield_stepping_wiring import _decision, _yield_setup
from .world import controller_of

_OVERLOAD = "reducing_current_due_to_active_import_overload"
_YIELD_REASONS = (
    "settling_hold",
    "confirmed_after_settling",
    "held_at_yield_setpoint",
    "held_at_floor_raw_pause",
    "held_while_verifying_down_step",
    "held_at_limit_battery_absorbs",
    "held_for_battery_probe",
    "held_battery_probe_in_flight",
    "probe_step",
    "confirmed_step",
    "revert_not_absorbed",
    "transient_step_back",
    "passthrough_raw_reduce",
    "passthrough_raw_holds",
    "passthrough_ceiling",
    "passthrough_no_basis",
)

GATES = st.fixed_dictionaries(
    {
        "previous": st.one_of(st.none(), st.integers(6, 32)),
        "proposed": st.integers(0, 32),
        "conflict": st.booleans(),
        "slow_meter": st.booleans(),
        "resume_claims": st.booleans(),
        "probe_claims": st.booleans(),
        "guard": st.sampled_from(["none", "hold", "trim", "raise"]),
        "yield": st.one_of(
            st.none(),
            st.tuples(
                st.sampled_from(["hold", "write", "passthrough"]),
                st.sampled_from(_YIELD_REASONS),
                st.integers(0, 40),
                st.booleans(),
            ),
        ),
        "battery_at_limit": st.booleans(),
        "battery_takes_fuse": st.booleans(),
        "margin": st.one_of(st.none(), st.floats(-6.0, 3.0)),
        "deadband": st.sampled_from([0.0, 2.0, 5.0]),
        "dwell": st.sampled_from([0.0, 60.0, 600.0]),
        "stop_instead": st.booleans(),
    }
)


def _gate_states(count: int) -> list[dict[str, Any]]:
    drawn: list[dict[str, Any]] = []

    @settings(
        max_examples=count,
        derandomize=True,
        database=None,
        deadline=None,
        suppress_health_check=list(HealthCheck),
    )
    @given(GATES)
    def collect(state: dict[str, Any]) -> None:
        drawn.append(state)

    collect()
    return drawn


class _Guard:
    def __init__(self, state: dict[str, Any]) -> None:
        self.mode = state["guard"]

    def guard(self, *, proposed_a: float, previous_a: float | None, **_: Any) -> GuardResult:
        if previous_a is None or self.mode == "none":
            return GuardResult(proposed_a)
        if self.mode == "hold":
            return GuardResult(previous_a, "held_same_reading")
        if self.mode == "trim":
            return GuardResult((proposed_a + previous_a) / 2.0, "trimmed_overshoot")
        return GuardResult(previous_a + 2.0, "up_margin")

    def note_write(self, **_: Any) -> None:
        return None


def _verdict(state: dict[str, Any]) -> YieldVerdict | None:
    if state["yield"] is None:
        return None
    action, reason, current, urgent = state["yield"]
    return YieldVerdict(
        action=action,
        current_a=float(current) if action == "write" else None,
        reason=reason,
        urgent=urgent,
        state="unknown",
        y=None,
        y_age_s=None,
        reference_a={},
    )


# Hypothesis's own PRNG bookkeeping warns when it is drawn from inside a running event loop's test; nothing of ours.
@pytest.mark.filterwarnings("ignore::hypothesis.errors.HypothesisWarning")
async def test_an_import_overload_always_ends_in_a_lower_write_or_a_safety_stop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_entry, charger, _calls = await _yield_setup(hass, entry_id="mustlower")
    controller = controller_of(hass, site_entry.entry_id)
    charger_controller = controller_of(hass, charger.entry_id)
    cid = charger.entry_id
    for cancel in controller._controller_listener_cancels:
        cancel()
    controller._controller_listener_cancels.clear()

    current: dict[str, Any] = {}
    writes: list[tuple[int, bool]] = []

    monkeypatch.setattr(controller, "config", dict(controller.config))
    monkeypatch.setattr(scc, "applyability_failure", lambda **_kwargs: None)
    monkeypatch.setattr(type(controller), "membership_conflicts", property(lambda self: ["x"] if current["conflict"] else []))
    monkeypatch.setattr(
        type(controller),
        "load_balancing_slow_meters",
        property(lambda self: {"sensor.slow": 420.0} if current["slow_meter"] else {}),
    )
    monkeypatch.setattr(controller, "_most_restrictive_margin_by_charger", lambda _fresh: {cid: current["margin"]})
    monkeypatch.setattr(controller, "_step_guard_for", lambda _cid: _Guard(current))
    monkeypatch.setattr(controller, "_consult_yield_stepper", lambda **_kwargs: _verdict(current))
    monkeypatch.setattr(controller, "_battery_holds_grid_at_limit", lambda *_args: current["battery_at_limit"])
    monkeypatch.setattr(controller, "_battery_takes_the_fuse", lambda: current["battery_takes_fuse"])
    monkeypatch.setattr(controller, "_car_draws", lambda *_args: False)

    async def resume(*_args: Any) -> bool:
        return current["resume_claims"]

    async def probe(*_args: Any) -> bool:
        return current["probe_claims"]

    async def write(amps: int, *, must_lower: bool) -> RegulatedWrite:
        writes.append((amps, must_lower))
        if current["stop_instead"]:
            return RegulatedWrite(REGULATED_STOPPED, "safety_stop", False)
        return RegulatedWrite(REGULATED_WROTE, "ok", True)

    monkeypatch.setattr(controller, "_async_maybe_resume_paused_charge", resume)
    monkeypatch.setattr(controller, "_async_maybe_start_battery_probe", probe)
    monkeypatch.setattr(charger_controller, "async_apply_regulated_current", write)

    states = _gate_states(400)
    for state in states:
        current.clear()
        current.update(state)
        writes.clear()
        # No verdict drawn: yield stepping is off on the site.
        controller.config[CONF_YIELD_STEPPING_ENABLED] = state["yield"] is not None
        damper = RegulatorDamper(deadband_a=state["deadband"], dwell_s=state["dwell"])
        if state["previous"] is not None:
            damper.record_write(float(state["previous"]))
        controller._dampers[cid] = damper
        controller._written_at.clear()
        controller._regulated_writes.clear()
        controller._battery_probes.clear()
        controller.regulator_decisions = {
            cid: _decision(proposed_a=float(state["proposed"]), requested_a=16.0, reason=_OVERLOAD)
        }

        await controller._async_apply_active_control()

        previous = state["previous"]
        if previous is None or state["proposed"] < previous:
            assert len(writes) == 1, state
            setpoint, must_lower = writes[0]
            assert must_lower, state
            if previous is None:
                assert setpoint <= state["proposed"], state
            else:
                assert setpoint < previous, state
        else:
            assert all(previous is not None and setpoint <= previous for setpoint, _ in writes), state
