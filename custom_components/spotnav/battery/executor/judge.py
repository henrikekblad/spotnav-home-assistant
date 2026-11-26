"""What the running command does, judged on the plant: the monitor's watch of a hold, a charge and a discharge, the
export limit on the grid's power, and a charge per phase on its phases (a runaway, a phase that does not follow)."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from ..commands import Phases
from ..monitor import (
    CHARGE_TAPER_SOC_PCT,
    CONFIRM_READINGS,
    PHASE_GAP_S,
    PHASE_GRACE_S,
    PHASE_OVER_GAP_S,
    PHASE_OVER_GRACE_S,
    Capability,
    Command,
    Reading,
    judge_phases,
)
from ..plan_model import ACTION_DISCHARGE_HOUSE, ACTION_GRID_CHARGE, ACTION_HOLD
from .const import FIXED_CHARGE_JUDGE_KW, _MONITORED
from .fuse import _finite3, house_net_kw
from .helpers import _event
from .state import ExecInputs


class Judge:
    """The judging of a command's effect. A mixin of `Executor`, on its state, control and monitor."""

    def _judge_effect(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """What the running command does, judged on the plant: the export limit on the grid's power, a charge per
        phase on its phases, anything else by the monitor."""
        now = inputs.now
        events: list[dict[str, Any]] = []
        if self.state.phase == "active" and self._export:
            # The export limit is judged on the site's grid power: the export must stop.
            grid = inputs.grid_import_kw
            fresh = inputs.measurement_ok and grid is not None and math.isfinite(grid)
            judged = self.monitor.observe(
                Reading(grid, 0.0 if fresh else None, excused_kw=inputs.excused_import_kw), Capability(), now
            )
            if judged is not None and judged.kind == "failure":
                events.append(_event(now, "monitor", judged.outcome, detail=judged.as_dict()))
                events += self._begin_restore(now, f"not_obeyed:{self.state.action}:{judged.outcome}")
        elif self.state.phase == "active" and self.state.phase_kw is not None:
            events += self._judge_phases(inputs)
        elif self.state.phase == "active" and not self._probing(inputs):
            # The expiry test judges the effect itself: a fallback is what it looks for, not a command not obeyed.
            facts = inputs.facts
            capability = Capability(
                rated_charge_kw=facts.max_charge_kw,
                rated_discharge_kw=facts.max_discharge_kw,
                soc_min_pct=facts.reserve_soc_pct or 0.0,
                site_cap_kw=(
                    inputs.house_deficit_kw if self.state.action == ACTION_DISCHARGE_HOUSE else self._house_cap(inputs)
                ),
            )
            # A hold is judged on the grid's power too; a grid charge only shown to act with it (`monitor.proves`).
            grid = (
                inputs.grid_import_kw
                if self.state.action in (ACTION_HOLD, ACTION_GRID_CHARGE) and inputs.measurement_ok else None
            )
            judged = self.monitor.observe(
                Reading(facts.power_kw, inputs.battery_age_s, facts.soc_pct, grid), capability, now
            )
            if judged is not None and judged.kind == "proved":
                # The command was seen to act (`monitor.proves`): the shell may count it for the plant's report.
                events.append(_event(
                    now, "proved", self.state.action or "", variant=self.control.variant, detail=judged.as_dict()
                ))
            if judged is not None and judged.kind == "failure":
                events.append(_event(now, "monitor", judged.outcome, detail=judged.as_dict()))
                if judged.outcome == "no_effect":
                    events += self._begin_restore(now, f"no_effect:{self.state.action}")
        return events

    def _grid_kind(self) -> bool:
        """Whether the control's targets per phase are the grid's (`PhaseControl.target_kind`)."""
        spec = self.control.phase_control
        return spec is not None and spec.target_kind == "grid"

    def _phase_measured(self, inputs: ExecInputs) -> Phases | None:
        """What a phase taking more than its target is judged on, as power put out on each phase (kW): the plant's
        own power, or for a recipe whose targets are the grid's the power at the target's point (negated: + from
        the grid is a draw), the site meter's phase where the recipe has no sensor at the point."""
        if self._grid_kind():
            point = inputs.phase_point_kw if _finite3(inputs.phase_point_kw) else inputs.grid_phase_kw
            return (-point[0], -point[1], -point[2]) if _finite3(point) and point is not None else None
        return inputs.plant_phase_kw if _finite3(inputs.plant_phase_kw) else None

    def _phase_aims(self, phases: Phases, behind: Phases | None) -> Phases:
        """What each phase may take where `_phase_measured` reads it (kW): its charge, or for a `grid` target the
        power at the point, the load behind it plus the charge."""
        if not self._grid_kind() or behind is None:
            return phases
        return (phases[0] + behind[0], phases[1] + behind[1], phases[2] + behind[2])

    def _behind(self, inputs: ExecInputs) -> Phases | None:
        """The load behind each phase's point now (kW), for a `grid` target; `None` for a plant target."""
        if not self._grid_kind():
            return None
        behind = inputs.phase_behind_kw
        assert behind is not None and _finite3(behind)  # `_aim` refuses a charge per phase without it
        return (round(behind[0], 2), round(behind[1], 2), round(behind[2], 2))

    def _house_cap(self, inputs: ExecInputs) -> float | None:
        """For an action run by the grid's power (`BrandControl.house_states`): what the battery can take or give of
        its rate now that the house moved since the values were written (the setpoint is not moved with it), for the
        monitor; `None` for any other action or when the house is not known."""
        state = self.state
        if state.action not in self.control.house_states or state.house_kw is None or state.phase_kw is not None:
            return None
        now_kw = house_net_kw(inputs)
        if now_kw is None:
            return None
        moved = now_kw - state.house_kw
        return state.rate_kw - moved if state.action == ACTION_GRID_CHARGE else state.rate_kw + moved

    def _judge_phases(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A running charge per phase against what each phase takes (`monitor.judge_phases`, `_phase_measured`).

        **Over its target** (the runaway): from `PHASE_OVER_GRACE_S` after the charge's first writes were taken,
        every `PHASE_OVER_GAP_S`, each phase against the larger of its target and the one before the last change
        while that change settles (`PHASE_GRACE_S`); a change of the targets does not start the count again.
        **Taking nothing or too little**: after `PHASE_GRACE_S` since the last change, every `PHASE_GAP_S`, not near
        full. `CONFIRM_READINGS` in a row hand the charge back, blocked. Nothing is judged while a write is awaited
        or without the reading; for the verification's probe only the runaway (the rest it judges itself), and in the
        expiry test's silence nothing (`_probing`: a passthru is no charge; the test ends on a real one itself)."""
        state = self.state
        now = inputs.now
        measured = self._phase_measured(inputs)
        changed = state.rate_changed_at
        phases = state.phase_kw
        entered = state.entered_at
        if (
            phases is None or state.priming or state.pending is not None
            or self._next_role(state, inputs) is not None or measured is None or changed is None or entered is None
        ):
            if state.phase_bad:
                self.state = replace(state, phase_bad=())
            return []
        settling = (now - changed).total_seconds() < PHASE_GRACE_S
        if not self._probing(inputs) and (now - entered).total_seconds() >= PHASE_OVER_GRACE_S and (
            state.phase_over_at is None or (now - state.phase_over_at).total_seconds() >= PHASE_OVER_GAP_S
        ):
            aims = self._phase_aims(phases, state.phase_behind_kw)
            was = aims
            if settling and state.phase_was_kw is not None:
                was = self._phase_aims(state.phase_was_kw, state.phase_was_behind_kw or state.phase_behind_kw)
            most = (max(aims[0], was[0]), max(aims[1], was[1]), max(aims[2], was[2]))
            over = judge_phases(most, measured, tapering=True) == "over"
            count = state.phase_over + 1 if over else 0
            self.state = state = replace(state, phase_over=count, phase_over_at=now)
            if count >= CONFIRM_READINGS:
                self.state = replace(state, phase_over=0)
                return [
                    _event(now, "monitor", "phase_over", targets_kw=list(most),
                           plant_kw=[round(value, 3) for value in measured]),
                    *self._begin_restore(now, f"not_obeyed:{ACTION_GRID_CHARGE}:phase_over"),
                ]
        if settling or inputs.phase_probe_kw is not None:
            if state.phase_bad:
                self.state = replace(state, phase_bad=())
            return []
        if state.phase_judged_at is not None and (now - state.phase_judged_at).total_seconds() < PHASE_GAP_S:
            return []
        soc = inputs.facts.soc_pct
        if self._grid_kind():
            # Whether the battery takes its charge is the plant's own power (the load behind the point less the
            # point's), not the point's: a point that only feeds its load reads its target without charging.
            if not _finite3(inputs.plant_phase_kw):
                return []
            assert inputs.plant_phase_kw is not None
            measured = inputs.plant_phase_kw
        verdict = judge_phases(phases, measured, tapering=soc is None or soc >= CHARGE_TAPER_SOC_PCT)
        if verdict in ("ok", "over"):
            # Over is the count above's.
            self.state = replace(state, phase_bad=(), phase_judged_at=now)
            return []
        bad = (*state.phase_bad, verdict)
        if len(bad) < CONFIRM_READINGS:
            self.state = replace(state, phase_bad=bad, phase_judged_at=now)
            return []
        self.state = replace(state, phase_bad=(), phase_judged_at=now)
        outcome = bad[0] if len(set(bad)) == 1 else "partial"
        events = [_event(now, "monitor", f"phase_{outcome}", targets_kw=list(phases),
                         plant_kw=[round(value, 3) for value in measured])]
        if outcome == "no_effect":
            events += self._begin_restore(now, f"no_effect:{ACTION_GRID_CHARGE}")
        return events

    def _watch(self, inputs: ExecInputs, action: str, rate: float) -> None:
        """The monitor watches the new command; a discharge for the house only when the house's deficit is known."""
        kind = _MONITORED.get(action)
        if (
            kind is None or (action == ACTION_DISCHARGE_HOUSE and inputs.house_deficit_kw is None)
            # A charge per phase is judged per phase, on the plant's own power (`_judge_phases`).
            or self.state.phase_kw is not None
        ):
            self.monitor.stop(inputs.now)
            return
        if action == ACTION_DISCHARGE_HOUSE:
            rate = max(inputs.house_deficit_kw or 0.0, 0.0)
        elif action == ACTION_GRID_CHARGE and self.control.fixed_charge:
            rate = FIXED_CHARGE_JUDGE_KW
        before = inputs.grid_import_kw if kind == "curtail" else inputs.facts.power_kw
        idle = kind == "hold" and self.control.hold_idle()
        self.monitor.start(Command(kind, inputs.now, rate, before_kw=before, written=True, idle=idle))
