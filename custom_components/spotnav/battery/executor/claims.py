"""The executor's ownership of what it wrote: each claimed role read against the ledger (confirmed, not read back,
changed by someone else), another system's command on the command role (`command_role`, `mode_limits`), a command
that lapsed by itself, and the read before every write."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from ..commands import Role, reading
from ..ledger import same
from .const import LAPSES_A_DAY, _FUSE_REASONS
from .helpers import _event, _without
from .state import ExecInputs
from .values import _tolerance


class Claims:
    """What SpotNav owns and what is someone else's. A mixin of `Executor`, on its state, control and ledger."""

    def _observe_failed_calls(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """The last look's writes whose service call failed or was given up on: not taken."""
        now = inputs.now
        events: list[dict[str, Any]] = []
        for role_name in inputs.failed:
            if self.state.pending == role_name or role_name in self.state.in_flight:
                entity_id = self.state.entities.get(role_name)
                if entity_id is not None:
                    self.ledger.release(entity_id)
                self.state = replace(self.state, pending=None if self.state.pending == role_name else self.state.pending,
                                     in_flight=_without(self.state.in_flight, role_name))
                why = "call_timeout" if role_name in inputs.timed_out else "service_failed"
                if why == "service_failed" and self._silent_since(inputs, None):
                    # A call that failed while the plant says nothing at all: no finding about the control.
                    events += self._silent_not_taken(now, role_name, why)
                else:
                    events += self._not_taken(now, role_name, why)
        return events

    def _observe_claims(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """Each role SpotNav has a claim on, read against it: someone else's command, a command that lapsed, the
        expiry test's fallback, and the ledger's verdict (confirmed, not read back, changed outside)."""
        now = inputs.now
        events: list[dict[str, Any]] = []
        for role in self.control.roles:
            entity_id = self.state.entities.get(role.name)
            claim = None if entity_id is None else self.ledger.claim(entity_id)
            if entity_id is None or claim is None:
                continue
            read = inputs.reads.get(role.name)
            now_read = reading(role, None if read is None else read.state)
            expiring = False
            command = role.name == self.control.command_role
            foreign = command and self._foreign(role, claim, now_read)
            if foreign:
                # Another system's command on the role that holds the whole command: given up at once, and from now
                # on nothing is written to it or to the limits that act in it (`_gave_up`).
                events += self._gave_up(now, role.name, entity_id, now_read, foreign=True)
                self.ledger.give_up(entity_id, now_read, now, claim.intended)
                continue
            if (
                command and self.state.altered == role.name and self.state.phase == "active"
                and now_read is not None and now_read == self._fallback_values().get(role.name)
            ):
                # SpotNav's own command, its value set from outside, ended by the device's own expiry: handed back.
                events += self._begin_restore(now, f"altered_ended:{role.name}")
                continue
            owned = claim.owned if claim.owned is not None else self.renewal_owned.get(role.name)
            if (
                command and not self._probing(inputs) and owned is not None and now_read is not None
                and self.state.phase == "active" and role.name not in self.state.given_up
            ):
                fallback = self._fallback_values().get(role.name)
                step = None if read is None else _tolerance(role, read)
                if fallback is not None and same(fallback, now_read, step) and same(owned, now_read, step) is False:
                    # The command lapsed by itself (a renewal that never landed): the device's own fallback, not
                    # someone else's change. Handed back and started again; blocked only on repeats.
                    events += self._lapsed(now, role.name, entity_id, now_read)
                    self.ledger.release(entity_id)
                    self.renewal_owned.pop(role.name, None)
                    continue
            if self._probing(inputs) and role.name in self.control.watchdog_roles and claim.owned is not None:
                step = None if read is None else _tolerance(role, read)
                if now_read is not None and same(claim.owned, now_read, step) is False:
                    fallback = self._fallback_values().get(role.name)
                    if fallback is not None and same(fallback, now_read, step):
                        # The device's own documented fallback.
                        expiring = True
                    elif role.name not in self.diverged:
                        # Changed to something else in the silence: another writer, never the expiry. Left to the
                        # ledger's dwell as any outside change; the test ends on it.
                        self.diverged = (*self.diverged, role.name)
                        events.append(_event(now, "diverged", role.name, entity_id=entity_id, read=now_read))
            if (
                self.control.command_role is not None and role.name in self.control.mode_limits
                and role.name not in self.state.given_up and claim.owned is not None and now_read is not None
                and role.name in self.state.target and same(claim.owned, now_read, claim.step) is False
                and same(self.state.target[role.name], now_read, claim.step)
            ):
                # The command's own call set this limit to the value the action wants (Solis's force carries its
                # power and timeout): SpotNav's own write, owned again.
                self.ledger.intend(entity_id, self.state.target[role.name], now, step=claim.step)
            event = self.ledger.observe(entity_id, now_read, now, expiring=expiring)
            if event is None:
                continue
            if event.kind == "expired":
                # The device's own fallback while the test renews nothing: not written again for the test's span.
                self.expired = (*self.expired, role.name)
                events.append(_event(now, "expired", role.name, entity_id=entity_id, read=event.read))
            elif event.kind == "confirmed":
                if self.state.pending == role.name or role.name in self.state.in_flight:
                    retries = {name: row for name, row in self.state.retries.items() if name != role.name}
                    self.state = replace(
                        self.state, pending=None if self.state.pending == role.name else self.state.pending,
                        in_flight=_without(self.state.in_flight, role.name), retries=retries,
                    )
                if self.state.phase == "restoring" and role.name not in self.state.confirmed:
                    self.state = replace(self.state, confirmed=(*self.state.confirmed, role.name))
                events.append(_event(now, "confirmed", role.name, entity_id=entity_id, read=event.read))
            elif event.kind == "unconfirmed":
                if self.state.pending == role.name:
                    self.state = replace(self.state, pending=None)
                self.state = replace(self.state, in_flight=_without(self.state.in_flight, role.name))
                if role.name not in self.state.target or (
                    self.state.phase == "restoring" and event.intended != self.state.target.get(role.name)
                ):
                    # A write made for the action, still awaited when the hand-back began: no restore to retry,
                    # the role is judged afresh against its restore value. Likewise one for a role the action no
                    # longer sets.
                    events.append(_event(now, "not_taken", role.name, why="superseded", read=event.read))
                elif self._silent_since(inputs, claim.at):
                    events += self._silent_not_taken(now, role.name, "no_read_back", read=event.read)
                else:
                    events += self._not_taken(now, role.name, "no_read_back", read=event.read)
            else:
                events += self._gave_up(now, role.name, entity_id, event.read)
        return events

    def _gave_up(self, now: datetime, role: str, entity_id: str, read: str | None, **detail: Any) -> list[dict[str, Any]]:
        """A role someone else changed: given up (never fought); an active part hands the rest back. The role that
        holds the whole command (`BrandControl.command_role`) takes the limits that act in it along: another system's
        command runs on them, so the hand-back writes none of them (`left_alone`, logged)."""
        given = (*self.state.given_up, role) if role not in self.state.given_up else self.state.given_up
        events: list[dict[str, Any]] = []
        if role == self.control.command_role:
            left = tuple(name for name in self.control.mode_limits if name not in given)
            given = (*given, *left)
            events.append(_event(now, "left_alone", role, roles=[role, *left]))
        self.state = replace(
            self.state, given_up=given,
            pending=None if self.state.pending in given else self.state.pending,
            in_flight=tuple(name for name in self.state.in_flight if name not in given),
        )
        events.insert(0, _event(now, "external", role, entity_id=entity_id, read=read, **detail))
        if role == self.control.command_role and self.state.phase == "active" and self._own_command_altered(role, read):
            # SpotNav's own command with a value set from outside (the power under its force): never fought. It stays
            # SpotNav's to end only for the fuse (`_begin_restore`); otherwise the device's own expiry ends it. The
            # action still runs, writing nothing, until the plan or a rule hands back.
            self.state = replace(self.state, altered=role)
            events.append(_event(now, "altered", role, read=read))
            return events
        if self.state.phase == "active":
            events += self._begin_restore(now, f"changed_outside_spotnav:{role}")
        return events

    def _foreign(self, role: Role, claim: Any, now_read: str | None) -> bool:
        """Whether the command role reads another system's command (`BrandControl.command_role`): a value that is none
        of SpotNav's for it: the one owned (also the one owned before the write awaited now), the one written or
        awaited, the one the action wants now, the one found, or the device's documented fallback. Every write to
        the command's limits goes before or with the command (`mode_limits`, the command's own call), so SpotNav's
        own change of power reads as the value it wants, and its own call taken only in part as the value written
        before it."""
        if now_read is None or role.name in self.state.given_up:
            return False
        state = self.state
        known = {
            claim.intended, claim.owned, self.renewal_owned.get(role.name), state.target.get(role.name),
            state.wrote.get(role.name), state.restore_values.get(role.name), self._fallback_values().get(role.name),
        }
        return now_read not in known

    def _command_unread(self, inputs: ExecInputs) -> bool:
        """Whether a hand-back waits for a first reading of the command role (`BrandControl.command_role`) before it
        writes anything: after a restart the integration may not have read the plant yet, and a command found then
        may be another system's (`_foreign` judges it once it reads). Never for the fuse. Bounded by the command's
        own expiry window (SpotNav's own command has ended by then): past it, the command and its limits are left as
        they are (`left_alone`), nothing written blind."""
        state = self.state
        name = self.control.command_role
        if (
            name is None or name in state.given_up or name not in state.target or state.reason in _FUSE_REASONS
        ):
            self.command_wait_since = None
            return False
        read = inputs.reads.get(name)
        if reading(self.control.role(name), None if read is None else read.state) is not None:
            self.command_wait_since = None
            return False
        since = self.command_wait_since or inputs.now
        self.command_wait_since = since
        if (inputs.now - since).total_seconds() < (self.control.expiry.window_s or 0.0):
            return True
        left = tuple(item for item in (name, *self.control.mode_limits) if item not in state.given_up)
        self.state = replace(state, given_up=(*state.given_up, *left))
        self.command_wait_since = None
        return False

    def _own_command_altered(self, name: str, now_read: str | None) -> bool:
        """Whether a foreign reading of the command role is SpotNav's own command with another value (its kind, the
        text before `:`, is the one SpotNav owns: Solis's 43135 still SpotNav's, only the power moved from outside)."""
        claim_owned = self.renewal_owned.get(name) or self.state.wrote.get(name)
        return (
            now_read is not None and claim_owned is not None
            and str(now_read).partition(":")[0] == str(claim_owned).partition(":")[0]
            and str(now_read).partition(":")[0] != str(self._fallback_values().get(name)).partition(":")[0]
        )

    def _lapsed(self, now: datetime, role: str, entity_id: str, read: str | None) -> list[dict[str, Any]]:
        """The command lapsed by itself (`BrandControl.command_role` read at its documented fallback while it runs):
        handed back and started again after the re-arm, unless it lapsed `LAPSES_A_DAY` times today."""
        lapses = self.state.lapses_today + 1
        self.state = replace(
            self.state, lapses_today=lapses, pending=None if self.state.pending == role else self.state.pending,
            in_flight=_without(self.state.in_flight, role),
        )
        events = [_event(now, "lapsed", role, entity_id=entity_id, read=read, lapses_today=lapses)]
        why = "lapsed_repeatedly" if lapses >= LAPSES_A_DAY else "lapsed"
        return events + self._begin_restore(now, f"{why}:{role}")

    def _precheck(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """Re-read the role about to be written: if it is not what SpotNav expects (the value it owns, or for a role
        not written yet the value found at enter) someone else changed it: give it up instead of writing over it.
        A reading that is no reading (unknown, unavailable) is no evidence and never stops a write."""
        events: list[dict[str, Any]] = []
        for _ in self.control.roles:
            state = self.state
            if state.phase not in ("active", "restoring") or state.pending is not None:
                break
            name = self._next_role(state, inputs)
            if name is None:
                break
            if self._capped(state, inputs, name):
                break
            role = self.control.role(name)
            read = inputs.reads.get(name)
            entity_id = state.entities.get(name)
            if role.method == "button" or read is None or entity_id is None:
                break
            claim = self.ledger.claim(entity_id)
            if claim is not None and claim.owned is not None:
                expected: str | None = claim.owned
            elif name not in state.written and name in state.snapshot:
                expected = reading(role, state.snapshot[name])
            elif self._restores_own_only(state, name):
                # No claim (the ledger lost or released it): SpotNav's own value or the one found; anything else
                # someone else set meanwhile, never written back over (`BrandControl.restore_own_only`).
                now_read = reading(role, read.state)
                ours = (state.wrote.get(name), state.restore_values.get(name))
                if now_read is None or any(
                    value is not None and same(value, now_read, _tolerance(role, read)) for value in ours
                ):
                    break
                events += self._gave_up(inputs.now, name, entity_id, now_read, why="changed_while_not_owned")
                continue
            else:
                break
            if expected is None:
                break
            now_read = reading(role, read.state)
            if same(expected, now_read, _tolerance(role, read)) is not False:
                break
            if name == self.control.command_role and claim is not None and not self._foreign(role, claim, now_read):
                # The command reads one of SpotNav's own values (its own change of power, written before the command's
                # call): no one else's.
                break
            if claim is not None:
                self.ledger.give_up(entity_id, now_read, inputs.now, state.target.get(name, expected))
            events += self._gave_up(
                inputs.now, name, entity_id, now_read, expected=expected, why="changed_before_write"
            )
        return events
