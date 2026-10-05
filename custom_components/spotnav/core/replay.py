"""Feed the events a debug bundle recorded to the charge-ownership core again.

Pure: no Home Assistant imports. The shadow (`execution/ownership_shadow.py`) keeps, per charger, the last events it
fed the core: the event, the instant, the session it decided from when it lined that up with today's code (`pre`,
its non-default fields), the result of the command it asked for, what the core asked for, what today's code did,
and today's owner and intent after it. `replay` decides each again, from the recorded `pre` where there is one and
from its own result otherwise, and says where the core now decides differently from the recording (a change of the
core since), and where it differs from today's code (what a disagreement is).

    python -m custom_components.spotnav.core.replay bundle.json
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


@dataclass(frozen=True)
class ReplayReport:
    """What a replay found: how many events it decided, the mismatches, and the session it ended with."""

    events: int
    mismatches: tuple[ReplayMismatch, ...] = field(default=())
    session: ChargeSession = field(default_factory=ChargeSession)

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


def replay(records: Iterable[Mapping[str, Any]], *, start: ChargeSession | None = None) -> ReplayReport:
    """Decide every recorded event again, in order."""
    session = start if start is not None else ChargeSession()
    mismatches: list[ReplayMismatch] = []
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
        if any(today.get(name) != mine[name] for name in mine):
            mismatches.append(ReplayMismatch(index, kind, "today", dict(today), mine))

    for index, record in enumerate(records):
        if not record.get("queued"):
            settle()
        if "pre" in record:
            session = expand(record["pre"])
        event = event_from_dict(record["event"])
        now = datetime.fromisoformat(record["at"])
        session, commands = decide(session, event, now)
        if "result" in record:
            session, _ = decide(session, event_from_dict(record["result"]), now)
        count += 1
        replayed = sorted(command.kind for command in commands if command.kind != "keep")
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
    settle()
    return ReplayReport(count, tuple(mismatches), session)


def bundle_events(bundle: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Each charger's recorded events in a debug bundle (version 5 or later), by entry id."""
    found: dict[str, list[dict[str, Any]]] = {}
    for charger in bundle.get("chargers") or ():
        controller = ((charger or {}).get("diagnostics") or {}).get("controller") or {}
        shadow = controller.get("ownership_shadow") or {}
        found[str(charger.get("entry_id"))] = list(shadow.get("events") or ())
    return found


def replay_bundle(bundle: Mapping[str, Any]) -> dict[str, ReplayReport]:
    """Replay every charger of a debug bundle."""
    return {entry_id: replay(records) for entry_id, records in bundle_events(bundle).items()}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m custom_components.spotnav.core.replay bundle.json")
        return 2
    with open(args[0], encoding="utf-8") as handle:
        bundle = json.load(handle)
    worst = 0
    for entry_id, report in replay_bundle(bundle).items():
        print(f"{entry_id}: {report.events} events, {len(report.differs_from_recording)} differ from the recording, "
              f"{len(report.differs_from_today)} from today's code")
        for item in report.mismatches:
            print(f"  #{item.index} {item.kind} against {item.against}: recorded {item.recorded}, replayed {item.replayed}")
        worst = max(worst, 1 if report.mismatches else 0)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
