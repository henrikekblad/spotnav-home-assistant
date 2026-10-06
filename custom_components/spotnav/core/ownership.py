"""Who owns a charger's charge and which person intent holds: one pure, total, deterministic decision.

`decide(session, event, now) -> (session, commands)`. No Home Assistant imports, no clock of its own (`now` is
passed), no I/O. Commands are intents for a shell to carry out (`Start(reason)`, `Stop(reason)`, `Keep()`,
`Notify(code)`); what became of one comes back as a `CommandResult` event, and only then does the owner change
(a start that never went out owns nothing, a stop that never went out keeps the owner: findings I4 and S-a).

The rules are today's, as the manual-pause specs and their three review rounds settled them
(plans/ha_manual_override_and_fixes.md, ha_override_review_fixes*.md), encoded once:

* The gate every automatic decision asks (`automatic_allowed`): a pause holds Auto's starts, claims and
  balancing resumes; a stop still may, except of a person's Start (A). Under a person's Start, load balancing
  resumes their charge; a charge a person started is resumed under any pause but their Stop (C5, R4).
* A person's Start or Stop pauses Auto for the plug-in session (`ManualPause`): a Stop with no car plugged in is
  for the next plug-in, which a first definite connection after a restart starts (C1, C2); the pause ends at
  the unplug (or a plug-in that shows the session ended unseen), at Resume or Follow, at another pause choice,
  and for a Start when the car ends the charge by itself (R3, C8). A span pause the person chose is kept.
* Under a person's Stop a charge the charger begins by itself is stopped at once (C7), at most once per
  `PERSON_HOLD_STOP_GAP_S`, and after `PERSON_HOLD_MAX_STOPS` stops in `PERSON_HOLD_WINDOW_S` SpotNav gives up
  and says so (`charger_ignores_stop`, R5).
* After the car ended a person's charge, a car known full skips every later window of the plug-in unless its
  need grew; otherwise only the window open then is skipped (R3).
* The hold of a charge that began by itself outside a window, its override, the claim of one inside a window
  as the plan's, the stop of a plan charge that strays, the re-arm's stop outside the windows, the window ends,
  the top-off, the target and need-met stops, the sun's start, stop and take-over (I4), load balancing's pause
  and resume: as `execution/controller.py` and `execution/auto_execution.py` decide them today, including the
  places the research found questionable (left as they are in step 1, see docs/architecture-state.md).
* A window's end stops only the plan's own charge (`window_end_spared`): a person's Start, a Charge-now start
  and the sun's charge go on, and so does one load balancing holds back for them; the last window's end then
  ends the plan with no stop and no top-off (I3, decided in today's code and here together).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Final

from .events import (
    BalancingPause,
    BalancingResume,
    CarEnded,
    ChargerReportedOff,
    ChargerReportedOn,
    CommandResult,
    ConnectionUnknown,
    DirectStart,
    DirectStop,
    Event,
    FinalWindowEnd,
    NeedMet,
    PauseChoiceMade,
    PersonStart,
    PersonStop,
    PlanDropped,
    PlanInstalled,
    PlugIn,
    Rearm,
    Recheck,
    RECHECK_CLAIM,
    RECHECK_HOLD,
    RECHECK_PERSON_HOLD,
    RECHECK_STRAY,
    Restart,
    Resume,
    RESUME_EXPIRED,
    RESUME_FOLLOW,
    SolarStart,
    SolarStop,
    StrategyChange,
    TargetReached,
    Timer,
    TopOffEnd,
    TRIGGER_PLUG_IN,
    Unplug,
    WindowEnd,
    WindowStart,
)
from .session import (
    ChargeSession,
    ManualPause,
    MANUAL_START,
    MANUAL_STOP,
    OWNER_CHARGE_NOW,
    OWNER_CHARGER_SELF,
    OWNER_NONE,
    OWNER_PERSON,
    OWNER_PLAN,
    OWNER_SOLAR,
    OWNER_TOP_OFF,
    PendingCommand,
    SCOPE_NEXT_PLUG_IN,
    SCOPE_PLUG_IN,
    SPOTNAV_OWNERS,
)

#: The gate's kinds (`controller.AUTOMATIC_*`).
GATE_START: Final = "start"
GATE_STOP: Final = "stop"
GATE_BALANCING_RESUME: Final = "balancing_resume"
GATE_PERSON_RESUME: Final = "person_resume"

#: Today's constants (`execution/controller.py`; a test keeps the two equal).
PERSON_HOLD_STOP_GAP_S: Final = 30.0
PERSON_HOLD_MAX_STOPS: Final = 3
PERSON_HOLD_WINDOW_S: Final = 600.0
SAFETY_RESUME_GAP_S: Final = 300.0

#: The notification code when SpotNav gives up stopping a charger under a person's Stop.
NOTIFY_CHARGER_IGNORES_STOP: Final = "charger_ignores_stop"

#: Command reasons.
REASON_PLAN_WINDOW: Final = "plan_window"
REASON_CLAIM: Final = "claim"
REASON_PERSON: Final = "person"
REASON_DIRECT: Final = "direct"
REASON_SOLAR: Final = "solar"
REASON_TAKE_OVER: Final = "take_over"
REASON_BALANCING: Final = "balancing"
REASON_BALANCING_RESUME: Final = "balancing_resume"
REASON_WINDOW_END: Final = "window_end"
REASON_FINAL_WINDOW_END: Final = "final_window_end"
REASON_REARM: Final = "rearm"
REASON_STRAY: Final = "stray"
REASON_HOLD: Final = "hold"
REASON_PERSON_HOLD: Final = "person_hold"
REASON_PAUSE: Final = "pause"
REASON_STRATEGY: Final = "strategy"
REASON_TARGET: Final = "target"
REASON_NEED_MET: Final = "need_met"
REASON_TOP_OFF_END: Final = "top_off_end"

#: Who owns a charge a start of this reason began, when no pending command says (a result that came back after
#: a later command replaced the pending one).
_START_OWNER: Final = {
    REASON_PLAN_WINDOW: OWNER_PLAN,
    REASON_CLAIM: OWNER_PLAN,
    REASON_PERSON: OWNER_PERSON,
    REASON_SOLAR: OWNER_SOLAR,
}

#: Who owns a charge a plan window's end leaves running (`controller.WINDOW_END_SPARED_ORIGINS`): a person, a
#: Charge-now start and the sun. Never the plan, its top-off, or a charge the charger began by itself.
WINDOW_END_SPARED: Final = frozenset({OWNER_PERSON, OWNER_CHARGE_NOW, OWNER_SOLAR})

#: Today's `charge_origin` values, as owners (`None`: nobody's).
ORIGIN_OWNER: Final = {
    "manual": OWNER_PERSON,
    "solar": OWNER_SOLAR,
    "plan_window": OWNER_PLAN,
    "other": OWNER_CHARGE_NOW,
}


@dataclass(frozen=True)
class Command:
    """An intent for the shell."""

    kind: ClassVar[str] = "command"

    def to_dict(self) -> dict[str, str]:
        record = {"kind": self.kind}
        record.update({key: value for key, value in self.__dict__.items() if isinstance(value, str)})
        return record


@dataclass(frozen=True)
class Start(Command):
    kind: ClassVar[str] = "start"
    reason: str


@dataclass(frozen=True)
class Stop(Command):
    kind: ClassVar[str] = "stop"
    reason: str
    clear_schedule: bool = False


@dataclass(frozen=True)
class Keep(Command):
    kind: ClassVar[str] = "keep"


@dataclass(frozen=True)
class Notify(Command):
    kind: ClassVar[str] = "notify"
    code: str


KEEP: Final = Keep()

Decision = tuple[ChargeSession, tuple[Command, ...]]


# ---------------------------------------------------------------------------------------------- the queries


def automatic_allowed(session: ChargeSession, kind: str) -> bool:
    """Whether an automatic decision of this kind may act now (`AutoExecutor.automatic_allowed`)."""
    manual = session.manual
    if kind == GATE_PERSON_RESUME:
        return not (manual is not None and manual.action == MANUAL_STOP)
    if not session.paused:
        return True
    if manual is not None:
        # The person owns the charger for the plug-in session: a stop agrees with their Stop and never overrules
        # their Start; load balancing resumes only a charge they started.
        if kind == GATE_BALANCING_RESUME:
            return manual.action == MANUAL_START
        return kind == GATE_STOP and manual.action == MANUAL_STOP
    return kind == GATE_STOP


def hold_blocked(session: ChargeSession, *, solar_holds: bool, plan_auto_owned: bool) -> bool:
    """Whether something else owns the charger, so nothing is held: a pause (for an Auto plan), or the sun."""
    return (plan_auto_owned and session.paused) or solar_holds


def car_ended_holds_window(
    session: ChargeSession, *, open_window_start: datetime | None, known_full: bool, need_grew: bool
) -> bool:
    """R3: after the car ended a person's charge, a car known full keeps every later window of the plug-in from
    starting it unless its need grew; otherwise only the window already open then is skipped."""
    ended_at = session.car_ended_at
    if ended_at is None or open_window_start is None:
        return False
    if known_full:
        return not need_grew
    return open_window_start <= ended_at


def self_started(session: ChargeSession, *, charging: bool, start_pending: bool, stop_recent: bool) -> bool:
    """A charge the charger began by itself: charging, nobody here started it, no start or stop of ours on its
    way, and not a person's override of the hold."""
    return (
        charging
        and not stop_recent
        and session.owner in (OWNER_NONE, OWNER_CHARGER_SELF)
        and not start_pending
        and not session.overridden
    )


def window_end_spared(session: ChargeSession) -> bool:
    """Whether a plan window's end leaves the charge running (`controller._window_end_spared_owner`): it is a
    person's, a Charge-now start's or the sun's, or nobody runs one and load balancing holds such a charge back
    (its regulator still gives it back)."""
    owner = session.owner
    if owner in (OWNER_NONE, OWNER_CHARGER_SELF) and session.balancing_paused:
        owner = session.paused_origin
    return owner in WINDOW_END_SPARED


# ---------------------------------------------------------------------------------------------- decide


def decide(session: ChargeSession, event: Event, now: datetime) -> Decision:
    """The one decision: the session after `event` at `now`, and what the shell is asked to do. Total: every
    event type has a rule; one it does not know is refused (`TypeError`), never guessed."""
    handler = _HANDLERS.get(type(event))
    if handler is None:
        raise TypeError(f"no rule for {type(event).__name__}")
    new, commands = handler(session, event, now)
    return new, commands or (KEEP,)


def _no_balancing(session: ChargeSession) -> ChargeSession:
    return session.with_changes(balancing_paused=False, paused_origin=None)


def _reset_person_hold(session: ChargeSession) -> ChargeSession:
    return session.with_changes(hold_stop_times=(), hold_tried_at=None, hold_gave_up=False)


def _awaiting(session: ChargeSession, command: PendingCommand) -> tuple[PendingCommand, ...]:
    """The commands awaiting a result, with `command` in place of an earlier one of the same kind and reason."""
    kept = tuple(item for item in session.pending if (item.command, item.reason) != (command.command, command.reason))
    return (*kept, command)


def _start(session: ChargeSession, reason: str, owner_after: str, **pending: object) -> Decision:
    """Ask for a start; whatever balancing paused before is over (`_start_locked`)."""
    command = PendingCommand("start", reason, owner_before=session.owner, owner_after=owner_after, **pending)  # type: ignore[arg-type]
    return _no_balancing(session).with_changes(pending=_awaiting(session, command)), (Start(reason),)


def _stop(session: ChargeSession, reason: str, *, clear_schedule: bool = False, **pending: object) -> Decision:
    command = PendingCommand(
        "stop", reason, owner_before=session.owner, clear_schedule=clear_schedule, **pending  # type: ignore[arg-type]
    )
    return session.with_changes(pending=_awaiting(session, command)), (Stop(reason, clear_schedule),)


def _plug_in(session: ChargeSession, event: PlugIn, now: datetime) -> Decision:
    previous = event.previous
    s = session.with_changes(plugged=True)
    if previous is False:
        # A new plug-in session: what load balancing held and a safety stop belong to the one that ended.
        s = _no_balancing(s).with_changes(held_for_safety=False, safety_stopped_at=None)
    if previous is not None:
        # The stops under a person's Stop and what the car ended belong to the plug-in they were in. Not for the
        # first connection known after a restart: nothing says the plug-in changed.
        s = _reset_person_hold(s).with_changes(car_ended_at=None)
    manual = s.manual
    if manual is not None:
        if manual.scope == SCOPE_NEXT_PLUG_IN and previous is not True:
            # The plug-in a Stop with no car was for (also the first one known after a restart, C1).
            s = s.with_changes(manual=ManualPause(manual.action, SCOPE_PLUG_IN))
        elif manual.scope == SCOPE_PLUG_IN and previous is False:
            # A plug-in after a known unplug: the session the pause was given in is over (an unplug unseen).
            s = s.with_changes(manual=None)
    return s, ()


def _unplug(session: ChargeSession, event: Unplug, now: datetime) -> Decision:
    s = _no_balancing(session).with_changes(
        plugged=False, held_for_safety=False, safety_stopped_at=None, held=False, overridden=False
    )
    if event.previous is not None:
        s = _reset_person_hold(s).with_changes(car_ended_at=None)
    manual = s.manual
    if manual is not None and manual.scope == SCOPE_PLUG_IN:
        # The plug-in session the pause was given in is over (also when the first connection known after a
        # restart says no car: it ended while nobody looked).
        s = s.with_changes(manual=None)
    return s, ()


def _connection_unknown(session: ChargeSession, event: ConnectionUnknown, now: datetime) -> Decision:
    return session, ()


def _window_start(session: ChargeSession, event: WindowStart, now: datetime) -> Decision:
    blocked = hold_blocked(session, solar_holds=event.solar_holds, plan_auto_owned=event.plan_auto_owned)
    ended_holds = car_ended_holds_window(
        session,
        open_window_start=event.open_window_start,
        known_full=event.car_ended_known_full,
        need_grew=event.need_grew,
    )
    if event.trigger != TRIGGER_PLUG_IN:
        # The plug-in session a hold belonged to ends where the next window starts.
        s = session.with_changes(held=False, overridden=False)
        if event.target_stopping or event.target_reached:
            return s, ()
        if not automatic_allowed(s, GATE_START):
            # A pause holds Auto (one whose stop failed leaves its plan): no window of it starts (bug 8).
            return s, ()
        if ended_holds:
            return s, ()
        return _start(s, REASON_PLAN_WINDOW, OWNER_PLAN)
    s = session
    if not event.window_open or event.target_stopping:
        return s, ()
    if not automatic_allowed(s, GATE_START):
        return s, ()
    if event.top_off:
        # Past the last window nothing starts: a top-off only lets a running charge finish.
        return s, ()
    if blocked or ended_holds:
        return s, ()
    if event.control_on:
        # The charger began by itself at plug-in: the charge is the plan's (its stops apply).
        if s.owner in (OWNER_NONE, OWNER_CHARGER_SELF):
            return _start(s, REASON_CLAIM, OWNER_PLAN)
        return s, ()
    if event.connected is False or event.start_pending or s.balancing_paused or event.target_reached:
        return s, ()
    return _start(s, REASON_PLAN_WINDOW, OWNER_PLAN)


def _window_end(session: ChargeSession, event: WindowEnd, now: datetime) -> Decision:
    if (
        event.handed_off
        or event.continued
        or not automatic_allowed(session, GATE_STOP)
        or window_end_spared(session)
    ):
        # The sun carries it, the next plan takes it over, a pause holds the stop, or the charge is not the
        # plan's: it goes on.
        return session, ()
    return _stop(session, REASON_WINDOW_END)


def _final_window_end(session: ChargeSession, event: FinalWindowEnd, now: datetime) -> Decision:
    if event.handed_off or event.continued or not automatic_allowed(session, GATE_STOP):
        # The sun carries it, the next plan takes it over (no stop, no top-off), or a pause holds the stop.
        return session, ()
    if window_end_spared(session):
        # Not the plan's charge: it goes on with no top-off, and the shell ends the plan with no stop.
        return session, ()
    if event.top_off_wanted:
        if session.owner == OWNER_PLAN:
            return session.with_changes(owner=OWNER_TOP_OFF), ()
        return session, ()
    return _stop(session, REASON_FINAL_WINDOW_END, clear_schedule=True)


def _rearm(session: ChargeSession, event: Rearm, now: datetime) -> Decision:
    blocked = hold_blocked(session, solar_holds=event.solar_holds, plan_auto_owned=event.plan_auto_owned)
    if event.past_last:
        if event.top_off_resumes:
            return session, ()
        s = session
        if s.owner == OWNER_TOP_OFF:
            # A stored top-off that is over is forgotten: the charge is the plan's again, and strays.
            s = s.with_changes(owner=OWNER_PLAN)
        strays = s.owner == OWNER_PLAN and event.control_on and not blocked and not event.handed_off
        if strays and automatic_allowed(s, GATE_STOP):
            return _stop(s, REASON_STRAY)
        return s, ()
    spared = (
        # Only the plan's own charge is the plan's to stop: whatever a window's end spares goes on here too.
        session.owner in WINDOW_END_SPARED
        or window_end_spared(session)
        or (session.overridden and session.held)
        or not automatic_allowed(session, GATE_STOP)
        or blocked
        or event.handed_off
    )
    if spared:
        return session, ()
    return _stop(session, REASON_REARM, was_on=event.charging)


def _person_start(session: ChargeSession, event: PersonStart, now: datetime) -> Decision:
    if event.connected is False:
        # No car: refused, nothing sent and nothing paused (C6).
        return session, ()
    # A person decided to charge: what the car ended before is theirs to overrule.
    return _start(session.with_changes(car_ended_at=None), REASON_PERSON, OWNER_PERSON)


def _person_stop(session: ChargeSession, event: PersonStop, now: datetime) -> Decision:
    s = _reset_person_hold(session)
    if s.span_pause is None:
        manual = s.manual
        waiting = manual is not None and manual.scope == SCOPE_NEXT_PLUG_IN and event.connected is not True
        scope = SCOPE_NEXT_PLUG_IN if event.connected is False or waiting else SCOPE_PLUG_IN
        # Stored whatever becomes of the stop: the charger is told first, and Stop again is the retry (C3).
        s = s.with_changes(manual=ManualPause(MANUAL_STOP, scope))
    return _stop(s, REASON_PERSON, clear_schedule=True)


def _direct_start(session: ChargeSession, event: DirectStart, now: datetime) -> Decision:
    s = session.with_changes(car_ended_at=None) if event.manual else session
    owner = OWNER_PERSON if event.manual else ORIGIN_OWNER.get(event.cause or "other", OWNER_CHARGE_NOW)
    return _start(s, REASON_DIRECT, owner)


def _direct_stop(session: ChargeSession, event: DirectStop, now: datetime) -> Decision:
    return _stop(session, REASON_DIRECT, clear_schedule=event.clear_schedule)


def _resume(session: ChargeSession, event: Resume, now: datetime) -> Decision:
    if event.reason == RESUME_FOLLOW:
        return session.with_changes(manual=None), ()
    if event.reason == RESUME_EXPIRED:
        return session.with_changes(span_pause=None), ()
    return session.with_changes(manual=None, span_pause=None), ()


def _pause_choice(session: ChargeSession, event: PauseChoiceMade, now: datetime) -> Decision:
    s = session.with_changes(manual=None, span_pause=event.choice)
    if event.plan_applied:
        return _stop(s, REASON_PAUSE, clear_schedule=True)
    return s, ()


def _strategy_change(session: ChargeSession, event: StrategyChange, now: datetime) -> Decision:
    if event.strategy == "solar" and event.plan_applied:
        return _stop(session, REASON_STRATEGY, clear_schedule=True)
    return session, ()


def _solar_start(session: ChargeSession, event: SolarStart, now: datetime) -> Decision:
    if session.paused or not event.strategy_ok:
        return session, ()
    return _start(session, REASON_SOLAR, OWNER_SOLAR)


def _solar_stop(session: ChargeSession, event: SolarStop, now: datetime) -> Decision:
    if event.take_over:
        if session.paused or not event.strategy_ok or event.top_off_or_window:
            return session, ()
        if not self_started(
            session, charging=event.charging, start_pending=event.start_pending, stop_recent=event.stop_recent
        ):
            return session, ()
        return _stop(session, REASON_TAKE_OVER)
    if not automatic_allowed(session, GATE_STOP):
        # Never a charge a person started under their manual pause; nothing is left for the sun to do.
        return session, ()
    return _stop(session, REASON_SOLAR)


def _person_hold(session: ChargeSession, now: datetime, *, control_on: bool, start_pending: bool) -> Decision:
    """C7 and R5: under a person's Stop a charge the charger begins is stopped, with a gap and a give-up."""
    if not session.held_off_by_person:
        return _reset_person_hold(session), ()
    if session.hold_stop_pending or not control_on or session.hold_gave_up:
        return session, ()
    if start_pending or session.start_pending:
        # A start on its way (a person's Start under their own Stop) is never stopped: the report of the charge
        # may come before the start's result does, and today's code reads no start pending once it charges.
        return session, ()
    tried_at = session.hold_tried_at
    if tried_at is not None and (now - tried_at).total_seconds() < PERSON_HOLD_STOP_GAP_S:
        return session, ()
    recent = tuple(sent for sent in session.hold_stop_times if (now - sent).total_seconds() < PERSON_HOLD_WINDOW_S)
    if len(recent) >= PERSON_HOLD_MAX_STOPS:
        return session.with_changes(hold_stop_times=recent, hold_gave_up=True), (Notify(NOTIFY_CHARGER_IGNORES_STOP),)
    return (
        session.with_changes(
            hold_stop_times=(*recent, now),
            hold_tried_at=now,
            hold_stop_pending=True,
            pending=_awaiting(session, PendingCommand("stop", REASON_PERSON_HOLD, owner_before=session.owner)),
        ),
        (Stop(REASON_PERSON_HOLD),),
    )


def _reported_on(session: ChargeSession, event: ChargerReportedOn, now: datetime) -> Decision:
    s = session
    if (
        s.owner == OWNER_NONE
        and event.charging
        and not event.start_pending
        and not event.stop_recent
        and not s.start_pending
    ):
        # Nobody here started it: the charger began it by itself (not a start of ours awaiting its result).
        s = s.with_changes(owner=OWNER_CHARGER_SELF)
    if event.connected is False:
        s = s.with_changes(held=False, overridden=False)
    blocked = hold_blocked(s, solar_holds=event.solar_holds, plan_auto_owned=event.plan_auto_owned)
    gap = event.window_ahead_outside and not blocked
    owned = event.owned if event.owned is not None else s.owner in SPOTNAV_OWNERS
    commands: list[Command] = []
    hold = False
    if event.was_on is False and gap and not owned:
        if s.held:
            # Started again after the hold, by nobody here: a person overrode the plan.
            s = s.with_changes(overridden=True)
        else:
            s = s.with_changes(held=True)
            hold = True
    ended_holds = car_ended_holds_window(
        s, open_window_start=event.open_window_start, known_full=event.car_ended_known_full, need_grew=event.need_grew
    )
    unclaimed = (
        s.owner in (OWNER_NONE, OWNER_CHARGER_SELF)
        and not s.start_pending
        and event.window_open
        and not blocked
        and not ended_holds
    )
    strays = (
        s.owner == OWNER_PLAN
        and event.plan_present
        and not event.in_window
        and not blocked
        and not event.handed_off
    )
    # Each is decided again through the gate when it acts (today's tasks do): a pause holds a claim, and a
    # person's Start is never stopped by the hold or a stray charge's stop.
    if hold:
        if automatic_allowed(s, GATE_STOP):
            s, stop = _stop(s, REASON_HOLD)
            commands.extend(stop)
    elif unclaimed:
        if automatic_allowed(s, GATE_START):
            s, start = _start(s, REASON_CLAIM, OWNER_PLAN)
            commands.extend(start)
    elif strays:
        if automatic_allowed(s, GATE_STOP):
            s, stop = _stop(s, REASON_STRAY)
            commands.extend(stop)
    s, held_off = _person_hold(s, now, control_on=True, start_pending=event.start_pending)
    commands.extend(held_off)
    return s, tuple(commands)


def _reported_off(session: ChargeSession, event: ChargerReportedOff, now: datetime) -> Decision:
    if event.notified:
        # The pass that tells readers: a charge that ended by itself must not label the next one. A start the
        # charger has not answered keeps its owner.
        if event.start_pending:
            return session, ()
        return session.with_changes(owner=OWNER_NONE), ()
    s = session.with_changes(overridden=False)
    if event.connected is False:
        s = s.with_changes(held=False)
    if not event.start_pending:
        # Seen off with no start of ours on its way: the charge is nobody's any more, so one the charger later
        # begins by itself does not inherit its owner (inside a window it is the plan's, by the claim).
        s = s.with_changes(owner=OWNER_NONE)
    return _person_hold(s, now, control_on=False, start_pending=event.start_pending)


def _car_ended(session: ChargeSession, event: CarEnded, now: datetime) -> Decision:
    if not session.person_started:
        return session, ()
    return session.with_changes(manual=None, car_ended_at=now), ()


def _balancing_pause(session: ChargeSession, event: BalancingPause, now: datetime) -> Decision:
    return _stop(session, REASON_BALANCING, was_on=event.was_on, code=event.code)


def _balancing_resume(session: ChargeSession, event: BalancingResume, now: datetime) -> Decision:
    origin = session.paused_origin
    gate = GATE_PERSON_RESUME if origin == OWNER_PERSON else GATE_BALANCING_RESUME
    if not automatic_allowed(session, gate) or not session.balancing_paused or event.charging:
        return session, ()
    if session.held_for_safety and session.safety_stopped_at is not None:
        since = (now - session.safety_stopped_at).total_seconds()
        if 0 <= since < SAFETY_RESUME_GAP_S:
            return session, ()
    # The charge balancing paused goes on as what it was; nobody's stays nobody's (the charger's own once it is
    # seen charging), never a Charge-now start a window's end would spare.
    return _start(session, REASON_BALANCING_RESUME, origin or OWNER_NONE, paused_origin=origin)


def _target_reached(session: ChargeSession, event: TargetReached, now: datetime) -> Decision:
    if event.gated and not automatic_allowed(session, GATE_STOP):
        return session, ()
    return _stop(session, REASON_TARGET, clear_schedule=True)


def _need_met(session: ChargeSession, event: NeedMet, now: datetime) -> Decision:
    blocked = hold_blocked(session, solar_holds=event.solar_holds, plan_auto_owned=event.plan_auto_owned)
    if session.owner == OWNER_PLAN and event.control_on and not blocked and not event.handed_off:
        return _stop(session, REASON_NEED_MET, clear_schedule=True)
    if session.owner == OWNER_TOP_OFF:
        return session.with_changes(owner=OWNER_PLAN), ()
    return session, ()


def _top_off_end(session: ChargeSession, event: TopOffEnd, now: datetime) -> Decision:
    if not event.valid or not automatic_allowed(session, GATE_STOP):
        return session, ()
    return _stop(session, REASON_TOP_OFF_END, clear_schedule=True)


def _plan_installed(session: ChargeSession, event: PlanInstalled, now: datetime) -> Decision:
    s = session.with_changes(owner=OWNER_PLAN) if session.owner == OWNER_TOP_OFF else session
    if not event.window_open:
        s = s.with_changes(balancing_paused=False)
    return s, ()


def _plan_dropped(session: ChargeSession, event: PlanDropped, now: datetime) -> Decision:
    if session.owner == OWNER_TOP_OFF:
        return session.with_changes(owner=OWNER_PLAN), ()
    return session, ()


def _restart(session: ChargeSession, event: Restart, now: datetime) -> Decision:
    # What today keeps in memory only is gone; the connection is unknown until the charger states one.
    s = _reset_person_hold(session).with_changes(
        plugged=None, hold_stop_pending=False, held_for_safety=False, safety_stopped_at=None, pending=()
    )
    if event.legacy_person_stop and not s.paused:
        s = s.with_changes(manual=ManualPause(MANUAL_STOP, SCOPE_PLUG_IN))
    return s, ()


def _timer(session: ChargeSession, event: Timer, now: datetime) -> Decision:
    return _person_hold(session, now, control_on=event.control_on, start_pending=event.start_pending)


def _recheck(session: ChargeSession, event: Recheck, now: datetime) -> Decision:
    """A background task decides again, under the boundary's lock, whether its command is still due (today's
    tasks re-check their facts the same way). The command the report or the timer asked for is this task's: it is
    taken back first and asked for again only when it is still due, so a task that sends nothing leaves nothing
    awaited. What the task then sent comes back as a `CommandResult`."""
    what = event.what
    command = "start" if what == RECHECK_CLAIM else "stop"
    s = session.with_changes(
        pending=tuple(item for item in session.pending if (item.command, item.reason) != (command, what))
    )
    if what == RECHECK_PERSON_HOLD:
        s = s.with_changes(hold_stop_pending=False)
    blocked = hold_blocked(s, solar_holds=event.solar_holds, plan_auto_owned=event.plan_auto_owned)
    if what == RECHECK_HOLD:
        # A window opened, something else took the charger, SpotNav started the charge, or it is off by now.
        owned = event.owned if event.owned is not None else s.owner in SPOTNAV_OWNERS
        if (
            automatic_allowed(s, GATE_STOP)
            and event.window_ahead_outside
            and not blocked
            and not owned
            and event.control_on
        ):
            return _stop(s, REASON_HOLD)
        return s, ()
    if what == RECHECK_STRAY:
        # A plan installed meanwhile with a window open now, a top-off, the sun's hand-off, or the charge ended.
        if (
            automatic_allowed(s, GATE_STOP)
            and s.owner == OWNER_PLAN
            and event.plan_present
            and event.control_on
            and not event.in_window
            and not blocked
            and not event.handed_off
            and not event.top_off
        ):
            return _stop(s, REASON_STRAY)
        return s, ()
    if what == RECHECK_CLAIM:
        ended_holds = car_ended_holds_window(
            s,
            open_window_start=event.open_window_start,
            known_full=event.car_ended_known_full,
            need_grew=event.need_grew,
        )
        if (
            automatic_allowed(s, GATE_START)
            and event.control_on
            and s.owner in (OWNER_NONE, OWNER_CHARGER_SELF)
            and not s.start_pending
            and event.window_open
            and not blocked
            and not ended_holds
        ):
            return _start(s, REASON_CLAIM, OWNER_PLAN)
        return s, ()
    if what == RECHECK_PERSON_HOLD:
        # A person's Start replaced their Stop meanwhile (it never lands between this and the stop), or a start of
        # theirs is still on its way: nothing is stopped. The gap and the give-up were decided when it was spawned.
        if (
            automatic_allowed(s, GATE_STOP)
            and s.held_off_by_person
            and event.control_on
            and not event.start_pending
            and not s.start_pending
        ):
            return (
                s.with_changes(
                    hold_stop_times=(*s.hold_stop_times, now),
                    hold_tried_at=now,
                    hold_stop_pending=True,
                    pending=_awaiting(s, PendingCommand("stop", REASON_PERSON_HOLD, owner_before=s.owner)),
                ),
                (Stop(REASON_PERSON_HOLD),),
            )
        return s, ()
    # A task this core does not know sends nothing.
    return s, ()


def _command_result(session: ChargeSession, event: CommandResult, now: datetime) -> Decision:
    pending = next(
        (item for item in session.pending if (item.command, item.reason) == (event.command, event.reason)), None
    )
    if pending is not None:
        s = session.with_changes(pending=tuple(item for item in session.pending if item is not pending))
    else:
        s = session
    if event.command == "start":
        if event.reason == REASON_BALANCING_RESUME:
            if event.executed:
                owner = pending.owner_after if pending is not None and pending.owner_after else s.owner
                return _no_balancing(s).with_changes(owner=owner, held_for_safety=False), ()
            origin = pending.paused_origin if pending is not None else s.paused_origin
            return s.with_changes(balancing_paused=True, paused_origin=origin), ()
        owner_after = pending.owner_after if pending is not None else _START_OWNER.get(event.reason)
        if event.executed:
            if owner_after is not None:
                s = s.with_changes(owner=owner_after)
        elif event.balancing_held:
            # Held back for want of room: still that charge, waiting for the regulator.
            s = s.with_changes(balancing_paused=True, paused_origin=owner_after)
        else:
            return s, ()
        if event.reason == REASON_PERSON and s.span_pause is None:
            # The person owns the charger for the plug-in session (A); a span pause they chose is kept.
            s = s.with_changes(manual=ManualPause(MANUAL_START, SCOPE_PLUG_IN))
        return s, ()
    if event.reason == REASON_PERSON_HOLD:
        s = s.with_changes(hold_stop_pending=False)
    if not event.executed:
        # A stop that never went out is not done: the owner is kept (I4, bug 3).
        return s, ()
    if event.unobserved and not (pending is not None and pending.clear_schedule):
        # Nothing was sent and nothing says whether the charge still runs: its owner is kept until it can be seen.
        return _no_balancing(s), ()
    s = _no_balancing(s).with_changes(owner=OWNER_NONE)
    if pending is not None and event.reason == REASON_BALANCING:
        if session.balancing_paused:
            # A repeat pause of a charge balancing already holds back (`_regulated_stop`): still that charge.
            s = s.with_changes(balancing_paused=True, paused_origin=session.paused_origin)
        elif pending.was_on and (pending.code == "pause" or pending.owner_before == OWNER_PERSON):
            # Load balancing holds the charge back (a safety stop of a person's charge too, P3): its regulator
            # gives it back what it was.
            origin = pending.owner_before if pending.owner_before in SPOTNAV_OWNERS else None
            safety = pending.code != "pause"
            s = s.with_changes(
                balancing_paused=True,
                paused_origin=origin,
                held_for_safety=safety,
                safety_stopped_at=now if safety else s.safety_stopped_at,
            )
    elif pending is not None and event.reason == REASON_REARM and pending.was_on:
        # A charge that ran was stopped outside the windows: the rest of the plug-in session is held.
        s = s.with_changes(held=True, overridden=False)
    return s, ()


_HANDLERS: Final[dict[type[Event], Callable[[ChargeSession, Event, datetime], Decision]]] = {
    PlugIn: _plug_in,  # type: ignore[dict-item]
    Unplug: _unplug,  # type: ignore[dict-item]
    ConnectionUnknown: _connection_unknown,  # type: ignore[dict-item]
    WindowStart: _window_start,  # type: ignore[dict-item]
    WindowEnd: _window_end,  # type: ignore[dict-item]
    FinalWindowEnd: _final_window_end,  # type: ignore[dict-item]
    Rearm: _rearm,  # type: ignore[dict-item]
    PersonStart: _person_start,  # type: ignore[dict-item]
    PersonStop: _person_stop,  # type: ignore[dict-item]
    DirectStart: _direct_start,  # type: ignore[dict-item]
    DirectStop: _direct_stop,  # type: ignore[dict-item]
    Resume: _resume,  # type: ignore[dict-item]
    PauseChoiceMade: _pause_choice,  # type: ignore[dict-item]
    StrategyChange: _strategy_change,  # type: ignore[dict-item]
    SolarStart: _solar_start,  # type: ignore[dict-item]
    SolarStop: _solar_stop,  # type: ignore[dict-item]
    ChargerReportedOn: _reported_on,  # type: ignore[dict-item]
    ChargerReportedOff: _reported_off,  # type: ignore[dict-item]
    CarEnded: _car_ended,  # type: ignore[dict-item]
    BalancingPause: _balancing_pause,  # type: ignore[dict-item]
    BalancingResume: _balancing_resume,  # type: ignore[dict-item]
    TargetReached: _target_reached,  # type: ignore[dict-item]
    NeedMet: _need_met,  # type: ignore[dict-item]
    TopOffEnd: _top_off_end,  # type: ignore[dict-item]
    PlanInstalled: _plan_installed,  # type: ignore[dict-item]
    PlanDropped: _plan_dropped,  # type: ignore[dict-item]
    Restart: _restart,  # type: ignore[dict-item]
    Timer: _timer,  # type: ignore[dict-item]
    Recheck: _recheck,  # type: ignore[dict-item]
    CommandResult: _command_result,  # type: ignore[dict-item]
}
