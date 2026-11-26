"""What the executor aims a command at: the wanted state per role, the rate of a grid charge or a sale under the fuse
(`fixed_charge`, the live clamp, the charge per phase), the cover a change of action gives up, and a hold's limit
following the sun's surplus (`hold_surplus`)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from ..commands import SNAPSHOT, Native, Phases, fixed_rate_kw, native
from ..gate import MIN_RATE_KW
from ..plan_model import ACTION_EXPORT_DISCHARGE, ACTION_GRID_CHARGE, ACTION_HOLD
from .const import (
    HEADROOM_MARGIN_A,
    PHASE_RAISE_SETTLE_S,
    PHASE_STEP_KW,
    PHASE_TOLERANCE_KW,
    PHASE_TRACK_KW,
    PHASE_TRACK_S,
    RATE_STEP_KW,
    RATE_TOLERANCE_KW,
    RATE_UP_DWELL_S,
)
from .fuse import (
    _finite3,
    _kw,
    cover_a,
    cover_phase_a,
    grid_charge_cap_kw,
    hold_headroom_ok,
    house_net_kw,
    phase_charge_kw,
    uncovered_a,
)
from .helpers import _event, _writes_today
from .state import ExecInputs


class Rate:
    """The command's aim and rate. A mixin of `Executor`, on its state and control."""

    def _wanted(
        self, action: str, rate_kw: float, restore_values: Mapping[str, Native], phases: Phases | None = None, *,
        house_kw: float | None = None, behind: Phases | None = None,
    ) -> dict[str, Native]:
        wanted = self.control.wanted(
            action, rate_kw, self.watchdog_s, phases, house_kw=house_kw, behind_kw=behind,
            soc_pct=self._soc_pct, rated_kw=self._rated_kw, absent=self._absent, capacity_kwh=self._capacity_kwh,
        )
        out = {
            name: (restore_values[name] if value == SNAPSHOT else value)
            for name, value in wanted.items()
            if not (value == SNAPSHOT and name not in restore_values)
        }
        if self.state.priming:
            out |= {name: value for name, value in self.control.prime.items() if name in out}
        return out

    def _aim(self, inputs: ExecInputs, starting: bool) -> tuple[float, Phases | None, str | None]:
        """The rate to run the planned action at, the charge per phase when it runs so (the rate is then their sum),
        and why it cannot run."""
        state = self.state
        if not self._per_phase(inputs):
            if inputs.action in self.control.house_states and house_net_kw(inputs) is None:
                # The brand sets the grid's power: without the house's load now there is nothing to write.
                return 0.0, None, "house_unknown"
            # The rate commanded counts only while the same action runs: another action's rate (a sale's) is no
            # charge the battery runs, and a change into the charge is judged as its start.
            rate, why = self._rate(inputs, 0.0 if starting or state.action != inputs.action else state.rate_kw)
            return rate, None, why
        if self._grid_kind() and not _finite3(inputs.phase_behind_kw):
            return 0.0, None, "phase_load_unknown"
        changed = state.rate_changed_at
        yield_now = (
            starting or state.phase != "active" or changed is None
            or (inputs.now - changed).total_seconds() >= RATE_UP_DWELL_S
        )
        phases, why = phase_charge_kw(
            inputs, None if starting else state.phase_kw, yield_now=yield_now, grid=self._grid_kind()
        )
        if phases is None:
            return 0.0, None, why
        return round(sum(max(value, 0.0) for value in phases), 3), phases, None

    def _rate(self, inputs: ExecInputs, commanded_kw: float) -> tuple[float, str | None]:
        """The rate to run the planned action at, and why it cannot run."""
        facts = inputs.facts
        action = inputs.action
        rate = abs(inputs.rate_kw)
        covered_a = self._change_cover_a(inputs) if action == ACTION_GRID_CHARGE else 0.0
        if action == ACTION_GRID_CHARGE and self.control.fixed_charge:
            # The plant charges at its own rate: it must fit under the fuse cap as it is, never lowered.
            fixed = fixed_rate_kw(self.control, facts)
            if fixed is None or not math.isfinite(fixed):
                return 0.0, "size_unknown"
            cap = grid_charge_cap_kw(inputs, commanded_kw, yield_now=False, covered_a=covered_a)
            if cap is None:
                return 0.0, "headroom_unknown"
            if fixed > cap + RATE_TOLERANCE_KW:
                return 0.0, "fuse_headroom"
            return round(fixed, 3), None
        if action == ACTION_GRID_CHARGE:
            limit = facts.max_charge_kw
            if limit is not None:
                rate = min(rate, limit)
            changed = self.state.rate_changed_at
            yield_now = (
                self.state.phase != "active" or changed is None
                or (inputs.now - changed).total_seconds() >= RATE_UP_DWELL_S
            )
            cap = grid_charge_cap_kw(inputs, commanded_kw, yield_now=yield_now, covered_a=covered_a)
            if cap is None:
                return 0.0, "headroom_unknown"
            rate = min(rate, cap)
            if rate < MIN_RATE_KW:
                return 0.0, "fuse_headroom"
        elif action == ACTION_EXPORT_DISCHARGE:
            limit = facts.max_discharge_kw
            if limit is not None:
                rate = min(rate, limit)
            if rate < MIN_RATE_KW:
                return 0.0, "rate_too_small"
        return round(rate, 3), None

    def _change_cover_a(self, inputs: ExecInputs) -> float | Phases:
        """On a start of a grid charge, or a change into one from a running action, what the battery's discharge now
        gives the most loaded phase (`cover_a`; a sale's spread as the sale is): the headroom is measured under that
        cover, which ends with the charge. From idle the plant covers the house in its own mode, which follows the
        house's load on each phase: with the plant's power per phase read, each phase's own cover (`cover_phase_a`),
        else all of it on one phase, as for a hold from idle (`hold_headroom_ok`). With the battery idle or
        charging, the sun the plant puts on a phase above its average over the three is that phase's cover (read
        per phase only; without the reading the sun is taken to stay where it is). The first rate is the lower for it
        and the charge goes up once the cover has ended (`RATE_UP_DWELL_S`). 0 within a running charge (its own charge
        is counted back instead)."""
        state = self.state
        running = state.phase == "active"
        if running and state.action == ACTION_GRID_CHARGE:
            return 0.0
        if running and state.action == ACTION_EXPORT_DISCHARGE:
            return cover_a(inputs, sale=True)
        per_phase = cover_phase_a(inputs)
        return per_phase if per_phase is not None else cover_a(inputs)

    def _hold_charge_problem(self, inputs: ExecInputs) -> str | None:
        """Why a hold that may itself charge from the grid at the plant's fixed rate
        (`BrandControl.hold_charges_without`, a role this plant lacks) cannot start: that rate does not fit in the
        headroom left once the battery stops covering what it discharges now (`size_unknown`, `fuse_headroom`)."""
        if not any(name in self._absent for name in self.control.hold_charges_without):
            return None
        fixed = fixed_rate_kw(self.control, inputs.facts)
        if fixed is None or not math.isfinite(fixed):
            return "size_unknown"
        if inputs.headroom_a is None or inputs.volts_between_phases <= 0:
            return "headroom_unknown"
        power = inputs.facts.power_kw
        covered_a = -power * 1000.0 * math.sqrt(3.0) / inputs.volts_between_phases if power is not None and power < 0 else 0.0
        phases = min(max(inputs.charge_phases, 1), 3)
        room = _kw(inputs.headroom_a - covered_a - HEADROOM_MARGIN_A, inputs.volts_between_phases) * phases / 3
        return "fuse_headroom" if fixed > room + RATE_TOLERANCE_KW else None

    def _sale_headroom_ok(self, inputs: ExecInputs) -> bool:
        """A sale's start rule (from idle or as a change): the hold's, less the sale's own share on each phase at the
        rate it would run (`hold_headroom_ok`)."""
        rate, _why = self._rate(inputs, 0.0)
        return hold_headroom_ok(inputs, rate)

    @staticmethod
    def _sale_step_too_large(inputs: ExecInputs, rate: float) -> bool:
        """Whether a sale stepped down to `rate` fails the sale's start rule on a phase: the step takes back the cover
        the sale gives now beyond its new share (`uncovered_a`, from the running sale), as an end into a hold would,
        and never more off one phase than the step itself (what the battery discharges now less `rate`, all of it on
        one phase): the sun in the plant's power per phase, and a plant not known to spread its sale over three
        phases, are then judged by the step and not by the whole sale.

        Each phase is judged by the site meter's sign. One that exports at least what is taken back only exports
        less. Any other must keep `HEADROOM_MARGIN_A`: its headroom, plus what it exports now (the fuse is at least
        the two together), less what is taken back. With the meter's signs or a phase's headroom not known, the
        least headroom of any phase less what is taken back. Judged only for a step that may take at least
        `HEADROOM_MARGIN_A` off a phase: a smaller one is the fuse hand-back's (`RESTORE_HEADROOM_A`, the next
        look)."""
        power = inputs.facts.power_kw
        if power is None or not math.isfinite(power) or inputs.volts_between_phases <= 0:
            return False
        phase_v = inputs.volts_between_phases / math.sqrt(3.0)
        step_a = max(-power - rate, 0.0) * 1000.0 / phase_v
        taken_a = min(uncovered_a(inputs, rate, from_sale=True), step_a)
        if taken_a < HEADROOM_MARGIN_A:
            return False
        signed, rooms = inputs.grid_phase_kw, inputs.phase_headroom_a
        if not _finite3(signed) or not _finite3(rooms):
            return inputs.headroom_a is None or inputs.headroom_a - taken_a < HEADROOM_MARGIN_A
        assert signed is not None and rooms is not None
        for grid_kw, room in zip(signed, rooms, strict=True):
            export_a = max(-grid_kw, 0.0) * 1000.0 / phase_v
            if export_a < taken_a and room + export_a - taken_a < HEADROOM_MARGIN_A:
                return True
        return False

    def _grid_terms(
        self, inputs: ExecInputs, action: str, phases: Phases | None
    ) -> tuple[float | None, Phases | None]:
        """What a brand that sets the grid's power is given beside the rate: the house's net load now for an action
        of `BrandControl.house_states` (not for a charge per phase), and the load behind each phase's point for a
        `grid` target per phase. `None` each for a brand that sets the battery's."""
        if phases is not None:
            return None, self._behind(inputs)
        return (house_net_kw(inputs) if action in self.control.house_states else None), None

    def _follow_surplus(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A running hold that follows a sun surplus (`BrandControl.hold_surplus`): its charge limit set to the
        surplus now, past the deadband (see the module)."""
        follow = self.control.hold_surplus
        state = self.state
        if (
            follow is None or state.action != ACTION_HOLD or inputs.action != ACTION_HOLD
            or state.covered_at is not None or state.priming
            # Only once the hold's own writes are in: its limit at 0 first, then Remote EMS and the mode.
            or state.pending is not None or self._next_role(state, inputs) is not None
        ):
            return []
        standing = state.target.get(follow.role)
        if not isinstance(standing, int | float) or isinstance(standing, bool):
            return []
        now = inputs.now
        net = house_net_kw(inputs) if inputs.measurement_ok else None
        surplus = 0.0 if net is None else max(-net, 0.0)
        rated = inputs.facts.max_charge_kw
        if rated is not None and math.isfinite(rated):
            surplus = min(surplus, rated)
        surplus = min(surplus, max(inputs.hold_charge_cap_kw, 0.0))
        new = math.floor(surplus / follow.step_kw + 1e-9) * follow.step_kw
        if new < follow.deadband_kw or _writes_today(state, inputs) >= self._daily_cap - self._charge_reserve:
            # Too little to follow, or the day's writes are nearly spent: the limit back at 0 and kept there.
            new = 0.0
        new = native(self.control.role(follow.role), new)
        if new == standing or (new != 0.0 and abs(new - standing) < follow.deadband_kw):
            return []
        raised = self._surplus_raised_at
        if new > standing and raised is not None and (now - raised).total_seconds() < follow.raise_gap_s:
            return []
        if new > standing:
            self._surplus_raised_at = now
        self.state = replace(
            state, target={**state.target, follow.role: new},
            sent=tuple(name for name in state.sent if name != follow.role),
        )
        return [_event(now, "surplus", ACTION_HOLD, role=follow.role, kw=new, was_kw=standing)]

    def _retarget_phases(self, inputs: ExecInputs, rate: float, phases: Phases) -> list[dict[str, Any]]:
        """A running charge per phase: a phase whose room fell under its target goes down at once (every phase then
        takes its new value, each within its own room); the charge goes up after `RATE_UP_DWELL_S`, by at least
        `RATE_STEP_KW` in all or back to what the caps allow with the house quiet. The verification's probe is
        written as it changes, a charge in it raised only after the dwell too."""
        state = self.state
        now = inputs.now
        old = state.phase_kw
        assert old is not None
        since = (now - (state.rate_changed_at or now)).total_seconds()
        behind, old_behind = self._behind(inputs), state.phase_behind_kw
        # A `grid` target follows the load behind its point: written again once a target would move enough.
        moved = (
            behind is not None and old_behind is not None and since >= PHASE_TRACK_S
            and any(abs(new - was) > PHASE_TRACK_KW for new, was in zip(behind, old_behind, strict=True))
        )
        if inputs.phase_probe_kw is not None:
            # A charge in it goes up again, as any charge per phase, only `RATE_UP_DWELL_S` after its room took it
            # down (the last change lowered a charge on that phase); and from a discharge only through 0, so that
            # its room is read without the discharge that eased the phase.
            lowered = state.phase_was_kw
            held = []
            for index, (new, was) in enumerate(zip(phases, old, strict=True)):
                if new > 0 and was < 0:
                    new = 0.0
                elif (
                    new > max(was, 0.0) and since < RATE_UP_DWELL_S and lowered is not None
                    and lowered[index] > max(was, 0.0) + 1e-9
                ):
                    new = max(was, 0.0)
                held.append(new)
            phases = (held[0], held[1], held[2])
            raised = state.phase_raised_at
            if raised is not None and (now - raised).total_seconds() < PHASE_RAISE_SETTLE_S:
                # Just raised: the plant's own power does not show it yet, so the room reads too small. A charge
                # the probe itself lowers (the verification's next step) still goes down at once.
                asked = inputs.phase_probe_kw
                kept = [was if 0 <= new < was and new < want - 1e-9 else new
                        for new, was, want in zip(phases, old, asked, strict=True)]
                phases = (kept[0], kept[1], kept[2])
            rate = round(sum(max(value, 0.0) for value in phases), 3)
            change = any(abs(new - was) > 1e-9 for new, was in zip(phases, old, strict=True))
        else:
            gain = sum(phases) - sum(old)
            ceiling, _why = phase_charge_kw(inputs, None, live=False, grid=self._grid_kind())
            full = ceiling is not None and all(
                new >= most - PHASE_STEP_KW - 1e-9 for new, most in zip(phases, ceiling, strict=True)
            )
            down = any(new < was - PHASE_TOLERANCE_KW for new, was in zip(phases, old, strict=True))
            raised = state.phase_raised_at
            if down and raised is not None and (now - raised).total_seconds() < PHASE_RAISE_SETTLE_S:
                # Just raised: the plant's own power does not show it yet, so the room reads too small.
                down = False
            up = since >= RATE_UP_DWELL_S and (gain >= RATE_STEP_KW or (full and gain > PHASE_TOLERANCE_KW))
            change = down or up
        if not change and not moved:
            return []
        if not change:
            # Only the load moved: the charge per phase stays as commanded (never above what the phase allows with
            # the load as it is now), and only the targets whose load moved follow it, so a busy house spends the
            # day's setpoint writes on the phases that need them.
            phases = (min(old[0], phases[0]), min(old[1], phases[1]), min(old[2], phases[2]))
            rate = round(sum(max(value, 0.0) for value in phases), 3)
            assert behind is not None and old_behind is not None
            kept = [new if abs(new - was) > PHASE_TRACK_KW else was for new, was in zip(behind, old_behind, strict=True)]
            behind = (kept[0], kept[1], kept[2])
        spec = self.control.phase_control
        names = () if spec is None else spec.names()
        # The falling phases are written first: no phase is raised while another still draws what it must give up.
        if inputs.phase_probe_kw is not None:
            # The verification's probe is a discharge: the phase that ends is returned to 0 and read back before
            # the next one's is written, so never two phases discharge at once.
            falling = tuple(name for name, new, was in zip(names, phases, old, strict=False) if abs(new) < abs(was))
        else:
            aims, was_aims = self._phase_aims(phases, behind), self._phase_aims(old, old_behind)
            falling = tuple(name for name, new, was in zip(names, aims, was_aims, strict=False) if new < was)
        rising = any(new > was for new, was in zip(phases, old, strict=True))
        self.state = replace(
            state, rate_kw=rate, phase_kw=phases, sent=(), rate_changed_at=now, phase_bad=(), phase_judged_at=None,
            target=self._wanted(ACTION_GRID_CHARGE, rate, state.restore_values, phases, behind=behind), lead=falling,
            phase_was_kw=old, phase_raised_at=now if rising else state.phase_raised_at,
            phase_behind_kw=behind, phase_was_behind_kw=old_behind,
        )
        return [_event(now, "rate", ACTION_GRID_CHARGE, rate_kw=rate, was_kw=state.rate_kw, phase_kw=list(phases),
                       was_phase_kw=list(old))]
