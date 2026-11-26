"""The order the executor writes in: which roles lead a change of action (`mode_limits`, `rising_first`, `peak_first`),
the next role owed in the brand's enter or restore order with renewals last, a limit that waits for its mode, and
the end of a priming action (`prime`)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from ..commands import Native, reading
from ..plan_model import ACTION_GRID_CHARGE
from .helpers import _event
from .state import ExecInputs, ExecState
from .values import _rises, restore_order


class Ordering:
    """The order of the writes. A mixin of `Executor`, on its state and control."""

    def _lead(
        self, previous: str | None, before: Mapping[str, Native] | None = None,
        after: Mapping[str, Native] | None = None,
    ) -> tuple[str, ...]:
        """The roles a change of action away from `previous` writes first: a grid charge's stop (its `peak_first`
        roles; Sigenergy: the control mode), so a limit the next action loosens never acts in the grid charge's mode
        (the charge limit back at the rating in Command Charging would charge from the grid at full power). A button
        is never moved ahead of the values it sends. Where the recipe names its mode limits (`mode_limits`), the
        order is the whole of it: those that fall from `before` to `after` first, then the stop and the brand's
        order, then those that rise."""
        stop: tuple[str, ...] = ()
        if previous == ACTION_GRID_CHARGE:
            first = self.control.peak_first.get(previous, ())
            stop = tuple(name for name in first if self.control.role(name).method != "button")
        if not self.control.mode_limits or before is None or after is None:
            return stop

        def moved(name: str) -> float:
            old, new = before.get(name), after.get(name)
            if any(isinstance(value, bool) or not isinstance(value, int | float) for value in (old, new)):
                return 0.0
            return float(new) - float(old)  # type: ignore[arg-type]

        limits = [name for name in self.control.mode_limits if name in after]
        falling = tuple(name for name in limits if moved(name) < 0)
        rising = tuple(name for name in limits if moved(name) > 0)
        middle = tuple(dict.fromkeys(
            name for name in (*stop, *self.control.enter_order) if name not in falling and name not in rising
        ))
        return (*falling, *middle, *rising)

    def _end_priming(self, inputs: ExecInputs, action: str) -> list[dict[str, Any]]:
        """A priming action (`BrandControl.prime`) whose writes are all read back takes its own values."""
        state = self.state
        if not (state.priming and state.pending is None and self._next_role(state, inputs) is None):
            return []
        # Primed: the switch is on and the role that was unsure reads what this action wrote; now its own values.
        self.state = state = replace(state, priming=False, unsure=())
        self.state = state = replace(
            state, sent=(),
            target=self._wanted(
                state.action or action, state.rate_kw, state.restore_values, state.phase_kw,
                house_kw=state.house_kw, behind=state.phase_behind_kw,
            ),
        )
        return [_event(inputs.now, "primed", state.action or action)]

    def _next_role(self, state: ExecState, inputs: ExecInputs) -> str | None:
        """The first role, in the brand's order, that does not read what it should (or must be renewed)."""
        restoring = state.phase == "restoring"
        retaking = restoring and state.retake
        if retaking:
            # After a silence: the plant taken back in the enter order (the switch before the roles it makes
            # available), no idle role counted as done (`ExecState.retake`).
            order = self.control.enter_order
        elif restoring:
            order = restore_order(self.control, state.action)
        else:
            order = (*state.lead, *(name for name in self.control.enter_order if name not in state.lead))
            if self.control.rising_first:
                # A limit that rises goes ahead of one that falls (after a change's stop): an enabled pair never
                # sums below 0 on the way (`BrandControl.rising_first`).
                rising = tuple(
                    name for name in self.control.rising_first
                    if name not in state.lead and _rises(state.target.get(name), inputs.reads.get(name))
                )
                order = (*state.lead, *rising, *(name for name in order if name not in state.lead and name not in rising))
        # A role whose value must change goes before a role that is only renewed: renewals never hold back a write
        # the action owes (an action that flips at every look must still write its command, and be counted).
        renewals: list[str] = []
        for name in order:
            if name not in state.target or name in state.given_up or name in state.unrestored or name in state.in_flight:
                continue
            if not restoring and name in self.expired and not self._covering(state, name):
                # Expired in the running expiry test: writing it would renew the command the test watches end.
                continue
            retry = state.retries.get(name)
            if retry is not None and inputs.now < retry[1]:
                continue
            entity_id = state.entities.get(name)
            claim = None if entity_id is None else self.ledger.claim(entity_id)
            if claim is not None and claim.owned is not None and claim.intended == state.target[name]:
                # Written and read back: a later difference is the ledger's to judge (another system, or a
                # gap), never a reason to write it again.
                if name in state.renew:
                    renewals.append(name)
                continue
            if not self._satisfied(
                self.control.role(name), state.target[name], inputs.reads.get(name), restoring=restoring and not retaking
            ):
                return name
            if name in state.renew:
                renewals.append(name)
        return renewals[0] if renewals else None

    def _limit_waits(self, state: ExecState, inputs: ExecInputs, name: str) -> bool:
        """In a hand-back, whether a limit that acts in the plant's modes (`BrandControl.mode_limits`) must wait before
        it is raised: until every role the hand-back writes before it (the mode, the switch) reads back what it
        should (a mode idle once the switch is off counts: the plant is out of its Command modes). A fuse hand-back
        writes its roles in one look; were the mode write lost or given up while the
        raised limit landed, Sigenergy would sit in Command Charging (PV First) at its full charge limit just as the
        fuse is closest. Waiting, the limit stays where the hold left it (0) and the next look tries again."""
        if name not in self.control.mode_limits:
            return False
        role = self.control.role(name)
        wanted = state.target[name]
        read = inputs.reads.get(name)
        now_read = reading(role, None if read is None else read.state)
        if (
            isinstance(wanted, bool) or not isinstance(wanted, (int, float))
            or (isinstance(now_read, (int, float)) and not isinstance(now_read, bool) and wanted <= now_read)
        ):
            return False  # not raised: lowering a limit never waits
        for before in restore_order(self.control, state.action):
            if before == name:
                return False
            if before in self.control.mode_limits or before not in state.target:
                continue
            if not self._satisfied(
                self.control.role(before), state.target[before], inputs.reads.get(before), restoring=True
            ):
                if (before in state.unrestored or before in state.given_up) and name not in state.unrestored:
                    # The role it waits for will not be written again here: the limit is left where it is (low) and
                    # counted as not handed back, so the hand-back ends and the person is told what to set by hand.
                    self.state = replace(self.state, unrestored=(*self.state.unrestored, name))
                return True
        return False
