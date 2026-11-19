"""SpotNav's hot-water writes, so its own boosts are told from the household's (plan `plans/v3_dhw_plan.md`
section 7). Pure; kept by the hot-water shadow in the site's heat store.

Each write is an entry: the entity, the value, when, why (`plan`: SpotNav's planned boost; `button`: the Boost
button, a person's own action), whether it was read back, and when it ended. A boost that reads on with no entry
written within `OWN_WINDOW` before it began is the household's (the pump's display, its app, its own schedule).
A button's boost counts as the household's for every rule (never cut, never shed, outside the plan's budget); only
the plan's own boosts are SpotNav's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Final

SOURCE_PLAN: Final = "plan"
SOURCE_BUTTON: Final = "button"
SOURCES: Final = (SOURCE_PLAN, SOURCE_BUTTON)

#: A boost that began this soon after a write is that write's.
OWN_WINDOW: Final = timedelta(minutes=10)
#: A boost SpotNav (or the button) started is counted its own at most this long while it reads on.
OWN_MAX: Final = timedelta(hours=13)
#: A meter run that began this soon after a write is that write's run.
RUN_MATCH: Final = timedelta(minutes=30)
KEEP: Final = timedelta(days=8)


@dataclass(frozen=True, slots=True)
class Entry:
    entity_id: str
    value: str
    at: datetime
    source: str
    #: `None` while the read-back is pending, then whether the write was seen.
    read_back: bool | None = None
    ended: datetime | None = None

    def stored(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "value": self.value,
            "at": self.at.isoformat(),
            "source": self.source,
            "read_back": self.read_back,
            "ended": None if self.ended is None else self.ended.isoformat(),
        }

    @classmethod
    def from_stored(cls, raw: Any) -> Entry | None:
        try:
            ended = raw.get("ended")
            read_back = raw.get("read_back")
            source = raw["source"]
            if source not in SOURCES:
                return None
            return cls(
                str(raw["entity_id"]),
                str(raw["value"]),
                datetime.fromisoformat(raw["at"]),
                source,
                read_back if isinstance(read_back, bool) else None,
                None if ended is None else datetime.fromisoformat(ended),
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None


class Ledger:
    """The entries of the last `KEEP`, oldest first."""

    def __init__(self, entries: Sequence[Entry] = ()) -> None:
        self.entries: list[Entry] = list(entries)

    def add(self, entry: Entry) -> Entry:
        self.entries.append(entry)
        return entry

    def update(self, entry: Entry, **changes: Any) -> Entry:
        """The entry with `changes`, in its place."""
        updated = replace(entry, **changes)
        self.entries = [updated if item is entry else item for item in self.entries]
        return updated

    def trim(self, now: datetime) -> None:
        self.entries = [entry for entry in self.entries if now - entry.at <= KEEP]

    def source_of_on(self, entity_id: str, on_since: datetime | None) -> str | None:
        """Whose a boost reading on since `on_since` is: a write's source, `None` for the household's own."""
        if on_since is None:
            return None
        for entry in reversed(self.entries):
            if entry.entity_id != entity_id or entry.read_back is False:
                continue
            if entry.at - timedelta(minutes=1) <= on_since and on_since - entry.at <= OWN_WINDOW:
                return entry.source
        return None

    def owner(self, entity_id: str, now: datetime) -> str | None:
        """Whose the boost reading on now is: the source of the last write to it that was read back and has not
        ended (`close`), `None` for the household's own. Kept in the ledger, so a restart or a timer that counts down
        (its state changes each minute) never hands SpotNav's boost to the household."""
        for entry in reversed(self.entries):
            if entry.entity_id != entity_id:
                continue
            if entry.read_back is True and entry.ended is None and now - entry.at <= OWN_MAX:
                return entry.source
            return None
        return None

    def close(self, entity_id: str, now: datetime) -> None:
        """The boost on `entity_id` reads off: the writes to it have ended."""
        self.entries = [
            replace(entry, ended=now) if entry.entity_id == entity_id and entry.ended is None and entry.read_back is True else entry
            for entry in self.entries
        ]

    def run_source(self, run_start: datetime) -> str | None:
        """The source of the write that started a meter run beginning at `run_start`, `None` for the pump's own."""
        for entry in reversed(self.entries):
            if entry.read_back is False:
                continue
            if entry.at - timedelta(minutes=5) <= run_start and run_start - entry.at <= RUN_MATCH:
                return entry.source
        return None

    def starts(self) -> list[datetime]:
        """When each write that was not refused was made (the peaks learner leaves the runs after them out)."""
        return [entry.at for entry in self.entries if entry.read_back is not False]

    def used_on(self, day: date, zone: tzinfo, source: str = SOURCE_PLAN) -> int:
        """The writes of `source` on a local day (the plan's budget)."""
        return sum(1 for entry in self.entries if entry.source == source and entry.at.astimezone(zone).date() == day)

    def last(self, source: str | None = None) -> Entry | None:
        for entry in reversed(self.entries):
            if source is None or entry.source == source:
                return entry
        return None

    def stored(self) -> list[dict[str, Any]]:
        return [entry.stored() for entry in self.entries]

    @classmethod
    def from_stored(cls, raw: Any) -> Ledger:
        if not isinstance(raw, list):
            return cls()
        return cls([entry for item in raw if (entry := Entry.from_stored(item)) is not None])


__all__ = ["KEEP", "OWN_WINDOW", "SOURCE_BUTTON", "SOURCE_PLAN", "Entry", "Ledger"]
