"""The charge-ownership core's runner: the one place `core/ownership.decide` is called for a charger.

At each place `ChargingController` or `AutoExecutor` meets a happening that may change who owns a charge or which
person intent holds, or that may send a start or stop because of it, the happening is fed to the pure core as an
event with the facts it needs, read right before the decision. The core's session is the truth: the controller and
the boundary read ownership from it (`session`) and act on the commands it decides (`verdict`); nothing else changes
it.

* `begin()` opens a feed; `verdict(event, token)` (or `begin(early=event)`) decides the event at once, so what its
  effects do meanwhile is decided after it; `end(token, event, outcome=...)` closes the feed, deciding the event
  there if it was not yet, and feeds back what became of the start or stop it asked for (`CommandOutcome`, as the
  core's `CommandResult`). A start or stop in flight is the session's `pending` until then.
* A decision that could not be taken (the core raised, its facts or the stored session could not be read) is a
  *failure* (`failed`): a warning once per `(where, kind)`, a count, and a Repairs issue only when they repeat. That
  event decides nothing: nothing automatic is sent for it (as the core's `Keep`), the session is left as it was, and
  the next event decides again. Load balancing's safety stop does not depend on this (the site regulator sends it).

What it decided is kept (`EVENT_RING`, no entity ids, no secrets) with the session before each event, so a debug
bundle's events are replayable (`core/replay.py`), and tallied per event kind, across restarts
(`ownership_coverage.py`). It never raises into the controller.
"""

from __future__ import annotations

import asyncio
from collections import deque
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any, Final

from homeassistant.util import dt as dt_util

from .ownership_coverage import OwnershipCoverage

from ..core.events import CommandResult, Event, PlugIn, Unplug
from ..core.ownership import Command, decide
from ..core.session import ChargeSession, ManualPause

_LOGGER = logging.getLogger(__name__)

#: How many events are kept.
EVENT_RING: Final = 200
#: Events whose commands' results come back later, as events of their own (a report's or a timer's spawned stop).
_RESULTS_LATER: Final = frozenset({"charger_reported_on", "charger_reported_off", "timer"})
#: A charger's report of its control: recorded only when it decided something or something moved, so a charger
#: that reports every few seconds does not push the plug-in and a person's actions out of the event ring.
_REPORTS: Final = frozenset({"charger_reported_on", "charger_reported_off", "charger_reported_unreadable"})
#: Who hears every failure and runner error (`kind`, record): the test suite's collector.
LISTENERS: list[Callable[[str, dict[str, Any]], None]] = []
#: What every runner in this process counted (a test run's summary).
TOTALS: dict[str, int] = {}
#: Every runner alive (a test's dump of what each recorded).
RUNNERS: weakref.WeakSet[SessionRunner] = weakref.WeakSet()


@dataclass(frozen=True)
class CommandOutcome:
    """What became of the start or stop the core asked for: it went out, or not; a start load balancing held back is
    `balancing_held`."""

    executed: bool
    balancing_held: bool = False
    #: A stop that sent nothing because the charge control said nothing (unreadable): who owns the charge is kept.
    unobserved: bool = False
    #: A stop that sent nothing because a stop of ours was still on its way (`_stop_settling`).
    settled: bool = False


@dataclass(eq=False)
class FeedToken:
    """One open feed: the task it was opened in. Compared by identity."""

    key: object = None
    ok: bool = True
    #: A failure this feed met is already told (`failed`): one event is one failure, however many steps it failed at.
    #: A feed not decided at once that met one (its facts, its event or its verdict could not be had) is not decided at
    #: its `end` either: nothing was sent for it.
    reported: bool = False
    #: The event decided at once (`begin(early=...)`, `verdict`), the session it was decided from, and the commands it
    #: asked for. `end` then only feeds back the result.
    early: Event | None = None
    early_pre: ChargeSession | None = None
    early_commands: tuple[Command, ...] = ()
    #: When it was decided, on the runner's decision clock (`_tick`): its record says so.
    early_order: int = 0
    #: How many plug-ins had ended (`SessionRunner._plug_in_ends`) at `begin`: a command of this feed whose result
    #: comes back after another ended was decided in an earlier plug-in (`CommandResult.earlier_plug_in`).
    plug_ins: int = 0
    #: The result of the command of the event decided at once, decided as soon as it is known (`result`), and when, on
    #: the decision clock (its record's `order`).
    result: CommandResult | None = None
    resulted: bool = False
    result_order: int = 0


class NullRunner:
    """A runner that decides nothing, for a boundary built around something that is not a `ChargingController`:
    every verdict is a failure's (`None`), so nothing automatic is sent through it."""

    drives = True
    session = ChargeSession()

    def begin(self, early: Event | None = None) -> FeedToken:
        return FeedToken(ok=False)

    def verdict(self, event: Event, token: FeedToken | None = None) -> None:
        return None

    def end(self, token: FeedToken, event: Event, **_kwargs: Any) -> None:
        return

    def cancel(self, token: FeedToken) -> None:
        return

    def result(self, token: FeedToken, outcome: CommandOutcome) -> None:
        return

    def feed(self, event: Event, **_kwargs: Any) -> None:
        return

    def check_intent(self, where: str, *, told: bool = False) -> None:
        return

    def failed(self, where: str, kind: str | None, error: BaseException | None = None, **_kwargs: Any) -> None:
        return


def _task_key() -> object:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _compact(session: ChargeSession) -> dict[str, Any]:
    """A session as its non-default fields (a debug bundle keeps 200 of them): `core/replay.py` fills the rest."""
    full = session.to_dict()
    default = ChargeSession().to_dict()
    return {key: value for key, value in full.items() if key == "version" or value != default[key]}


class SessionRunner:
    """The charge-ownership core's runner for one charger. `now` is the clock; `intent` reads the person intent the
    execution boundary keeps (`PauseIntent`, as the session's `manual` and `span_pause`) for `check_intent`."""

    #: The core decides (kept for the debug bundle's readers, which read it since the core could also only shadow).
    drives = True

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        persist: Callable[[ChargeSession], None] | None = None,
        coverage: OwnershipCoverage | None = None,
        name: str = "",
        on_failure: Callable[[], None] | None = None,
        settled: Callable[[], None] | None = None,
        intent: Callable[[], tuple[ManualPause | None, str | None]] | None = None,
    ) -> None:
        RUNNERS.add(self)
        # Which charger this is, in a log line; and who hears a failure (Repairs, `failed`).
        self._name = name
        self._on_failure = on_failure
        # The failures already logged as a warning this run, as `(where, kind)`: each is logged once, then counted.
        self._warned: set[tuple[str, str | None]] = set()
        # Told the session after each decision once no feed is open, to keep it across a restart (debounced by the
        # caller: a decision that leaves the stored record as it was writes nothing).
        self._persist = persist
        # Told when the last open feed is over (ended, decided or not, or cancelled), before `persist`: what its
        # commands did is the session's from then on.
        self._settled = settled
        self._intent = intent
        self._now = now if now is not None else dt_util.utcnow
        # What it has seen per event kind, cumulative (kept across restarts when the controller gives it a store).
        self.coverage = coverage if coverage is not None else OwnershipCoverage(now=self._now)
        self.session = ChargeSession()
        # How many feeds are open, per asyncio task (`None`: a callback outside any task). A command in flight is the
        # session's `pending` until its feed ends; the stored record and readers are told once none is open.
        self._open: dict[object, int] = {}
        # `failures`: per `where/kind`, the decisions that could not be taken (`failed`); `last_failure`: when.
        self.counts: dict[str, Any] = {
            "events": 0,
            "quiet": 0,
            "errors": 0,
            "failures": {},
            "last_failure": None,
        }
        self.events: deque[dict[str, Any]] = deque(maxlen=EVENT_RING)
        # Who also hears each record kept (the debug bundle's plug-ins, `debug_day.py`); never raises into the runner.
        self.on_record: Callable[[dict[str, Any]], None] | None = None
        # The decision clock (`_tick`).
        self._ticks = 0
        # How many plug-ins ended (an unplug, or a plug-in after a known unplug, decided): what a feed's command was
        # decided in (`FeedToken.plug_ins`). Transient: no feed outlives a restart.
        self._plug_in_ends = 0
        # A check of the person intent asked for while a feed was open (`check_intent`), run once none is (`_settle`).
        self._intent_pending: tuple[str, bool] | None = None

    # ------------------------------------------------------------------ feeding

    @property
    def depth(self) -> int:
        """How many feeds the running task has open: a command through the controller's entry inside one (a person's
        Start through the execution boundary) is that feed's, not an event of its own."""
        return self._open.get(_task_key(), 0)

    def _any_open(self) -> bool:
        # A task that ended with a feed open (cancelled between `begin` and `end`) holds nothing any more.
        for key in [key for key in self._open if key is not None]:
            done = getattr(key, "done", None)
            if done is not None and done():
                self._open.pop(key, None)
        return any(count > 0 for count in self._open.values())

    def begin(self, early: Event | None = None) -> FeedToken:
        """Open a feed. Never raises. An `early` event (one whose facts are all known before anything acts: a plug-in,
        a person's Start or Stop) is decided here, so what its effects do meanwhile is decided after it."""
        key = _task_key()
        self._open[key] = self._open.get(key, 0) + 1
        token = FeedToken(key=key, plug_ins=self._plug_in_ends)
        if early is not None:
            self._decide_early(token, early)
        return token

    def _decide_early(self, token: FeedToken, event: Event) -> tuple[Command, ...]:
        try:
            token.early_pre = self.session
            token.early_order = self._tick()
            self.session, token.early_commands = self._decide_or_observe(self.session, event)
            token.early = event
            self._note_plug_in_end(event)
        except Exception as err:  # noqa: BLE001 - nothing is sent for it: a failure, told
            self._error("begin_early", event.kind)
            self.failed("decide_early", event.kind, err, token=token)
        return token.early_commands

    def _close(self, token: FeedToken) -> None:
        count = self._open.get(token.key, 0) - 1
        if count > 0:
            self._open[token.key] = count
        else:
            self._open.pop(token.key, None)

    def cancel(self, token: FeedToken) -> None:
        """A `begin` whose happening turned out to be none (nothing to feed)."""
        if token.early is not None:
            # Decided already, and acted on: it is fed as it was decided.
            self.end(token, token.early)
            return
        self._close(token)
        self._settle()

    def end(self, token: FeedToken, event: Event, *, outcome: CommandOutcome | None = None) -> None:
        """Close a feed: `event` is decided now unless it was at once, and `outcome` is what became of the start or
        stop it asked for. Never raises."""
        self._close(token)
        try:
            self._decide(token, event, outcome)
        except Exception as err:  # noqa: BLE001 - the runner never raises into the controller
            self._error(event.kind, event.kind)
            self.failed("decide", event.kind, err, token=token)
        self._persisted()

    def result(self, token: FeedToken, outcome: CommandOutcome) -> None:
        """The command of a feed decided at once returned with `outcome`: its result is decided now, before whatever
        the feed still awaits (a person's Start storing its pause), so a happening that lands meanwhile (load
        balancing's pause) is decided after it, as the commands went out. `end` then only keeps its record. Never
        raises; a feed not decided at once is decided at its `end` as ever."""
        try:
            self._result_now(token, outcome)
        except Exception as err:  # noqa: BLE001 - the runner never raises into the controller
            kind = token.early.kind if token.early is not None else None
            self._error("result", kind)
            self.failed("result", kind, err, token=token)

    def _result_now(self, token: FeedToken, outcome: CommandOutcome) -> None:
        """Decide the result of the command of `token`'s decision at once, once (`result`)."""
        if token.resulted or token.early is None or token.early_pre is None:
            return
        acted = next((command for command in token.early_commands if command.kind in ("start", "stop")), None)
        if acted is None:
            return
        result = CommandResult(
            command=acted.kind,
            reason=getattr(acted, "reason", ""),
            executed=outcome.executed,
            balancing_held=outcome.balancing_held,
            unobserved=outcome.unobserved,
            settled=outcome.settled,
            earlier_plug_in=self._plug_in_ends != token.plug_ins,
        )
        self.session, _ = decide(self.session, result, self._now())
        token.result = result
        token.resulted = True
        token.result_order = self._tick()

    def feed(self, event: Event, *, outcome: CommandOutcome | None = None) -> None:
        """`begin` and `end` at once."""
        self.end(self.begin(), event, outcome=outcome)

    def restart(self, event: Event, session: ChargeSession) -> None:
        """The record was read back after a restart (`session`: the core's own record, or one migrated from today's
        keys), and the restart is fed."""
        self.session = session
        self.end(self.begin(), event)

    def check_intent(self, where: str, *, told: bool = False) -> None:
        """The execution boundary wrote its person intent (`PauseIntent`) from a decision of the core's: the two must
        say the same. A difference is a failure (a bug: something wrote one without the other), and the session takes
        the boundary's, which the card shows and the gate reads. `told`: the core could not decide the person's own
        action that wrote it (a failure told already), so the session takes the boundary's with no second failure.
        While a feed is open (its decision may not be written yet) the check waits, and runs once no feed is open any
        more (`_settle`)."""
        reader = self._intent
        if reader is None:
            return
        if self._any_open():
            pending = self._intent_pending
            self._intent_pending = (where, told and (pending is None or pending[1]))
            return
        self._intent_pending = None
        try:
            manual, span = reader()
            if (manual, span) == (self.session.manual, self.session.span_pause):
                return
            record = {
                "where": where,
                "core": {"manual": _manual_dict(self.session.manual), "span_pause": self.session.span_pause},
                "boundary": {"manual": _manual_dict(manual), "span_pause": span},
            }
            self.session = self.session.with_changes(manual=manual, span_pause=span)
            if not told:
                self.failed("intent", None, None)
            _LOGGER.debug("SpotNav charger %s: person intent differed %s", self._name, record)
            self._persisted()
        except Exception:  # noqa: BLE001 - the runner never raises into the controller
            self._error("check_intent")

    # ------------------------------------------------------------------ inside

    def _decide(self, token: FeedToken, event: Event, outcome: CommandOutcome | None) -> None:
        if token.early is None and (token.early_pre is not None or token.reported):
            # Its decision at once failed, or its facts, its event or its verdict could not be had (told then): nothing
            # was sent for it, and it is not decided here, where a success would leave the session awaiting a command
            # nobody sent.
            return
        now = self._now()
        if token.early is not None and token.early_pre is not None:
            # Decided at once: only its result is new.
            pre, session, commands = token.early_pre, self.session, token.early_commands
        else:
            pre = self.session
            session, commands = self._decide_or_observe(pre, event, now)
        acted = next((command for command in commands if command.kind in ("start", "stop")), None)
        result: CommandResult | None = None
        if token.resulted:
            # Its result was decided when it was known (`result`).
            result = token.result
        elif acted is not None and (outcome is not None or event.kind not in _RESULTS_LATER):
            # What became of the command: told by the feed, or, the feed over with nothing told, it did not go out.
            result = CommandResult(
                command=acted.kind,
                reason=getattr(acted, "reason", ""),
                executed=outcome is not None and outcome.executed,
                balancing_held=outcome is not None and outcome.balancing_held,
                unobserved=outcome is not None and outcome.unobserved,
                settled=outcome is not None and outcome.settled,
                earlier_plug_in=self._plug_in_ends != token.plug_ins,
            )
            session, _ = decide(session, result, now)
        self.session = session
        if token.early is None or token.early_pre is None:
            self._note_plug_in_end(event)
        self._count("events")
        self._cover(event.kind, "events")
        trigger = getattr(event, "trigger", None)
        if isinstance(trigger, str):
            try:
                self.coverage.add_trigger(event.kind, trigger)
            except Exception:  # noqa: BLE001 - the tally never breaks the runner
                _LOGGER.debug("SpotNav ownership coverage failed", exc_info=True)
        TOTALS[f"event:{event.kind}"] = TOTALS.get(f"event:{event.kind}", 0) + 1
        record: dict[str, Any] = {
            "at": now.isoformat(),
            "event": event.to_dict(),
            "core": [command.to_dict() for command in commands if command.kind != "keep"],
            "pre": _compact(pre),
        }
        if result is not None:
            record["result"] = result.to_dict()
        # The decision clock: when this record's event (or, decided at once, its result) was decided, and when an
        # event decided at once was. A replay decides each in that order (`core/replay.py`).
        # A result decided when it was known (`result`) is in its place on the clock.
        record["order"] = token.result_order if token.resulted else self._tick()
        if token.early is not None and token.early_pre is not None:
            record["early"] = token.early_order
        if (
            event.kind in _REPORTS
            and session == pre
            and result is None
            and all(command.kind == "keep" for command in commands)
        ):
            # Nothing decided, nothing moved: not kept.
            self._count("quiet")
        else:
            self.events.append(record)
            self._tell_record(record)

    def _tell_record(self, record: dict[str, Any]) -> None:
        """Tell `on_record` a record kept (never raises)."""
        listener = self.on_record
        if listener is not None:
            try:
                listener(record)
            except Exception:  # noqa: BLE001 - a diagnostic listener must never break the runner
                _LOGGER.debug("Ownership record listener failed", exc_info=True)

    def _decide_or_observe(
        self, session: ChargeSession, event: Event, now: datetime | None = None
    ) -> tuple[ChargeSession, tuple[Command, ...]]:
        """`decide`, which raises as it does (a failure, told by the caller). A plug-in or an unplug whose decision
        failed still moves the connection the session keeps: it is what the charger stated, an observation and not a
        decision, and the controller reads it from the session. Left as it was, every next report would find the
        connection changed again and feed the same plug-in or unplug, failing each time. What the plug-in's end
        decides (a pause, the car-ended record, the hold, load balancing's memory) is not guessed."""
        try:
            return decide(session, event, self._now() if now is None else now)
        except Exception:
            if isinstance(event, (PlugIn, Unplug)):
                self.session = session.with_changes(plugged=isinstance(event, PlugIn))
            raise

    def _note_plug_in_end(self, event: Event) -> None:
        """An unplug, or a plug-in after a known unplug, was decided: the plug-in a command in flight began in ended."""
        if event.kind == "unplug" or (event.kind == "plug_in" and getattr(event, "previous", None) is False):
            self._plug_in_ends += 1

    def _tick(self) -> int:
        self._ticks += 1
        return self._ticks

    def _settle(self) -> None:
        """No feed is open any more: tell `settled`, and run an intent check that waited for it (never raises)."""
        if self._any_open():
            return
        settled = self._settled
        if settled is not None:
            try:
                settled()
            except Exception:  # noqa: BLE001 - the runner never raises into the controller
                self._error("settled")
        pending = self._intent_pending
        if pending is not None:
            # A boundary write checked while another feed was open: checked now.
            self.check_intent(pending[0], told=pending[1])

    def _persisted(self) -> None:
        """Once no feed is open: tell the keeper of the stored record the session as it is now (never raises)."""
        self._settle()
        persist = self._persist
        if persist is None or self._any_open():
            return
        try:
            persist(self.session)
        except Exception:  # noqa: BLE001 - the runner never raises into the controller
            self._error("persist")

    def verdict(self, event: Event, token: FeedToken | None = None) -> frozenset[tuple[str, str]] | None:
        """The commands the core decides for `event` now, as `(kind, reason)` (a notification's code as its reason),
        taken as the decision of the feed `token` (decided once: a second verdict in one feed only previews). `None`
        when it could not decide: a failure, and nothing automatic is sent for that event."""
        try:
            if token is not None and token.early is None:
                commands = self._decide_early(token, event)
                if token.early is None:
                    return None
            else:
                _session, commands = decide(self.session, event, self._now())
        except Exception as err:  # noqa: BLE001 - nothing is sent for it: a failure, told
            self._error("verdict", event.kind)
            self.failed("verdict", event.kind, err, token=token)
            return None
        return frozenset(
            (command.kind, str(getattr(command, "reason", getattr(command, "code", ""))))
            for command in commands
            if command.kind != "keep"
        )

    def _count(self, name: str) -> None:
        self.counts[name] += 1
        TOTALS[name] = TOTALS.get(name, 0) + 1

    def _cover(self, kind: str | None, field: str) -> None:
        try:
            self.coverage.add(kind, field)
        except Exception:  # noqa: BLE001 - the tally never breaks the runner
            _LOGGER.debug("SpotNav ownership coverage failed", exc_info=True)

    def _error(self, where: str, kind: str | None = None) -> None:
        """A failure at `where`, counted under the event `kind` it met (`None`: no event's)."""
        self._count("errors")
        self._cover(kind, "errors")
        _LOGGER.debug("SpotNav ownership runner failed at %s", where, exc_info=True)
        self._tell("error", {"where": where})

    def failed(
        self,
        where: str,
        kind: str | None,
        error: BaseException | None = None,
        *,
        token: FeedToken | None = None,
    ) -> None:
        """A decision that could not be taken at `where` for an event of `kind` (`None`: no event's): the core raised,
        or its facts or its stored session could not be read. Nothing automatic is sent for that event. Counted per
        `where/kind` with the time of the last (`counts`, so diagnostics and the debug bundle carry it); the first of
        each `(where, kind)` this run is a warning in the log, and every one is told to `on_failure` (which raises a
        Repairs issue only when failures repeat). A feed's `token` makes one event one failure, however many of its
        steps failed. Never raises."""
        try:
            if token is not None:
                if token.reported:
                    return
                token.reported = True
            failures = self.counts["failures"]
            label = f"{where}/{kind or '-'}"
            failures[label] = failures.get(label, 0) + 1
            self._cover(kind, "failures")
            self.counts["last_failure"] = self._now().isoformat()
            TOTALS["failures"] = TOTALS.get("failures", 0) + 1
            if (where, kind) not in self._warned:
                self._warned.add((where, kind))
                _LOGGER.warning(
                    "SpotNav charger %s: the charging decision could not be taken at %s (%s), so nothing automatic "
                    "was sent for it; later ones are only counted (diagnostics)",
                    self._name,
                    where,
                    kind or "no event",
                    exc_info=error,
                )
            self._tell("failure", {"where": where, "kind": kind})
            if self._on_failure is not None:
                self._on_failure()
        except Exception:  # noqa: BLE001 - the runner never raises into the controller
            _LOGGER.debug("SpotNav ownership runner: a failure could not be told", exc_info=True)

    def _tell(self, kind: str, record: dict[str, Any]) -> None:
        for listener in list(LISTENERS):
            try:
                listener(kind, record)
            except Exception:  # noqa: BLE001 - a listener must not break the runner
                _LOGGER.debug("Runner listener failed", exc_info=True)

    # ------------------------------------------------------------------ reading

    def diagnostics(self) -> dict[str, Any]:
        """For diagnostics and the debug bundle: the counts, the session, the event ring, and the cumulative tally per
        event kind (`coverage`)."""
        return {
            "drives": True,
            "counts": dict(self.counts),
            "session": self.session.to_dict(),
            "events": list(self.events),
            "coverage": self.coverage.as_dict(),
        }


def _manual_dict(manual: ManualPause | None) -> dict[str, str] | None:
    return None if manual is None else {"action": manual.action, "scope": manual.scope}
