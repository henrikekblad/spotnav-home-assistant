"""The outdoor temperature kept for a year, and the forecast temperatures as they were known (SpotNav 3.0).

Pure: the clock is passed in. The house forecast's own history (`site/house_history.py`) keeps eight weeks and the
demand tracker two months; a temperature that is gone cannot be fetched again (the weather entity has no history, and
an archive would be another source than the one the house was measured against). A later model trained on a winter
needs the winter's temperatures, and needs the forecast temperatures as they were known when a plan was made, not the
outcome (`plans/research_integrations/load_forecast_experiment_2026-11.md`, round 2, step 1). So this keeps:

* **Measured** (`measured`): each completed hour's outdoor temperature as the forecast reads it in its history
  (`HourMean.outdoor_c`, the mean of the readings of the hour), rounded to 0.1 °C, from the tracker's hours on each
  refresh. Its source is kept as change markers, not per hour (`sources`): from an hour on, `sensor:<entity_id>` (a
  heat pump's outdoor sensor) or `weather:<entity_id>`.
* **Forecast as issued** (`day_ahead`, `last`): for each hour ahead, the weather entity's forecast temperature at the
  day-ahead issue (the first refresh from `DAY_AHEAD_HOUR` local on for the next local day, the one that day's plans
  were made on; first wins, as in the forecast log) and at the last refresh before the hour started (overwritten on
  each refresh until then). The weather entity behind them is kept as change markers too (`forecast_sources`).
* **Bounds**: `KEEP_DAYS` days, at most `MAX_HOURS` hours per series. Storage is untrusted: a malformed value is
  dropped. Each series is stored as one list of tenths of a degree per hour from one first hour (`as_dict`), about
  40 kB a year, so the store can be written at most hourly without weight.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Final

from ..planning.demand_charge import HourMean
from .forecast_log import next_day_hours

#: Kept this long: a whole year and a month to spare, so one winter can be compared with the next.
KEEP_DAYS: Final = 400
#: A hard cap per series beside the age bound, with room for DST and the forecast's two days ahead.
MAX_HOURS: Final = (KEEP_DAYS + 3) * 24
#: The store's format.
FORMAT: Final = 1

#: A source marker: what the history temperature (or the forecast) is read from.
SOURCE_SENSOR: Final = "sensor"
SOURCE_WEATHER: Final = "weather"


def source_marker(kind: str, entity_id: str | None) -> str | None:
    """`<kind>:<entity_id>`, `None` without an entity."""
    return None if not entity_id else f"{kind}:{entity_id}"


def _key(start: datetime) -> int:
    """An hour's key: the UTC hour its start falls in (hours since the epoch)."""
    return int(start.timestamp()) // 3600


def _start(key: int) -> datetime:
    return datetime.fromtimestamp(key * 3600, timezone.utc)


def _tenths(value: Any) -> int | None:
    """A temperature (°C) as tenths of a degree, `None` when it is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(value * 10)


def _instant(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return None if moment.tzinfo is None else moment.astimezone(timezone.utc)


@dataclass(slots=True)
class TemperatureHistory:
    """The kept temperatures (see the module): tenths of a degree per hour key."""

    measured: dict[int, int] = field(default_factory=dict)
    day_ahead: dict[int, int] = field(default_factory=dict)
    last: dict[int, int] = field(default_factory=dict)
    #: `(from hour key, marker)`, oldest first; a marker is written only when the source changes.
    sources: list[tuple[int, str]] = field(default_factory=list)
    forecast_sources: list[tuple[int, str]] = field(default_factory=list)

    # ---- recording

    def merge_measured(self, hours: Iterable[HourMean], now: datetime) -> bool:
        """Take the completed hours' temperatures in (the tracker's figure wins); whether anything changed."""
        changed = False
        for hour in hours:
            tenths = _tenths(hour.outdoor_c)
            if tenths is None:
                continue
            key = _key(hour.start)
            if self.measured.get(key) != tenths:
                self.measured[key] = tenths
                changed = True
        return self._prune(now) or changed

    def mark_source(self, marker: str | None, start: datetime) -> bool:
        """The history temperature is read from `marker` from the hour starting at `start` on; whether that was new."""
        return _mark(self.sources, marker, _key(start))

    def record_forecast(
        self, temperatures: Mapping[datetime, float], marker: str | None, now: datetime, tz: str
    ) -> bool:
        """Log the weather entity's forecast temperatures just fetched: every hour not yet started as the last issue
        before it, and the next local day's as the day-ahead issue when this is the first from `DAY_AHEAD_HOUR` on.
        Whether anything changed."""
        if not temperatures:
            return False
        changed = _mark(self.forecast_sources, marker, _key(now))
        now = now.astimezone(timezone.utc)
        for start, celsius in temperatures.items():
            tenths = _tenths(celsius)
            if tenths is None or start <= now:
                continue
            key = _key(start)
            if self.last.get(key) != tenths:
                self.last[key] = tenths
                changed = True
        for start, celsius in next_day_hours(temperatures, now, tz).items():
            tenths = _tenths(celsius)
            key = _key(start)
            if tenths is not None and key not in self.day_ahead:
                self.day_ahead[key] = tenths
                changed = True
        return self._prune(now) or changed

    def _prune(self, now: datetime) -> bool:
        oldest = _key(now.astimezone(timezone.utc) - timedelta(days=KEEP_DAYS))
        changed = False
        for name in ("measured", "day_ahead", "last"):
            series: dict[int, int] = getattr(self, name)
            if series and (min(series) < oldest or len(series) > MAX_HOURS):
                kept = sorted((key, tenths) for key, tenths in series.items() if key >= oldest)
                setattr(self, name, dict(kept[-MAX_HOURS:]))
                changed = True
        for markers in (self.sources, self.forecast_sources):
            # The marker in force at the oldest hour kept stays; those before it go.
            while len(markers) > 1 and markers[1][0] <= oldest:
                markers.pop(0)
                changed = True
        return changed

    # ---- storage

    def as_dict(self) -> dict[str, Any]:
        """Each series as one list of tenths of a degree per hour from `first` (`None` for an hour without one)."""
        keys = [*self.measured, *self.day_ahead, *self.last]
        first = min(keys) if keys else None

        def series(values: dict[int, int]) -> list[int | None]:
            if first is None or not values:
                return []
            return [values.get(key) for key in range(first, max(values) + 1)]

        return {
            "format": FORMAT,
            "first": None if first is None else _start(first).isoformat(),
            "measured": series(self.measured),
            "day_ahead": series(self.day_ahead),
            "last": series(self.last),
            "sources": [[_start(key).isoformat(), marker] for key, marker in self.sources],
            "forecast_sources": [[_start(key).isoformat(), marker] for key, marker in self.forecast_sources],
        }

    def restore(self, raw: Any, now: datetime) -> None:
        """Take what the store kept; a malformed value or marker is dropped, old hours are pruned."""
        if not isinstance(raw, dict) or raw.get("format") != FORMAT:
            return
        first = _instant(raw.get("first"))
        if first is not None:
            base = _key(first)
            for name in ("measured", "day_ahead", "last"):
                values = raw.get(name)
                series: dict[int, int] = {}
                for offset, tenths in enumerate(values if isinstance(values, list) else []):
                    if isinstance(tenths, int) and not isinstance(tenths, bool) and abs(tenths) <= 1000:
                        series[base + offset] = tenths
                setattr(self, name, series)
        self.sources = _markers(raw.get("sources"))
        self.forecast_sources = _markers(raw.get("forecast_sources"))
        self._prune(now)

    def diagnostics(self) -> dict[str, Any]:
        """How much is kept, from when, and the sources (not the hours)."""
        return {
            "measured_hours": len(self.measured),
            "oldest": None if not self.measured else _start(min(self.measured)).isoformat(),
            "newest": None if not self.measured else _start(max(self.measured)).isoformat(),
            "day_ahead_hours": len(self.day_ahead),
            "last_hours": len(self.last),
            "sources": [[_start(key).isoformat(), marker] for key, marker in self.sources],
            "forecast_sources": [[_start(key).isoformat(), marker] for key, marker in self.forecast_sources],
        }


def _mark(markers: list[tuple[int, str]], marker: str | None, key: int) -> bool:
    if marker is None or (markers and markers[-1][1] == marker):
        return False
    if markers and markers[-1][0] >= key:
        # Changed again within the same hour (or a clock that went back): the hour takes the newest.
        markers[-1] = (markers[-1][0], marker)
        if len(markers) > 1 and markers[-2][1] == marker:
            markers.pop()
        return True
    markers.append((key, marker))
    return True


def _markers(raw: Any) -> list[tuple[int, str]]:
    markers: list[tuple[int, str]] = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, list) or len(row) != 2 or not isinstance(row[1], str) or not row[1]:
            continue
        start = _instant(row[0])
        if start is None or (markers and _key(start) <= markers[-1][0]):
            continue
        markers.append((_key(start), row[1]))
    return markers
