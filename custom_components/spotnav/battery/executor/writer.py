"""The executor's writes, one role a look: whether a role already reads what is wanted, the day's write caps
(`write_class`, `write_caps`), the write itself with its claim in the ledger, and how long it may take to read back
(`Role.readback_s`)."""

from __future__ import annotations

from dataclasses import replace

from ..commands import Native, Role, reading
from ..ledger import READBACK_DEADLINE_S, same
from .const import _FUSE_REASONS
from .helpers import _writes_today
from .state import ExecInputs, ExecState, Op, RoleRead
from .values import _tolerance


class Writer:
    """The writes. A mixin of `Executor`, on its state, control and ledger."""

    def _capped(self, state: ExecState, inputs: ExecInputs, name: str) -> bool:
        """Whether the day's write cap holds this write back (a restore, a peak cover and a watchdog renewal never
        are)."""
        if state.phase == "restoring" or self._covering(state, name) or name in state.renew:
            return False
        if self.control.role(name).write_class == "setpoint":
            # A setpoint written to 0 stops something: never held back.
            return self._setpoints_spent() and state.target.get(name) != 0.0
        return _writes_today(state, inputs) >= self._daily_cap

    def _satisfied(self, role: Role, wanted: Native, read: RoleRead | None, *, restoring: bool) -> bool:
        if role.method == "button":
            return role.name in self.state.sent
        state = None if read is None else read.state
        if restoring and role.idle_when and state in role.idle_when:
            return True
        snapshot = self.state.snapshot.get(role.name)
        never_written = role.name not in self.state.written
        if never_written and reading(role, state) is None and (
            restoring or wanted == self.state.restore_values.get(role.name)
        ):
            # Never written and no reading now (an entity that blinks unknown or unavailable): nothing SpotNav did to
            # undo, and no evidence it needs its snapshot written.
            return True
        if (
            snapshot is not None and state == snapshot and wanted == self.state.restore_values.get(role.name)
            and never_written
        ):
            # Never written and still as found (a never-set limit reads "unknown"): nothing to write back. A role
            # SpotNav wrote is restored by writing its snapshot and reading it back, whatever it reads now.
            return True
        return bool(same(wanted, reading(role, state), None if read is None else _tolerance(role, read)))

    def _progress(self, inputs: ExecInputs) -> list[Op]:
        state = self.state
        if state.phase not in ("active", "restoring") or state.pending is not None:
            return []
        restoring = state.phase == "restoring"
        if restoring and self._command_unread(inputs):
            return []
        state = self.state
        name = self._next_role(state, inputs)
        if restoring and name is not None and self._restore_unread(state, inputs, name):
            return []
        if name is None or self._capped(state, inputs, name) or (restoring and self._limit_waits(state, inputs, name)):
            return []
        role = self.control.role(name)
        value = state.target[name]
        entity_id = state.entities.get(name)
        read = inputs.reads.get(name)
        renew = tuple(item for item in state.renew if item != name)
        sent = state.sent if name in state.sent else (*state.sent, name)
        # A restore and a watchdog renewal are not counted: the renewal is the brand's dead-man, on a fixed timer
        # (`WATCHDOG_RENEW_S`, at most a few roles), never a change of the wanted state.
        renewing = self._is_renewal(state, name, role, value, read)
        counted = 0 if renewing or (restoring and not self.control.count_restores) else 1
        setpoint = role.write_class == "setpoint"
        writes = state.writes_today + (0 if setpoint else counted)
        setpoints = state.setpoint_writes_today + (counted if setpoint else 0)
        written = state.written if name in state.written else (*state.written, name)
        if not restoring and not renewing:
            state = replace(state, wrote=state.wrote | {name: value})
        if role.method == "button":
            # A button has no read-back: pressed once per target; the monitor judges what it did.
            self.state = replace(state, sent=sent, renew=renew, writes_today=writes, written=written,
                                 setpoint_writes_today=setpoints)
        else:
            if entity_id is not None:
                claim = self.ledger.claim(entity_id)
                if name == self.control.command_role and claim is not None and claim.owned is not None:
                    self.renewal_owned[name] = claim.owned
                elif name == self.control.command_role and claim is not None and claim.owned is None:
                    pass  # a write over one still awaited keeps the value owned before it
                else:
                    self.renewal_owned.pop(name, None)
                self.ledger.intend(
                    entity_id, value, inputs.now, step=None if read is None else _tolerance(role, read),
                    deadline_s=self._readback_s(state, name),
                )
            if self._fuse_hand_back() or (renewing and self._fast_renewal()):
                self.state = replace(state, in_flight=(*state.in_flight, name), sent=sent, renew=renew,
                                     writes_today=writes, written=written, setpoint_writes_today=setpoints)
            else:
                self.state = replace(state, pending=name, sent=sent, renew=renew, writes_today=writes, written=written,
                                     setpoint_writes_today=setpoints)
        return [Op(name, role.method, entity_id, value, device_id=state.device_id, restore=restoring)]

    def _readback_s(self, state: ExecState, name: str) -> float:
        """How long this write may take to read back: the role's longer wait (`Role.readback_s`) only for a write
        that starts or changes a command. A restore and a peak cover keep the ledger's own, so one the link dropped
        is sent again soon (both are idempotent, and the house stands on the grid without the battery meanwhile)."""
        wait = self.control.role(name).readback_s
        if wait is None or state.phase == "restoring" or self._covering(state, name):
            return READBACK_DEADLINE_S
        return wait

    def _fuse_hand_back(self) -> bool:
        """A hand-back for the fuse: its roles are written at once, each read back afterwards."""
        return self.state.phase == "restoring" and self.state.reason in _FUSE_REASONS
