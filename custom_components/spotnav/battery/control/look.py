"""One look of the battery control: what runs now in its order (a withdrawn takeover, a test action, the verification,
control or the release, then the export limit and the shadow), the slot control runs and the hand-back after looks that
failed."""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .. import gate
from ..commands import (
    ACTION_CURTAIL_EXPORT,
    control_for,
    fixed_rate_kw,
)
from ..executor import (
    Executor,
    StepResult,
    preview,
    taken_over,
)
from ..plan_model import (
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_SELF_CONSUME,
)
from ..plan_shadow import slot_at
from ..runtime import read_role_reads
from .const import (
    FAILED_LOOKS_HAND_BACK,
    NOTE_AI_MODE_BACK,
    RULE_CHARGE_POWER_ENTITY,
    _PLAN_ACTIONS,
)


_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class Look:
    """The look and what it runs. A mixin of `BatteryControl`, on its state."""

    # ---- one look

    async def async_look(self, now: datetime | None = None) -> None:
        """One look (see the module). Never raises into the site."""
        if self._closed or not self._started:
            return
        async with self._lock:
            now = now or dt_util.now()
            try:
                await self._async_look(now)
            except Exception:  # noqa: BLE001 - the battery control must never disturb the site; it looks again
                self._look_failed("look")
                self._failed_looks += 1
                if self._failed_looks >= FAILED_LOOKS_HAND_BACK:
                    await self._async_hand_back_after_failures(now)
            else:
                self._failed_looks = 0

    async def _async_hand_back_after_failures(self, now: datetime) -> None:
        """Looks keep failing while SpotNav may hold the plant: hand back what it holds on the fewest reads (one
        step per look), so a part that keeps raising cannot leave a command standing."""
        if not self._holds_anything():
            return
        self.trial = None
        with contextlib.suppress(Exception):
            self._end_verification(now)
        try:
            if self.executor is not None and self._holding(self.executor):
                await self._async_step(
                    self.executor, self._minimal_inputs(now, self.executor, "look_failed"), allowed=False
                )
            if self.export_executor is not None and self._holding(self.export_executor):
                await self._async_export_raw(now, self.export_executor, None, stopping="look_failed", allowed=False)
        except Exception:  # noqa: BLE001 - tried again at the next failed look
            self._look_failed("hand_back_after_failures")

    @staticmethod
    def _holding(executor: Executor) -> bool:
        return executor.state.phase in ("active", "restoring") or executor.state.holds_snapshot

    def _holds_anything(self) -> bool:
        return any(executor is not None and self._holding(executor) for executor in (self.executor, self.export_executor))

    def _look_failed(self, kind: str) -> None:
        if kind in self._look_failures:
            _LOGGER.debug("SpotNav battery control: the %s failed again", kind, exc_info=True)
            return
        self._look_failures.add(kind)
        _LOGGER.warning("SpotNav battery control: the %s failed; it is tried again at each look", kind, exc_info=True)

    async def _async_look(self, now: datetime) -> None:
        self._look_id += 1
        # The facts first: the brand read decides shadow only's default (`shadow_default`).
        self.facts = self._read_facts()
        self._ha_down_setup_look()
        self._grandfather_running()
        self._void_blocked_passes()
        options = self.options()
        self._note_key(now)
        await self._async_read_sources(now)
        self._transitions(options, now)
        self._preheat_look(now, options)
        trial = self.trial
        withdrawn = self._takeover_withdrawn(options)
        if trial is not None and (
            now >= trial.until or not self._trial_allowed(trial, options) or (withdrawn and not trial.export)
        ):
            self._log([{"at": now.isoformat(), "kind": "trial_ended", "reason": trial.action}])
            self.trial = trial = None
        if self._start_wait():
            await self._async_start_wait_look(now, options)
            return
        # The battery.
        if withdrawn:
            # The person withdrew the acknowledgement after SpotNav took the plant over from AI mode: handed back,
            # and nothing more until AI mode is seen again or the acknowledgement is given again.
            if self.verification is not None and not self.verification.done:
                self._end_verification(now)
            await self._async_release(now, "ai_takeover_withdrawn")
        elif trial is not None and not trial.export:
            self._plan_wrote = None  # what is held is no longer the plan's alone
            await self._async_battery_step(now, self._trial_slot(now, trial, options), allowed=True)
        elif self.verification is not None and not self.verification.done:
            if options.writes or (self._hw_run and options.enabled):
                # A person's hardware test runs under shadow only too (as a forced test action does); never without
                # the opt-in.
                await self._async_verify(now)
            else:
                self._end_verification(now)
                await self._async_release(now, "disabled")
        else:
            state = self.state(options)
            if state == "on":
                aside = self._plan_aside(now, options)
                slot = None if aside else self._control_slot(now, options)
                self._credit_plan(await self._async_battery_step(now, slot, allowed=True), now)
            else:
                self._plan_wrote = None
                await self._async_release(now, "shadow_only" if state == "shadow_only" else f"control_{state}")
            if options.writes and trial is None:
                await self._async_maybe_verify(now, options)
        # The export limit: its own switch and gates (`export_shell.py`), whatever the battery's options say.
        if trial is not None and trial.export:
            await self._async_export_step(now, ACTION_CURTAIL_EXPORT, allowed=True)
        else:
            await self.export.async_look(now, options)
        if options.enabled and options.shadow_only:
            self._shadow_look(now, options)
        self._note_stopped(now)
        await self._async_save()

    def _start_wait(self) -> bool:
        """Whether the start-up wait runs (`runtime.START_GRACE`): right after a start a battery planner may not show
        its states yet, so neither the write path nor the verification starts; only hand-backs are written. Without
        the site's battery shadow (never in a running site) there is none to wait for."""
        battery_shadow = getattr(self.site, "battery_shadow", None)
        start_wait = getattr(battery_shadow, "start_wait", None)
        return bool(start_wait()) if callable(start_wait) else False

    async def _async_start_wait_look(self, now: datetime, options: gate.Options) -> None:
        """A look during the start-up wait: whatever is held is handed back, nothing else is written."""
        if self.verification is not None and not self.verification.done:
            self._end_verification(now)
        await self._async_release(now, "start_wait")
        await self._async_export_release(now, "start_wait")
        self.waiting_for = "start_wait" if self.state(options) in ("waiting", "on") else None
        if options.enabled and options.shadow_only:
            self._shadow_look(now, options)
        self._note_stopped(now)
        await self._async_save()

    def _preheat_look(self, now: datetime, options: gate.Options) -> None:
        """The preheating line (`preheat.PreheatWatch`) while SpotNav runs the battery; logged when it shows or
        clears. Read only: nothing is written to the heater."""
        controlling = self.state(options) in ("on", "verifying") or self.trial is not None
        change = self.preheat.look(now, self.facts, controlling)
        if change is not None:
            self._log([{"at": now.isoformat(), "kind": f"preheat_{change[0]}", "reason": change[1],
                        "cell_c": self.facts.cell_temperature_c}])

    def _takeover_withdrawn(self, options: gate.Options) -> bool:
        return self.took_from_ai and not options.ai_takeover_ack

    def _note_stopped(self, now: datetime) -> None:
        """Control stopped running (out of on or verifying, a test action ended) on a plant SpotNav took over from its
        own optimiser: the reminder to set AI mode again, whatever the path (turned off, back to shadow only, a failed
        check, a firmware change, the acknowledgement withdrawn)."""
        options = self.options()
        running = not self._takeover_withdrawn(options) and (
            self.trial is not None or self.state(options) in ("on", "verifying")
        )
        if self._running and not running and self.took_from_ai:
            self._add_note(NOTE_AI_MODE_BACK, now)
        self._running = running

    def _transitions(self, options: gate.Options, now: datetime) -> None:
        """The opt-in turned on (blocks lifted, a failed check forgotten so it runs again, the reminder dropped) or
        off; the plant seen in AI mode again (the reminder done, the takeover forgotten)."""
        if options.enabled and not self._enabled_seen:
            for executor in (self.executor, self.export_executor):
                if executor is not None:
                    executor.reset()
            key = self.firmware_key()
            if gate.report_status(self.reports.get(key)) == "failed":
                # The check runs again; what passed before this failure is not trusted meanwhile.
                self.reports.pop(key, None)
                self.carry_after = now.isoformat()
            self.notes = [note for note in self.notes if note.get("code") != NOTE_AI_MODE_BACK]
            self._log([{"at": now.isoformat(), "kind": "enabled", "reason": "opt_in"}])
        elif not options.enabled and self._enabled_seen:
            self._log([{"at": now.isoformat(), "kind": "disabled", "reason": "opt_out"}])
        self._enabled_seen = options.enabled
        control = control_for(self.facts.control_brand)
        executor = self.executor
        handed_back = executor is None or (executor.state.phase in ("idle", "blocked") and not executor.state.holds_snapshot)
        if control is not None and handed_back and taken_over(self.facts, control):
            # The person set AI mode again (not a takeover still being written): the reminder is done, and the plant
            # is no longer SpotNav's takeover.
            self.notes = [note for note in self.notes if note.get("code") != NOTE_AI_MODE_BACK]
            self.took_from_ai = False

    async def _async_release(self, now: datetime, reason: str) -> None:
        """Hand back whatever the battery's executor holds (only restores are written)."""
        executor = self.executor
        self._plan_wrote = None
        if executor is None or (executor.state.phase in ("idle", "blocked") and not executor.state.holds_snapshot):
            return
        await self._async_step(executor, self._hand_back_inputs(now, executor, reason), allowed=False)

    async def _async_export_release(self, now: datetime, reason: str) -> None:
        executor = self.export_executor
        if executor is None or (executor.state.phase in ("idle", "blocked") and not executor.state.holds_snapshot):
            return
        await self._async_export_raw(now, executor, None, stopping=reason, allowed=False)

    async def _async_battery_step(
        self, now: datetime, slot: tuple[str, float, datetime | None] | None, *, allowed: bool
    ) -> StepResult | None:
        executor = self._control_executor()
        if executor is None:
            return None
        return await self._async_step(executor, self.inputs(now, executor.control, slot=slot), allowed=allowed)

    # ---- shadow only

    def _shadow_look(self, now: datetime, options: gate.Options) -> None:
        """While shadow only: what control would do now (`executor.preview`), logged when it changes."""
        control = control_for(self.facts.control_brand, self.hold_variant())
        if control is None:
            return
        # As control would run it: the brand's targets per phase where they are there and verified.
        control = control.with_phases(self._phased_wanted())
        allowed = _PLAN_ACTIONS | ({ACTION_EXPORT_DISCHARGE} if options.allow_sell else set())
        slot, why = gate.restricted(
            self._merged_slot(now), allowed, self._charge_limits(now, options, phased=control.phased)[0],
            self._export_cap_kw(), self._fixed_charge_kw(), hold_charges=self._hold_charges(),
        )
        action = None if slot is None else slot[0]
        if action in (None, ACTION_SELF_CONSUME):
            seen = None
            signature: tuple[Any, ...] = (action, None, why)
        else:
            assert slot is not None
            seen = preview(control, self.inputs(now, control, slot=slot))
            signature = (action, seen.blocker, why, tuple((op.role, op.value) for op in seen.writes))
        if signature == self._would_do:
            return
        self._would_do = signature
        self._log([{
            "at": now.isoformat(), "kind": "would_do", "reason": action or "nothing", "why": why,
            "blocker": None if seen is None else seen.blocker,
            "rate_kw": None if slot is None else slot[1],
            "writes": [] if seen is None else [op.as_dict() for op in seen.writes],
        }])

    # ---- what control runs

    def _merged_slot(self, now: datetime) -> tuple[str, float, datetime | None] | None:
        """The plan's running slot with the hold for the car (`gate.merged_action`)."""
        control = control_for(self.facts.control_brand)
        # A slow control (a cloud) runs planned actions only: no hold for a car, which comes and goes in minutes.
        hold_requested = self.site.battery_shadow.hold_requested() and not (control is not None and control.slow)
        return gate.merged_action(self._plan_now(now), hold_requested)

    def _fixed_charge_kw(self) -> float | None:
        """The rate a plant charges at when the recipe cannot set it (`commands.fixed_rate_kw`), `None` otherwise."""
        control = control_for(self.facts.control_brand)
        return None if control is None else fixed_rate_kw(control, self.facts)

    def _hold_charges(self) -> bool:
        """Whether a hold on this plant may itself charge from the grid at its fixed rate: a role whose absence lets it
        (`BrandControl.hold_charges_without`) is not found."""
        control = control_for(self.facts.control_brand)
        if control is None or not control.hold_charges_without:
            return False
        reads = read_role_reads(self.hass, self.site.config, control)
        return any((read := reads.get(name)) is None or not read.found for name in control.hold_charges_without)

    def _control_slot(self, now: datetime, options: gate.Options) -> tuple[str, float, datetime | None] | None:
        report = self.control_report()
        allowed = self._supported(gate.allowed_actions(report, options, export_needs_charge=self._export_needs_charge()))
        fuse_cap, caps = self._charge_limits(now, options)
        cap = self._within_charge_power(fuse_cap)
        if caps is None and gate.charge_by_phases(report):
            # The charge was shown only per phase: the balanced one is never run on that.
            allowed = allowed - {ACTION_GRID_CHARGE}
        slot, why = gate.restricted(
            self._merged_slot(now), allowed, cap, self._export_cap_kw(), self._fixed_charge_kw(),
            hold_charges=self._hold_charges(),
        )
        if why == "grid_charge_cap" and cap is not None and (fuse_cap is None or cap < fuse_cap):
            why = RULE_CHARGE_POWER_ENTITY  # the battery's own charge power entity, not the fuse
        if why != self._restricted:
            # Logged when it changes: an action the report does not allow, or the grid charge cap.
            self._restricted = why
            if why is not None:
                self._log([{"at": now.isoformat(), "kind": "restricted", "reason": why,
                            "rate_kw": None if slot is None else slot[1]}])
        return slot

    def _plan_now(self, now: datetime) -> tuple[str, float, datetime | None] | None:
        if self._plan_source is not None:
            return self._plan_source(now)
        plan = self.site.battery_shadow.plan
        slot = slot_at(plan.plan, now)
        return None if slot is None else (slot.action, abs(slot.battery_kw), slot.end)
