"""The executor's hand-back: what is written back and when it is done (`Role.restored`, the brand's normal state), the
retries of a restore that is not taken, the watch after it (`settle_s`), and the restart that hands a stored
snapshot back first."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from ..commands import NORMAL, Native, reading
from ..ledger import same
from .const import RESTORE_ATTEMPTS, RESTORE_READ_WAIT_S, RESTORE_RETRY_S, SETTLE_ATTEMPTS, _FUSE_REASONS
from .helpers import _blocking, _event, _without
from .state import ExecInputs, ExecState


class HandBack:
    """The hand-back and the restore's retries. A mixin of `Executor`, on its state, control, ledger and monitor."""

    def restart(self, now: datetime) -> list[dict[str, Any]]:
        """After a restart: a stored snapshot is restored before anything else."""
        state = self.state
        if not state.holds_snapshot:
            if state.phase in ("active", "restoring"):
                self.state = replace(state, phase="idle", pending=None)
            return []
        # A write in flight when HA stopped is no longer awaited: the restore reads every role afresh. A stored
        # `retake` counts only for a control that has a silence to undo (storage is untrusted).
        self.state = replace(
            state, pending=None, in_flight=(), retries={}, unrestored=(), retake=state.retake and self._can_retake()
        )
        return self._begin_restore(now, "restart")

    def reset(self) -> None:
        """Lift a block (the part's next explicit decision); never while a snapshot waits to be restored."""
        if self.state.phase == "blocked" and not self.state.holds_snapshot:
            # A restore not held gets its attempts again (the record stays: the next look writes it again).
            self.state = replace(self.state, phase="idle", blocked=None, reason=None, given_up=(), settle_misses=0)

    def _not_taken(self, now: datetime, role: str, why: str, **detail: Any) -> list[dict[str, Any]]:
        events = [_event(now, "not_taken", role, why=why, **detail)]
        state = self.state
        if state.phase == "restoring":
            attempts = state.retries.get(role, (0, now))[0] + 1
            if attempts > RESTORE_ATTEMPTS:
                # Every retry spent: the role is left; its snapshot stays and is tried again after a restart.
                retries = {name: row for name, row in state.retries.items() if name != role}
                self.state = replace(state, retries=retries, unrestored=(*state.unrestored, role))
                events.append(_event(now, "restore_failed", role))
            else:
                at = now + timedelta(seconds=RESTORE_RETRY_S[attempts - 1])
                self.state = replace(state, retries=state.retries | {role: (attempts, at)})
        elif state.phase == "active":
            # A call given up on is its own reason: its block clears by itself (`CALL_TIMEOUT_BLOCK_S`) and a
            # verification counts it as the moment's (`gate.FAILING_ABORTS` does not name it).
            reason = f"call_timeout:{role}" if why == "call_timeout" else f"write_not_taken:{role}:{why}"
            events += self._begin_restore(now, reason)
        return events

    def _begin_restore(self, now: datetime, reason: str) -> list[dict[str, Any]]:
        state = self.state
        if state.phase == "restoring" or (state.phase != "active" and not state.holds_snapshot):
            return []
        self.monitor.stop(now)
        if state.altered is not None and reason in _FUSE_REASONS:
            # SpotNav's own command with a value set from outside, near the fuse: SpotNav ends its own command (the
            # limits stay the other writer's).
            state = replace(state, given_up=_without(state.given_up, state.altered))
            entity_id = state.entities.get(state.altered)
            if entity_id is not None:
                self.ledger.release(entity_id)
        if state.retake and not self._can_retake():
            state = replace(state, retake=False)
        target = self._retake_target(state) if state.retake else self._restore_target(state)
        blocked = reason if _blocking(reason) else state.blocked
        if reason in _FUSE_REASONS and state.pending is not None:
            # A write still awaiting its read-back (the peak cover's lift) does not hold the rest back.
            state = replace(state, pending=None, in_flight=(*_without(state.in_flight, state.pending), state.pending))
        self.state = replace(
            state, phase="restoring", target=target, reason=reason, blocked=blocked, sent=(), renew=(),
            retries={}, unrestored=(), management_unknown_since=None, silent_since=None, covered_at=None, peak_low_since=None,
            charging_since=None, over_since=None, priming=False, confirmed=(), phase_bad=(), house_kw=None,
        )
        self._release_unset()
        # A new hand-back waits for its own first readings (`_restore_unread`), never on an earlier one's wait.
        self.restore_wait_since = {}
        return [_event(now, "hand_back", reason)]

    def _restore_target(self, state: ExecState) -> dict[str, Native]:
        """What a hand-back writes: each role's value as found, or the brand's normal one for a role without one."""
        target: dict[str, Native] = {}
        normal = NORMAL.get(self.control.brand, {})
        for role in self.control.roles:
            if role.name in state.given_up:
                continue
            if role.restored and role.name in state.restore_values:
                target[role.name] = state.restore_values[role.name]
            elif role.name in normal and role.name in state.entities:
                target[role.name] = normal[role.name]
        return target

    def _settle_retries(self, inputs: ExecInputs) -> None:
        """Drop the retry of a restore write that is no longer needed: the role now reads what the hand-back wants
        (Sigenergy's mode select goes unavailable once Remote EMS is off, before its own write reads back), or it
        is no longer the hand-back's to write. Its claim, if any, is released. Without this the hand-back would wait
        for a retry `_next_role` never makes."""
        state = self.state
        if state.phase != "restoring" or not state.retries:
            return
        retries = dict(state.retries)
        for name in list(retries):
            if name == state.pending or name in state.in_flight:
                continue
            wanted = state.target.get(name)
            if wanted is not None and name not in state.given_up and not self._satisfied(
                self.control.role(name), wanted, inputs.reads.get(name), restoring=True
            ):
                continue
            del retries[name]
            entity_id = state.entities.get(name)
            if entity_id is not None:
                self.ledger.release(entity_id)
        if retries != state.retries:
            self.state = replace(state, retries=retries)

    def _finish(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A restore with nothing left to write ends: claims released, the snapshot dropped (kept for a role whose
        restore was given up)."""
        state = self.state
        if state.phase != "restoring" or state.pending is not None or state.in_flight or state.retries:
            return []
        if self._next_role(state, inputs) is not None:
            return []
        now = inputs.now
        if state.retake:
            # Taken back after a silence: now the hand-back proper.
            self.state = replace(state, retake=False, target=self._restore_target(state), sent=(), confirmed=())
            return [_event(now, "retaken", state.reason or "")]
        for name, entity_id in state.entities.items():
            if name not in state.unrestored:
                self.ledger.release(entity_id)
        if state.unrestored:
            kept = set(state.unrestored)
            self.state = replace(
                state, phase="blocked", blocked=f"restore_failed:{','.join(state.unrestored)}", target={},
                snapshot={k: v for k, v in state.snapshot.items() if k in kept},
                restore_values={k: v for k, v in state.restore_values.items() if k in kept},
                entities={k: v for k, v in state.entities.items() if k in kept},
                written=tuple(name for name in state.written if name in kept),
                sent=(), renew=(), released_at=now, phase_kw=None,
            )
            return [_event(now, "restore_failed", ",".join(state.unrestored))]
        blocked = state.blocked
        if blocked is not None and blocked.startswith("restore_failed"):
            blocked = None
        if state.reason == "daily_write_cap":
            blocked = "daily_write_cap"
        settle: dict[str, tuple[str, Native, Native, str]] = {}
        if self.control.settle_s > 0:
            # Watched a while: a restore that only Home Assistant echoed may show SpotNav's value again.
            settle = {
                name: (state.snapshot[name], state.restore_values[name], state.wrote[name], state.entities[name])
                for name in state.written
                if name in state.snapshot and name in state.restore_values and name in state.wrote
                and name in state.entities and name not in state.given_up
            }
        # A role that goes idle with the switch, written by the action, and handed back without a read-back.
        unsure = tuple(
            role.name for role in self.control.roles
            if role.idle_when and role.name in state.written and role.name in state.target
            and role.name not in state.confirmed
        )
        self.state = replace(
            state, phase="blocked" if blocked else "idle", blocked=blocked, action=None, rate_kw=0.0, target={},
            snapshot={}, restore_values={}, entities={}, device_id=None, snapshot_at=None, sent=(), renew=(),
            released_at=now, given_up=state.given_up if blocked else (), written=(), confirmed=(),
            unsure=tuple(dict.fromkeys((*state.unsure, *unsure))), phase_kw=None, wrote={},
            settle=settle, settle_until=now + timedelta(seconds=self.control.settle_s) if settle else None,
            settle_misses=state.settle_misses if settle else 0,
        )
        # Whether this hand-back shows the plant restored (`gate.plan_proved`'s `normal`): something was written, every
        # role was written back or read as found (`_next_role` left none, none was given up to another writer or
        # could not be restored), nothing blocks, and no restore is still watched for holding (`settle`).
        clean = bool(state.written) and not blocked and not state.given_up and not settle
        return [_event(now, "handed_back", state.reason or "", written=list(state.written), clean=clean)]

    def _settle(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """After a hand-back (`ExecState.settle`): a role that reads SpotNav's value again did not keep its restore
        (a cloud refused it, or only Home Assistant echoed it). It is written again, `SETTLE_ATTEMPTS` times; then the
        control blocks (`restore_not_held:<roles>`, the value found is kept so the facts still read it) until the role
        reads the value found or someone else's. A role that reads someone else's value is no longer watched; the
        watch ends `settle_s` after the hand-back."""
        state = self.state
        now = inputs.now
        if not state.settle or state.phase not in ("idle", "blocked") or state.holds_snapshot:
            return []
        keep: dict[str, tuple[str, Native, Native, str]] = {}
        not_held: list[str] = []
        for name, row in state.settle.items():
            _found, restore, wrote, _entity = row
            try:
                role = self.control.role(name)
            except KeyError:
                continue
            read = inputs.reads.get(name)
            now_read = reading(role, None if read is None else read.state)
            step = None if read is None else (read.step or role.step)
            if now_read is None or same(restore, now_read, step):
                keep[name] = row
            elif same(wrote, now_read, step):
                keep[name] = row
                not_held.append(name)
        stuck = state.phase == "blocked" and (state.blocked or "").startswith("restore_not_held")
        if not not_held:
            if stuck:
                # Set back (by hand) or taken by someone else: nothing of SpotNav's is left standing.
                self.state = replace(state, phase="idle", blocked=None, reason=None, settle={}, settle_until=None,
                                     settle_misses=0)
                return [_event(now, "unblocked", state.blocked or "")]
            if not keep or state.settle_until is None or now >= state.settle_until:
                self.state = replace(state, settle={}, settle_until=None, settle_misses=0)
            elif keep != state.settle:
                self.state = replace(state, settle=keep)
            return []
        if stuck:
            if keep != state.settle:
                self.state = replace(state, settle=keep)
            return []
        misses = state.settle_misses + 1
        events = [_event(now, "restore_not_held", ",".join(not_held), attempt=misses)]
        if misses > SETTLE_ATTEMPTS:
            self.state = replace(
                state, phase="blocked", blocked=f"restore_not_held:{','.join(not_held)}", settle=keep,
                settle_until=None, settle_misses=misses,
            )
            return events
        rows = {name: keep[name] for name in not_held}
        self.state = replace(
            state, action=None, snapshot={name: row[0] for name, row in rows.items()},
            restore_values={name: row[1] for name, row in rows.items()},
            entities={name: row[3] for name, row in rows.items()},
            wrote={name: row[2] for name, row in rows.items()}, written=tuple(not_held),
            settle={}, settle_until=None, settle_misses=misses, released_at=now,
        )
        return events + self._begin_restore(now, "restore_not_held")

    def _release_unset(self) -> None:
        """A role the running action no longer sets (and no hand-back writes) is the plant's again: its claim is
        released, also for a write still awaited, so the plant's own writes to it are never read as someone else's
        change and nothing waits for its read-back. A brand whose roles are set in every action and handed back
        (every one but a recipe with unparked targets per phase) has no such role."""
        state = self.state
        for name, entity_id in state.entities.items():
            if name in state.target or name in state.given_up:
                continue
            if self.ledger.claim(entity_id) is not None:
                self.ledger.release(entity_id)
            if state.pending == name or name in state.in_flight:
                self.state = state = replace(
                    state, pending=None if state.pending == name else state.pending,
                    in_flight=_without(state.in_flight, name),
                )

    def _normal_value(self, name: str) -> Native | None:
        """What a hand-back writes to a role: its snapshot, or the brand's normal value for one without."""
        role = self.control.role(name)
        if role.restored and name in self.state.restore_values:
            return self.state.restore_values[name]
        if name in self.state.entities:
            return NORMAL.get(self.control.brand, {}).get(name)
        return None

    def _restores_own_only(self, state: ExecState, name: str) -> bool:
        """Whether a hand-back of `name` is checked against SpotNav's own values (`BrandControl.restore_own_only`): a
        role SpotNav wrote, restored now."""
        return self.control.restore_own_only and state.phase == "restoring" and name in state.written

    def _restore_unread(self, state: ExecState, inputs: ExecInputs, name: str) -> bool:
        """Whether the hand-back waits for a first reading of `name` before writing it back
        (`BrandControl.restore_own_only`): after a restart the integration may not have read the inverter yet, and a
        value found then may be someone else's (`_precheck` judges it once it reads). Never for the fuse; at most
        `RESTORE_READ_WAIT_S`, then written as before."""
        if state.reason in _FUSE_REASONS:
            self.restore_wait_since = {}
            return False
        waits: dict[str, datetime] = {}
        for other in state.target:
            if not self._restores_own_only(state, other):
                continue
            try:
                role = self.control.role(other)
            except KeyError:
                continue  # a stored target's role this version no longer has: nothing to wait for
            read = inputs.reads.get(other)
            if reading(role, None if read is None else read.state) is None:
                waits[other] = self.restore_wait_since.get(other, inputs.now)
        self.restore_wait_since = waits
        since = waits.get(name)
        return since is not None and (inputs.now - since).total_seconds() < RESTORE_READ_WAIT_S
