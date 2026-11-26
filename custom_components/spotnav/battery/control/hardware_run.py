"""The battery part of a person's hardware test: whether it may start, its start with the dead-man step where the test
times one, its progress and its stop."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .. import gate
from ..commands import (
    GATED_AFTER_RUNNING,
    BrandControl,
    Expiry,
    control_for,
)
from ..plan_model import (
    ACTION_HOLD,
)
from ..verify import (
    TEST_CHARGE_SOC_MAX_PCT,
    quick_for,
    Verification,
    expiry_long,
    expiry_max_s,
)
from .const import (
    NEED_TESTER_SIGNUP,
    _TEST_IGNORES,
)


_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class HardwareRun:
    """A person's hardware test. A mixin of `BatteryControl`, on its state."""

    # ---- a person's hardware test

    def hardware_test_moment(self, now: datetime) -> str | None:
        """Why the battery part of a hardware test should wait now, `None` when the moment suits every step: the
        routine's own rule (`gate.suitable`: a fresh reading, the battery between `SUITABLE_SOC_PCT` and covering the
        house, so a hold and a discharge can be judged and a charge seen; no car charging; the fuse with room; the
        plant SpotNav's to take). The test waits for such a moment by itself: nothing is asked of the person."""
        executor = self._executor()
        if executor is None:
            return "no_battery_control"
        # The test takes the plant over from control itself (`async_hardware_test_start`): a running executor is no
        # reason to wait.
        return gate.suitable(replace(self._moment(now, executor), executor_idle=True))

    def hardware_test_refusal(self) -> str | None:
        """Why the battery part of a hardware test cannot start now, `None` when it can: the opt-in, everything the
        person still has to do (`_needs`: an acknowledgement, a control to enable), a known firmware, nothing else
        running on the plant."""
        if not self._started or self._closed or self._start_wait():
            return "starting"
        if control_for(self.facts.control_brand) is None:
            return "no_battery_control"
        if not self.options().enabled:
            return "not_enabled"
        if self.tester_signup_needed():
            # A recipe never tested here: the person's sign-up as a tester unlocks its first test.
            return NEED_TESTER_SIGNUP
        # The hardware test is what a recipe that writes only after one waits for: never its own refusal.
        needs = [need for need in self._needs() if need not in _TEST_IGNORES]
        if needs:
            return f"needs:{needs[0]}"
        if self.trial is not None:
            return "trial"
        if self.verification is not None and not self.verification.done:
            return "verifying"
        return None

    async def async_hardware_test_start(self) -> str | None:
        """Start the battery part of a person's hardware test (`verify.quick_for`, `stop_on_fail`): hold, the plant's
        own dead-man timed on that hold where the test times one (`_hardware_test_deadman`), a small grid charge under
        the HA-down cap, a discharge for the house, and the hand-back, through the executor with every rule. Answers
        the refusal code, `None` once it started."""
        why = self.hardware_test_refusal()
        if why is not None:
            return why
        async with self._lock:
            # Asked again once the lock is held: what ran meanwhile (a test action asked for during the same look)
            # refuses it as it would a moment later.
            why = self.hardware_test_refusal()
            if why is not None:
                return why
            now = dt_util.now()
            executor = self._executor()
            if executor is None:
                return "no_battery_control"
            deadman = self._hardware_test_deadman(executor.control)
            self.verification = Verification(
                executor, now, firmware=self.facts.firmware,
                modes=(*gate.BATTERY_MODES, *((gate.MODE_EXPIRY,) if deadman is not None else ())),
                tried=gate.variants_tried(self.report()), timing=quick_for(executor.control), stop_on_fail=True,
                charge_soc_max_pct=TEST_CHARGE_SOC_MAX_PCT, expiry=deadman,
                # The dead-man is timed on the hold right after it passed, before the first grid charge.
                expiry_first=deadman is not None,
            )
            self._hw_run = True
            self._plan_wrote = None
            self._verify_key = None
            self.hardware_report = None
            self._log([{"at": now.isoformat(), "kind": "hardware_test_started", "reason": self.firmware_key()}])
            _LOGGER.info("SpotNav battery control: a hardware test started (%s)", self.firmware_key())
            await self._async_look(now)
        return None

    def _hardware_test_deadman(self, control: BrandControl) -> Expiry | None:
        """The dead-man a hardware test times after its hold (`gate.deadman_testable`: one the integration renews and
        SpotNav can silence, EcoFlow's; or, for a recipe that waits for a hardware test, one SpotNav renews in its
        hold, Solis's), `None` for every other recipe."""
        base = control.with_phases(False)
        expiry = base.expiry
        if gate.deadman_testable(expiry, slow=base.slow, fixed_charge=base.fixed_charge):
            return expiry
        # A dead-man SpotNav renews in the hold's own writes (Solis's RC Timeout, renewed by the force call): the
        # test stops renewing it after the hold and times the inverter's fallback, as the routine's mode `expiry`
        # does, so a recipe never run on a plant has its dead-man seen before control relies on it. Its window may be
        # longer than any the gate takes (`verify.expiry_long`): it then lifts no cap, the timing is the evidence.
        # Not for a recipe gated after its control could already run (`GATED_AFTER_RUNNING`): its first pass is the
        # routine's own modes, as its control ran on (SolarEdge's and SolaX's 30-minute timeouts would hold the test
        # that long, and Huawei's hold is a stop, which has no lease).
        hold = base.states.get(ACTION_HOLD)
        window = expiry.window_s
        if (
            base.hardware_test_first and base.brand not in GATED_AFTER_RUNNING
            and hold is not None and not base.slow and not base.fixed_charge
            and expiry.kind in ("device_deadman", "device_lease") and expiry.renewer == "spotnav"
            and window is not None and window > 0
            and set(expiry.renewed_roles) <= {want.role for want in hold(0.0, int(window))}
        ):
            return expiry
        return None

    def hardware_test_deadman_s(self) -> float | None:
        """How long the hardware test's dead-man step may take here (s: the hold until a renewal, the window and its
        slack), `None` where the test times none."""
        control = control_for(self.facts.control_brand)
        expiry = None if control is None else self._hardware_test_deadman(control)
        if expiry is None or expiry.window_s is None:
            return None
        renew = float(expiry.renew_s or 0) if expiry_long(expiry.window_s) else 0.0
        return renew + expiry_max_s(expiry.window_s)

    def hardware_test_deadman(self) -> bool:
        """Whether the battery part of a hardware test here also times the plant's own dead-man (a few minutes
        more: `hardware_test.DEADMAN_EXTRA`)."""
        control = control_for(self.facts.control_brand)
        return control is not None and self._hardware_test_deadman(control) is not None

    async def async_hardware_test_stop(self, reason: str) -> None:
        """End a running hardware test at once and hand the plant back (its deadline, an anomaly elsewhere, an
        unload). Nothing when none runs."""
        async with self._lock:
            if not self._hw_run:
                return
            self._end_verification(dt_util.now())
            if self.executor is not None and self._holding(self.executor):
                await self._async_until_handed_back(reason)
            await self._async_save()

    def hardware_test_progress(self) -> dict[str, Any]:
        """The battery part as the test runner reads it: running or not, the mode and stage now, and the report so
        far (the finished one once done)."""
        verification = self.verification if self._hw_run else None
        current = None if verification is None else verification.current
        executor = self.executor
        return {
            "running": verification is not None,
            "mode": None if current is None else current.mode,
            "stage": None if verification is None else verification.stage,
            "report": verification.report() if verification is not None else self.hardware_report,
            "holding": executor is not None and self._holding(executor),
            "firmware_key": self.firmware_key(),
        }
