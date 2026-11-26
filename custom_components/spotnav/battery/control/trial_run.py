"""The owner's test action through the executor: what refuses it, its bounded rate and its slot while it runs."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .. import gate
from ..commands import (
    ACTION_CURTAIL_EXPORT,
)
from ..plan_model import (
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
)
from ..verify import (
    TEST_CHARGE_KW,
)
from .const import (
    NEED_RECIPE_BLOCKED,
    TRIAL_MAX_MIN,
)
from .trial import Trial, TrialRefused


_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class TrialRun:
    """The owner's test action. A mixin of `BatteryControl`, on its state."""

    # ---- the owner's test action

    async def async_trial(self, action: str, power_kw: float | None, minutes: float, *, force: bool) -> dict[str, Any]:
        """Run one action through the executor, with every rule, for `minutes` (`stop` ends a running one). Refused
        unless the opt-in is on; while shadow only, and before hold and normal passed the verification, unless `force`
        (the owner's on-site tests); while the routine runs; and for selling unless it is allowed. Answers what it
        started."""
        if not self._started or self._closed:
            raise TrialRefused("starting")
        self._look_id += 1
        now = dt_util.now()
        options = self.options()
        if action == "stop":
            async with self._lock:
                trial, self.trial = self.trial, None
                if trial is not None and trial.export and self.export.state(options) != "on" and self.export_executor is not None:
                    await self._async_export_until_handed_back("trial_stopped")
                elif trial is not None and not trial.export and self.state(options) != "on" and self.executor is not None:
                    await self._async_until_handed_back("trial_stopped")
                await self._async_look(now)
            return {"stopped": trial is not None}
        if self._start_wait():
            # The start-up wait (`runtime.START_GRACE`): a planner may not be back yet.
            raise TrialRefused("starting")
        if not options.enabled:
            raise TrialRefused("not_enabled")
        if self.recipe_blocked():
            raise TrialRefused(NEED_RECIPE_BLOCKED)
        due = self.hardware_test_due()
        if due is not None:
            # A recipe that writes only after a person's hardware test: not even a forced test action before one.
            raise TrialRefused(due)
        if options.shadow_only and not force:
            raise TrialRefused("shadow_only")
        if not force and gate.report_status(self.control_report()) != "passed":
            raise TrialRefused("not_verified")
        why = gate.trial_refusal(action, options)
        if why is not None:
            raise TrialRefused(why)
        if self.verification is not None and not self.verification.done:
            raise TrialRefused("verifying")
        if action == ACTION_CURTAIL_EXPORT:
            if self._export_executor() is None:
                raise TrialRefused("no_export_control")
            rate = 0.0
        else:
            executor = self._executor()
            if executor is None:
                raise TrialRefused("no_battery_control")
            names = executor.control.variant_names()
            if action == ACTION_HOLD and names and gate.hold_variant_to_run(self.control_report(), names) is None:
                # Every way to hold failed on this firmware: not even a forced test runs one again.
                raise TrialRefused("hold_variants_failed")
            self.facts = self._read_facts()
            rate = 0.0
            if action in (ACTION_GRID_CHARGE, ACTION_EXPORT_DISCHARGE):
                rate = self._bounded_rate(action, abs(power_kw) if power_kw is not None else TEST_CHARGE_KW)
            if action == ACTION_GRID_CHARGE:
                cap = self._within_charge_power(self._charge_limits(now, options)[0])
                if cap is not None and cap < gate.MIN_RATE_KW:
                    raise TrialRefused("grid_charge_cap")
                rate = rate if cap is None else min(rate, cap)
        minutes = max(1.0, min(float(minutes), float(TRIAL_MAX_MIN)))
        async with self._lock:
            # Asked again once the lock is held: a routine or hardware test that started meanwhile (asked for during
            # the same look), or an unload, refuses it as it would a moment later.
            if self._closed:
                raise TrialRefused("starting")
            if self.verification is not None and not self.verification.done:
                raise TrialRefused("verifying")
            self.trial = Trial(action, round(rate, 3), now, now + timedelta(minutes=minutes), force)
            self._log([{"at": now.isoformat(), "kind": "trial", "reason": action, "rate_kw": round(rate, 3),
                        "minutes": minutes, "force_test": force}])
            _LOGGER.warning("SpotNav battery control: test action %s for %s min (%s kW)", action, minutes, rate)
            await self._async_look(now)
        executor = self.export_executor if action == ACTION_CURTAIL_EXPORT else self.executor
        return {
            "trial": None if self.trial is None else self.trial.as_dict(),
            "phase": None if executor is None else executor.state.phase,
            "events": list(self.log)[-6:],
        }

    def _bounded_rate(self, action: str, rate_kw: float) -> float:
        """A test action's power within the battery's own rating (`max_charge_kw`, `max_discharge_kw`) when known."""
        rating = (
            self.facts.max_charge_kw if action == ACTION_GRID_CHARGE
            else self.facts.max_discharge_kw if action == ACTION_EXPORT_DISCHARGE else None
        )
        return rate_kw if rating is None or rating <= 0 else min(rate_kw, rating)

    def _trial_slot(self, now: datetime, trial: Trial, options: gate.Options) -> tuple[str, float, datetime | None]:
        """The test action for this look: its power within the rating and, for a grid charge, the HA-down cap as the
        site reads now (the house's peak changes by the hour, the chargers' currents at any time); plain
        self-consumption while the cap leaves no grid charge."""
        rate = self._bounded_rate(trial.action, trial.rate_kw)
        if trial.action == ACTION_GRID_CHARGE:
            cap = self._within_charge_power(self._charge_limits(now, options)[0])
            if cap is not None and cap < gate.MIN_RATE_KW:
                return (ACTION_SELF_CONSUME, 0.0, trial.until)
            rate = rate if cap is None else min(rate, cap)
        elif trial.action == ACTION_EXPORT_DISCHARGE:
            cap = self._export_cap_kw()
            if cap < gate.MIN_RATE_KW:
                return (ACTION_SELF_CONSUME, 0.0, trial.until)
            rate = min(rate, cap)
        return (trial.action, round(rate, 3), trial.until)

    def _trial_allowed(self, trial: Trial, options: gate.Options) -> bool:
        """Whether a running test action may go on under these options: the opt-in on, and shadow only off unless
        the action was forced (`force_test` overrides shadow only and the verification, nothing else), and never
        while the recipe waits for a person's hardware test (`hardware_test_due`, a pass withdrawn ends it)."""
        return (
            options.enabled and (not options.shadow_only or trial.force) and self.hardware_test_due() is None
            and not self.recipe_blocked()
        )
