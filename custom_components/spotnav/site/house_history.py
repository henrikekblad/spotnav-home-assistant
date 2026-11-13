"""The house load forecast's own history: the measured hours of the last eight weeks, kept beside its log.

Pure: the clock is passed in. The demand tracker (`site/demand_tracker.py`) keeps only the running month and the one
before it, so early in a month the forecast's six-week window (`site/load_forecast.WINDOW_DAYS`) would hold barely
four weeks. This keeps its own rolling store of the hours it reads:

* **What**: per completed clock hour (its start, UTC) the house's measured load (`house_kw`) and the outdoor
  temperature (`outdoor_c`) when one was recorded. Only hours the forecast can use: never one read from Home
  Assistant's statistics (`imported`), never one without the house's share, never a negative or non-finite figure.
* **Merge**: every refresh merges the tracker's hours in (the tracker's figure wins for an hour both hold), so the first
  refresh after an update takes the tracker's two months over (`migrated_at` says when) and later ones add each
  completed hour.
* **Bounds**: `KEEP_DAYS` days, at most `MAX_ROWS` rows. Storage is untrusted: a malformed row is dropped.

It decides nothing on its own: the forecast reads it (`site/house_forecast.py`), the diagnostics count it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Final

from ..planning.demand_charge import HourMean

#: The hours of the last this many days are kept: the forecast's six weeks, and two weeks to spare.
KEEP_DAYS: Final = 56
#: A hard cap beside the age bound, with room for DST.
MAX_ROWS: Final = KEEP_DAYS * 24 + 48


def _usable(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def _temperature(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _instant(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return None if moment.tzinfo is None else moment.astimezone(timezone.utc)


@dataclass(slots=True)
class HouseHistory:
    """The kept hours (see the module), by start."""

    rows: dict[datetime, tuple[float, float | None]] = field(default_factory=dict)
    #: When the tracker's hours were first taken over (`None` until the first merge).
    migrated_at: datetime | None = None

    def merge(self, hours: Iterable[HourMean], now: datetime) -> int:
        """Take the usable hours in; how many were new. Old rows are dropped."""
        now = now.astimezone(timezone.utc)
        added = 0
        for hour in hours:
            if hour.imported or not _usable(hour.house_kw):
                continue
            start = hour.start.astimezone(timezone.utc)
            if start not in self.rows:
                added += 1
            self.rows[start] = (float(hour.house_kw), _temperature(hour.outdoor_c))  # type: ignore[arg-type]
        if self.migrated_at is None:
            self.migrated_at = now
        self._prune(now)
        return added

    def _prune(self, now: datetime) -> None:
        oldest = now - timedelta(days=KEEP_DAYS)
        kept = sorted((start, row) for start, row in self.rows.items() if start >= oldest)
        self.rows = dict(kept[-MAX_ROWS:])

    def hours(self) -> list[HourMean]:
        """The kept hours as the forecast reads them, oldest first."""
        return [
            HourMean(start=start, kw=kw, house_kw=kw, outdoor_c=outdoor)
            for start, (kw, outdoor) in sorted(self.rows.items())
        ]

    # ---- storage

    def as_dict(self) -> dict[str, Any]:
        return {
            "migrated_at": None if self.migrated_at is None else self.migrated_at.isoformat(),
            "hours": [
                [start.isoformat(), round(kw, 3), None if outdoor is None else round(outdoor, 1)]
                for start, (kw, outdoor) in sorted(self.rows.items())
            ],
        }

    def restore(self, raw: Any) -> None:
        """Take what the store kept; a malformed row is dropped."""
        if not isinstance(raw, dict):
            return
        rows: dict[datetime, tuple[float, float | None]] = {}
        for row in raw.get("hours") or []:
            if not isinstance(row, list) or len(row) != 3:
                continue
            start = _instant(row[0])
            if start is None or not _usable(row[1]):
                continue
            rows[start] = (float(row[1]), _temperature(row[2]))
        self.rows = dict(sorted(rows.items())[-MAX_ROWS:])
        self.migrated_at = _instant(raw.get("migrated_at"))

    def diagnostics(self) -> dict[str, Any]:
        """How much is kept (not the rows: the forecast's own window is in its block)."""
        starts = sorted(self.rows)
        return {
            "hours": len(starts),
            "first": starts[0].isoformat() if starts else None,
            "last": starts[-1].isoformat() if starts else None,
            "with_temperature": sum(1 for _kw, outdoor in self.rows.values() if outdoor is not None),
            "migrated_at": None if self.migrated_at is None else self.migrated_at.isoformat(),
        }
