"""The verification routine as the shell drives it: its start at a moment that suits, each step through the executor
with every rule, the charge it may ask for, and its end with the report kept under the plant's key."""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from ...planning.grid_voltage import stored_voltage_between_phases_v
from .. import gate
from ..commands import (
    BrandControl,
    control_for,
)
from ..executor import (
    Executor,
    StepResult,
    preview,
)
from ..monitor import Capability
from ..plan_model import (
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
)
from ..runtime import plant_entity_id
from ..verify import (
    Sample,
    Verification,
    phase_charge_step,
)
from .const import (
    EXCLUSIVE_BOUND_MARGIN,
)
from .helpers import _float, _least


_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class VerifyDriver:
    """The routine's check of a key. A mixin of `BatteryControl`, on its state."""

    # ---- the verification routine

    async def async_verify_step(self, now: datetime | None = None) -> StepResult | None:
        """One look of the verification routine; it starts a run when none runs. Refused unless the options allow
        writing."""
        self._look_id += 1
        self._require(self.options().writes, "The verification routine")
        now = now or dt_util.now()
        if self.verification is None or self.verification.done:
            if self._begin_verification(now) is None:
                return None
        return await self._async_verify(now)

    def _begin_verification(self, now: datetime) -> Verification | None:
        executor = self._executor()
        if executor is None:
            return None
        key = self.firmware_key()
        previous = self.reports.get(key) if isinstance(self.reports.get(key), dict) else None
        attempt = (previous or {}).get("attempts")
        self._verify_attempt = (attempt if isinstance(attempt, int) else 0) + 1
        self._verify_key = key
        modes = gate.BATTERY_MODES if previous is None else gate.modes_to_run(previous, self._supported_modes())
        if self._per_phase_due(previous):
            modes = (*modes, gate.MODE_PER_PHASE)
        expiry = self._expiry_to_test(previous) if self._expiry_due(previous) else None
        if expiry is not None:
            # Tried only after a hold that passed in this same run.
            modes = (*modes, *(() if "hold" in modes else ("hold",)), gate.MODE_EXPIRY)
        self._plan_wrote = None
        self._aside_until = None
        self.verification = Verification(
            executor, now, firmware=self.facts.firmware, modes=modes, tried=gate.variants_tried(previous),
            expiry=None if expiry is None else expiry[0], expiry_phased=expiry is not None and expiry[1] is not None,
            expiry_phase=0 if expiry is None or expiry[1] is None else expiry[1],
            phase_mapped=gate.per_phase(previous) == "verified",
        )
        # The attempt counts from its start: a restart in the middle neither forgets it nor runs again at once.
        self._keep_report(key, gate.merge_report(
            previous, {"brand": executor.control.brand, "firmware": self.facts.firmware, "started_at": now.isoformat()},
            key=key, attempts=self._verify_attempt, now=now,
        ))
        self._log([{"at": now.isoformat(), "kind": "verify_started", "reason": key,
                    "modes": [result.mode for result in self.verification.results]}])
        _LOGGER.info("SpotNav battery control: checking the battery (%s)", key)
        return self.verification

    def _end_verification(self, now: datetime) -> None:
        """Keep what a run found so far (a run ended by a stop or by turning off: its open modes stay pending)."""
        verification = self.verification
        if verification is not None and self._hw_run:
            # The hardware test's report is its own; the routine's per firmware is untouched.
            self._hw_run = False
            self.hardware_report = {**verification.report(), "completed": verification.done}
            self._keep_hardware_verdict(verification.done, now)
            self._log([{"at": now.isoformat(), "kind": "hardware_test_done" if verification.done else
                        "hardware_test_stopped", "modes": {
                            result.mode: result.outcome for result in verification.results}}])
            self.verification = None
            return
        if verification is None or self._verify_key is None:
            self.verification = None
            return
        report = gate.merge_report(
            self.reports.get(self._verify_key), verification.report(), key=self._verify_key,
            attempts=self._verify_attempt, now=now,
        )
        self._keep_report(self._verify_key, report)
        self._log([{"at": now.isoformat(), "kind": "verify_done" if verification.done else "verify_stopped",
                    "reason": gate.report_status(report), "modes": {
                        name: row.get("outcome") for name, row in report["modes"].items()}}])
        self.verification = None
        self._verify_key = None

    def _within_charge_power(self, cap: float | None) -> float | None:
        """A grid charge cap (`None`: none) lowered to what the plant's own charge power entity takes
        (`_charge_entity_max_kw`), for a live charge, a test action and the plan alike; never raised."""
        control = control_for(self.facts.control_brand)
        entity_kw = None if control is None else self._charge_entity_max_kw(control)
        bms_kw = None if control is None else self._bms_charge_kw(control)
        if bms_kw is not None:
            entity_kw = bms_kw if entity_kw is None else min(entity_kw, bms_kw)
        if entity_kw is None:
            return cap
        return entity_kw if cap is None else min(cap, entity_kw)

    def _bms_charge_kw(self, control: BrandControl) -> float | None:
        """What the battery's BMS takes now (kW: its present charge-current limit times the voltage,
        `facts.BatteryFacts.bms_charge_kw`; 0 while it takes nothing), only for a recipe that names it
        (`BrandControl.bms_charge_limit`: Solis, whose cold pack holds a force charge far under the rating); `None`
        for every other recipe and where it reads nothing. A bound only: it never raises a cap."""
        return self.facts.bms_charge_kw if control.bms_charge_limit else None

    def _charge_entity_max_kw(self, control: BrandControl) -> float | None:
        """The largest grid charge the recipe's charge power entity takes (kW: its `max` over the role's scale), only
        for a recipe that names one (`BrandControl.charge_power_bound`: Marstek, whose Venus A stops at 1500 W where
        the recipe allows 2500 W); `None` for every other recipe, where none is read, and where the entity says
        nothing or less than the least rate control commands (a range left at Home Assistant's default). Read once
        a look (`_look_id`). It bounds the check's charge step, a live grid charge, a test action and the plan's
        cap (`_within_charge_power`)."""
        name = control.charge_power_bound
        if name is None:
            return None
        cached = self._charge_bound
        if cached is not None and cached[0] == (self._look_id, control.brand):
            return cached[1]
        role = next(role for role in control.roles if role.name == name)
        entity_id = plant_entity_id(self.hass, self.site.config, role.match)
        state = None if entity_id is None else self.hass.states.get(entity_id)
        bound = None if state is None else _float(state.attributes.get("max"))
        if bound is not None and control.charge_power_bound_exclusive:
            # The maximum itself means "follow the battery's maximum" (Kostal's Set Charge Rate), and it moves with
            # the battery's voltage between this read and the write: a margin of `EXCLUSIVE_BOUND_MARGIN` or the
            # entity's step below it, whichever is larger, on the step.
            step = role.step or 0.0
            bound -= max(step, bound * EXCLUSIVE_BOUND_MARGIN)
            if step:
                bound = math.floor(bound / step + 1e-9) * step
        found = None if bound is None or not role.scale else bound / role.scale
        if found is not None and found < gate.MIN_RATE_KW:
            found = None
        self._charge_bound = ((self._look_id, control.brand), found)
        return found

    async def _async_verify(self, now: datetime) -> StepResult | None:
        verification = self.verification
        executor = self.executor
        if verification is None or executor is None:
            return None
        inputs = self.inputs(now, executor.control, slot=None)
        sample = Sample(
            inputs.facts.power_kw, inputs.battery_age_s, inputs.facts.soc_pct, inputs.house_deficit_kw,
            inputs.grid_import_kw if inputs.measurement_ok else None,
            self._grid_phase_kw() if inputs.measurement_ok else None, inputs.plant_phase_kw,
            curtailing=self._curtailing(), soc_min_pct=inputs.facts.reserve_soc_pct or 0.0,
            headroom_a=inputs.headroom_a, phase_point_kw=inputs.phase_point_kw,
            volts_between_phases=inputs.volts_between_phases,
        )
        # The charge step never asks for more than the battery's rating, its own charge power entity or its BMS takes.
        charge_max_kw = _least(
            _least(inputs.facts.max_charge_kw, self._charge_entity_max_kw(executor.control)),
            self._bms_charge_kw(executor.control),
        )
        action, rate = verification.command(now, sample, charge_max_kw)
        probe = verification.probe()
        if action == ACTION_GRID_CHARGE and probe is None:
            # The mode `charge` proves the brand's own balanced charge, under the balanced cap.
            cap = self._grid_charge_cap_kw(now, self.options(), held_off=self._hw_run)
            fixed = self._fixed_charge_kw() if self._hw_run else None
            if fixed is not None and math.isfinite(fixed) and cap is not None and fixed > cap:
                # The plant charges at its own power (Tesla) and that does not fit under the cap here: the step cannot
                # run at a safe size, so the test goes on without it (no pass of the charge: control charges from
                # the grid only once the routine's charge step passed, `gate.allowed_actions`).
                verification.not_applicable(now, gate.NOT_APPLICABLE_FIXED_CHARGE)
                action, rate = verification.command(now, sample, charge_max_kw)
                probe = verification.probe()
            bms_kw = self._bms_charge_kw(executor.control)
            if bms_kw is not None:
                # A BMS that takes too little now (a full or a cold pack) leaves the step nothing to judge.
                cap = bms_kw if cap is None else min(cap, bms_kw)
            if cap is not None and cap < gate.MIN_RATE_KW:
                if self._phase_charge_possible():
                    # An unbalanced house leaves the balanced charge no room: a charge on the phase with the most
                    # room instead, within that phase's own HA-down cap (`verify.phase_charge_step`), after a
                    # discharge on it has shown its sign and mapping unless `per_phase` has on this firmware.
                    step = phase_charge_step(
                        self._phase_caps_kw(now, held_off=self._hw_run),
                        stored_voltage_between_phases_v(self.site.config),
                    )
                    verification.charge_per_phase(
                        now, sample, step, mapped=gate.per_phase(self.report()) == "verified"
                    )
                else:
                    # No grid charge fits under the HA-down cap now: tried again at a later run.
                    verification.not_judgeable(now, "grid_charge_cap")
                action, rate = verification.command(now, sample, charge_max_kw)
                probe = verification.probe()
            rate = rate if cap is None or probe is not None else min(rate, cap)
        # The probe of `per_phase` is a discharge on one phase (`Verification.probe`), which needs no cap; `charge`
        # tried per phase is a charge on one phase, under the phases' HA-down caps and the executor's live room per
        # phase as any charge per phase. The reads are the control's as it is now (a phased mode has just made it
        # the phased one).
        charging = probe is not None and any(value > 0 for value in probe)
        inputs = replace(
            inputs, action=action, rate_kw=rate, slot_end=None,
            phase_caps_kw=self._phase_caps_kw(now, held_off=self._hw_run) if charging else None, phase_probe_kw=probe,
            reads=self._reads(executor.control),
            # The mode `expiry`'s silence: nothing renews the command (`verify.Verification.expiry_probe`).
            expiry_probe=verification.expiry_probe(),
        )
        result = await self._async_step(executor, inputs, allowed=True)
        capability = Capability(
            rated_charge_kw=inputs.facts.max_charge_kw,
            rated_discharge_kw=inputs.facts.max_discharge_kw,
            soc_min_pct=inputs.facts.reserve_soc_pct or 0.0,
        )
        verification.after(now, result, sample, capability)
        if verification.done:
            self._end_verification(now)
            await self._async_save()
        return result

    async def _async_maybe_verify(self, now: datetime, options: gate.Options) -> None:
        """Start a run at a suitable moment when the report for this firmware still needs one."""
        report = self.report()
        state = self.state(options)
        if state not in ("waiting", "on") or not self._check_due(report):
            self.waiting_for = None
            return
        if not self.firmware_known():
            self.waiting_for = "firmware_unknown"
            return
        last = gate.parse_time((report or {}).get("last_run_at"))
        if last is not None and (now - last).total_seconds() < gate.RETRY_GAP_S:
            self.waiting_for = "retry_gap"
            return
        executor = self._executor()
        if executor is None:
            self.waiting_for = "no_battery_control"
            return
        why = gate.suitable(self._moment(now, executor))
        self.waiting_for = why
        if why is None:
            self._begin_verification(now)
            await self._async_verify(now)

    def _check_due(self, report: dict[str, Any] | None) -> bool:
        """Whether the routine has anything left to try on the key's own report (`gate.needs_run`, the capability
        per phase, an expiry)."""
        return (
            gate.needs_run(report, self._supported_modes()) or self._per_phase_due(report) or self._expiry_due(report)
        )

    def _reverifying(self) -> bool:
        """Whether this is the check after a firmware, integration or recipe change that control runs through on
        the answers of the brand's earlier keys (`carried_from`)."""
        return self.carried_from() is not None

    def _moment(self, now: datetime, executor: Executor) -> gate.Moment:
        inputs = self.inputs(now, executor.control, slot=(ACTION_HOLD, 0.0, None))
        seen = preview(executor.control, inputs)
        chargers, _car_kw = self.site.battery_shadow.grid_charging()
        return gate.Moment(
            soc_pct=inputs.facts.soc_pct,
            power_kw=inputs.facts.power_kw,
            fresh=inputs.battery_age_s is not None and inputs.battery_age_s <= 120.0,
            measurement_ok=inputs.measurement_ok,
            headroom_a=inputs.headroom_a,
            car_charging=bool(chargers),
            blocker=seen.blocker,
            executor_idle=executor.state.phase == "idle" and not executor.state.holds_snapshot,
            house_deficit_kw=inputs.house_deficit_kw,
        )
