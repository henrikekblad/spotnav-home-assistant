"""The import target's hold on the fuse's decisions (`site/import_target.py` decides, this applies it).

`ImportTargetHold.hold` mins the running hour's budget into the regulator's decisions, once per recompute, for the
site controller it belongs to (`SiteCapacityController._hold_to_import_target`). It keeps no state of its own: what it
works out (the hour's budget, the shares, the chargers it lowered, paused or locked, the risks and set-asides) is kept
on the site controller, which the diagnostics, the status and the plans read.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
import math
from typing import Any

from homeassistant.util import dt as dt_util

from ..const import CONF_CHARGER_ENTRY_IDS, CONF_MAX_AGE_S, CONF_PHASE_WIRING, DEFAULT_MAX_AGE_S
from ..execution.min_soc_floor import ORIGIN_MIN_SOC
from ..planning.auto_settings import IF_NOT_READY_RELEASE
from ..runtime import controller_for, preview_for
from .import_target import (
    ACTION_PAUSED,
    battery_discharge_shares_w,
    departure_at_risk,
    departure_shortfall_kwh,
    EffectiveTarget,
    held_level_kw,
    hour_budget,
    hour_end_lock_a,
    HourBudget,
    lowering_floor_a,
    member_w,
    plan_remaining_kwh,
    share_budget,
    TargetMember,
)
from .regulator import RegulatorDecision
from .site_capacity import allocation_order, ChargerRequest, PhaseName, PHASES
from .site_capacity_controller import (
    _current_write_lock,
    _must_lower,
    REASON_IMPORT_TARGET_HELD,
    REASON_IMPORT_TARGET_PAUSED,
    RELEASE_END_TARGET_REMOVED,
    RELEASE_SOURCE_PLAN,
    SiteCapacityController,
    TARGET_INERT_EXPORTING,
    TARGET_INERT_NOT_JUDGED,
    TARGET_NOT_APPLIED_BATTERY_UNKNOWN,
    TARGET_NOT_APPLIED_LOAD_BALANCING_OFF,
    TARGET_NOT_APPLIED_OUTSIDE_LEVEL,
    TARGET_STATE_APPLIED,
    TARGET_STATE_OFF,
    TargetRisk,
)


class ImportTargetHold:
    """The import target's hold for one site controller (`site`), whose state it reads and writes."""

    def __init__(self, site: SiteCapacityController) -> None:
        self._site = site

    def hold(
        self,
        decisions: dict[str, RegulatorDecision],
        requests: Sequence[ChargerRequest],
        voltage_by_phase: Mapping[PhaseName, float | None],
        max_age_s: float,
    ) -> dict[str, RegulatorDecision]:
        """The fuse's decisions with the import target min-ed in (`site/import_target.py`).

        Only ever lowers: a decision is replaced only by a smaller proposal, so the smaller of the fuse's ceiling and
        the target's always wins. An overload's lowering (`_must_lower`) and a decision with nothing to write are
        never touched, and a lowering never goes below what keeps every phase the charger draws on an import
        (`_target_floor_a`), so the target can never raise the current the fuse carries. Not applied (nothing held)
        while load balancing is off, the hour cannot be measured well enough, or the site's target is not set. A
        failure here leaves the fuse's decisions as they are (`_compute_regulator_decisions`)."""
        previously_held = self._site._target_lowered | self._site._target_locked
        self._site._target_shares = {}
        self._site._target_lowered = set()
        self._site._target_locked = set()
        self._site._target_raise_lock = {}
        self._site._target_write_wait = {}
        self._site._target_inert = {}
        self._site._target_floor_limited = set()
        self._site._target_budget = None
        target_kw = self._site._hold_set_kw()
        if target_kw is None:
            self._site._target_not_applied = None
            self._site._note_target_state(TARGET_STATE_OFF)
            self._site._target_effective = None
            self._site._target_paused_hour.clear()
            self._site._target_risk = {}
            for charger_entry_id, until in self._site._target_auto_released.items():
                self._site._log_target(
                    charger_entry_id, "release_ended", reason=RELEASE_END_TARGET_REMOVED, until=until, automatic=True
                )
            self._site._target_auto_released = {}
            self._site._tell_target_risk()
            if self._site._target_released:
                for charger_entry_id, until in self._site._target_released.items():
                    self._site._log_target(
                        charger_entry_id, "release_ended", reason=RELEASE_END_TARGET_REMOVED, until=until
                    )
                self._site._target_released.clear()
                self._site._save_released()
            self._site._target_last_cap.clear()
            return decisions
        now = dt_util.utcnow()
        # The target lifted to the month's free level where a demand-charge part counts the running hour: only ever
        # higher than the target, to a level already paid for, and only the budget's; the fuse is untouched.
        self._site._target_effective = self._effective_import_target(target_kw, now)
        if self._site._import_target_kw() is None and self._site._target_effective.level_kw is None:
            # The power level alone, outside its hours: nothing holds this hour (a set-aside stays as it is).
            self._site._target_effective = None
            self._site._target_not_applied = TARGET_NOT_APPLIED_OUTSIDE_LEVEL
            self._site._note_target_state(TARGET_NOT_APPLIED_OUTSIDE_LEVEL)
            self._site._target_risk = {}
            self._site._tell_target_risk()
            return decisions
        budget, reason = self._import_target_budget(self._site._target_effective.kw, now)
        self._site._target_not_applied = reason
        self._site._note_target_state(TARGET_STATE_APPLIED if reason is None else reason)
        if budget is None:
            return decisions
        self._site._target_budget = budget
        # A pause and a held current hold to the end of their hour only.
        self._site._target_paused_hour = {
            charger_entry_id: hour
            for charger_entry_id, hour in self._site._target_paused_hour.items()
            if hour == budget.hour_start
        }
        self._site._target_last_cap = {
            charger_entry_id: held
            for charger_entry_id, held in self._site._target_last_cap.items()
            if held[0] == budget.hour_start
        }
        self._site._latch_target_pauses(budget)
        ordered = allocation_order(requests)
        self._site._prune_released(now)
        members = [
            self._target_member(request, decisions.get(request.charger_entry_id), budget, self._site._target_released)
            for request in ordered
        ]
        shares = share_budget(budget.allowed_w, members, remaining_s=budget.remaining_s)
        # Above the car's minimum level the target holds a plan's charge even past its departure: whether it would miss
        # it is said (`_target_risk`, decided again on every recompute, with a margin either way), and a person may set
        # the target aside for it (`async_release_import_target`).
        risks: dict[str, TargetRisk] = {}
        # A charge the plan's own choice set aside stays so until its departure (or an unplug, or a new departure),
        # as a person's does: no hold again mid-hour that would then pause it for the rest of the hour.
        automatic: dict[str, str] = dict(self._site._target_auto_released)
        for member in members:
            if not member.holdable or member.charger_entry_id in automatic:
                continue
            found = self._target_shortfall(member, budget, now)
            if found is None:
                continue
            departure, shortfall = found
            was = self._site._target_risk.get(member.charger_entry_id)
            if not departure_at_risk(shortfall, at_risk=was is not None and was.departure == departure):
                continue
            choice = self._site._if_not_ready(member.charger_entry_id)
            risks[member.charger_entry_id] = TargetRisk(
                departure=departure,
                missing_kwh=round(max(0.0, shortfall), 1),
                target_kw=budget.target_kw,
                choice=choice,
                set_kw=budget.target_kw
                if self._site._target_effective is None
                else self._site._target_effective.set_kw,
            )
            if choice == IF_NOT_READY_RELEASE:
                # The plan's own choice: the departure wins by itself, as long as holding would miss it.
                automatic[member.charger_entry_id] = departure
        for charger_entry_id, risk in risks.items():
            if charger_entry_id not in self._site._target_risk:
                self._site._note_target(charger_entry_id, "at_risk", budget, departure=risk.departure)
        for charger_entry_id, departure in automatic.items():
            if charger_entry_id not in self._site._target_auto_released:
                self._site._log_target(
                    charger_entry_id, "release", budget, source=RELEASE_SOURCE_PLAN, until=departure, automatic=True
                )
            risks.pop(charger_entry_id, None)
        self._site._target_risk = risks
        self._site._target_auto_released = automatic
        self._site._tell_target_risk()
        if automatic:
            released = {**self._site._target_released, **automatic}
            members = [
                self._target_member(request, decisions.get(request.charger_entry_id), budget, released)
                for request in ordered
            ]
            shares = share_budget(budget.allowed_w, members, remaining_s=budget.remaining_s)
        self._site._target_shares = shares
        self._site._target_write_wait = {member.charger_entry_id: member.write_wait_s for member in members}
        # The hour's last minutes raise nothing the target held this hour above what it held it to: what is left of
        # the budget is spread over too little time to be worth a raise the next hour takes back (and the lowering
        # then waits for the dwell, past the hour's start). A charger the target did not hold (one the fuse lowered,
        # say) is the fuse's and the budget's to raise, as at any other time. For a charger whose current may be
        # written only every few minutes the lock starts a whole interval and a dwell before the hour's end
        # (`raise_lock_s`), so the lowering that takes a raise back always comes within the hour, and it is kept
        # where it is set: a raise there would hold into the next hour, as no write may follow it before then. Such
        # a charger is locked whenever the target held it this hour, also when the budget freed it just before the
        # window; never below its minimum (a current set below it by hand is no reason to pause it).
        for member in members:
            held_cap = self._site._target_last_cap.get(member.charger_entry_id)
            lock = hour_end_lock_a(
                member,
                held_cap_a=None if held_cap is None else held_cap[1],
                held_last_pass=member.charger_entry_id in previously_held,
                remaining_s=budget.remaining_s,
            )
            if lock is not None:
                self._site._target_raise_lock[member.charger_entry_id] = lock
        held = dict(decisions)

        def holds_now(request: ChargerRequest) -> bool:
            """Whether this charger is one the target may lower in this pass (running, held, above its share)."""
            share = shares.get(request.charger_entry_id)
            decision = decisions.get(request.charger_entry_id)
            member = next(m for m in members if m.charger_entry_id == request.charger_entry_id)
            return not (
                share is None
                or decision is None
                or decision.proposed_current_a is None
                or not member.holdable
                or not request.requested_current_a
            )

        # Under `battery_first` the battery's live discharge is shared out among the chargers the target lowers in
        # this pass, so two cannot each count on the whole of it (`_target_floor_a`).
        discharge_shares = battery_discharge_shares_w(
            self._site._battery_first_discharge()[1] or 0.0,
            [
                request.charger_entry_id
                for request in ordered
                if holds_now(request)
                and (cap := shares[request.charger_entry_id].cap_a) is not None
                and decisions[request.charger_entry_id].proposed_current_a > cap  # type: ignore[operator]
            ],
        )
        for request in ordered:
            charger_entry_id = request.charger_entry_id
            share = shares.get(charger_entry_id)
            decision = decisions.get(charger_entry_id)
            if not holds_now(request):
                # Nothing held, or a charge that is not running (a paused one is held back by `_target_start_cap`).
                continue
            proposed = decision.proposed_current_a
            locked = None if share.action == ACTION_PAUSED else self._site._target_raise_lock.get(charger_entry_id)
            if share.cap_a is None or proposed <= share.cap_a:
                if locked is not None and proposed > locked:
                    # The budget has room, the hour's end does not: kept where the target held it, under the fuse's
                    # own reason (the target does not claim to hold what its budget would allow).
                    held[charger_entry_id] = replace(decision, proposed_current_a=locked)
                    self._site._target_locked.add(charger_entry_id)
                continue
            cap_a = share.cap_a if locked is None else min(share.cap_a, locked)
            floor = self._target_floor_a(
                request, voltage_by_phase, max_age_s, discharge_shares.get(charger_entry_id, 0.0)
            )
            if floor is None:
                self._site._target_inert[charger_entry_id] = TARGET_INERT_NOT_JUDGED
                continue
            if share.action == ACTION_PAUSED and floor <= 0.0:
                held[charger_entry_id] = replace(decision, proposed_current_a=0.0, reason=REASON_IMPORT_TARGET_PAUSED)
                if charger_entry_id not in self._site._target_paused_hour:
                    # Why this pause, until it is taken (a pause already latched keeps the reason it was taken for).
                    if share.reason is not None:
                        self._site._target_pause_reasons[charger_entry_id] = (budget.hour_start, share.reason)
                    else:
                        self._site._target_pause_reasons.pop(charger_entry_id, None)
                # Kept to the hour's end once the charger is seen paused for it (`_latch_target_pauses`): a pause
                # decided for a kettle's half minute and taken back before the dwell let it out pauses nothing.
                self._site._target_pause_pending[charger_entry_id] = budget.hour_start
                self._site._target_lowered.add(charger_entry_id)
                continue
            if request.switch_only:
                # It runs whole or not at all, and the pause would turn a phase it draws on to export.
                self._site._target_inert[charger_entry_id] = TARGET_INERT_EXPORTING
                continue
            lowered = max(cap_a, request.min_current_a, float(math.ceil(floor - 1e-9)))
            if lowered < proposed:
                held[charger_entry_id] = replace(decision, proposed_current_a=lowered, reason=REASON_IMPORT_TARGET_HELD)
                self._site._target_lowered.add(charger_entry_id)
                # The highest the target held it to this hour: the lock at the hour's end keeps no less, and a raise
                # back to it is the target giving back its own lowering (`_restores_target_lowering`).
                was_held = self._site._target_last_cap.get(charger_entry_id)
                highest = lowered if was_held is None or was_held[0] != budget.hour_start else max(was_held[1], lowered)
                self._site._target_last_cap[charger_entry_id] = (budget.hour_start, highest)
                self._site._note_target(charger_entry_id, "held", budget, cap_a=lowered)
            if lowered > max(cap_a, request.min_current_a):
                if lowered >= proposed:
                    # Nothing could be lowered: every amp of the drop would turn a phase it draws on to export.
                    self._site._target_inert[charger_entry_id] = TARGET_INERT_EXPORTING
                else:
                    # Lowered, but not as far as its share: the floor stopped it (`floor_limited`).
                    self._site._target_floor_limited.add(charger_entry_id)
        return held

    def _effective_import_target(self, set_kw: float, now: datetime) -> EffectiveTarget:
        """The import target lifted to the month's free level for the running hour (`effective_target`), from the
        hours the site's tracker kept this month and the electricity price's demand-charge parts. Worked out again
        when an hour completes, the hour turns or the parts or the target change."""
        found, key = self._site._worked_out_import_target(set_kw, now, self._site._target_effective_key)
        if found is None:
            assert self._site._target_effective is not None
            return self._site._target_effective
        self._site._target_effective_key = key
        return found

    def _import_target_budget(self, target_kw: float, now: datetime) -> tuple[HourBudget | None, str | None]:
        """The running hour's budget (`hour_budget`) on this recompute's readings, or why it is not applied."""
        from ..execution.solar_execution import site_car_w, site_net_grid_w  # noqa: PLC0415 - it imports us

        if not self._site._active_control_allowed():
            return None, TARGET_NOT_APPLIED_LOAD_BALANCING_OFF
        battery_counts, discharge_w = self._site._battery_first_discharge()
        if battery_counts and discharge_w is None:
            return None, TARGET_NOT_APPLIED_BATTERY_UNKNOWN
        grid_w = site_net_grid_w(self._site)
        # Steers chargers: a charger it cannot read keeps the house unknown even when it looks idle.
        car_w = None if grid_w is None else site_car_w(self._site.hass, self._site)
        house_w = None if grid_w is None or car_w is None else grid_w + (discharge_w or 0.0) - car_w
        if house_w is not None and self._a_start_not_yet_drawing():
            # A charger just started (written at or above its minimum) still reads below its minimum while the meter,
            # or a battery covering the car, already sees the car: its draw would count as the house's. Never more
            # than what the house typically draws then, so the hold does not flash on a start.
            typical_kw = self._site._typical_house_kw()
            if typical_kw is not None:
                house_w = min(house_w, max(0.0, typical_kw) * 1000.0)
        running = self._site.demand.tracker.running_hour(now)
        return hour_budget(
            target_kw=target_kw,
            running=running,
            now=now,
            house_w=house_w,
            ceiling_w=self._site._import_ceiling_w(),
            extra_wh=0.0 if running is None else running.discharge_wh,
            extra_covered_s=(None if running is None else running.discharge_covered_s) if battery_counts else None,
        )

    def _target_member(
        self,
        request: ChargerRequest,
        decision: RegulatorDecision | None,
        budget: HourBudget,
        released: Collection[str] = (),
    ) -> TargetMember:
        """One charger as the target's budget is shared: what it would draw, and whether the target may hold it (not
        a person's Start, not the car's minimum level's charge, nor a charge in `released`, set aside by a person
        until its departure)."""
        charger_entry_id = request.charger_entry_id
        charger_controller = controller_for(self._site.hass, charger_entry_id)
        phases = len(request.phases_used() or ()) or request.phases
        drawn = 0.0
        if request.measured_current_a is not None:
            values = [request.measured_current_a.get(phase).value for phase in (request.phases_used() or ())]
            drawn = max((value for value in values if value is not None), default=0.0)
        origin = None
        if charger_controller is not None:
            origin = getattr(charger_controller, "charge_origin", None) or getattr(
                charger_controller, "paused_charge_origin", None
            )
        paused = bool(getattr(charger_controller, "paused_by_balancing", False))
        proposed = None if decision is None else decision.proposed_current_a
        holdable = (
            charger_controller is not None
            # A person's Start charges now, within the target, as any charge does (its way past the target is a release,
            # `async_release_import_target`). Below the car's minimum level SpotNav charges at once
            # (`execution/min_soc_floor.py`): the floor wins over the target.
            and origin != ORIGIN_MIN_SOC
            # A person set the target aside for this charge until its departure (`async_release_import_target`).
            and charger_entry_id not in released
            # Neither an overload's lowering nor a decision with nothing to write is the target's to change.
            and (paused or (proposed is not None and decision is not None and not _must_lower(decision)))
        )
        if paused and holdable:
            # What the paused charge would resume at: a resume or a probe gives it no more (`_target_start_cap`).
            requested = getattr(charger_controller, "requested_current_a", None)
            wants = request.min_current_a if request.switch_only else float(requested or 0.0)
        elif holdable:
            wants = float(proposed or 0.0)
        elif paused:
            # A charge the target does not hold, paused by the fuse: it draws nothing while it waits, and when the fuse
            # resumes it, it is counted at what it draws then.
            wants = 0.0
        else:
            wants = drawn if proposed is None else max(0.0, proposed)
        write_wait_s, write_interval_s, set_a = _current_write_lock(charger_controller)
        return TargetMember(
            charger_entry_id=charger_entry_id,
            phases=phases,
            min_a=request.min_current_a,
            wants_a=wants,
            holdable=holdable,
            switch_only=request.switch_only,
            paused_this_hour=self._site._target_paused_hour.get(charger_entry_id) == budget.hour_start,
            draws_a=0.0 if paused else drawn,
            set_a=0.0 if paused else set_a,
            write_wait_s=write_wait_s,
            write_interval_s=write_interval_s,
        )

    def _a_start_not_yet_drawing(self) -> bool:
        """Whether a member charger is in a charge it was written at least its minimum for, but its own reading is
        still below that minimum: a start (or a resume) whose draw the charger has not reported yet."""
        max_age_s = float(self._site.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        wiring_by_charger: dict[str, Any] = self._site.config.get(CONF_PHASE_WIRING) or {}
        for charger_entry_id in self._site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
            charger_controller = controller_for(self._site.hass, charger_entry_id)
            if charger_controller is None or not (charger_controller.charging or charger_controller.start_pending):
                continue
            minimum = self._site._min_current_a(charger_entry_id)
            written = getattr(charger_controller.adapter, "last_current_a", None)
            if not isinstance(written, (int, float)) or isinstance(written, bool) or written < minimum:
                continue
            delivered = self._site._read_charger_measured_current(wiring_by_charger.get(charger_entry_id) or {})
            readings = (
                []
                if delivered is None
                else [
                    value.value
                    for value in (delivered.get(phase) for phase in PHASES)
                    if value.value is not None
                    and value.problem is None
                    and value.age_s is not None
                    and value.age_s <= max_age_s
                ]
            )
            if readings and max(readings) < minimum:
                return True
        return False

    def _target_shortfall(self, member: TargetMember, budget: HourBudget, now: datetime) -> tuple[str, float] | None:
        """`(departure, kWh)`: how much a plan's charge would miss its departure by under the target
        (`departure_shortfall_kwh`, negative: to spare), or `None` for a charge with no plan and departure ahead.

        This hour and later ones are reckoned at the target less the house's typical load (`_typical_house_kw`, the
        estimate the plan counts on too, `import_target_plan_kw`), a later hour the house load forecast knows at the
        target less its expected load there (`import_target_plan_kw_by_hour`), never its load this instant: a
        kettle's half minute must not say the car will not be ready. What it still needs is what the plan expects of its windows' time
        left, plus what the running window is already behind (`PlanWatch.shortfall_kwh`): a window the target held
        is planned again at its end, so earlier windows' shortfalls are in the plan already
        (`planning/plan_watch.py`)."""
        charger_controller = controller_for(self._site.hass, member.charger_entry_id)
        plan = None if charger_controller is None else getattr(charger_controller, "plan", None)
        if plan is None or not plan.departure or not getattr(plan, "auto_owned", False):
            return None
        departure = dt_util.parse_datetime(plan.departure)
        if departure is None or departure.tzinfo is None:
            return None
        # What the plan still expects to deliver before the departure: its energy spread over its windows (the power
        # it planned at, which may already be the target's, `import_target_plan_kw`), for the windows' time left.
        windows = plan.windows
        window_h = sum(max(0.0, (end - start).total_seconds()) for start, end in windows) / 3600.0
        if plan.energy_kwh and window_h > 0:
            power_kw = plan.energy_kwh / window_h
        else:
            power_kw = plan.power_kw if plan.power_kw else member_w(plan.phases, plan.amps) / 1000.0
        need_kwh = plan_remaining_kwh(windows, power_kw, now, departure)
        preview = preview_for(self._site.hass, member.charger_entry_id)
        if preview is not None:
            need_kwh += preview.import_target_shortfall_kwh()
        house_kw = self._site._typical_house_kw()
        if house_kw is None:
            house_kw = max(0.0, budget.house_w) / 1000.0
        # This hour at what its budget leaves beside the house's typical load (never above the plan's power, nothing
        # below the charger's minimum), every later hour at the target less that load.
        car_kw = member_w(member.phases, max(member.min_a, member.wants_a)) / 1000.0
        ceiling_kw = max(power_kw, car_kw)
        held_kw = min(ceiling_kw, max(0.0, budget.rate_w / 1000.0 - max(0.0, house_kw)))
        # Later hours at the target as set, less its margin as the hold keeps it: the month's free level lifts the
        # running hour only.
        set_kw = held_level_kw(
            budget.target_kw if self._site._target_effective is None else self._site._target_effective.set_kw
        )
        minimum_kw = member_w(member.phases, member.min_a) / 1000.0

        def later(house: float, hold_kw: float = set_kw) -> float:
            kw = min(ceiling_kw, max(0.0, hold_kw - max(0.0, house)))
            return 0.0 if kw < minimum_kw else kw

        later_kw = later(house_kw)
        # A later hour the house load forecast knows at the target less its own expected load (`site/load_forecast.py`),
        # the target no more than the power level where it holds then (the level alone without a target).
        target_kw = self._site._import_target_kw()
        power_level = self._site._power_level()
        later_by_hour = {}
        for start, kw in self._site.house_forecast.plan_kw_by_hour(now).items():
            if start < budget.hour_end:
                continue
            hold_kw = self._site.plan_hold_kw_at(start, target_kw, power_level)
            later_by_hour[start] = ceiling_kw if hold_kw is None else later(kw, held_level_kw(hold_kw))
        held_kw = 0.0 if held_kw < minimum_kw else held_kw
        shortfall = departure_shortfall_kwh(
            need_kwh=need_kwh,
            now=now,
            departure=departure,
            hour_end=budget.hour_end,
            held_kw=held_kw,
            later_kw=later_kw,
            later_kw_by_hour=later_by_hour,
        )
        return None if shortfall is None else (plan.departure, shortfall)

    def _target_floor_a(
        self,
        request: ChargerRequest,
        voltage_by_phase: Mapping[PhaseName, float | None],
        max_age_s: float,
        battery_discharge_w: float = 0.0,
    ) -> float | None:
        """How far the target may lower this charger without raising what the fuse carries (`lowering_floor_a`), on
        this recompute's own readings; `None` when a phase cannot be judged. Under `battery_first` with the home
        battery read live and discharging, a drop up to this charger's part of its discharge (`battery_discharge_w`, the
        caller's share of `_battery_first_discharge`) only lowers the discharge, within the smallest fuse margin over
        its phases; under `car_first`, or with a battery that cannot be read live, the grid's phases alone decide."""
        phases = request.phases_used() or ()
        result = self._site.result
        if not phases or request.measured_current_a is None:
            return None
        for phase in phases:
            age = result.phase_signed_active_power_age_s.get(phase)
            if result.phase_signed_active_power_reason.get(phase) is not None or age is None or age > max_age_s:
                return None
        signed_current = any(result.phase_direction_source.get(phase) == "signed_current" for phase in phases)
        return lowering_floor_a(
            phases=phases,
            measured_a={phase: request.measured_current_a.get(phase).value for phase in phases},
            signed_power_w={phase: result.phase_signed_active_power_w.get(phase) for phase in phases},
            voltage_v={phase: voltage_by_phase.get(phase) for phase in phases},
            margin_a={phase: result.measured_margin_a.get(phase) for phase in phases} if signed_current else None,
            battery_discharge_w=battery_discharge_w,
            fuse_margin_a={phase: result.measured_margin_a.get(phase) for phase in phases},
        )
