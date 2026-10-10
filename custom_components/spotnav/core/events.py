"""What can happen to a charger's charge session, as typed, frozen events.

Pure: no Home Assistant imports. An event carries the facts the decision needs that are not the session's own
(the plan's windows, the sun's phase, what the charger reports, the clock is passed apart): the core decides
who owns the charge and which person intent holds from the session plus these facts, never by reading Home
Assistant. Each event names the trigger it stands for today in `execution/` (see docs/architecture-state.md).

Every event serialises to plain JSON (`to_dict`, `event_from_dict`), so a debug bundle's recorded events can
be fed to the core again (`core/replay.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, ClassVar, Final

from .session import SessionError

#: A window start's triggers: its timer, a re-arm inside an open window (a restore, a follow, an install),
#: or a plug-in inside one.
TRIGGER_TIMER: Final = "timer"
TRIGGER_REARM: Final = "rearm"
TRIGGER_PLUG_IN: Final = "plug_in"

#: Why a pause ended (`Resume.reason`).
RESUME_PERSON: Final = "resume"
RESUME_FOLLOW: Final = "follow"
RESUME_EXPIRED: Final = "expired"


@dataclass(frozen=True)
class Event:
    """Base of every event: `kind` names it in the record."""

    kind: ClassVar[str] = "event"

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"kind": self.kind}
        for item in fields(self):
            value = getattr(self, item.name)
            record[item.name] = value.isoformat() if isinstance(value, datetime) else value
        return record


@dataclass(frozen=True)
class PlugIn(Event):
    """The charger stated a car is plugged in. `previous` is the connection it last stated (`None`: none since a
    restart, so this is the first known, not necessarily a new plug-in)."""

    kind: ClassVar[str] = "plug_in"
    previous: bool | None = False


@dataclass(frozen=True)
class Unplug(Event):
    """The charger stated no car is plugged in (`previous` as for `PlugIn`)."""

    kind: ClassVar[str] = "unplug"
    previous: bool | None = True


@dataclass(frozen=True)
class ConnectionUnknown(Event):
    """The charger's status says nothing about a car (a blip through `unavailable`, a `Ready`): never a
    plug-in or an unplug (C2)."""

    kind: ClassVar[str] = "connection_unknown"


@dataclass(frozen=True)
class WindowStart(Event):
    """A window of the plan opens: its timer, a re-arm inside it, or a plug-in inside it.

    Facts: whether the target's stop fired or is on its way (it ends the plan instead), whether a window is open
    (a plug-in), the open window's start (the car-ended rule, R3), whether the car that ended a person's charge is
    known full for the plan and whether its need grew since, what the charger shows, and what else may own it (the
    sun's phase; a pause holds the hold only for an Auto plan)."""

    kind: ClassVar[str] = "window_start"
    trigger: str = TRIGGER_TIMER
    target_reached: bool = False
    target_stopping: bool = False
    window_open: bool = True
    open_window_start: datetime | None = None
    car_ended_known_full: bool = False
    need_grew: bool = False
    control_on: bool = False
    connected: bool | None = None
    start_pending: bool = False
    top_off: bool = False
    solar_holds: bool = False
    plan_auto_owned: bool = True


@dataclass(frozen=True)
class WindowEnd(Event):
    """A window ends and another follows. `handed_off`: the sun can carry the charge past it (hybrid).
    `continued`: the next plan, waiting for this boundary, has a window open now and takes the charge over."""

    kind: ClassVar[str] = "window_end"
    handed_off: bool = False
    continued: bool = False


@dataclass(frozen=True)
class FinalWindowEnd(Event):
    """The plan's last window ends. `top_off_wanted`: a charge to the car's own limit with the car still drawing
    goes on as a top-off (`top_off.py`). `continued`: the next plan, waiting for this boundary, has a window open
    now and takes the charge over (a best-effort plan ending at its departure, the next departure's plan
    beginning then)."""

    kind: ClassVar[str] = "final_window_end"
    handed_off: bool = False
    top_off_wanted: bool = False
    continued: bool = False


@dataclass(frozen=True)
class Rearm(Event):
    """The plan is armed again outside its windows (a restore, a follow, an install, a retry): with a window
    ahead a charge is stopped unless it is spared; with every window past a stored top-off goes on
    (`top_off_resumes`) or a plan charge that strays is stopped."""

    kind: ClassVar[str] = "rearm"
    past_last: bool = False
    top_off_resumes: bool = False
    handed_off: bool = False
    control_on: bool = False
    charging: bool = False
    solar_holds: bool = False
    plan_auto_owned: bool = True


@dataclass(frozen=True)
class PersonStart(Event):
    """A person's Start through the execution boundary (card, app, button, webhook). `connected` is what the
    charger says now (`False` refuses it: no car)."""

    kind: ClassVar[str] = "person_start"
    connected: bool | None = True


@dataclass(frozen=True)
class PersonStop(Event):
    """A person's Stop (or Cancel) through the execution boundary. `connected` is what the charger says now, or
    the last connection it stated when it says nothing (C2)."""

    kind: ClassVar[str] = "person_stop"
    connected: bool | None = True


@dataclass(frozen=True)
class DirectStart(Event):
    """A start through the controller's own entry with no execution boundary (a button with no Auto, a caller of
    `ChargingController.async_start`): no pause. `manual` marks a person's; `cause` is who asked."""

    kind: ClassVar[str] = "direct_start"
    manual: bool = False
    cause: str | None = None


@dataclass(frozen=True)
class DirectStop(Event):
    """A stop through the controller's own entry with no execution boundary."""

    kind: ClassVar[str] = "direct_stop"
    clear_schedule: bool = False


@dataclass(frozen=True)
class Resume(Event):
    """A pause ends: the person resumed Auto (any pause), followed the plan again (a manual pause), or a span
    pause's instant passed and its clear was written (`expired`)."""

    kind: ClassVar[str] = "resume"
    reason: str = RESUME_PERSON


@dataclass(frozen=True)
class PauseChoiceMade(Event):
    """A person picked a pause for a span (`choice`); it replaces a manual pause. `plan_applied`: an Auto plan is
    on the charger, so it is stopped and cleared."""

    kind: ClassVar[str] = "pause_choice"
    choice: str = "until_resumed"
    plan_applied: bool = False


@dataclass(frozen=True)
class StrategyChange(Event):
    """The strategy changed. To `solar` with an Auto plan on the charger: the plan is cleared, and its charge is
    stopped unless the sun keeps it (`sun_keeps`: by the sun's rules for a charge that runs, its surplus carries
    it), when the charge is handed over to the sun with no stop. (The other way, a plan window that opens while the
    sun runs a charge takes it over as the plan's at its `WindowStart`, with no command.)"""

    kind: ClassVar[str] = "strategy_change"
    strategy: str = "solar"
    plan_applied: bool = False
    sun_keeps: bool = False


@dataclass(frozen=True)
class SolarStart(Event):
    """The sun's verdict is a start. `strategy_ok`: the strategy is solar or hybrid."""

    kind: ClassVar[str] = "solar_start"
    strategy_ok: bool = True


@dataclass(frozen=True)
class SolarStop(Event):
    """The sun's verdict is a stop, or (`take_over`) its first reading of a charge the charger began by itself:
    with no surplus it is stopped at once (I4). For a take-over: `top_off_or_window` (a top-off, or a hybrid plan
    window, owns the charger), and what the charger shows."""

    kind: ClassVar[str] = "solar_stop"
    strategy_ok: bool = True
    take_over: bool = False
    top_off_or_window: bool = False
    charging: bool = False
    start_pending: bool = False
    stop_recent: bool = False


#: Who takes a floor charge over at the floor (`MinSocEnd.handed_to`): a plan window open now, the sun, or nobody
#: (the charge is stopped).
HAND_TO_PLAN: Final = "plan"
HAND_TO_SOLAR: Final = "solar"
HAND_TO_NOBODY: Final = ""


@dataclass(frozen=True)
class MinSocStart(Event):
    """The car's known state of charge is below its minimum charge level (`execution/min_soc_floor.py`): SpotNav
    charges at once, at the full current set, whatever the strategy, the plan's windows or the sun say. A person's
    pause or Stop wins, and a charge a person started stays theirs. `connected` is what the charger says now."""

    kind: ClassVar[str] = "min_soc_start"
    connected: bool | None = True


@dataclass(frozen=True)
class MinSocEnd(Event):
    """The floor charge is over: the car reached its minimum charge level, its level is no longer known, the floor
    was turned off, or a pause holds Auto. The strategy decides from here: `handed_to` a plan window open now
    (`plan`) or the sun (`solar`) takes the charge over with no command, or nobody does and it is stopped once."""

    kind: ClassVar[str] = "min_soc_end"
    handed_to: str = HAND_TO_NOBODY


@dataclass(frozen=True)
class ChargerReportedOn(Event):
    """The charger reported its charge control on (charging, or enabled). Decided at every such report: the hold
    of a charge that began by itself outside a window, the claim of one inside a window as the plan's, the stop
    of a plan charge that strays outside every window, and the stop under a person's Stop (C7).

    `was_on` is the control the hold last saw (`None`: nothing yet), `owned` whether SpotNav started the charge
    as the hold remembers it (`None`: read it from the owner)."""

    kind: ClassVar[str] = "charger_reported_on"
    charging: bool = True
    was_on: bool | None = False
    connected: bool | None = True
    window_ahead_outside: bool = False
    window_open: bool = False
    in_window: bool = False
    plan_present: bool = False
    open_window_start: datetime | None = None
    car_ended_known_full: bool = False
    need_grew: bool = False
    solar_holds: bool = False
    plan_auto_owned: bool = True
    handed_off: bool = False
    start_pending: bool = False
    stop_recent: bool = False
    owned: bool | None = None


@dataclass(frozen=True)
class ChargerReportedOff(Event):
    """The charger reported its charge control off (readably). With no start of ours on its way (`start_pending`)
    the charge is nobody's any more, whoever owned it. `notified`: the pass that tells readers ran (`_notify`), which
    ends an owner left over the same way."""

    kind: ClassVar[str] = "charger_reported_off"
    notified: bool = False
    start_pending: bool = False
    connected: bool | None = True


@dataclass(frozen=True)
class CarEnded(Event):
    """The car ended a charge by itself (full, or stopped drawing): the car-ended record (R3) starts now. A person's
    charge (`plan` false): their Start's pause ends. The plan's charge in an open window (`plan`): nothing else
    changes; whether the car is full is the target's or the need's own event after it."""

    kind: ClassVar[str] = "car_ended"
    plan: bool = False


@dataclass(frozen=True)
class BalancingPause(Event):
    """Load balancing stops the charge: `pause` (no room for the minimum current) or a safety stop's code.
    `was_on`: the charge was on (only such a charge is one balancing interrupted)."""

    kind: ClassVar[str] = "balancing_pause"
    code: str = "pause"
    was_on: bool = True


@dataclass(frozen=True)
class BalancingResume(Event):
    """Load balancing's regulator resumes the charge it holds back (a battery probe). `charging`: the charger
    already charges."""

    kind: ClassVar[str] = "balancing_resume"
    charging: bool = False


@dataclass(frozen=True)
class TargetReached(Event):
    """The plan's target is reached: its stop, which ends the plan. `gated`: decided through the execution
    boundary's gate (the reading's own listener); a window start or an install decides it without the gate."""

    kind: ClassVar[str] = "target_reached"
    gated: bool = True


@dataclass(frozen=True)
class NeedMet(Event):
    """A plan without a target has delivered what was asked: its charge is stopped and the plan cleared."""

    kind: ClassVar[str] = "need_met"
    control_on: bool = True
    handed_off: bool = False
    solar_holds: bool = False
    plan_auto_owned: bool = True


@dataclass(frozen=True)
class TopOffEnd(Event):
    """A top-off ends (deadline, unplug, the charge off, the car idle): `valid` once its own check holds."""

    kind: ClassVar[str] = "top_off_end"
    reason: str = "deadline"
    valid: bool = True


@dataclass(frozen=True)
class PlanInstalled(Event):
    """A new plan replaces the one in force: a top-off ends; with no window open now a balancing pause's wish
    ends."""

    kind: ClassVar[str] = "plan_installed"
    window_open: bool = False


@dataclass(frozen=True)
class PlanDropped(Event):
    """Auto's plan is dropped without touching the charger (a person's Start paused Auto)."""

    kind: ClassVar[str] = "plan_dropped"


@dataclass(frozen=True)
class Restart(Event):
    """Home Assistant restarted and the session was read back. `legacy_person_stop`: an older release stored a
    person's Stop (`person_stopped`), which becomes the manual pause it now is."""

    kind: ClassVar[str] = "restart"
    legacy_person_stop: bool = False


@dataclass(frozen=True)
class Timer(Event):
    """A timer of the session's own fired: the stop under a person's Stop looks again (`person_hold`)."""

    kind: ClassVar[str] = "timer"
    what: str = "person_hold"
    control_on: bool = False
    start_pending: bool = False


#: What a background task decides again (`Recheck.what`): the command's reason (`ownership.REASON_*`).
RECHECK_HOLD: Final = "hold"
RECHECK_STRAY: Final = "stray"
RECHECK_CLAIM: Final = "claim"
RECHECK_PERSON_HOLD: Final = "person_hold"
RECHECKS: Final = (RECHECK_HOLD, RECHECK_STRAY, RECHECK_CLAIM, RECHECK_PERSON_HOLD)


@dataclass(frozen=True)
class Recheck(Event):
    """A background task a report or a timer spawned (the hold's stop, a stray charge's stop, the claim of a window
    charge, the stop under a person's Stop) has the boundary's lock and decides again whether its command is still
    due, on the session as it is now: a window may have opened, a plan been installed, a person acted meanwhile.

    `what` is the command's reason. The facts are read again under the lock, as at a report: `control_on` is the
    one the task's own rule reads (the charge control on as commanded for the hold and the stop under a person's
    Stop, the control reported on for a claim and a stray charge's stop)."""

    kind: ClassVar[str] = "recheck"
    what: str = RECHECK_HOLD
    control_on: bool = False
    window_ahead_outside: bool = False
    window_open: bool = False
    in_window: bool = False
    plan_present: bool = False
    open_window_start: datetime | None = None
    car_ended_known_full: bool = False
    need_grew: bool = False
    solar_holds: bool = False
    plan_auto_owned: bool = True
    handed_off: bool = False
    top_off: bool = False
    start_pending: bool = False
    owned: bool | None = None


@dataclass(frozen=True)
class CommandResult(Event):
    """What became of a command the core asked for: `executed` (it went out), or not; a start load balancing held
    back for want of room is `balancing_held`; a stop that sent nothing because the charge control said nothing is
    `unobserved` (nothing says whether the charge still runs, so its owner is kept unless the plan was cleared)."""

    kind: ClassVar[str] = "command_result"
    command: str = "start"
    reason: str = ""
    executed: bool = True
    balancing_held: bool = False
    unobserved: bool = False


EVENT_TYPES: Final[dict[str, type[Event]]] = {
    cls.kind: cls
    for cls in (
        PlugIn,
        Unplug,
        ConnectionUnknown,
        WindowStart,
        WindowEnd,
        FinalWindowEnd,
        Rearm,
        PersonStart,
        PersonStop,
        DirectStart,
        DirectStop,
        Resume,
        PauseChoiceMade,
        StrategyChange,
        SolarStart,
        SolarStop,
        MinSocStart,
        MinSocEnd,
        ChargerReportedOn,
        ChargerReportedOff,
        CarEnded,
        BalancingPause,
        BalancingResume,
        TargetReached,
        NeedMet,
        TopOffEnd,
        PlanInstalled,
        PlanDropped,
        Restart,
        Timer,
        Recheck,
        CommandResult,
    )
}


def event_from_dict(raw: Any) -> Event:
    """An event from `Event.to_dict`'s output, or `SessionError`."""
    if not isinstance(raw, dict):
        raise SessionError("an event is an object")
    cls = EVENT_TYPES.get(raw.get("kind"))  # type: ignore[arg-type]
    if cls is None:
        raise SessionError(f"not an event kind: {raw.get('kind')!r}")
    values: dict[str, Any] = {}
    for item in fields(cls):
        if item.name not in raw:
            continue
        value = raw[item.name]
        if isinstance(value, str) and "datetime" in str(item.type):
            try:
                value = datetime.fromisoformat(value)
            except ValueError as err:
                raise SessionError(f"{item.name} is not an instant") from err
        values[item.name] = value
    try:
        return cls(**values)
    except TypeError as err:
        raise SessionError(str(err)) from err
