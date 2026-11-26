"""The expiry test's part of the executor (`ExecInputs.expiry_probe`): nothing is renewed for its span, a control with
a silence (`Expiry.silence`) is silenced and taken back, and the device's documented fallback is told from another
writer's value."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from ..commands import Native
from ..plan_model import ACTION_DISCHARGE_HOUSE, ACTION_GRID_CHARGE, ACTION_HOLD
from .helpers import _event
from .state import ExecInputs, ExecState


class ExpiryProbe:
    """The expiry test: its silence and the take-back after it. A mixin of `Executor`, on its state and control."""

    def _silence_in_target(self) -> bool:
        """Whether the target still holds a silence's values (`Expiry.silence`)."""
        target = self.state.target
        return any(name in target and target[name] == value for name, value in self.control.expiry.silence)

    def _can_retake(self) -> bool:
        """Whether a silence of this control can be undone by its own discharge to the house (`retake`)."""
        return bool(self.control.expiry.silence) and self.control.supports(ACTION_DISCHARGE_HOUSE)

    def _retake_target(self, state: ExecState) -> dict[str, Native]:
        """A hand-back's first stage after a silence (`ExecState.retake`): the discharge to the house, written in the
        enter order (EcoFlow: Modbus Control on, then Automatic), with no role idle counted as done."""
        wanted = self._wanted(ACTION_DISCHARGE_HOUSE, 0.0, state.restore_values)
        return {name: value for name, value in wanted.items() if name not in state.given_up}

    def _retaken(self, inputs: ExecInputs) -> None:
        """A silence undone while the action runs: once every silenced role reads the action's value again, nothing
        is owed for it (`ExecState.retake`)."""
        state = self.state
        if not state.retake or self.silenced or state.phase != "active":
            return
        for name, _value in self.control.expiry.silence:
            wanted = state.target.get(name)
            if wanted is None or not self._satisfied(
                self.control.role(name), wanted, inputs.reads.get(name), restoring=False
            ):
                return
        self.state = replace(state, retake=False)

    def _fallback_values(self) -> dict[str, str]:
        """The values the device documents its renewed roles fall back to (`Expiry.fallback_values`), the control's
        own and, on a phased control, the phase spec's."""
        spec = self.control.phase_control
        pairs = (*self.control.expiry.fallback_values, *(
            spec.expiry.fallback_values if self.control.phased and spec is not None else ()
        ))
        return dict(pairs)

    def _probing(self, inputs: ExecInputs) -> bool:
        """Whether the verification's expiry test runs on the active command (`ExecInputs.expiry_probe`): only on a
        hold or a discharge per phase (every phase's target at most 0), so a command left unrenewed, and a fallback,
        can never add import. On anything else the flag is ignored and the command runs (and is renewed) as ever."""
        state = self.state
        if not inputs.expiry_probe or state.phase != "active":
            return False
        if state.action == ACTION_HOLD and state.phase_kw is None:
            return True
        return state.action == ACTION_GRID_CHARGE and state.phase_kw is not None and max(state.phase_kw) <= 0.0

    def _silence(self, now: datetime) -> list[dict[str, Any]]:
        """In the expiry test, for a dead-man an integration renews (`Expiry.silence`): once, the writes that stop
        that renewal go into the target (EcoFlow: Modbus Control off), every other role left as written. A role that
        goes idle with them (`Role.idle_when`) leaves the target and is given back to the ledger: it reads nothing
        until the silence ends, and then whatever the integration shows, which is no one else's change."""
        silence = self.control.expiry.silence
        if self.silenced or not silence:
            return []
        state = self.state
        target = dict(state.target)
        for name, value in silence:
            target[name] = value
        for role in self.control.roles:
            if not role.idle_when or role.name in target and any(name == role.name for name, _value in silence):
                continue
            # Idle with the silence: neither written nor judged until it ends.
            target.pop(role.name, None)
            entity_id = state.entities.get(role.name)
            if entity_id is not None:
                self.ledger.release(entity_id)
        self.silenced = True
        self.state = replace(state, target=target, sent=(), retake=self._can_retake())
        return [_event(now, "silenced", ",".join(name for name, _value in silence))]

    def _unsilence(self) -> None:
        """The expiry test's silence is over: a running action wants its own state again (the next looks write it
        back, the silenced roles first in the brand's order), whatever the test found."""
        self.silenced = False
        state = self.state
        if state.phase != "active" or state.action is None:
            return
        self.state = replace(state, sent=(), target=self._wanted(
            state.action, state.rate_kw, state.restore_values, state.phase_kw, house_kw=state.house_kw,
            behind=state.phase_behind_kw,
        ))
