"""The executor's start and exit rules, in the order they are asked: why an action does not start (`requires`,
`action_requires`, `watched`, the fuse, stale data, the day's caps, `rearm_s`), why a running one is handed back,
and the settings a person must change first."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..commands import TOLD_ACTION_REASONS, forbidden_read, reading
from ..ledger import same
from ..plan_model import ACTION_EXPORT_DISCHARGE, ACTION_GRID_CHARGE, ACTION_HOLD, ACTION_SELF_CONSUME
from .const import (
    CHARGE_OVER_GRACE_S,
    CHARGE_OVER_KW,
    CHARGE_OVER_S,
    HEADROOM_MARGIN_A,
    HOLD_CHARGE_KW,
    HOLD_CHARGE_S,
    MANAGEMENT_GRACE_S,
    PEAK_COVER_A,
    RATE_TOLERANCE_KW,
    REARM_S,
    RESTORE_HEADROOM_A,
    SETPOINT_SELLING,
    SILENCE_GRACE_S,
    _FUSE_HANDED_BACK,
    _RAISES_IMPORT,
)
from .fuse import _fresh, _import_headroom_a, grid_charge_cap_kw, hold_headroom_ok
from .helpers import _event, _writes_today
from .management import management_problem
from .state import ExecInputs, RoleRead
from .values import _tolerance


class Rules:
    """The start and exit rules. A mixin of `Executor`, on its state and control."""

    def _action_problem(self, inputs: ExecInputs, action: str) -> str | None:
        """Why the brand cannot run this action now (an AC charge policy that forbids grid charging)."""
        if not self.control.supports(action):
            return f"action_not_supported:{action}"
        for act, name, forbidden, why in self.control.action_requires:
            if act == action and forbidden_read(self.person_state(name, inputs.reads.get(name)), forbidden):
                return why
        return None

    def person_state(self, name: str, read: RoleRead | None) -> str | None:
        """A role's state as the person set it: the value found at enter while SpotNav's own write stands in it (a
        Victron hold writes the discharge power 0, which must not read as the person's "never discharge"), else
        the state now."""
        state = self.state
        if state.phase != "idle" and name in state.written and name in state.snapshot:
            return state.snapshot[name]
        return None if read is None else read.state

    def told_problems(self, reads: Mapping[str, RoleRead]) -> tuple[str, ...]:
        """The person's own settings that keep an action from ever running (`commands.TOLD_ACTION_REASONS`), and a
        sale left standing in a setpoint SpotNav holds no snapshot of (`SETPOINT_SELLING`), for the status."""
        found: list[str] = []
        for _act, name, forbidden, why in self.control.action_requires:
            read = reads.get(name)
            if why not in TOLD_ACTION_REASONS or why in found or read is None or not read.found:
                continue
            state = self.person_state(name, read)
            if state is not None and reading(self.control.role(name), state) is not None and forbidden_read(
                state, forbidden
            ):
                found.append(why)
        for role in self.control.roles:
            # A sale left standing (`Role.restored_below`: Victron's grid setpoint far below 0) that SpotNav holds no
            # snapshot of: a hold or a discharge for the house does not write it, so it sells on. Told, never written.
            read = reads.get(role.name)
            if role.restored_below is None or read is None or not read.found or role.name in self.state.snapshot:
                continue
            text = reading(role, read.state)
            try:
                value = None if text is None else float(text)
            except ValueError:
                value = None
            if value is not None and math.isfinite(value) and value < role.restored_below[0]:
                found.append(SETPOINT_SELLING)
        return tuple(found)

    def _exit_reason(self, inputs: ExecInputs) -> str | None:
        facts = inputs.facts
        action = inputs.action
        if inputs.stopping is not None:
            return inputs.stopping
        if action is None:
            return "no_plan_now"
        if action == ACTION_SELF_CONSUME:
            return "curtail_ended" if self._export else "plan_self_consume"
        if facts.brand != self.control.brand:
            return "battery_changed"
        problem = self._action_problem(inputs, action)
        if problem is not None:
            return problem
        if (
            action == ACTION_GRID_CHARGE and self.state.action == ACTION_GRID_CHARGE
            and (self.state.phase_kw is not None) != self._per_phase(inputs)
        ):
            # Between a charge per phase and the brand's own: handed back first, never changed while it runs.
            return "per_phase_changed"
        if facts.on_grid is False:
            return "off_grid"
        if not inputs.measurement_ok:
            return "stale_data"
        # The fuse before the battery's own reading: the headroom is the site meter's, so a plant that is silent or
        # stale is still handed back for the fuse, every role at once (`_fuse_hand_back`), never a role a look.
        if action in _FUSE_HANDED_BACK or self.state.action in _FUSE_HANDED_BACK:
            headroom = (
                inputs.headroom_a if action in _RAISES_IMPORT or self.state.action in _RAISES_IMPORT
                else _import_headroom_a(inputs)
            )
            if headroom is not None and headroom < RESTORE_HEADROOM_A:
                return "fuse_headroom"
        if self._export or _fresh(inputs):
            self.state = replace(self.state, silent_since=None)
        elif not self._ride_out(inputs):
            return "stale_data"
        if inputs.headroom_a is None and not self._export:
            return "headroom_unknown"
        if self._hold_charges(inputs):
            return f"not_obeyed:{ACTION_HOLD}:charging"
        if self._charge_over(inputs):
            return f"not_obeyed:{ACTION_GRID_CHARGE}:over"
        # The battery's own instance runs the Remote EMS it turned on; the export instance only while the battery's
        # runs it (`sibling_active`): one taken by another controller (the app, a VPP) ends the curtail.
        management = management_problem(
            facts, self.control, active=inputs.sibling_active if self._export else True, takeover=inputs.takeover
        )
        if management == "unknown":
            since = self.state.management_unknown_since or inputs.now
            self.state = replace(self.state, management_unknown_since=since)
            grace = MANAGEMENT_GRACE_S if self.state.silent_since is None else SILENCE_GRACE_S
            if (inputs.now - since).total_seconds() >= grace:
                # No evidence of another owner: handed back as stale data is, the next start re-arms.
                return "management_unknown"
        elif management is not None:
            return f"management_changed:{management}"
        else:
            self.state = replace(self.state, management_unknown_since=None)
        if _writes_today(self.state, inputs) >= self._daily_cap:
            return "daily_write_cap"
        return self._charge_budget(inputs)

    def _ride_out(self, inputs: ExecInputs) -> bool:
        """Whether a running action may ride out a battery reading that is missing or stale (kept in the state):
        within `SILENCE_GRACE_S`, the plan's action unchanged, and the fuse safe on what is known
        without the battery's reading. A hold keeps `PEAK_COVER_A` and is not lifted for a peak; a balanced grid
        charge fits under the cap counting none of its own charge back (the least it can be); a sale keeps the rate
        the plan allows now (its export cap); a charge per phase is not ridden out. Judged at every look on the
        site meter's headroom now. Meanwhile nothing is written (`step`)."""
        state = self.state
        now = inputs.now
        since = state.silent_since or now
        self.state = replace(state, silent_since=since)
        if (now - since).total_seconds() >= SILENCE_GRACE_S or inputs.action != state.action:
            return False
        if state.phase_kw is not None or inputs.headroom_a is None:
            return False
        if state.action == ACTION_HOLD:
            return state.covered_at is None and inputs.headroom_a >= PEAK_COVER_A
        if state.action == ACTION_GRID_CHARGE:
            blind = replace(inputs, facts=replace(inputs.facts, power_kw=None))
            cap = grid_charge_cap_kw(blind, state.rate_kw, yield_now=False)
            return cap is not None and cap >= state.rate_kw - RATE_TOLERANCE_KW
        if state.action == ACTION_EXPORT_DISCHARGE:
            # The export side's fuse rule is the rate the plan gives each look (`gate.export_discharge_cap_kw`), and
            # no rate is written meanwhile: a sale asked to go lower is handed back instead.
            return abs(inputs.rate_kw) >= state.rate_kw - RATE_TOLERANCE_KW
        return True

    def _silent_since(self, inputs: ExecInputs, written_at: datetime | None) -> bool:
        """Whether a running action's plant has said nothing since `written_at` (or is silent now, `None`): its
        battery reading is missing, or (for a write) not reported since the write was made, or stale."""
        if self.state.phase != "active" or self._export:
            return False
        age = inputs.battery_age_s
        if inputs.facts.power_kw is None or age is None or not _fresh(inputs):
            return True
        return written_at is not None and age >= (inputs.now - written_at).total_seconds()

    def _silent_not_taken(self, now: datetime, role: str, why: str, **detail: Any) -> list[dict[str, Any]]:
        """A write not taken while the plant was silent: handed back as stale data is (no block, the next start
        re-arms), never repeated, and no finding against the control or its verification."""
        return [_event(now, "not_taken", role, why=why, silent=True, **detail), *self._begin_restore(now, "stale_data")]

    def _riding_out(self) -> bool:
        return self.state.phase == "active" and self.state.silent_since is not None

    def _setpoints_spent(self) -> bool:
        return self.state.setpoint_writes_today >= self.control.write_cap("setpoint")

    def _rate_is_setpoint(self, action: str | None) -> bool:
        """Whether the brand's own command for the action carries its rate in a setpoint (a battery power target)."""
        build = self.control.states.get(action or "")
        return build is not None and any(
            want.value != 0.0 and self.control.role(want.role).write_class == "setpoint"
            for want in build(1.0, self.watchdog_s)
        )

    def _charge_budget(self, inputs: ExecInputs) -> str | None:
        """Why an action may not run (on) for the day's writes: a charge per phase, or an action whose rate is a
        setpoint, once the setpoints' cap is reached (the runaway guard), and a grid charge of either kind once only
        `CHARGE_WRITE_RESERVE` of the settings' writes are left (a hold must still be able to start)."""
        if self._export:
            return None
        if inputs.phase_probe_kw is None and self._setpoints_spent() and (
            self._per_phase(inputs) or self._rate_is_setpoint(inputs.action)
        ):
            return "setpoint_write_cap"
        if inputs.action != ACTION_GRID_CHARGE:
            return None
        if _writes_today(self.state, inputs) >= self._daily_cap - self._charge_reserve:
            return "charge_write_budget"
        return None

    def _hold_charges(self, inputs: ExecInputs) -> bool:
        """Whether a running idle hold's battery has charged for `HOLD_CHARGE_S` (kept in the state), or one under a
        recipe's grid guard (`BrandControl.hold_grid_guard`) has charged from the grid that long: the lesser of its
        charge and the site's import, so a sun it stores does not count. Not while a peak cover has the plant back in
        its own mode."""
        state = self.state
        power = inputs.facts.power_kw
        grid = inputs.grid_import_kw if inputs.measurement_ok else None
        from_grid = (
            (self.control.hold_grid_guard or self.control.hold_surplus is not None)
            and power is not None and grid is not None and math.isfinite(grid)
            and min(power, grid) > HOLD_CHARGE_KW
        )
        charging = (
            not self._export and state.action == ACTION_HOLD and state.covered_at is None
            and power is not None and math.isfinite(power) and power > HOLD_CHARGE_KW
            and (self.control.hold_idle() or from_grid)
        )
        if not charging:
            if state.charging_since is not None:
                self.state = replace(state, charging_since=None)
            return False
        since = state.charging_since or inputs.now
        if state.charging_since is None:
            self.state = replace(state, charging_since=since)
        return (inputs.now - since).total_seconds() >= HOLD_CHARGE_S

    def _charge_over(self, inputs: ExecInputs) -> bool:
        """Whether a running balanced grid charge has taken more from the grid than commanded for `CHARGE_OVER_S`
        (kept in the state): the lesser of the battery's charge and the site's import (so a sun it stores does not
        count) above the rate by `CHARGE_OVER_KW` or a quarter of it. Judged only once its writes are read back and
        `CHARGE_OVER_GRACE_S` (or the recipe's read-back wait) after the rate last changed. A charge per phase is
        judged per phase."""
        state = self.state
        power = inputs.facts.power_kw
        grid = inputs.grid_import_kw if inputs.measurement_ok else None
        changed = state.rate_changed_at
        grace = max(CHARGE_OVER_GRACE_S, self.control.readback_s or 0.0)
        over = (
            not self._export and state.action == ACTION_GRID_CHARGE and state.phase_kw is None
            and changed is not None and (inputs.now - changed).total_seconds() >= grace
            and state.pending is None and self._next_role(state, inputs) is None
            and power is not None and grid is not None and math.isfinite(power) and math.isfinite(grid)
            and min(power, grid) > state.rate_kw + max(CHARGE_OVER_KW, 0.25 * state.rate_kw)
        )
        if not over:
            if state.over_since is not None:
                self.state = replace(state, over_since=None)
            return False
        since = state.over_since or inputs.now
        if state.over_since is None:
            self.state = replace(state, over_since=since)
        return (inputs.now - since).total_seconds() >= CHARGE_OVER_S

    def _blocker(self, inputs: ExecInputs) -> str | None:
        facts = inputs.facts
        action = inputs.action
        state = self.state
        if action is None or action == ACTION_SELF_CONSUME:
            return "nothing_to_do"
        problem = self._action_problem(inputs, action)
        if problem is not None:
            return problem
        if facts.brand != self.control.brand:
            return "brand_mismatch"
        management = management_problem(facts, self.control, active=inputs.sibling_active, takeover=inputs.takeover)
        if management is not None:
            return management
        if facts.on_grid is False:
            return "off_grid"
        if not inputs.measurement_ok or (not self._export and not _fresh(inputs)):
            return "stale_data"
        if self._export and (inputs.grid_import_kw is None or not math.isfinite(inputs.grid_import_kw)):
            # Nothing could judge whether the limit is obeyed: the export limit is not taken without the site's
            # signed grid power.
            return "no_grid_reading"
        if inputs.headroom_a is None and not self._export:
            return "headroom_unknown"
        if action == ACTION_HOLD and not hold_headroom_ok(inputs):
            return "headroom_too_small"
        if action == ACTION_EXPORT_DISCHARGE and not self._sale_headroom_ok(inputs):
            return "headroom_too_small"
        if action == ACTION_HOLD:
            self._sense(inputs)
            problem = self._hold_charge_problem(inputs)
            if problem is not None:
                return problem
        if action == ACTION_GRID_CHARGE and inputs.headroom_a < HEADROOM_MARGIN_A:
            return "headroom_too_small"
        if action == ACTION_GRID_CHARGE and inputs.phase_probe_kw is not None and not hold_headroom_ok(inputs):
            # The verification's probe leaves two phases' house on the grid, as a hold does: the hold's start rule.
            return "headroom_too_small"
        if _writes_today(state, inputs) >= self._daily_cap:
            return "daily_write_cap"
        if self.control.renewal_cap is not None and state.renewals_today >= self.control.renewal_cap:
            return "renewal_cap"
        budget = self._charge_budget(inputs)
        if budget is not None:
            return budget
        rearm = REARM_S if self.control.rearm_s is None else max(REARM_S, self.control.rearm_s)
        if state.released_at is not None and (inputs.now - state.released_at).total_seconds() < rearm:
            return "rearm"
        for role, value, why in self.control.watched:
            read = inputs.reads.get(role.name)
            if read is not None and read.found and read.enabled and same(
                value, reading(role, read.state), _tolerance(role, read)
            ) is False:
                # Another control's target left standing would act under this one.
                return f"{why}:{role.name}"
        self._sense(inputs)
        wanted = self.control.wanted(
            action, 1.0, self.watchdog_s, soc_pct=self._soc_pct, rated_kw=self._rated_kw, absent=self._absent,
            capacity_kwh=self._capacity_kwh,
        )
        unresolved = self.control.unresolved(wanted)
        if unresolved is not None:
            return unresolved
        needed = set(wanted) | {name for name, _ok, _why in self.control.requires}
        for name in sorted(needed):
            read = inputs.reads.get(name)
            role = self.control.role(name)
            if read is None or not read.found or read.entity_id is None:
                return "needs_hacs_integration" if name in self.control.hacs_roles else f"control_not_found:{name}"
            if role.method != "service" and not read.enabled:
                return f"control_disabled:{name}"
        for name, allowed, why in self.control.requires:
            if reading(self.control.role(name), inputs.reads[name].state) not in allowed:
                return why
        return None
