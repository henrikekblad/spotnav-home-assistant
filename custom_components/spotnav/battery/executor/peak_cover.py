"""The peak cover under a hold (`peak_first`): the hold is lifted when the fuse's headroom falls, handed back in full
when it does not recover, and held again later."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..plan_model import ACTION_HOLD
from .const import PEAK_COVER_A, PEAK_RECOVER_S, PEAK_REHOLD_S
from .fuse import _own_import_a, hold_headroom_ok
from .helpers import _event
from .state import ExecInputs, ExecState


class PeakCover:
    """The peak cover under a hold. A mixin of `Executor`, on its state, control and monitor."""

    def _covering(self, state: ExecState, name: str) -> bool:
        """Whether `name` is a write of a lifted hold (never held back: the battery must cover the peak)."""
        return state.covered_at is not None and name in self.control.peak_first.get(state.action or "", ())

    def _cover(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """Under a hold: lift it when the headroom falls under `PEAK_COVER_A`, hand back in full when it does not
        recover within `PEAK_RECOVER_S`, hold again after `PEAK_REHOLD_S` when the start rule allows it."""
        state = self.state
        now = inputs.now
        # What the plant may import by itself now comes off the room the peak cover reads.
        headroom = None if inputs.headroom_a is None else inputs.headroom_a - _own_import_a(inputs)
        first = self.control.peak_first.get(ACTION_HOLD, ())
        if self.control.slow or state.action != ACTION_HOLD or inputs.action != ACTION_HOLD or headroom is None or not first:
            return []
        if state.covered_at is None:
            if headroom >= PEAK_COVER_A:
                return []
            target = dict(state.target)
            for name in first:
                value = self._normal_value(name)
                if value is not None and name in target:
                    target[name] = value
            # The battery is meant to discharge now: the hold's monitor would read that as a hold not taken.
            self.monitor.stop(now)
            self.state = replace(
                state, target=target, covered_at=now, peak_low_since=now,
                sent=tuple(name for name in state.sent if name not in first),
            )
            return [_event(now, "peak_cover", ACTION_HOLD, headroom_a=round(headroom, 2), roles=list(first))]
        if headroom < PEAK_COVER_A:
            since = state.peak_low_since or now
            if (now - since).total_seconds() >= PEAK_RECOVER_S:
                return self._begin_restore(now, "fuse_headroom")
            if state.peak_low_since is None:
                self.state = replace(state, peak_low_since=since)
            return []
        if state.peak_low_since is not None:
            self.state = state = replace(state, peak_low_since=None)
        if (now - state.covered_at).total_seconds() >= PEAK_REHOLD_S and hold_headroom_ok(inputs):
            self.state = replace(
                state, target=self._wanted(ACTION_HOLD, 0.0, state.restore_values), covered_at=None,
                sent=tuple(name for name in state.sent if name not in first),
            )
            self._watch(inputs, ACTION_HOLD, 0.0)
            return [_event(now, "peak_rehold", ACTION_HOLD, headroom_a=round(headroom, 2))]
        return []
