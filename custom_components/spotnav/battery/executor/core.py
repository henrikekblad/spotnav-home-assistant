"""The executor itself: one battery's write path as a state machine stepped once a look. The order of a look is here
(`step`); its rules live in the mixins beside it, each on the one state."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from ..commands import WATCHDOG_S, BrandControl, Native
from ..learned import SOURCE_LEARNED
from ..ledger import Ledger
from ..monitor import Monitor
from ..plan_model import ACTION_EXPORT_DISCHARGE, ACTION_GRID_CHARGE, ACTION_HOLD
from .claims import Claims
from .const import (
    BLOCKED_RESTORE_S,
    CALL_TIMEOUT_BLOCK_S,
    CHARGE_WRITE_RESERVE,
    DAILY_WRITE_CAP,
    MANAGEMENT_BLOCK_S,
    RATE_STEP_KW,
    RATE_TOLERANCE_KW,
    RATE_UP_DWELL_S,
    _BUDGET_REASONS,
    _NO_VALUE,
    _PROBE_UNBLOCKED,
    _SELF_CLEARING,
)
from .expiry_probe import ExpiryProbe
from .fuse import hold_headroom_ok
from .handback import HandBack
from .helpers import _event
from .judge import Judge
from .ordering import Ordering
from .peak_cover import PeakCover
from .rate import Rate
from .renewal import Renewal
from .rules import Rules
from .state import ExecInputs, ExecState, Op, StepResult
from .values import _restore_value
from .writer import Writer


class Executor(ExpiryProbe, HandBack, Ordering, Claims, Renewal, PeakCover, Judge, Writer, Rate, Rules):
    """One battery's write path: the brand's control, the ledger and the monitor, and the state."""

    def __init__(
        self, control: BrandControl, ledger: Ledger | None = None, monitor: Monitor | None = None,
        state: ExecState | None = None, *, watchdog_s: int | None = None,
    ) -> None:
        self.ledger = ledger or Ledger()
        self.monitor = monitor or Monitor()
        self.state = state or ExecState()
        # A stored state runs (and hands back) with the variant it was written with, and with the targets per phase
        # when it ran them (their roles are the phased control's).
        control = control.with_hold(self.state.variant) if self.state.variant else control
        self.control = control.with_phases(True) if self.state.phased else control
        self.state = replace(self.state, variant=self.control.variant, phased=self.control.phased)
        #: The window written into a command that carries one (a timeout, an autorepeat, a lease's duration): the
        #: recipe's expiry window (`Expiry.window_s`), `WATCHDOG_S` where it states none; a caller's own wins.
        self.watchdog_s = watchdog_s if watchdog_s is not None else int(self.control.expiry.window_s or WATCHDOG_S)
        #: What a state that depends on the plant is filled in with (`BrandControl.wanted`): the SoC, the rating and the
        #: optional roles this plant lacks, as of the last look; not stored.
        self._soc_pct: float | None = None
        self._rated_kw: float | None = None
        self._capacity_kwh: float | None = None
        #: The renewed roles read as expired in the running expiry test (`ExecInputs.expiry_probe`), and those that
        #: changed there to anything but the device's documented fallback (`diverged`: another writer); not stored:
        #: a restart ends the test.
        self.expired: tuple[str, ...] = ()
        self.diverged: tuple[str, ...] = ()
        #: The value the command role was owned at when SpotNav's next write to it went out (a renewal, a change of
        #: power or of action; `BrandControl.command_role`): kept across that write's read-back, so a reading that is
        #: none of SpotNav's is someone else's at once, also while the write is awaited. Not stored.
        self.renewal_owned: dict[str, str] = {}
        #: Since when a hand-back has waited for a first reading of the command role (`BrandControl.command_role`):
        #: nothing is written blind (after a restart the integration may not have polled yet); `_command_unread`.
        self.command_wait_since: datetime | None = None
        #: Since when the running hand-back has waited for a first reading of each role it would write back
        #: (`BrandControl.restore_own_only`, `_restore_unread`), every unread role from the look it was first seen
        #: unread: they wait together, not one after the other; not stored.
        self.restore_wait_since: dict[str, datetime] = {}
        #: In the running expiry test, the control's silence was put in the target (`Expiry.silence`: a dead-man the
        #: integration renews, stopped by a write); not stored: a restart ends the test.
        self.silenced = False
        self._absent: frozenset[str] = frozenset()
        #: When a hold's surplus follow last raised its limit (`_follow_surplus`); not stored.
        self._surplus_raised_at: datetime | None = None

    def _sense(self, inputs: ExecInputs) -> None:
        facts = inputs.facts
        self._soc_pct, self._rated_kw, self._capacity_kwh = facts.soc_pct, facts.max_charge_kw, facts.capacity_kwh
        if self.control.rating_read:
            # Shares of the rating as the plant reads it (Fronius's WChaMax), never of a learnt one or a lower plant
            # maximum: a learnt rating below the real base would ask for more than the rate (`BrandControl.rating_read`).
            learnt = facts.sources.get("rated_charge") == SOURCE_LEARNED
            self._rated_kw = None if learnt else facts.rated_charge_kw
        optional = self.control.optional_names()
        self._absent = frozenset(
            name for name in optional if (read := inputs.reads.get(name)) is None or not read.found
        )

    def found_states(self) -> dict[str, str]:
        """The state found before SpotNav first wrote each role it keeps a snapshot of, by entity id: what the
        battery's facts read in place of SpotNav's own value (`facts.battery_facts`), so a floor set for a hold is
        never taken for the battery's own and the plan does not see an empty battery."""
        state = self.state
        found = {
            entity_id: row[0] for row in state.settle.values()
            if (entity_id := row[3]) and row[0].strip().lower() not in _NO_VALUE
        }
        return found | {
            state.entities[name]: value for name, value in state.snapshot.items()
            if name in state.entities and value.strip().lower() not in _NO_VALUE
        }

    @property
    def _daily_cap(self) -> int:
        """The day's setting writes (`BrandControl.write_cap`; 96 unless the recipe says less)."""
        return min(DAILY_WRITE_CAP, self.control.write_cap("setting"))

    @property
    def _charge_reserve(self) -> int:
        return min(CHARGE_WRITE_RESERVE, self._daily_cap // 4)

    def use_phases(self, on: bool) -> bool:
        """Run the brand's targets per phase from now on, or not (`BrandControl.with_phases`); only while nothing is
        held, since the two controls have different roles. Whether the control now is as asked."""
        control = self.control.with_phases(on)
        if control is self.control:
            return self.control.phased == on
        if self.state.phase in ("active", "restoring") or self.state.holds_snapshot:
            return False
        self.control = control
        self.state = replace(self.state, phased=control.phased)
        return True

    def _per_phase(self, inputs: ExecInputs) -> bool:
        """Whether a grid charge asked for now runs as targets per phase."""
        return (
            self.control.phased and inputs.action == ACTION_GRID_CHARGE
            and (inputs.phase_caps_kw is not None or inputs.phase_probe_kw is not None)
        )

    def use_variant(self, name: str | None) -> bool:
        """Run the hold variant `name` from now on; only while nothing is held (never two variants' writes at once).
        Whether the control now runs it."""
        control = self.control.with_hold(name)
        if control is self.control:
            return self.control.variant == name
        if self.state.phase in ("active", "restoring") or self.state.holds_snapshot:
            return False
        self.control = control
        self.state = replace(self.state, variant=control.variant)
        return True

    @property
    def _export(self) -> bool:
        """Whether this instance runs the export limit (`commands.EXPORT_CONTROLS`)."""
        return self.control.kind == "export"

    # --- one look ---

    def step(self, inputs: ExecInputs) -> StepResult:
        now = inputs.now
        self._sense(inputs)
        probing = self._probing(inputs)
        if not probing:
            self.expired = self.diverged = ()
            if self.silenced or (self.state.retake and self._silence_in_target()):
                # Also after a restart in the silence (`ExecState.retake` is stored, `silenced` is not).
                self._unsilence()
        events: list[dict[str, Any]] = []
        today = now.date().isoformat()
        if self.state.day != today:
            self.state = replace(
                self.state, day=today, writes_today=0, setpoint_writes_today=0, renewals_today=0, lapses_today=0,
            )
            if self.state.phase == "blocked" and self.state.blocked == "daily_write_cap" and not self.state.holds_snapshot:
                self.state = replace(self.state, phase="idle", blocked=None)
        state = self.state
        if (
            state.phase == "blocked" and (state.blocked or "").startswith(_SELF_CLEARING)
            and not state.holds_snapshot and state.released_at is not None
            and (now - state.released_at).total_seconds() >= (
                CALL_TIMEOUT_BLOCK_S if (state.blocked or "").startswith("call_timeout") else MANAGEMENT_BLOCK_S
            )
        ):
            self.state = replace(state, phase="idle", blocked=None, reason=None, given_up=())
            events.append(_event(now, "unblocked", state.blocked or ""))
        if (
            state.phase == "blocked" and state.holds_snapshot and (state.blocked or "").startswith("restore_failed")
            and (
                state.released_at is None
                or (now - state.released_at).total_seconds() >= max(BLOCKED_RESTORE_S, self.control.restore_retry_s or 0.0)
            )
        ):
            events += self._begin_restore(now, "restore_again")
        events += self._settle(inputs)
        self._retaken(inputs)

        events += self._observe(inputs)
        if self.state.phase == "active":
            reason = self._exit_reason(inputs)
            if reason is not None:
                events += self._begin_restore(now, reason)
        if probing and self.state.phase == "restoring" and (self.state.blocked or "").startswith(_PROBE_UNBLOCKED):
            # Handed back in the expiry test's silence for what the plant did after nothing more was written: that is
            # the test's to judge, never a lasting block of the control (the hand-back itself goes on).
            self.state = replace(self.state, blocked=None)
        persist_first = False
        if self.state.phase == "idle" and inputs.stopping is None:
            started, start_events = self._try_start(inputs)
            events += start_events
            persist_first = started
        elif self.state.phase == "active" and not self._riding_out():
            events += self._cover(inputs)
            if self.state.phase == "active":
                events += self._retarget(inputs)
        self._settle_retries(inputs)
        ops: list[Op] = []
        while not self._riding_out():
            events += self._precheck(inputs)
            batch = self._progress(inputs)
            ops += batch
            # A fuse hand-back writes all its roles in one look; so does a round of a fast watchdog's renewals.
            if not batch or not (self._fuse_hand_back() or (self._fast_renewal() and self.state.renew)):
                break
        events += [_event(now, "write", op.role, entity_id=op.entity_id, value=op.value, restore=op.restore)
                   for op in ops]
        if not ops:
            events += self._finish(inputs)
        return StepResult(self.state, tuple(ops), tuple(events), persist_first)

    # --- observing ---

    def _observe(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        events = self._observe_failed_calls(inputs)
        events += self._observe_claims(inputs)
        events += self._judge_effect(inputs)
        return events

    # --- entering ---

    def _try_start(self, inputs: ExecInputs) -> tuple[bool, list[dict[str, Any]]]:
        now = inputs.now
        blocker = self._blocker(inputs)
        if blocker is not None:
            quiet = blocker in ("nothing_to_do", "rearm", *_BUDGET_REASONS)
            return False, ([] if quiet else [_event(now, "not_started", blocker)])
        rate, phases, why = self._aim(inputs, True)
        if why is not None:
            return False, [_event(now, "not_started", why)]
        snapshot: dict[str, str] = {}
        restore_values: dict[str, Native] = {}
        replaced: dict[str, dict[str, Any]] = {}
        entities: dict[str, str] = {}
        device_id: str | None = None
        for role in self.control.roles:
            read = inputs.reads.get(role.name)
            if read is None or read.entity_id is None:
                continue
            entities[role.name] = read.entity_id
            device_id = device_id or read.device_id
            if not role.restored:
                continue
            value = _restore_value(role, read.state, inputs.facts)
            if value is None:
                return False, [_event(now, "not_started", f"no_restore_value:{role.name}", read=read.state)]
            snapshot[role.name] = read.state or ""
            restore_values[role.name] = value
            if role.restored_below is not None and value == role.restored_below[1] and _restore_value(
                replace(role, restored_below=None), read.state, inputs.facts
            ) != value:
                replaced[role.name] = {"found": read.state, "restored_as": value}
        action = inputs.action
        assert action is not None
        house, behind = self._grid_terms(inputs, action, phases)
        self.state = replace(
            self.state, priming=bool(self.state.unsure and self.control.prime), wrote={}, settle={},
            settle_until=None, settle_misses=0,
        )
        self.state = replace(
            self.state, phase="active", brand=self.control.brand, action=action, rate_kw=rate,
            target=self._wanted(action, rate, restore_values, phases, house_kw=house, behind=behind),
            snapshot=snapshot, house_kw=house, phase_behind_kw=behind, phase_was_behind_kw=None,
            restore_values=restore_values,
            entities=entities, device_id=device_id, snapshot_at=now, entered_at=now, rate_changed_at=now,
            watchdog_at=now, sent=(), renew=(), reason=None, given_up=(), management_unknown_since=None, written=(),
            covered_at=None, peak_low_since=None, lead=(), phase_kw=phases, phase_bad=(), phase_judged_at=None,
            phase_was_kw=None, phase_raised_at=None, phase_over=0, phase_over_at=None,
        )
        self._watch(inputs, action, rate)
        detail: dict[str, Any] = {} if phases is None else {"phase_kw": list(phases)}
        events = [_event(now, "enter", action, rate_kw=rate, snapshot=snapshot, **detail)]
        # A value found that is handed back as something else (`Role.restored_below`): said in the change log.
        events += [_event(now, "restore_replaced", name, **row) for name, row in replaced.items()]
        return True, events

    def _retarget(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A new action, a new rate past the hysteresis, or a watchdog to renew."""
        now = inputs.now
        state = self.state
        action = inputs.action
        assert action is not None
        rate, phases, why = self._aim(inputs, False)
        if why is not None:
            return self._begin_restore(now, why)
        if self._probing(inputs):
            # The expiry test: the command stands as written, nothing renews it and nothing changes it.
            if state.renew:
                self.state = state = replace(state, renew=())
            return self._silence(now)
        events = self._end_priming(inputs, action)
        state = self.state
        if action != state.action and self.control.slow and state.rate_changed_at is not None and (
            (now - state.rate_changed_at).total_seconds() < self.control.min_change_s
        ):
            # A slow control (a cloud) changes its action no sooner than its own minimum: the plan's next look asks again.
            return events
        sale = state.action == ACTION_EXPORT_DISCHARGE
        if action != state.action and state.action != ACTION_GRID_CHARGE and (
            (action == ACTION_HOLD and not hold_headroom_ok(inputs, from_sale=sale))
            or (action == ACTION_EXPORT_DISCHARGE and not self._sale_headroom_ok(inputs))
        ):
            # A change into a hold or a sale from an action in which the battery may cover the house is held to that
            # action's start rule: the cover the battery gives now must not come off a phase that cannot carry it.
            # Handed back (plain self-consumption) instead. A sale covers the house and the cars behind the meter
            # before it exports, so a sale into a hold is judged too (on a plant known to spread a sale evenly over
            # three phases, a third of it on each: `cover_a`). Only from a grid charge is the change made directly:
            # ending a charge only frees room.
            return events + self._begin_restore(now, "headroom_too_small")
        if action != state.action:
            house, behind = self._grid_terms(inputs, action, phases)
            target = self._wanted(action, rate, state.restore_values, phases, house_kw=house, behind=behind)
            unresolved = self.control.unresolved(target)
            if unresolved is not None:
                # A value that depends on a reading gone missing (the rating, the SoC) is never written as it stands.
                return events + self._begin_restore(now, unresolved)
            renew, watchdog_at = self._renew_on_change(state, target, now)
            self.state = replace(
                state, action=action, rate_kw=rate,
                target=target,
                house_kw=house, phase_behind_kw=behind, phase_was_behind_kw=None,
                sent=(), renew=renew, rate_changed_at=now, watchdog_at=watchdog_at, covered_at=None,
                peak_low_since=None,
                lead=self._lead(state.action, state.target, target), phase_kw=phases, phase_bad=(),
                phase_judged_at=None, phase_was_kw=None, phase_raised_at=None, phase_over=0, phase_over_at=None,
                # The runaway's grace counts from the charge's own start.
                entered_at=now if phases is not None else state.entered_at,
            )
            self._release_unset()
            self._watch(inputs, action, rate)
            detail: dict[str, Any] = {} if phases is None else {"phase_kw": list(phases)}
            return [_event(now, "change", action, rate_kw=rate, **detail)]
        if phases is not None and state.phase_kw is not None:
            events += self._retarget_phases(inputs, rate, phases)
        elif action in (ACTION_GRID_CHARGE, ACTION_EXPORT_DISCHARGE) and not (
            action == ACTION_GRID_CHARGE and self.control.fixed_charge
        ):
            since = (now - (state.rate_changed_at or now)).total_seconds()
            down = rate < state.rate_kw - RATE_TOLERANCE_KW
            up = rate >= state.rate_kw + RATE_STEP_KW and since >= RATE_UP_DWELL_S
            if down and action == ACTION_EXPORT_DISCHARGE and self._sale_step_too_large(inputs, rate):
                return events + self._begin_restore(now, "headroom_too_small")
            if down or up:
                house, _behind = self._grid_terms(inputs, action, None)
                target = self._wanted(action, rate, state.restore_values, house_kw=house)
                unresolved = self.control.unresolved(target)
                if unresolved is not None:
                    return events + self._begin_restore(now, unresolved)
                self.state = replace(
                    state, rate_kw=rate, target=target, sent=(), rate_changed_at=now, house_kw=house,
                )
                self._watch(inputs, action, rate)
                events.append(_event(now, "rate", action, rate_kw=rate, was_kw=state.rate_kw))
        events += self._follow_surplus(inputs)
        events += self._renew_block(inputs)
        return events + self._renewal(inputs)

    # --- what it shows ---

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state.as_dict(), "ledger": self.ledger.as_dict(), "monitor": self.monitor.as_dict()}
