"""The executor's renewals of a command that expires (`Expiry.renewed_roles`): the shared quarter-hour round and a fast
watchdog's own (`Expiry.renew_s`, `renewal_cap`), the roles renewed with a change of action, and a grid charge
commanded in blocks (`charge_block_s`)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..commands import WATCHDOG_RENEW_S, Native, Role
from ..plan_model import ACTION_GRID_CHARGE
from .const import RENEW_ON_CHANGE_GAP_S
from .helpers import _event
from .state import ExecInputs, ExecState, RoleRead


class Renewal:
    """The watchdog's renewals. A mixin of `Executor`, on its state and control."""

    def _renew_on_change(
        self, state: ExecState, target: Mapping[str, Native], now: datetime
    ) -> tuple[tuple[str, ...], datetime | None]:
        """The roles renewed with a change of action to `target`, and the renewal's time from it.
        The renewal counts from this change, so a watchdog role the new action leaves as it was (SolarEdge's
        timeout) is written again with the new command: with an action change every quarter hour it would
        otherwise never be renewed after the start. The roles it changes are written anyway, and counted; so is
        a button, pressed once per target (SolaX's trigger sends the whole new command).
        At most once a `RENEW_ON_CHANGE_GAP_S` (an action that flips at every look would otherwise renew at
        every look, uncounted): a change sooner keeps the last renewal's time, so the shared timer still
        renews `WATCHDOG_RENEW_S` after it."""
        unchanged = () if self._fast_renewal() else tuple(
            name for name in self.control.watchdog_roles
            if name in target and state.target.get(name) == target[name]
            and self.control.role(name).method != "button"
        )
        due = state.watchdog_at is None or (now - state.watchdog_at).total_seconds() >= RENEW_ON_CHANGE_GAP_S
        # Within the gap, a renewal the timer already owed (and `watchdog_at` records as done) is kept.
        renew = unchanged if due else tuple(name for name in state.renew if name in unchanged)
        watchdog_at = state.watchdog_at if unchanged and not due else now
        return renew, watchdog_at

    def _renewal(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A watchdog's renewal falling due: the shared round, or a fast watchdog's own (handed back when the day's
        renewals are spent)."""
        now = inputs.now
        events: list[dict[str, Any]] = []
        state = self.state
        if not self._fast_renewal():
            # A watchdog on the shared `WATCHDOG_RENEW_S` (SolarEdge, Huawei, SolaX): renewed as it always was, every
            # role whatever is still owed, a round a look per role.
            if (
                self.control.watchdog_roles and state.watchdog_at is not None and state.pending is None
                and (now - state.watchdog_at).total_seconds() >= WATCHDOG_RENEW_S
            ):
                self.state = replace(state, renew=self.control.watchdog_roles, watchdog_at=now)
                events.append(_event(now, "renew", ",".join(self.control.watchdog_roles)))
            return events
        # A fast watchdog (`BrandControl.watchdog_renew_s`): only what the running action sets is renewed (a
        # watchdog of the charge per phase has nothing to keep alive under a hold).
        # A renewed role the action leaves at its value found holds no command (Solis's force select back at "None",
        # in the inverter's own Self-Use or under a hold's peak cover): nothing to keep alive, so not written again.
        renewed = tuple(
            name for name in self.control.watchdog_roles
            if name in state.target and name not in state.given_up and not (
                name in state.restore_values and state.target[name] == state.restore_values[name]
            )
        )
        if (
            renewed and state.watchdog_at is not None and state.pending is None
            and (now - state.watchdog_at).total_seconds() >= (self.control.watchdog_renew_s or WATCHDOG_RENEW_S)
            # One round at a time, and never ahead of a write the action still owes: a round begun before the last
            # one ended would never reach its last role.
            and not state.renew and not state.in_flight and self._next_role(state, inputs) is None
        ):
            cap = self.control.renewal_cap
            if cap is not None and state.renewals_today >= cap:
                # The day's renewals are spent: handed back (the command then ends as found), none started again today.
                return events + self._begin_restore(now, "renewal_cap")
            self.state = replace(state, renew=renewed, watchdog_at=now, renewals_today=state.renewals_today + 1)
            events.append(_event(now, "renew", ",".join(renewed)))
        return events

    def _renew_block(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A grid charge commanded in blocks (`BrandControl.charge_block_s`): once a block has run, its ceiling is
        set one block ahead of the SoC now (written only where it changed). The block's start is `watchdog_at`."""
        state = self.state
        now = inputs.now
        block = self.control.charge_block_s
        if (
            not block or state.action != ACTION_GRID_CHARGE or inputs.action != ACTION_GRID_CHARGE
            or state.pending is not None or state.watchdog_at is None
            or (now - state.watchdog_at).total_seconds() < block
        ):
            return []
        target = self._wanted(ACTION_GRID_CHARGE, state.rate_kw, state.restore_values, state.phase_kw)
        if self.control.unresolved(target) is not None:
            return []
        self.state = replace(state, target=target, watchdog_at=now)
        changed = {name: value for name, value in target.items() if state.target.get(name) != value}
        return [_event(now, "block", ACTION_GRID_CHARGE, **({"target": changed} if changed else {}))]

    def _is_renewal(self, state: ExecState, name: str, role: Role, value: Native, read: RoleRead | None) -> bool:
        """Whether this write of `name` is a renewal (never counted against the day's writes).
        On a fast watchdog a role in a renewal round whose value changed meanwhile is a change, and is counted;
        the brands on `WATCHDOG_RENEW_S` count no renewal, as before (their round is a few roles a quarter-hour)."""
        return name in state.renew and (
            not self._fast_renewal() or self._satisfied(role, value, read, restoring=False)
        )

    def _fast_renewal(self) -> bool:
        """Whether the control's watchdog is renewed on the recipe's own short interval
        (`BrandControl.watchdog_renew_s`): its round is then written in one look, each role read back after, as the
        values are the ones the roles already hold. A round a look apart per role would leave no look without a
        write, and nothing is judged while a write is awaited."""
        return self.control.watchdog_renew_s is not None
