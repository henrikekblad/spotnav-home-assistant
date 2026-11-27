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

**The house's whole load** (`gross`), on a site with a home battery's power reading: per hour `grid - car - battery`
(`HourMean.ex_battery_kw`, the house less the sun) plus the hour's solar production from the Energy dashboard's
statistics (kWh in the hour, its mean kW), never below 0. The import less the chargers (`rows`) counts what the battery
charged and discharged, so on a battery site it is not the house: the owner's read ~0 kW while the battery covered it.

* **Production**: an hour is filed once the statistics have its figure (they are compiled after the hour); it waits
  `PRODUCTION_WAIT_H` hours at most and is then left out, never filed net of the sun. A site without solar statistics
  files every hour as it is and marks it (`sun` false): the rows are then the house less the sun.
* **Migration** (`take_rows`): the battery plan kept its own rows of `grid - car - battery` before this; they are
  taken over once (`gross_migrated_at`), each hour before the first one filed here, with its production added.
* **Filled from the statistics** (`gap_hours`, `fill_gross`): an hour still not filed once it is past the production
  wait (the hour tracker had no `grid - car - battery` for it: a charger that could not be read, a restart; or its
  production never came) is filled from the Energy dashboard's hourly statistics, `FILL_DAYS` back at most: the whole
  load is `grid_in - grid_out + solar + battery_out - battery_in - car` (`HourFlows`, kWh in the hour, its mean kW),
  `car` the chargers' own energy for the hour (their energy registers, the battery plan's measure, their sessions).
  Only where every statistic the site has, and the car, has the hour; never over an hour already filed, and a
  measured hour never waits on it (the gaps are the hours past the wait). The figures are measured (meters, not an
  estimate), so the forecast learns from a filled hour as from any other; it is marked (`filled`, with the outdoor
  temperature the tracker read in it) and counted in the diagnostics (`gross_filled`). On a hybrid whose dashboard
  meters its battery at the battery the filled hour holds the inverter's loss as the battery's, a little off the
  tracker's AC-side figure (`site/demand_tracker.py`); a filled hour below 0 by more than `FILL_SLACK_KWH` (the meters
  disagree) is left out, a smaller shortfall is 0.
* **Basis** (`gross_ready`): on a solar site without a battery the house's whole load is the site's house figure from
  `MIN_GROSS_HOURS` hours in the forecast's window on; on a battery site from the first (`house_forecast.house_basis`).

It decides nothing on its own: the forecast reads it (`site/house_forecast.py`), the diagnostics count it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Final

from ..core.parse import aware_utc
from ..planning.demand_charge import HourMean
from .load_forecast import WINDOW_DAYS

#: The hours of the last this many days are kept: the forecast's six weeks, and two weeks to spare.
KEEP_DAYS: Final = 56
#: A hard cap beside the age bound, with room for DST.
MAX_ROWS: Final = KEEP_DAYS * 24 + 48
#: On a solar site without a battery the house's whole load is the site's house figure once this many of its hours
#: are in the forecast's window (three days); a battery site does not wait (`house_forecast.house_basis`).
MIN_GROSS_HOURS: Final = 72
#: An hour waits this long (hours) for its solar production in the statistics; then it is left out.
PRODUCTION_WAIT_H: Final = 24
#: The flat figure of the whole load is the mean of its last this many hours.
FLAT_HOURS: Final = 24
#: Hours of the whole load still missing are filled from the statistics this many days back at most (`fill_gross`).
FILL_DAYS: Final = 7
#: A filled hour may come out below 0 by this much (kWh, the meters read at different moments) and is then 0.
FILL_SLACK_KWH: Final = 0.1


@dataclass(frozen=True, slots=True)
class HourFlows:
    """The Energy dashboard's hourly energy (kWh per UTC hour start) for `HouseHistory.fill_gross`; `None` where the
    site has no such meter (its term is then 0)."""

    grid_in: Mapping[datetime, float]
    grid_out: Mapping[datetime, float] | None = None
    solar: Mapping[datetime, float] | None = None
    battery_in: Mapping[datetime, float] | None = None
    battery_out: Mapping[datetime, float] | None = None

    def house_kwh(self, start: datetime, car_kwh: float) -> float | None:
        """The hour's whole load (kWh), `None` when a meter the site has lacks the hour."""
        terms: list[tuple[Mapping[datetime, float] | None, float]] = [
            (self.grid_in, 1.0),
            (self.grid_out, -1.0),
            (self.solar, 1.0),
            (self.battery_out, 1.0),
            (self.battery_in, -1.0),
        ]
        total = -car_kwh
        for series, sign in terms:
            if series is None:
                continue
            value = series.get(start)
            if value is None or not _usable(value):
                return None
            total += sign * float(value)
        return total


def _usable(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def _temperature(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


@dataclass(slots=True)
class HouseHistory:
    """The kept hours (see the module), by start."""

    rows: dict[datetime, tuple[float, float | None]] = field(default_factory=dict)
    #: When the tracker's hours were first taken over (`None` until the first merge).
    migrated_at: datetime | None = None
    #: The house's whole load per hour (kW) and whether the sun's production was added (see the module).
    gross: dict[datetime, tuple[float, bool]] = field(default_factory=dict)
    #: When the battery plan's own rows were taken over (`None` until they were).
    gross_migrated_at: datetime | None = None
    #: The hours of the whole load filled from the statistics (`fill_gross`), with the outdoor temperature the
    #: tracker read in each (`None` without one).
    filled: dict[datetime, float | None] = field(default_factory=dict)

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
        gross = sorted((start, row) for start, row in self.gross.items() if start >= oldest)
        self.gross = dict(gross[-MAX_ROWS:])
        self.filled = {start: outdoor for start, outdoor in self.filled.items() if start in self.gross}

    # ---- the house's whole load

    def waiting(self, hours: Iterable[HourMean], now: datetime) -> list[HourMean]:
        """The tracker's hours with a `grid - car - battery` figure not filed yet, of the last `PRODUCTION_WAIT_H`
        hours: those that wait for their production."""
        oldest = now.astimezone(timezone.utc) - timedelta(hours=PRODUCTION_WAIT_H)
        return sorted(
            (
                hour
                for hour in hours
                if not hour.imported
                and hour.ex_battery_kw is not None
                and math.isfinite(hour.ex_battery_kw)
                and hour.start >= oldest
                and hour.start.astimezone(timezone.utc) not in self.gross
            ),
            key=lambda hour: hour.start,
        )

    def merge_gross(
        self, hours: Iterable[HourMean], production_kwh: Mapping[datetime, float] | None, now: datetime
    ) -> int:
        """File the waiting hours (`waiting`) with their production added (`production_kwh`: kWh per UTC hour start;
        `None` when the site has no solar statistics: filed as they are, marked); an hour without a figure waits. How
        many were filed."""
        filed = 0
        for hour in self.waiting(hours, now):
            start = hour.start.astimezone(timezone.utc)
            sun = None if production_kwh is None else production_kwh.get(start)
            if production_kwh is not None and (sun is None or not _usable(sun)):
                continue
            self.gross[start] = (max(0.0, float(hour.ex_battery_kw) + (sun or 0.0)), production_kwh is not None)  # type: ignore[arg-type]
            filed += 1
        if filed:
            self._prune(now.astimezone(timezone.utc))
        return filed

    def take_rows(
        self, rows: Sequence[tuple[datetime, float]], production_kwh: Mapping[datetime, float] | None, now: datetime
    ) -> int:
        """Take the battery plan's own rows over, once (`(hour start, mean kW of grid - car - battery)`): each hour
        before the first one filed here and not held, its production added as `merge_gross` adds it. How many."""
        now = now.astimezone(timezone.utc)
        # The first hour filed from the tracker: an hour filled from the statistics (`fill_gross`) holds none back.
        first = min((start for start in self.gross if start not in self.filled), default=None)
        taken = 0
        for start, kw in rows:
            start = start.astimezone(timezone.utc)  # noqa: PLW2901
            if (first is not None and start >= first) or start in self.gross:
                continue
            if isinstance(kw, bool) or not isinstance(kw, (int, float)) or not math.isfinite(kw):
                continue
            sun = None if production_kwh is None else production_kwh.get(start)
            if production_kwh is not None and (sun is None or not _usable(sun)):
                continue
            self.gross[start] = (max(0.0, float(kw) + (sun or 0.0)), production_kwh is not None)
            taken += 1
        self.gross_migrated_at = now
        self._prune(now)
        return taken

    def gap_hours(self, now: datetime) -> list[datetime]:
        """The hours of the last `FILL_DAYS` days not filed and no longer waiting for their production (`waiting`):
        those `fill_gross` may fill, oldest first (UTC hour starts)."""
        now = now.astimezone(timezone.utc)
        current = now.replace(minute=0, second=0, microsecond=0)
        waited = now - timedelta(hours=PRODUCTION_WAIT_H)
        moment = current - timedelta(days=FILL_DAYS)
        gaps: list[datetime] = []
        while moment < current:
            if moment < waited and moment not in self.gross:
                gaps.append(moment)
            moment += timedelta(hours=1)
        return gaps

    def fill_gross(
        self,
        flows: HourFlows,
        car_kwh: Mapping[datetime, float],
        now: datetime,
        *,
        outdoor: Mapping[datetime, float | None] | None = None,
    ) -> int:
        """Fill the gaps (`gap_hours`) from the statistics (see the module): an hour is filed only where every meter
        the site has and the cars' energy (`car_kwh`) have it; marked `filled`, with its `outdoor` temperature. Never
        over a filed hour. How many were filled."""
        filled = 0
        sun = flows.solar is not None
        for start in self.gap_hours(now):
            car = car_kwh.get(start)
            if car is None or not _usable(car):
                continue
            kwh = flows.house_kwh(start, float(car))
            if kwh is None or not math.isfinite(kwh) or kwh < -FILL_SLACK_KWH:
                continue
            self.gross[start] = (max(0.0, kwh), sun)
            self.filled[start] = _temperature((outdoor or {}).get(start))
            filled += 1
        if filled:
            self._prune(now.astimezone(timezone.utc))
        return filled

    def gross_in_window(self, now: datetime) -> int:
        """How many hours of the whole load are in the forecast's window."""
        oldest = now.astimezone(timezone.utc) - timedelta(days=WINDOW_DAYS)
        return sum(1 for start in self.gross if start >= oldest)

    def gross_ready(self, now: datetime) -> bool:
        """Whether the house's whole load has enough hours to be the site's house figure (`MIN_GROSS_HOURS`)."""
        return self.gross_in_window(now) >= MIN_GROSS_HOURS

    def gross_hours(self) -> list[HourMean]:
        """The whole load's hours as the forecast reads them, oldest first, each with the outdoor temperature the
        measured hour of the same start recorded (a filled hour's own, `filled`)."""
        return [
            HourMean(
                start=start,
                kw=kw,
                house_kw=kw,
                outdoor_c=self.rows[start][1] if start in self.rows else self.filled.get(start),
            )
            for start, (kw, _sun) in sorted(self.gross.items())
        ]

    def gross_with_sun(self) -> bool:
        """Whether every hour of the whole load had the sun's production added (false: some are the house less the
        sun, as on a site without solar statistics)."""
        return bool(self.gross) and all(sun for _kw, sun in self.gross.values())

    def gross_flat_kw(self) -> float | None:
        """The whole load's flat figure: the mean of its last `FLAT_HOURS` hours, `None` without any."""
        recent = [kw for _start, (kw, _sun) in sorted(self.gross.items())[-FLAT_HOURS:]]
        return sum(recent) / len(recent) if recent else None

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
            # Beside the hours: a build before the whole load ignores the key.
            "gross": [[start.isoformat(), round(kw, 3), 1 if sun else 0] for start, (kw, sun) in sorted(self.gross.items())],
            "gross_migrated_at": None if self.gross_migrated_at is None else self.gross_migrated_at.isoformat(),
            # Beside the whole load's rows, so a build before the fill reads them as any other hour.
            "gross_filled": [
                [start.isoformat(), None if outdoor is None else round(outdoor, 1)]
                for start, outdoor in sorted(self.filled.items())
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
            start = aware_utc(row[0])
            if start is None or not _usable(row[1]):
                continue
            rows[start] = (float(row[1]), _temperature(row[2]))
        self.rows = dict(sorted(rows.items())[-MAX_ROWS:])
        self.migrated_at = aware_utc(raw.get("migrated_at"))
        gross: dict[datetime, tuple[float, bool]] = {}
        for row in raw.get("gross") or []:
            if not isinstance(row, list) or len(row) != 3 or row[2] not in (0, 1) or isinstance(row[2], bool):
                continue
            start = aware_utc(row[0])
            if start is None or not _usable(row[1]):
                continue
            gross[start] = (float(row[1]), row[2] == 1)
        self.gross = dict(sorted(gross.items())[-MAX_ROWS:])
        self.gross_migrated_at = aware_utc(raw.get("gross_migrated_at"))
        filled: dict[datetime, float | None] = {}
        kept = raw.get("gross_filled")
        for row in kept if isinstance(kept, list) else []:
            if not isinstance(row, list) or len(row) != 2:
                continue
            start = aware_utc(row[0])
            if start is not None and start in self.gross:
                filled[start] = _temperature(row[1])
        self.filled = filled

    def diagnostics(self) -> dict[str, Any]:
        """How much is kept (not the rows: the forecast's own window is in its block)."""
        starts = sorted(self.rows)
        return {
            "hours": len(starts),
            "first": starts[0].isoformat() if starts else None,
            "last": starts[-1].isoformat() if starts else None,
            "with_temperature": sum(1 for _kw, outdoor in self.rows.values() if outdoor is not None),
            "migrated_at": None if self.migrated_at is None else self.migrated_at.isoformat(),
            "gross_hours": len(self.gross),
            "gross_first": min(self.gross).isoformat() if self.gross else None,
            "gross_last": max(self.gross).isoformat() if self.gross else None,
            "gross_with_sun": sum(1 for _kw, sun in self.gross.values() if sun),
            "gross_filled": len(self.filled),
            "gross_migrated_at": None if self.gross_migrated_at is None else self.gross_migrated_at.isoformat(),
        }
