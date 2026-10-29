"""The charge-ownership core in shadow mode: fed beside today's code, compared, never acting.

Step 1 of the state-machine refactor (docs/architecture-state.md). At each place `ChargingController` or
`AutoExecutor` changes who owns a charge or which person intent holds, or sends a start or stop because of it, the
same happening is fed to the pure core (`core/ownership.py`) as an event with the facts it needs, and what the core
decides is compared with what today's code did:

* `begin()` before today's code acts: the core's session is lined up with today's state, read from the
  controller and the executor's stored pause (`legacy_session`). A difference in the owner or the person intent
  that no event explains is *drift* (something changed it where no event is fed): it is recorded and the core
  takes today's state, so one gap never cascades.
* `end(token, event, legacy=..., outcome=...)` after it: the core decides from the event; a start or stop it asks
  for is followed by what became of today's one (`CommandOutcome`); then the owner and intent are read from today's
  code again and compared, with the commands. A difference is a *disagreement*: logged at debug level and kept
  in a bounded ring with the events before it.

Nested feeds (a `_notify` inside a stop, say) decide and record but compare nothing; the outermost one compares.
Nesting is counted per asyncio task, so a report callback that lands while a command of another task is on its way
is a feed of its own; while another task's feed is open, today's state is mid-way, so nothing is compared or
called drift then (`skipped`). A plug-in or an unplug ends a manual pause in the core at once, while today's
execution boundary writes that a moment later: the core's intent is kept until the boundary's own `check`.
The shadow never raises into the real path: every failure is caught and counted (`errors`). A decision that could not
be taken (the core raised, or its facts or its stored session could not be read) is a *failure* (`failed`): today's
code decides that event, as ever, but not silently. The events it saw are
kept (`EVENT_RING`, no entity ids, no secrets), so a debug bundle's are replayable (`core/replay.py`). What it decided,
compared, disagreed on, found drift at and failed on is also tallied per event kind, across restarts
(`ownership_coverage.py`).
"""

from __future__ import annotations

import asyncio
from collections import deque
import weakref
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
import logging
from typing import Any, Final

from homeassistant.util import dt as dt_util

from .ownership_coverage import OwnershipCoverage

from ..core.events import CommandResult, Event
from ..core.ownership import Command, decide, ORIGIN_OWNER
from ..core.replay import CLOCK_TOLERANCE_S
from ..core.session import (
    ChargeSession,
    ManualPause,
    MANUAL_STOP,
    OWNER_CHARGER_SELF,
    OWNER_NONE,
    OWNER_PLAN,
    OWNER_TOP_OFF,
    SCOPE_PLUG_IN,
)

_LOGGER = logging.getLogger(__name__)

#: How many disagreements, drifts and events are kept.
DISAGREEMENT_RING: Final = 50
DRIFT_RING: Final = 50
EVENT_RING: Final = 200
#: How many of the events before a disagreement are kept with it.
CONTEXT_EVENTS: Final = 20

#: What is compared after an event: the owner, and the person intent.
FIELD_OWNER: Final = "owner"
FIELD_MANUAL: Final = "manual"
FIELD_SPAN: Final = "span_pause"
COMPARED: Final = (FIELD_OWNER, FIELD_MANUAL, FIELD_SPAN)
INTENT: Final = (FIELD_MANUAL, FIELD_SPAN)
#: The session's fields a group the core owns keeps as it decided them (`OwnershipShadow.owned_fields`, the core end
#: state plan's steps 3-6): the line-up does not take them from today's state. Step 3: the safety hold and C7's stops,
#: all transient (`core.session.TRANSIENT_FIELDS`), so nothing stored changes.
SAFETY_AND_C7: Final = (
    "held_for_safety",
    "safety_stopped_at",
    "hold_stop_times",
    "hold_tried_at",
    "hold_gave_up",
    "hold_stop_pending",
)
#: Step 4: load balancing's memory, whether it holds a charge back and whose it was. Stored in the session's record,
#: and still written to today's `balancing_pause` key, derived from the session, so an older build reads it as before.
BALANCING: Final = ("balancing_paused", "paused_origin")
#: How far apart two readings of the clock may be and still be one instant in an owned field: the core takes the
#: instant it decided, today's code its own a moment earlier or later (`core.replay.CLOCK_TOLERANCE_S`).
MIRROR_CLOCK_S: Final = CLOCK_TOLERANCE_S
#: Events whose commands' results come back later, as events of their own (a report's or a timer's spawned stop).
_RESULTS_LATER: Final = frozenset({"charger_reported_on", "charger_reported_off", "timer"})
#: A charger's report of its control: recorded only when it decided something or something moved, so a charger
#: that reports every few seconds does not push the plug-in and a person's actions out of the event ring.
_REPORTS: Final = frozenset({"charger_reported_on", "charger_reported_off"})
#: Who hears every disagreement, drift and shadow error (`kind`, record): the test suite's collector.
LISTENERS: list[Callable[[str, dict[str, Any]], None]] = []
#: What every shadow in this process counted (a test run's summary).
TOTALS: dict[str, int] = {}
#: Whether the core drives ownership for a charger whose entry data does not say (`const.CONF_CORE_OWNERSHIP`). On:
#: the core decides and today's code is still computed beside it and compared. An entry with the key false keeps the
#: previous logic (today's code decides, the core only shadows it). A test run turns it off to run the suite in both
#: modes.
CORE_OWNERSHIP_DEFAULT = True
#: Every shadow alive (a test's dump of what each recorded).
SHADOWS: weakref.WeakSet[OwnershipShadow] = weakref.WeakSet()


@dataclass(frozen=True)
class CommandOutcome:
    """What became of today's start or stop: it went out, or not; a start load balancing held back is
    `balancing_held`."""

    executed: bool
    balancing_held: bool = False
    #: A stop that sent nothing because the charge control said nothing (unreadable): who owns the charge is kept.
    unobserved: bool = False
    #: A stop that sent nothing because a stop of ours was still on its way (`_stop_settling`).
    settled: bool = False


@dataclass(eq=False)
class ShadowToken:
    """One `begin`: the task it belongs to, whether it is the outermost feed of that task, whether it lined the
    session up with today's state (not mid-way through another task's feed), and whether it worked. Compared by
    identity: two nested feeds of one task look alike, and closing the inner must not take the outer off the stack."""

    key: object
    outermost: bool
    ok: bool = True
    lined_up: bool = True
    #: A failure this feed met is already told (`failed`): one event is one failure, however many steps it failed at.
    reported: bool = False
    #: What lining the feed up failed with, told at its end (`failed`) under the kind of the event it ends with.
    error: BaseException | None = None
    #: Whether lining up found drift, or failed: counted under the kind of the event this feed ends with.
    drift: bool = False
    failed: bool = False
    #: When the core drives: the event decided at `begin` (its facts known up front), the session it was decided
    #: from, and the commands it asked for. `end` then only feeds back the result.
    early: Event | None = None
    early_pre: ChargeSession | None = None
    early_commands: tuple[Command, ...] = ()
    #: When it was decided at `begin`, on the shadow's decision clock (`_decide`'s `order`): its record says so.
    early_order: int = 0


@dataclass(frozen=True)
class _Feed:
    """One ended feed, decided at once or queued behind another task's."""

    token: ShadowToken
    event: Event
    legacy: tuple[str, ...]
    outcome: CommandOutcome | None
    fields: tuple[str, ...]
    defer_intent: bool


def _task_key() -> object:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class NullShadow:
    """A shadow that does nothing, for a boundary built around something that is not a `ChargingController`."""

    depth = 0
    drives = False

    def begin(self, early: Event | None = None) -> ShadowToken:
        return ShadowToken(None, False, ok=False)

    def verdict(self, event: Event) -> None:
        return None

    def alone(self, own: int = 0) -> bool:
        return True

    def choose(self, site: str, today: bool, core: bool) -> bool:
        return core

    def end(self, token: ShadowToken, event: Event, **_kwargs: Any) -> None:
        return

    def cancel(self, token: ShadowToken) -> None:
        return

    def feed(self, event: Event, **_kwargs: Any) -> None:
        return

    def check(self, fields: tuple[str, ...], where: str) -> None:
        return

    def failed(self, where: str, kind: str | None, error: BaseException | None = None) -> None:
        return


def _manual_dict(manual: ManualPause | None) -> dict[str, str] | None:
    return None if manual is None else {"action": manual.action, "scope": manual.scope}


def _field(session: ChargeSession, name: str) -> Any:
    if name == FIELD_MANUAL:
        return _manual_dict(session.manual)
    return getattr(session, name)


def _same(core: Any, today: Any) -> bool:
    """One value of an owned field, read from the core and from today's state: an instant, or a list of them of the
    same length, within `MIRROR_CLOCK_S` of each other is the same."""
    if core == today:
        return True
    if isinstance(core, datetime) and isinstance(today, datetime):
        return abs((core - today).total_seconds()) <= MIRROR_CLOCK_S
    if isinstance(core, tuple) and isinstance(today, tuple) and len(core) == len(today):
        return all(_same(a, b) for a, b in zip(core, today, strict=True))
    return False


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _compact(session: ChargeSession) -> dict[str, Any]:
    """A session as its non-default fields (a debug bundle keeps 200 of them): `core/replay.py` fills the rest."""
    full = session.to_dict()
    default = ChargeSession().to_dict()
    return {key: value for key, value in full.items() if key == "version" or value != default[key]}


class OwnershipShadow:
    """The core in shadow mode for one charger. `legacy` reads today's state as a session; `now` is the clock."""

    def __init__(
        self,
        legacy: Callable[[], ChargeSession],
        *,
        now: Callable[[], datetime] | None = None,
        drives: bool = False,
        writer: Callable[[ChargeSession], bool] | None = None,
        persist: Callable[[ChargeSession], None] | None = None,
        coverage: OwnershipCoverage | None = None,
        name: str = "",
        on_failure: Callable[[], None] | None = None,
        owned_fields: Iterable[str] = (),
    ) -> None:
        SHADOWS.add(self)
        self._legacy = legacy
        # The session's fields the core keeps as it decided them (`SAFETY_AND_C7` when the core drives): never taken
        # from today's state, which is only compared with them (`mirror_drift`).
        self.owned_fields: tuple[str, ...] = tuple(owned_fields)
        # Which charger this is, in a log line; and who hears a failure while the core drives (Repairs, `failed`).
        self._name = name
        self._on_failure = on_failure
        # The failures already logged as a warning this run, as `(where, kind)`: each is logged once, then counted.
        self._warned: set[tuple[str, str | None]] = set()
        # Step 2: the core drives (today's code acts on `verdict`, and a charge the core says nobody owns clears
        # today's owner fields, `writer`), or it only shadows (today's state stays the truth after a comparison).
        self.drives = drives
        self._writer = writer
        # When the core drives: told the session after each decision, to keep it across a restart (debounced by the
        # caller: a decision that leaves the stored record as it was writes nothing).
        self._persist = persist
        self._now = now if now is not None else dt_util.utcnow
        # What it has seen per event kind, cumulative (kept across restarts when the controller gives it a store).
        self.coverage = coverage if coverage is not None else OwnershipCoverage(now=self._now)
        self.session = ChargeSession()
        # Open feeds per asyncio task (`None`: a callback outside any task), and the tokens themselves.
        self._open: dict[object, int] = {}
        self._stack: dict[object, list[ShadowToken]] = {}
        # A plug-in or an unplug changed the core's intent; today's boundary writes it later (`check`).
        self._intent_deferred = False
        # Feeds that ended while another task's was open, decided after it (`end`).
        self._queue: deque[_Feed] = deque()
        # `failures`: per `where/kind`, the decisions that could not be taken (`failed`); `last_failure`: when.
        self.counts: dict[str, Any] = {
            "events": 0,
            "compared": 0,
            "disagreements": 0,
            "drift": 0,
            "mirror_drift": 0,
            "skipped": 0,
            "observed": 0,
            "written_back": 0,
            "verdict_differs": 0,
            "quiet": 0,
            "errors": 0,
            "failures": {},
            "last_failure": None,
        }
        self.disagreements: deque[dict[str, Any]] = deque(maxlen=DISAGREEMENT_RING)
        self.drift: deque[dict[str, Any]] = deque(maxlen=DRIFT_RING)
        self.events: deque[dict[str, Any]] = deque(maxlen=EVENT_RING)
        # Records kept so far, and a quiet report's record held back with the count at the time (`_decide`).
        self._recorded = 0
        self._quiet: tuple[dict[str, Any], int] | None = None
        # The decision clock (`_tick`).
        self._ticks = 0

    # ------------------------------------------------------------------ feeding

    @property
    def depth(self) -> int:
        """How many feeds the running task has open."""
        return self._open.get(_task_key(), 0)

    def alone(self, own: int = 0) -> bool:
        """Whether no feed is open but the running task's own `own` ones: the core's session then says what it
        decided so far, with nothing queued behind another feed."""
        key = _task_key()
        return self._open.get(key, 0) <= own and not self._others_open(key)

    def _others_open(self, key: object) -> bool:
        # A task that ended with a feed open (cancelled between `begin` and `end`) holds nothing any more.
        for other in [other for other in self._open if other is not None and other is not key]:
            done = getattr(other, "done", None)
            if done is not None and done():
                self._open.pop(other, None)
        return any(count > 0 for other, count in self._open.items() if other is not key)

    def begin(self, early: Event | None = None, *, line_up: bool = True) -> ShadowToken:
        """Before today's code acts. Never raises. When the core drives, an `early` event (one whose facts are all
        known before anything acts: a plug-in, a person's Start or Stop) is decided here, so what its effects do
        meanwhile (a task Home Assistant starts eagerly inside it) is decided after it. `line_up=False` decides from
        the core's own session as it is (a restart from the stored record)."""
        key = _task_key()
        depth = self._open.get(key, 0)
        others = self._others_open(key)
        self._open[key] = depth + 1
        if depth:
            token = ShadowToken(key, False)
        elif others:
            # Mid-way through another task's feed (a task Home Assistant started eagerly inside it, or one that
            # landed while it awaited a command): today's state is not to be read as a whole now.
            token = ShadowToken(key, True, lined_up=False)
        else:
            try:
                drifted = self._line_up() if line_up else False
            except Exception as err:  # noqa: BLE001 - the shadow never raises into the real path
                # Counted under the kind of the event the feed ends with (`end`), or none (`cancel`).
                self._error("begin", cover=False)
                return ShadowToken(key, True, ok=False, failed=True, error=err)
            token = ShadowToken(key, True, drift=drifted)
        self._stack.setdefault(key, []).append(token)
        if early is not None and self.drives:
            self._decide_early(token, early)
        return token

    def _decide_early(self, token: ShadowToken, event: Event) -> tuple[Command, ...]:
        try:
            token.early_pre = self.session
            token.early_order = self._tick()
            self.session, token.early_commands = decide(self.session, event, self._now())
            token.early = event
        except Exception as err:  # noqa: BLE001 - decided at `end` as ever
            self._error("begin_early", event.kind)
            self.failed("decide_early", event.kind, err, token=token)
        return token.early_commands

    def _close(self, token: ShadowToken) -> None:
        stack = self._stack.get(token.key)
        if stack is not None:
            for index in range(len(stack) - 1, -1, -1):
                if stack[index] is token:
                    del stack[index]
                    break
            if not stack:
                self._stack.pop(token.key, None)
        count = self._open.get(token.key, 0) - 1
        if count > 0:
            self._open[token.key] = count
        else:
            self._open.pop(token.key, None)

    def _idle(self) -> bool:
        return not self._others_open(None) and not self._open.get(None, 0)

    def cancel(self, token: ShadowToken) -> None:
        """A `begin` whose happening turned out to be none (nothing to feed)."""
        if token.early is not None:
            # Decided already, and acted on: it is fed as it was decided.
            self.end(token, token.early)
            return
        self._close(token)
        self._cover_token(token, None)
        if self._queue and self._idle():
            try:
                self._drain()
                self._compare(None, "queued", (), (), COMPARED)
            except Exception:  # noqa: BLE001 - the shadow never raises into the real path
                self._error("cancel")

    def end(
        self,
        token: ShadowToken,
        event: Event,
        *,
        legacy: Iterable[str] = (),
        outcome: CommandOutcome | None = None,
        fields: tuple[str, ...] = COMPARED,
        defer_intent: bool = False,
    ) -> None:
        """After today's code acted: `legacy` the commands it decided (`start`, `stop`, `notify`), `outcome` what
        became of its start or stop, `fields` what to compare; `defer_intent` for a plug-in or an unplug, whose
        intent today's boundary writes later. Never raises.

        A feed that ends while another is open (inside it, or in another task) waits in a queue and is decided after
        it, which compares the state for all of them: a command a report spawned eagerly has its result before the
        report itself ends, and a report that lands while a start awaits its command is decided after the start.
        The commands of a queued feed are compared with what today's code decided for it."""
        self._close(token)
        self._cover_token(token, event.kind)
        item = _Feed(token, event, tuple(sorted(legacy)), outcome, fields, defer_intent)
        try:
            if self._open.get(token.key, 0) or self._others_open(token.key):
                # Something that happened while another feed was on its way: decided after that one.
                self._queue.append(item)
                return
            record, core_kinds = self._decide(item)
            self._drain()
            if token.outermost and token.ok and token.lined_up:
                self._compare(record, event.kind, core_kinds, item.legacy, fields, kind=event.kind)
            else:
                record["unchecked"] = True
        except Exception as err:  # noqa: BLE001 - the shadow never raises into the real path
            self._error(event.kind, event.kind)
            self.failed("decide", event.kind, err, token=token)
        finally:
            self._quiet = None
        self._persisted()

    def feed(
        self,
        event: Event,
        *,
        legacy: Iterable[str] = (),
        outcome: CommandOutcome | None = None,
        fields: tuple[str, ...] = COMPARED,
    ) -> None:
        """`begin` and `end` at once, for a happening that has not changed today's state yet, or that today's code
        decides with no state of its own changing (a hand-off at a window's end)."""
        self.end(self.begin(), event, legacy=legacy, outcome=outcome, fields=fields)

    def restart(self, event: Event, stored: ChargeSession | None = None, *, keys_newer: bool = False) -> None:
        """The record was read back after a restart, and the restart is fed. The core's session is today's as read;
        when the core drives and its own stored record was read (`stored`), that record, decided from as it is and
        then compared with today's. Whether the charger charges by itself is seen now, not stored: an owner of
        nobody or of the charger itself is today's. Today's keys written after the record's last change
        (`keys_newer`) are the newer: what the session reads from them is today's, and only the person intent
        (kept by the execution boundary, not in those keys) is the record's."""
        from_store = stored is not None and self.drives
        try:
            today = self._legacy()
            if stored is not None and from_store:
                if keys_newer:
                    stored = replace(today, manual=stored.manual, span_pause=stored.span_pause)
                if {stored.owner, today.owner} <= {OWNER_NONE, OWNER_CHARGER_SELF}:
                    stored = replace(stored, owner=today.owner)
                self.session = stored
            else:
                self.session = today
            self._intent_deferred = False
        except Exception as err:  # noqa: BLE001 - the shadow never raises into the real path
            self._error("restart", event.kind)
            self.failed("restart", event.kind, err)
            return
        self.end(self.begin(line_up=not from_store), event)

    def check(self, fields: tuple[str, ...], where: str) -> None:
        """Compare now, with no event: for an effect today's code makes later than the event that causes it (a
        manual pause ended at an unplug, which the execution boundary writes after the controller saw it)."""
        key = _task_key()
        try:
            if self._open.get(key, 0) or self._others_open(key):
                self._count("skipped")
            else:
                self._compare(None, where, (), (), fields, settle=True)
            self._intent_deferred = False
        except Exception:  # noqa: BLE001
            self._error("check")

    # ------------------------------------------------------------------ inside

    def _line_up(self) -> bool:
        """Line the core's session up with today's state; whether it found drift."""
        today = self._legacy()
        if self._intent_deferred:
            # The boundary has not written the plug-in's or the unplug's effect on the intent yet.
            today = replace(today, manual=self.session.manual, span_pause=self.session.span_pause)
        moved = [name for name in COMPARED if _field(today, name) != _field(self.session, name)]
        if moved == [FIELD_OWNER] and {today.owner, self.session.owner} == {OWNER_NONE, OWNER_CHARGER_SELF}:
            # The charger's own charge is an observation (it charges, nobody here started it): one that began or
            # ended with no report (a test's state, a stop's answer window running out) is no ownership drift.
            self._count("observed")
            moved = []
        if moved:
            record = {
                "at": self._now().isoformat(),
                "fields": {name: {"core": _field(self.session, name), "today": _field(today, name)} for name in moved},
                "after": self.events[-1]["event"]["kind"] if self.events else None,
            }
            self._count("drift")
            self.drift.append(record)
            _LOGGER.debug("SpotNav ownership shadow: drift %s", record)
            self._tell("drift", record)
        self._mirror_drift(today)
        # Today's state is the truth the core decides from; only the core's own pending command and the fields it
        # owns are its.
        self.session = self._adopt(today, self.session)
        return bool(moved)

    def _adopt(self, today: ChargeSession, core: ChargeSession) -> ChargeSession:
        """Today's state, with the core's pending command and owned fields kept."""
        owned = {name: getattr(core, name) for name in self.owned_fields}
        return replace(today, pending=core.pending, **owned)

    def _mirror_drift(self, today: ChargeSession) -> None:
        """An owned field today's state reads otherwise than the core decided it: recorded and told, never taken.
        The controller reads an owned group from the session (its accessor), so there is none unless something reads
        or keeps that group around the accessor."""
        moved = [
            name for name in self.owned_fields if not _same(getattr(self.session, name), getattr(today, name))
        ]
        if not moved:
            return
        record = {
            "at": self._now().isoformat(),
            "mirror": True,
            "fields": {
                name: {"core": _plain(getattr(self.session, name)), "today": _plain(getattr(today, name))}
                for name in moved
            },
            "after": self.events[-1]["event"]["kind"] if self.events else None,
        }
        self._count("mirror_drift")
        self.drift.append(record)
        _LOGGER.debug("SpotNav ownership shadow: mirror drift %s", record)
        self._tell("mirror_drift", record)

    def _drain(self) -> None:
        held = self._quiet
        while self._queue:
            item = self._queue.popleft()
            record, _kinds = self._decide(item)
            # A quiet queued report is compared with nothing: it is not kept.
            self._quiet = held
            # Decided after the feed it landed in, while today's code decided it mid-way through that one: its
            # commands are kept in the record, and only the state after both is compared.
            record["queued"] = True
            record["unchecked"] = True

    def _decide(self, item: _Feed) -> tuple[dict[str, Any], tuple[str, ...]]:
        now = self._now()
        if item.token.early is not None and item.token.early_pre is not None:
            # Decided at `begin`: only its result is new.
            pre, session, commands = item.token.early_pre, self.session, item.token.early_commands
        else:
            pre = self.session
            session, commands = decide(pre, item.event, now)
        acted = next((command for command in commands if command.kind in ("start", "stop")), None)
        result: CommandResult | None = None
        if item.outcome is not None and acted is not None:
            result = CommandResult(
                command=acted.kind,
                reason=getattr(acted, "reason", ""),
                executed=item.outcome.executed,
                balancing_held=item.outcome.balancing_held,
                unobserved=item.outcome.unobserved,
                settled=item.outcome.settled,
            )
            session, _ = decide(session, result, now)
        elif acted is not None and item.event.kind not in _RESULTS_LATER:
            # The feed is over and today's code sent nothing: the command the core asked for did not happen.
            result = CommandResult(command=acted.kind, reason=getattr(acted, "reason", ""), executed=False)
            session, _ = decide(session, result, now)
        self.session = session
        self._count("events")
        self._cover(item.event.kind, "events")
        TOTALS[f"event:{item.event.kind}"] = TOTALS.get(f"event:{item.event.kind}", 0) + 1
        record: dict[str, Any] = {
            "at": now.isoformat(),
            "event": item.event.to_dict(),
            "core": [command.to_dict() for command in commands if command.kind != "keep"],
            "today": list(item.legacy),
        }
        if result is not None:
            record["result"] = result.to_dict()
        if item.token.outermost and item.token.lined_up:
            record["pre"] = _compact(pre)
        # The decision clock: when this record's event (or, decided at `begin`, its result) was decided, and when
        # an event decided at `begin` was. A replay decides each in that order (`core/replay.py`), so a feed decided
        # at its `begin` comes before the feeds in it that are kept before its own record.
        record["order"] = self._tick()
        if item.token.early is not None and item.token.early_pre is not None:
            record["early"] = item.token.early_order
        if (
            item.event.kind in _REPORTS
            and session == pre
            and not item.legacy
            and result is None
            and all(command.kind == "keep" for command in commands)
        ):
            # Nothing decided, nothing moved: kept only if a disagreement comes of it (`_compare`).
            self._count("quiet")
            self._quiet = (record, self._recorded)
        else:
            self._record(record)
        if item.defer_intent and (session.manual, session.span_pause) != (pre.manual, pre.span_pause):
            self._intent_deferred = True
        return record, tuple(sorted(command.kind for command in commands if command.kind != "keep"))

    def _tick(self) -> int:
        self._ticks += 1
        return self._ticks

    def _record(self, record: dict[str, Any]) -> None:
        self.events.append(record)
        self._recorded += 1

    def _keep_quiet(self, record: dict[str, Any] | None) -> None:
        """A quiet report a disagreement came of is kept after all, in its place before the records decided after
        it."""
        quiet = self._quiet
        if record is None or quiet is None or quiet[0] is not record:
            return
        self._quiet = None
        items = list(self.events)
        items.insert(len(items) - min(self._recorded - quiet[1], len(items)), record)
        self.events = deque(items, maxlen=EVENT_RING)
        self._recorded += 1

    def _compare(
        self,
        record: dict[str, Any] | None,
        where: str,
        core_kinds: tuple[str, ...],
        legacy: tuple[str, ...],
        fields: tuple[str, ...],
        *,
        settle: bool = False,
        kind: str | None = None,
    ) -> None:
        """`kind`: the event kind the comparison follows (`None`: a comparison of no event's own)."""
        today = self._legacy()
        if self._intent_deferred and not settle:
            fields = tuple(name for name in fields if name not in INTENT)
        self._count("compared")
        self._cover(kind, "compared")
        differs = {
            name: {"core": _field(self.session, name), "today": _field(today, name)}
            for name in fields
            if _field(today, name) != _field(self.session, name)
        }
        if record is not None:
            record["today_after"] = {name: _field(today, name) for name in COMPARED}
        if self.drives:
            self._write_back(self._today_fields())
        if not differs and core_kinds == legacy:
            return
        self._keep_quiet(record)
        self._disagree(where, differs, core_kinds, legacy)
        self._cover(kind, "disagreements")
        if self.drives:
            # The core's state is the truth (today's owner fields are cleared one-way, `_write_back`).
            return
        # Today's state stays the truth, but for the fields the core owns.
        keep = self.session
        self.session = self._adopt(today, keep)
        if self._intent_deferred and not settle:
            self.session = replace(self.session, manual=keep.manual, span_pause=keep.span_pause)

    def _disagree(
        self,
        where: str,
        differs: dict[str, Any],
        core_kinds: tuple[str, ...],
        legacy: tuple[str, ...],
        *,
        queued: bool = False,
    ) -> None:
        disagreement: dict[str, Any] = {
            "at": self._now().isoformat(),
            "where": where,
            "queued": queued,
            "fields": differs,
            "commands": None if core_kinds == legacy else {"core": list(core_kinds), "today": list(legacy)},
            "events": list(self.events)[-CONTEXT_EVENTS:],
        }
        self._count("disagreements")
        self.disagreements.append(disagreement)
        _LOGGER.debug("SpotNav ownership shadow: disagreement at %s: %s %s", where, differs, disagreement["commands"])
        self._tell("disagreement", disagreement)

    def _persisted(self) -> None:
        """When the core drives: tell the keeper of the stored record the session as it is now (never raises)."""
        persist = self._persist
        if not self.drives or persist is None or self.depth:
            return
        try:
            persist(self.session)
        except Exception:  # noqa: BLE001 - the shadow never raises into the real path
            self._error("persist")

    def _write_back(self, today: str | None = None) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            changed = writer(self.session)
        except Exception:  # noqa: BLE001 - the shadow never raises into the real path
            self._error("write_back")
            return
        if changed:
            # Today's own fields said otherwise: the core's owner overrode them.
            self._count("written_back")
            self._tell(
                "write_back",
                {"owner": self.session.owner, "today": today, "events": list(self.events)[-CONTEXT_EVENTS:]},
            )

    def _today_fields(self) -> str | None:
        reader = getattr(self, "today_fields", None)
        return None if reader is None else reader()

    def preview(self, event: Event) -> tuple[ChargeSession, tuple[Command, ...]]:
        """What the core decides for `event` now, from its session (lined up by the `begin` before), without taking
        it: today's code acts on it when the core drives (`verdict`)."""
        return decide(self.session, event, self._now())

    def verdict(self, event: Event) -> frozenset[tuple[str, str]] | None:
        """When the core drives: the commands it decides for `event` now, as `(kind, reason)` (a notification's code
        as its reason). `None` when it does not drive, or could not decide: today's code then decides as ever."""
        if not self.drives:
            return None
        stack = self._stack.get(_task_key())
        token = stack[-1] if stack else None
        try:
            if token is not None and token.early is None:
                # The decision today's code is about to act on is this feed's: taken now, before its effects.
                commands = self._decide_early(token, event)
                if token.early is None:
                    return None
            else:
                _session, commands = self.preview(event)
        except Exception as err:  # noqa: BLE001 - today's decision stands
            self._error("verdict", event.kind)
            self.failed("verdict", event.kind, err, token=token)
            return None
        return frozenset(
            (command.kind, str(getattr(command, "reason", getattr(command, "code", ""))))
            for command in commands
            if command.kind != "keep"
        )

    def choose(self, site: str, today: bool, core: bool) -> bool:
        """When the core drives: the core's choice at `site`; one today's rule (on the same state) would have made
        differently is counted and told (`verdict_differs`)."""
        if today != core:
            self._count("verdict_differs")
            self._tell("verdict_differs", {"site": site, "today": today, "core": core, "events": list(self.events)[-8:]})
        return core

    def _count(self, name: str) -> None:
        self.counts[name] += 1
        TOTALS[name] = TOTALS.get(name, 0) + 1

    def _cover(self, kind: str | None, field: str) -> None:
        try:
            self.coverage.add(kind, field)
        except Exception:  # noqa: BLE001 - the tally never breaks the shadow
            _LOGGER.debug("SpotNav ownership coverage failed", exc_info=True)

    def _cover_token(self, token: ShadowToken, kind: str | None) -> None:
        """What lining the feed up met, under the kind of the event it ends with (`None`: it ended with none)."""
        if token.drift:
            self._cover(kind, "drift")
        if token.failed:
            self._cover(kind, "errors")
            # The session could not be lined up with today's state: whatever was decided from it is not to be trusted.
            self.failed("line_up", kind, token.error, token=token)

    def _error(self, where: str, kind: str | None = None, *, cover: bool = True) -> None:
        """A failure at `where`, counted under the event `kind` it met (`None`: no event's), unless counted later
        (`cover=False`: lining a feed up, counted at its end)."""
        self._count("errors")
        if cover:
            self._cover(kind, "errors")
        _LOGGER.debug("SpotNav ownership shadow failed at %s", where, exc_info=True)
        self._tell("error", {"where": where})

    def failed(
        self,
        where: str,
        kind: str | None,
        error: BaseException | None = None,
        *,
        token: ShadowToken | None = None,
    ) -> None:
        """A decision that could not be taken at `where` for an event of `kind` (`None`: no event's): the core raised,
        or its facts or its stored session could not be read. Today's code decides that event, as ever. Counted per
        `where/kind` with the time of the last (`counts`, so diagnostics and the debug bundle carry it). While the core
        drives, the first of each `(where, kind)` this run is a warning in the log, and every one is told to
        `on_failure` (which raises a Repairs issue only when failures repeat); while it only shadows, a debug line.
        A feed's `token` makes one event one failure, however many of its steps failed. Never raises."""
        try:
            if token is not None:
                if token.reported:
                    return
                token.reported = True
            failures = self.counts["failures"]
            label = f"{where}/{kind or '-'}"
            failures[label] = failures.get(label, 0) + 1
            self.counts["last_failure"] = self._now().isoformat()
            TOTALS["failures"] = TOTALS.get("failures", 0) + 1
            if self.drives and (where, kind) not in self._warned:
                self._warned.add((where, kind))
                _LOGGER.warning(
                    "SpotNav charger %s: the charging decision could not be taken at %s (%s), so the previous logic "
                    "decided it; later ones are only counted (diagnostics)",
                    self._name,
                    where,
                    kind or "no event",
                    exc_info=error,
                )
            else:
                _LOGGER.debug(
                    "SpotNav charger %s: the charging decision could not be taken at %s (%s)",
                    self._name,
                    where,
                    kind or "no event",
                    exc_info=error,
                )
            self._tell("failure", {"where": where, "kind": kind})
            if self.drives and self._on_failure is not None:
                self._on_failure()
        except Exception:  # noqa: BLE001 - the shadow never raises into the real path
            _LOGGER.debug("SpotNav ownership shadow: a failure could not be told", exc_info=True)

    def _tell(self, kind: str, record: dict[str, Any]) -> None:
        for listener in list(LISTENERS):
            try:
                listener(kind, record)
            except Exception:  # noqa: BLE001 - a listener must not break the shadow
                _LOGGER.debug("Shadow listener failed", exc_info=True)

    # ------------------------------------------------------------------ reading

    def diagnostics(self) -> dict[str, Any]:
        """For diagnostics and the debug bundle: the counts, the session, the rings, and the cumulative tally per
        event kind (`coverage`)."""
        return {
            "drives": self.drives,
            "counts": dict(self.counts),
            "session": self.session.to_dict(),
            "disagreements": list(self.disagreements),
            "drift": list(self.drift),
            "events": list(self.events),
            "coverage": self.coverage.as_dict(),
        }


# ---------------------------------------------------------------------------------------------- today's state


def legacy_owner(
    *,
    origin: str | None,
    top_off: bool,
    charging: bool,
    start_pending: bool,
    stop_recent: bool,
) -> str:
    """Today's owner, in the core's words: from `charge_origin`, a top-off past the last window, and, with no
    origin, a charge the charger began by itself (charging, no start or stop of ours on its way)."""
    owner = ORIGIN_OWNER.get(origin) if origin is not None else None
    if owner == OWNER_PLAN and top_off:
        return OWNER_TOP_OFF
    if owner is not None:
        return owner
    if charging and not start_pending and not stop_recent:
        return OWNER_CHARGER_SELF
    return OWNER_NONE


def legacy_intent(pause: Any, legacy_person_stopped: bool) -> tuple[ManualPause | None, str | None]:
    """Today's person intent from the executor's stored pause (`PauseIntent`), or a person's Stop an older release
    stored that the boundary has not taken over yet."""
    if pause is not None and getattr(pause, "manual", False):
        try:
            return ManualPause(pause.action, pause.scope), None
        except ValueError:
            return None, None
    if pause is not None and getattr(pause, "admitted", False):
        return None, pause.choice
    if legacy_person_stopped:
        return ManualPause(MANUAL_STOP, SCOPE_PLUG_IN), None
    return None, None


def commands_kinds(commands: Iterable[Command]) -> tuple[str, ...]:
    return tuple(sorted(command.kind for command in commands if command.kind != "keep"))
