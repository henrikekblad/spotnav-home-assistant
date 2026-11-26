"""The battery control's write path: one step of an executor with its state stored before its writes, each write as a
service call behind the gate, the hand-back stepped until it ends or its deadline, and the same for the export limit's
instance."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime

from homeassistant.util import dt as dt_util

from ..commands import service_call
from ..executor import ExecInputs, Executor, Op, StepResult, taken_over
from .const import HAND_BACK_DEADLINE_S, HAND_BACK_LOOK_S
from .io import _async_service, _error_text, _failure, resolve_follows_max

_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class WritePath:
    """The steps, the writes and the hand-back. A mixin of `BatteryControl`, on its state."""

    async def async_tick(self, now: datetime | None = None, *, stopping: str | None = None) -> StepResult | None:
        """One look of control as it runs the plan: refused unless control is on (the routine passed)."""
        self._look_id += 1
        options = self.options()
        self._require(self.state(options) == "on", "Battery control")
        now = now or dt_util.now()
        if stopping is None:
            await self.export.async_look(now, options)
        else:
            await self._async_export_step(now, None, stopping=stopping, allowed=True)
        executor = self._control_executor()
        if executor is None:
            return None
        slot = None if stopping is not None else self._control_slot(now, options)
        return await self._async_step(
            executor, self.inputs(now, executor.control, stopping=stopping, slot=slot), allowed=True
        )

    async def async_export_tick(self, now: datetime | None = None, *, stopping: str | None = None) -> StepResult | None:
        """One look of the export limit as its own switch and gates run it (`ExportShell.async_look`; a `stopping`
        hands it back). Refused unless they allow writing."""
        self._require(self.export.state() == "on", "The export limit")
        now = now or dt_util.now()
        if stopping is not None:
            return await self._async_export_step(now, None, stopping=stopping, allowed=True)
        await self.export.async_look(now, self.options())
        return None

    async def _async_export_step(
        self, now: datetime, action: str | None, *, stopping: str | None = None, allowed: bool
    ) -> StepResult | None:
        executor = self._export_executor()
        if executor is None:
            return None
        return await self._async_export_raw(now, executor, action, stopping=stopping, allowed=allowed)

    async def _async_export_raw(
        self, now: datetime, executor: Executor, action: str | None, *, stopping: str | None, allowed: bool
    ) -> StepResult:
        if stopping is not None or not allowed:
            inputs = self._export_hand_back_inputs(now, executor, stopping)
        else:
            inputs = self.export_inputs(now, executor.control, stopping=stopping, action=action)
        inputs, allowed = self._gated(inputs, allowed, export=True)
        result = executor.step(inputs)
        self._log(result.events)
        if result.persist_first:
            await self._async_save()
        self._export_failed, self._export_timed_out = await self._async_writes(result, executor, allowed=allowed)
        await self._async_save()
        return result

    async def _async_export_until_handed_back(self, reason: str) -> None:
        executor = self.export_executor
        assert executor is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HAND_BACK_DEADLINE_S
        stopping: str | None = reason
        while True:
            result = await self._async_export_raw(dt_util.now(), executor, None, stopping=stopping, allowed=False)
            stopping = None if executor.state.phase == "restoring" else stopping
            if executor.state.phase in ("idle", "blocked") or loop.time() >= deadline:
                return
            await asyncio.sleep(0 if result.ops else HAND_BACK_LOOK_S)

    def _export_writes_today(self, now: datetime) -> int:
        executor = self.export_executor
        if executor is None or executor.state.day != now.date().isoformat():
            return 0
        return executor.state.writes_today

    # ---- writes

    def _writes_allowed_now(self, *, export: bool = False) -> bool:
        """Whether the options as stored now still allow a write that is not a hand-back (read again before every
        step: the entry can change while a look waits on a write). `export`: for the export limit, whose own switch
        and gates decide (`ExportShell.state`), or a test action of its own."""
        options = self.options()
        trial = self.trial
        if export:
            return self.export.state(options) == "on" or (
                trial is not None and trial.export and self._trial_allowed(trial, options)
            )
        return (
            options.writes or (trial is not None and self._trial_allowed(trial, options))
            or (self._hw_run and options.enabled)
        )

    def _gated(self, inputs: ExecInputs, allowed: bool, *, export: bool = False) -> tuple[ExecInputs, bool]:
        """A step the options no longer allow, or one during the start-up wait, becomes a hand-back."""
        if allowed and self._start_wait():
            return replace(
                inputs, stopping=inputs.stopping or "start_wait", action=None, rate_kw=0.0, slot_end=None
            ), False
        if not allowed or self._writes_allowed_now(export=export):
            return inputs, allowed
        return replace(
            inputs, stopping=inputs.stopping or "options_changed", action=None, rate_kw=0.0, slot_end=None
        ), False

    async def _async_step(self, executor: Executor, inputs: ExecInputs, *, allowed: bool) -> StepResult:
        inputs, allowed = self._gated(inputs, allowed)
        result = executor.step(inputs)
        self._failed = self._timed_out = ()
        self._log(result.events)
        self._log_expiry(executor, result.events)
        if any(event["kind"] == "enter" for event in result.events) and taken_over(inputs.facts, executor.control):
            # Taken over from the plant's own optimiser: turning control off reminds the person to set it again.
            self.took_from_ai = True
        self._withdraw_per_phase(result, inputs.now)
        if result.persist_first:
            # The snapshot is on disk before the first write, so a crash in between still restores it.
            await self._async_save()
        self._failed, self._timed_out = await self._async_writes(result, executor, allowed=allowed)
        await self._async_save()
        return result

    async def _async_until_handed_back(self, reason: str) -> None:
        executor = self.executor
        assert executor is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HAND_BACK_DEADLINE_S
        stopping: str | None = reason
        while True:
            now = dt_util.now()
            result = await self._async_step(executor, self._hand_back_inputs(now, executor, stopping), allowed=False)
            stopping = None if executor.state.phase == "restoring" else stopping
            if executor.state.phase in ("idle", "blocked") or loop.time() >= deadline:
                return
            await asyncio.sleep(0 if result.ops else HAND_BACK_LOOK_S)

    async def _async_writes(
        self, result: StepResult, executor: Executor, *, allowed: bool
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """A step's writes in order: the roles not taken (the next look's `failed`), and those of them whose call was
        given up on (its `timed_out`)."""
        failed: list[str] = []
        timed_out: list[str] = []
        for op in result.ops:
            why = await self._async_write(op, executor, allowed=allowed)
            if why is not None:
                failed.append(op.role)
                if why == "call_timeout":
                    timed_out.append(op.role)
        return tuple(failed), tuple(timed_out)

    async def _async_write(self, op: Op, executor: Executor, *, allowed: bool) -> str | None:
        """One write; why it was not taken (refused, or the service call raised or was given up on: the executor
        counts it as not taken), `None` when it was. A write that is not a restore is refused unless the caller's
        gate allows it: a hand-back never is."""
        if not op.restore and not allowed:
            _LOGGER.warning("SpotNav battery control: a write of %s was refused by the gate", op.role)
            self._log([{"at": dt_util.now().isoformat(), "kind": "refused", "reason": op.role}])
            return "refused"
        role = executor.control.role(op.role)
        resolved = resolve_follows_max(self.hass, op)
        if resolved is None:
            _LOGGER.warning("Battery control: %s shows no maximum to follow; it was not written", op.role)
            return "service_failed"
        op = resolved
        domain, service, data = service_call(
            op.method, op.entity_id, op.value, device_id=op.device_id,  # type: ignore[arg-type]
            services=role.services, watchdog_s=executor.watchdog_s, device_field=role.device_field,
            duration=role.lease, power_field=role.power_field,
            duration_field=role.duration_field,
        )
        try:
            await _async_service(self.hass, domain, service, data)
        except Exception as err:  # noqa: BLE001 - any error (a Modbus library's too, a call given up on) is a write
            # not taken; the state must still be stored and a stop or unload must still hand back.
            _LOGGER.warning("Battery control: %s.%s for %s failed: %s", domain, service, op.role, _error_text(err))
            self._log([{"at": dt_util.now().isoformat(), "kind": "service_failed", "reason": op.role,
                        "error": _error_text(err)}])
            return _failure(err)
        return None
