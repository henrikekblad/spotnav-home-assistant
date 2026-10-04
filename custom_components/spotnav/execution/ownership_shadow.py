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
The shadow never raises into the real path: every failure is caught and counted (`errors`). The events it saw are
kept (`EVENT_RING`, no entity ids, no secrets), so a debug bundle's are replayable (`core/replay.py`).
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

from ..core.events import CommandResult, Event
from ..core.ownership import Command, decide, ORIGIN_OWNER
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
#: Events whose commands' results come back later, as events of their own (a report's or a timer's spawned stop).
_RESULTS_LATER: Final = frozenset({"charger_reported_on", "charger_reported_off", "timer"})
#: Who hears every disagreement, drift and shadow error (`kind`, record): the test suite's collector.
LISTENERS: list[Callable[[str, dict[str, Any]], None]] = []
#: What every shadow in this process counted (a test run's summary).
TOTALS: dict[str, int] = {}
#: Whether the core drives ownership for a charger whose entry data does not say (`const.CONF_CORE_OWNERSHIP`). Off:
#: today's code decides and the core only shadows it. A test run turns it on to run the suite in both modes.
CORE_OWNERSHIP_DEFAULT = False
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


@dataclass
class ShadowToken:
    """One `begin`: the task it belongs to, whether it is the outermost feed of that task, whether it lined the
    session up with today's state (not mid-way through another task's feed), and whether it worked."""

    key: object
    outermost: bool
    ok: bool = True
    lined_up: bool = True
    #: When the core drives: the event decided at `begin` (its facts known up front), the session it was decided
    #: from, and the commands it asked for. `end` then only feeds back the result.
    early: Event | None = None
    early_pre: ChargeSession | None = None
    early_commands: tuple[Command, ...] = ()


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


def _manual_dict(manual: ManualPause | None) -> dict[str, str] | None:
    return None if manual is None else {"action": manual.action, "scope": manual.scope}


def _field(session: ChargeSession, name: str) -> Any:
    if name == FIELD_MANUAL:
        return _manual_dict(session.manual)
    return getattr(session, name)


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
    ) -> None:
        SHADOWS.add(self)
        self._legacy = legacy
        # Step 2: the core drives (today's code acts on `verdict` and takes the core's owner back, `writer`), or it
        # only shadows (today's state stays the truth after a comparison).
        self.drives = drives
        self._writer = writer
        self._now = now if now is not None else dt_util.utcnow
        self.session = ChargeSession()
        # Open feeds per asyncio task (`None`: a callback outside any task), and the tokens themselves.
        self._open: dict[object, int] = {}
        self._stack: dict[object, list[ShadowToken]] = {}
        # A plug-in or an unplug changed the core's intent; today's boundary writes it later (`check`).
        self._intent_deferred = False
        # Feeds that ended while another task's was open, decided after it (`end`).
        self._queue: deque[_Feed] = deque()
        self.counts: dict[str, int] = {
            "events": 0,
            "compared": 0,
            "disagreements": 0,
            "drift": 0,
            "skipped": 0,
            "observed": 0,
            "written_back": 0,
            "verdict_differs": 0,
            "errors": 0,
        }
        self.disagreements: deque[dict[str, Any]] = deque(maxlen=DISAGREEMENT_RING)
        self.drift: deque[dict[str, Any]] = deque(maxlen=DRIFT_RING)
        self.events: deque[dict[str, Any]] = deque(maxlen=EVENT_RING)

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

    def begin(self, early: Event | None = None) -> ShadowToken:
        """Before today's code acts. Never raises. When the core drives, an `early` event (one whose facts are all
        known before anything acts: a plug-in, a person's Start or Stop) is decided here, so what its effects do
        meanwhile (a task Home Assistant starts eagerly inside it) is decided after it."""
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
                self._line_up()
            except Exception:  # noqa: BLE001 - the shadow never raises into the real path
                self._error("begin")
                return ShadowToken(key, True, ok=False)
            token = ShadowToken(key, True)
        self._stack.setdefault(key, []).append(token)
        if early is not None and self.drives:
            self._decide_early(token, early)
        return token

    def _decide_early(self, token: ShadowToken, event: Event) -> tuple[Command, ...]:
        try:
            token.early_pre = self.session
            self.session, token.early_commands = decide(self.session, event, self._now())
            token.early = event
        except Exception:  # noqa: BLE001 - decided at `end` as ever
            self._error("begin_early")
        return token.early_commands

    def _close(self, token: ShadowToken) -> None:
        stack = self._stack.get(token.key)
        if stack is not None:
            if token in stack:
                stack.remove(token)
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
        item = _Feed(token, event, tuple(sorted(legacy)), outcome, fields, defer_intent)
        try:
            if self._open.get(token.key, 0) or self._others_open(token.key):
                # Something that happened while another feed was on its way: decided after that one.
                self._queue.append(item)
                return
            record, core_kinds = self._decide(item)
            self._drain()
            if token.outermost and token.ok and token.lined_up:
                self._compare(record, event.kind, core_kinds, item.legacy, fields)
            else:
                record["unchecked"] = True
        except Exception:  # noqa: BLE001 - the shadow never raises into the real path
            self._error(event.kind)

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

    def restart(self, event: Event) -> None:
        """The record was read back after a restart: the core's session is today's as read, and the restart is fed."""
        try:
            self.session = self._legacy()
            self._intent_deferred = False
        except Exception:  # noqa: BLE001 - the shadow never raises into the real path
            self._error("restart")
            return
        self.feed(event)

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

    def _line_up(self) -> None:
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
        # Today's state is the truth the core decides from; only the core's own pending command is its.
        self.session = replace(today, pending=self.session.pending)

    def _drain(self) -> None:
        while self._queue:
            item = self._queue.popleft()
            record, _kinds = self._decide(item)
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
            )
            session, _ = decide(session, result, now)
        elif acted is not None and item.event.kind not in _RESULTS_LATER:
            # The feed is over and today's code sent nothing: the command the core asked for did not happen.
            result = CommandResult(command=acted.kind, reason=getattr(acted, "reason", ""), executed=False)
            session, _ = decide(session, result, now)
        self.session = session
        self._count("events")
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
        self.events.append(record)
        if item.defer_intent and (session.manual, session.span_pause) != (pre.manual, pre.span_pause):
            self._intent_deferred = True
        return record, tuple(sorted(command.kind for command in commands if command.kind != "keep"))

    def _compare(
        self,
        record: dict[str, Any] | None,
        where: str,
        core_kinds: tuple[str, ...],
        legacy: tuple[str, ...],
        fields: tuple[str, ...],
        *,
        settle: bool = False,
    ) -> None:
        today = self._legacy()
        if self._intent_deferred and not settle:
            fields = tuple(name for name in fields if name not in INTENT)
        self._count("compared")
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
        self._disagree(where, differs, core_kinds, legacy)
        if self.drives:
            # The core's state is the truth: today's code takes its owner back (`_write_back`).
            return
        # Today's state stays the truth.
        keep = self.session
        self.session = replace(today, pending=keep.pending)
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
        except Exception:  # noqa: BLE001 - today's decision stands
            self._error("verdict")
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

    def _error(self, where: str) -> None:
        self._count("errors")
        _LOGGER.debug("SpotNav ownership shadow failed at %s", where, exc_info=True)
        self._tell("error", {"where": where})

    def _tell(self, kind: str, record: dict[str, Any]) -> None:
        for listener in list(LISTENERS):
            try:
                listener(kind, record)
            except Exception:  # noqa: BLE001 - a listener must not break the shadow
                _LOGGER.debug("Shadow listener failed", exc_info=True)

    # ------------------------------------------------------------------ reading

    def diagnostics(self) -> dict[str, Any]:
        """For diagnostics and the debug bundle: the counts, the session, the rings."""
        return {
            "drives": self.drives,
            "counts": dict(self.counts),
            "session": self.session.to_dict(),
            "disagreements": list(self.disagreements),
            "drift": list(self.drift),
            "events": list(self.events),
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
