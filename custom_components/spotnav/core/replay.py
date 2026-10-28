"""Feed the events a debug bundle recorded to the charge-ownership core again.

Pure: no Home Assistant imports. The shadow (`execution/ownership_shadow.py`) keeps, per charger, the last events it
fed the core: the event, the instant, the session it decided from when it lined that up with today's code (`pre`,
its non-default fields), the result of the command it asked for, what the core asked for, what today's code did,
and today's owner and intent after it. `replay` decides each again, from the recorded `pre` where there is one and
from its own result otherwise, and says where the core now decides differently from the recording (a change of the
core since), and where it differs from today's code (what a disagreement is).

With `line_up=False` (`--no-line-up`) it never takes a record's `pre`: it starts from the first recorded session and
decides every event from the session it carried forward itself, then compares that session with what today's code
held (`today_after` after the event, and the next record's full `pre` before it). A difference no event explains is
a feed missing where today's code changes the state (`SessionDifference`); the charger's own charge seen beginning
or ending, and one instant read off two clocks, are only observations (`OBSERVED`, `CLOCK_TOLERANCE_S`).

    python -m custom_components.spotnav.core.replay [--no-line-up] bundle.json
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import json
import sys
from typing import Any

from .events import event_from_dict
from .ownership import decide
from .session import ChargeSession


@dataclass(frozen=True)
class ReplayMismatch:
    """One recorded event the core now decides differently: from the recording (`against` is `core`), or from
    today's code (`today`)."""

    index: int
    kind: str
    against: str
    recorded: Any
    replayed: Any


#: Owners between which a change is an observation, not ownership: whether the charger charges by itself (nobody
#: here started it) is seen in its reports, and the line-up counts the same change as `observed`, not as drift
#: (`execution/ownership_shadow._line_up`). A charge that begins or ends with no report fed (a stop's answer window
#: running out, a restart reading the charger as it is) moves today's owner between these two with no event.
OBSERVED: frozenset[str] = frozenset({"none", "charger_self"})
#: How far apart two readings of the clock may be and still be one instant: the core takes the instant it decided
#: (the shadow's clock when the event came), today's code its own a moment later (when its stop went out, say). An
#: instant, or a list of them of the same length, apart by no more than this is an observation of the time, not a
#: change of ownership state. Well below the shortest gap the rules measure (`PERSON_HOLD_STOP_GAP_S`, 30 s).
CLOCK_TOLERANCE_S = 2.0


@dataclass(frozen=True)
class SessionDifference:
    """One field where the session a replay with no line-up carried forward differs from today's code: after the
    event (`when` is `after`, from `today_after`) or before it (`before`, from the record's `pre`). `drift` is a
    known observation (`OBSERVED`, `CLOCK_TOLERANCE_S`), not a missing feed."""

    index: int
    kind: str
    when: str
    field: str
    expected: Any
    got: Any
    drift: bool = False
    #: Why it is drift: `owner` (the charger's own charge seen), `clock` (two readings of the time).
    reason: str = ""


@dataclass(frozen=True)
class ReplayReport:
    """What a replay found: how many events it decided, the mismatches, the session it ended with, and with no
    line-up, the differences from today's state and how many records before the first recorded session it skipped."""

    events: int
    mismatches: tuple[ReplayMismatch, ...] = field(default=())
    session: ChargeSession = field(default_factory=ChargeSession)
    differences: tuple[SessionDifference, ...] = field(default=())
    skipped: int = 0

    @property
    def drift(self) -> tuple[SessionDifference, ...]:
        return tuple(item for item in self.differences if item.drift)

    @property
    def real(self) -> tuple[SessionDifference, ...]:
        """Differences that are no observation: a feed is missing where today's code changed the state."""
        return tuple(item for item in self.differences if not item.drift)

    @property
    def differs_from_recording(self) -> tuple[ReplayMismatch, ...]:
        return tuple(item for item in self.mismatches if item.against == "core")

    @property
    def differs_from_today(self) -> tuple[ReplayMismatch, ...]:
        return tuple(item for item in self.mismatches if item.against == "today")


def expand(compact: Mapping[str, Any]) -> ChargeSession:
    """A session from a recorded `pre` (its non-default fields only)."""
    return ChargeSession.from_dict({**ChargeSession().to_dict(), **compact})


def _intent(session: ChargeSession) -> dict[str, Any]:
    return {
        "owner": session.owner,
        "manual": None if session.manual is None else {"action": session.manual.action, "scope": session.manual.scope},
        "span_pause": session.span_pause,
    }


def _instants(value: Any) -> list[datetime] | None:
    """A serialised instant, or a list of them, as instants (`None`: not that)."""
    items = value if isinstance(value, list) else [value]
    try:
        return [datetime.fromisoformat(item) for item in items]
    except (TypeError, ValueError):
        return None


def _one_instant(expected: Any, got: Any) -> bool:
    mine, today = _instants(got), _instants(expected)
    return (
        mine is not None
        and today is not None
        and len(mine) == len(today)
        and all(abs((a - b).total_seconds()) <= CLOCK_TOLERANCE_S for a, b in zip(mine, today, strict=True))
    )


def _differences(
    index: int, kind: str, when: str, expected: Mapping[str, Any], got: Mapping[str, Any]
) -> list[SessionDifference]:
    found = []
    for name in expected:
        if name == "version" or expected[name] == got.get(name):
            continue
        reason = ""
        if name == "owner" and {expected[name], got.get(name)} <= OBSERVED:
            reason = "owner"
        elif _one_instant(expected[name], got.get(name)):
            reason = "clock"
        found.append(SessionDifference(index, kind, when, name, expected[name], got.get(name), bool(reason), reason))
    return found


def replay(
    records: Iterable[Mapping[str, Any]], *, start: ChargeSession | None = None, line_up: bool = True
) -> ReplayReport:
    """Decide every recorded event again, in order. `line_up=False`: from the first recorded session (or `start`)
    on, from the session carried forward only, compared with today's state before and after each event."""
    records = list(records)
    skipped = 0
    if not line_up and start is None:
        # The first recorded session is the start; the records before it have none to decide from.
        skipped = next((index for index, record in enumerate(records) if "pre" in record), len(records))
        start = expand(records[skipped]["pre"]) if skipped < len(records) else None
    session = start if start is not None else ChargeSession()
    mismatches: list[ReplayMismatch] = []
    differences: list[SessionDifference] = []
    count = 0
    # Today's state after an event was read once the events queued behind it were decided too.
    after: tuple[int, str, Mapping[str, Any]] | None = None

    def settle() -> None:
        nonlocal after
        if after is None:
            return
        index, kind, today = after
        after = None
        mine = _intent(session)
        if not line_up:
            differences.extend(_differences(index, kind, "after", today, mine))
        elif any(today.get(name) != mine[name] for name in mine):
            mismatches.append(ReplayMismatch(index, kind, "today", dict(today), mine))

    # Each record is two steps: its event, then its result and the comparisons, in the order the shadow decided
    # them (its decision clock, `order`; a bundle without one, in the order kept). With no line-up, an event the
    # core decided when its feed began (`early`) is decided in that place, before the feeds kept before its record.
    kept = records[skipped:]
    clocked = not line_up and all("order" in record for record in kept)
    steps: list[tuple[float, int, int]] = []
    for index, record in enumerate(kept, skipped):
        when = float(record["order"]) if clocked else float(index)
        steps.append((float(record.get("early", when)) if clocked else when, 0, index))
        steps.append((when, 1, index))
    # Today's state after a feed was read once the feeds queued behind it were kept too: the last of them.
    read_at: dict[int, float] = {}
    for index in range(len(records) - 1, skipped - 1, -1):
        following = read_at.get(index + 1) if index + 1 < len(records) and records[index + 1].get("queued") else None
        read_at[index] = following if following is not None else (
            float(records[index]["order"]) if clocked else float(index)
        )
    pending_until = -1.0
    decided: dict[int, list[str]] = {}
    for when, phase, index in sorted(steps):
        record = records[index]
        event = event_from_dict(record["event"])
        now = datetime.fromisoformat(record["at"])
        if when > pending_until:
            settle()
        if phase == 0:
            if "pre" in record:
                if line_up:
                    session = expand(record["pre"])
                else:
                    # What today's code held when this event came, against what the events so far made of it.
                    differences.extend(
                        _differences(index, event.kind, "before", expand(record["pre"]).to_dict(), session.to_dict())
                    )
            session, commands = decide(session, event, now)
            decided[index] = sorted(command.kind for command in commands if command.kind != "keep")
            continue
        if "result" in record:
            session, _ = decide(session, event_from_dict(record["result"]), now)
        count += 1
        replayed = decided.pop(index)
        recorded_core = sorted(command["kind"] for command in record.get("core", ()))
        if replayed != recorded_core:
            mismatches.append(ReplayMismatch(index, event.kind, "core", recorded_core, replayed))
        if record.get("unchecked"):
            # Today's code decided it mid-way through another feed: only the state after both is comparable.
            continue
        today = sorted(record.get("today", ()))
        if replayed != today:
            mismatches.append(ReplayMismatch(index, event.kind, "today", today, replayed))
        if record.get("today_after") is not None:
            after = (index, event.kind, record["today_after"])
            pending_until = read_at[index]
    settle()
    return ReplayReport(count, tuple(mismatches), session, tuple(differences), skipped)


def bundle_events(bundle: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Each charger's recorded events in a debug bundle (version 5 or later), by entry id."""
    found: dict[str, list[dict[str, Any]]] = {}
    for charger in bundle.get("chargers") or ():
        controller = ((charger or {}).get("diagnostics") or {}).get("controller") or {}
        shadow = controller.get("ownership_shadow") or {}
        found[str(charger.get("entry_id"))] = list(shadow.get("events") or ())
    return found


def replay_bundle(bundle: Mapping[str, Any], *, line_up: bool = True) -> dict[str, ReplayReport]:
    """Replay every charger of a debug bundle."""
    return {entry_id: replay(records, line_up=line_up) for entry_id, records in bundle_events(bundle).items()}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    line_up = "--no-line-up" not in args
    args = [arg for arg in args if arg != "--no-line-up"]
    if len(args) != 1:
        print("usage: python -m custom_components.spotnav.core.replay [--no-line-up] bundle.json")
        return 2
    with open(args[0], encoding="utf-8") as handle:
        bundle = json.load(handle)
    worst = 0
    for entry_id, report in replay_bundle(bundle, line_up=line_up).items():
        print(f"{entry_id}: {report.events} events, {len(report.differs_from_recording)} differ from the recording, "
              f"{len(report.differs_from_today)} from today's code")
        for item in report.mismatches:
            print(f"  #{item.index} {item.kind} against {item.against}: recorded {item.recorded}, replayed {item.replayed}")
        if line_up:
            worst = max(worst, 1 if report.mismatches else 0)
            continue
        # With no line-up a command can differ only because the state did: only a real difference fails.
        print(f"  no line-up: {report.skipped} records before the first session skipped, "
              f"{len(report.drift)} observation drift, {len(report.real)} real differences")
        for diff in report.differences:
            label = f"drift ({diff.reason})" if diff.drift else "REAL"
            print(f"  {label} #{diff.index} {diff.kind} {diff.when}: {diff.field} expected {diff.expected!r}, "
                  f"got {diff.got!r}")
        worst = max(worst, 1 if report.real else 0)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
