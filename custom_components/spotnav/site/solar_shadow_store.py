"""What the own solar forecast in shadow keeps (SpotNav 3.0, `site/solar_shadow.py`): the factors' inputs for a
year, the candidates' forecasts as issued and their nightly review.

Pure: the clock is passed in. Storage is untrusted: a malformed value is dropped, never raised on.

* **Inputs** (`SolarHistory`), per UTC hour, `KEEP_DAYS` days:
  * `pv`: the measured production (hundredths of a kWh), from the Energy dashboard's statistics.
  * Per weather run (`RUNS`: 1 and 2, as Open-Meteo's `_previous_day1` / `_previous_day2`): the GHI and beam (W/m2),
    cloud cover (%) and temperature (°C) as forecast. A live fetch fills an hour's run 1 from the first fetch made at
    most `SNAPSHOT_MAX_LEAD_H[1]` hours before it, run 2 from the first made within `SNAPSHOT_MAX_LEAD_H[2]`
    hours and more than run 1's; the archive fills what the live fetches did not. Hours without radiation are not
    kept (nothing is learned from them).
  * Per run, the provider's forecast (hundredths of a kWh), by the same lead rule, from the first refresh of the
    solar correction that saw it (`site/solar_correction_shadow.py`). There is no archive of it.
  * Each series is stored as one string of integers per hour from one first hour, empty for an hour without one, so
    a year stays about 200 kB however the store is indented. Hundredths, not tenths, of a kWh: tenths would round
    away the low-sun winter hours the sun-position factor is about.
* **Forecasts as issued** (`IssueLog.forecasts`): per candidate, issue (`ISSUE_DAY_AHEAD` 13:00 for tomorrow,
  `ISSUE_MORNING` 06:00 for the rest of today) and target local day, the hours as first issued (a later issue for the
  same day never replaces it); with what each issue was made from (`IssueLog.issues`). `KEEP_LOG_DAYS` days.
* **Review** (`IssueLog.review`): each issued day once it has ended and its statistics are in (the day's last hour
  measured, or two days later): its hourly MAE and its daily kWh error (forecast less measured) over the hours both
  have. `KEEP_LOG_DAYS` days; `summary` combines the last `SUMMARY_DAYS`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Final

from ..core.parse import aware_utc
from .. import time_zones
from .open_meteo import Weather
from .sun_geometry import Plane, hour_key, key_start

FORMAT: Final = 1
KEEP_DAYS: Final = 400
KEEP_LOG_DAYS: Final = 60
SUMMARY_DAYS: Final = 30
RUNS: Final = (1, 2)
SNAPSHOT_MAX_LEAD_H: Final = {1: 30.0, 2: 54.0}
ISSUE_DAY_AHEAD: Final = "day_ahead"
ISSUE_MORNING: Final = "morning"
#: A day not fully measured is reviewed anyway this many days after it.
REVIEW_WAIT_DAYS: Final = 2
_WEATHER_FIELDS: Final = ("g", "b", "c", "t")


def _int(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    number = round(value)
    return number if low <= number <= high else None


def encode(values: Mapping[int, int], first: int) -> str:
    """`values` as one string from hour `first` to the last one held (empty for an hour without a value)."""
    if not values:
        return ""
    return ",".join("" if (v := values.get(key)) is None else str(v) for key in range(first, max(values) + 1))


def decode(raw: Any, first: int, low: int, high: int) -> dict[int, int]:
    if not isinstance(raw, str) or not raw:
        return {}
    out: dict[int, int] = {}
    for offset, part in enumerate(raw.split(",")):
        if not part:
            continue
        try:
            number = int(part)
        except ValueError:
            continue
        if low <= number <= high:
            out[first + offset] = number
    return out


#: Per stored weather field: (lowest, highest) accepted.
_BOUNDS: Final = {"g": (0, 1500), "b": (0, 1500), "c": (0, 100), "t": (-80, 70)}
_KWH_MAX: Final = 1_000_000  # hundredths: 10 MWh in an hour


@dataclass(slots=True)
class SolarHistory:
    """The factors' inputs (see the module), by hour key (hours since the epoch, UTC)."""

    pv: dict[int, int] = field(default_factory=dict)
    #: run -> field (`g`, `b`, `c`, `t`) -> hour key -> value.
    weather: dict[int, dict[str, dict[int, int]]] = field(
        default_factory=lambda: {run: {name: {} for name in _WEATHER_FIELDS} for run in RUNS}
    )
    provider: dict[int, dict[int, int]] = field(default_factory=lambda: {run: {} for run in RUNS})

    # ---- recording

    def merge_pv(self, kwh_by_hour: Mapping[datetime, float]) -> bool:
        """The measured production per completed hour (kWh); whether anything changed."""
        changed = False
        for start, kwh in kwh_by_hour.items():
            value = _int(kwh * 100 if isinstance(kwh, (int, float)) else None, 0, _KWH_MAX)
            if value is None:
                continue
            key = hour_key(start)
            if self.pv.get(key) != value:
                self.pv[key] = value
                changed = True
        return changed

    @staticmethod
    def run_for_lead(lead_h: float) -> int | None:
        """The run a forecast made `lead_h` hours before the hour is kept as (see the module), `None` beyond both."""
        if lead_h <= 0:
            return None
        for run in RUNS:
            if lead_h <= SNAPSHOT_MAX_LEAD_H[run]:
                return run
        return None

    def note_weather(self, by_hour: Mapping[datetime, Weather], fetched_at: datetime) -> bool:
        """A live fetch: each hour ahead with radiation fills its run by its lead when that run has none yet."""
        changed = False
        for start, weather in by_hour.items():
            run = self.run_for_lead((start - fetched_at).total_seconds() / 3600)
            if run is not None:
                changed |= self._put_weather(run, hour_key(start), weather)
        return changed

    def note_archive(self, run: int, by_hour: Mapping[datetime, Weather], first: date, last: date) -> bool:
        """An archived run asked for the UTC dates `first`..`last`: fills the hours that run has none for; an hour
        outside those dates is not the answer asked for and is left out."""
        changed = False
        for start, weather in by_hour.items():
            if not first <= start.astimezone(timezone.utc).date() <= last:
                continue
            changed |= self._put_weather(run, hour_key(start), weather)
        return changed

    def _put_weather(self, run: int, key: int, weather: Weather) -> bool:
        series = self.weather[run]
        if key in series["g"] or weather.ghi <= 0:
            return False
        values = {"g": weather.ghi, "b": weather.beam, "c": weather.cloud, "t": weather.temp}
        ghi = _int(values["g"], *_BOUNDS["g"])
        if ghi is None or ghi <= 0:
            return False
        for name in _WEATHER_FIELDS:
            value = _int(values[name], *_BOUNDS[name])
            if value is not None:
                series[name][key] = value
        return True

    def note_provider(self, kwh_by_hour: Mapping[datetime, float], made_at: datetime) -> bool:
        """The provider's forecast as read at `made_at`: fills each hour's run by its lead when it has none yet."""
        changed = False
        for start, kwh in kwh_by_hour.items():
            run = self.run_for_lead((start - made_at).total_seconds() / 3600)
            value = _int(kwh * 100 if isinstance(kwh, (int, float)) else None, 0, _KWH_MAX)
            if run is None or value is None or value <= 0:
                continue
            key = hour_key(start)
            if key not in self.provider[run]:
                self.provider[run][key] = value
                changed = True
        return changed

    # ---- reading

    def weather_at(self, run: int, key: int) -> Weather | None:
        series = self.weather[run]
        ghi = series["g"].get(key)
        if ghi is None:
            return None
        cloud, temp = series["c"].get(key), series["t"].get(key)
        return Weather(
            ghi=float(ghi),
            beam=float(series["b"].get(key, 0)),
            cloud=None if cloud is None else float(cloud),
            temp=None if temp is None else float(temp),
        )

    def pv_kwh(self, key: int) -> float | None:
        value = self.pv.get(key)
        return None if value is None else value / 100

    def copy(self) -> SolarHistory:
        """A copy for the executor (the event loop goes on changing this one)."""
        return SolarHistory(
            pv=dict(self.pv),
            weather={run: {name: dict(values) for name, values in fields.items()} for run, fields in self.weather.items()},
            provider={run: dict(values) for run, values in self.provider.items()},
        )

    # ---- bounds and storage

    def prune(self, now: datetime) -> bool:
        oldest = hour_key(now) - KEEP_DAYS * 24
        changed = False
        series_list: list[dict[int, int]] = [self.pv, *self.provider.values()]
        for fields in self.weather.values():
            series_list.extend(fields.values())
        for series in series_list:
            stale = [key for key in series if key < oldest]
            for key in stale:
                del series[key]
            changed |= bool(stale)
        return changed

    def _named(self) -> dict[str, dict[int, int]]:
        named: dict[str, dict[int, int]] = {"pv": self.pv}
        for run in RUNS:
            for name in _WEATHER_FIELDS:
                named[f"{name}{run}"] = self.weather[run][name]
            named[f"p{run}"] = self.provider[run]
        return named

    def as_dict(self) -> dict[str, Any]:
        named = self._named()
        keys = [min(series) for series in named.values() if series]
        first = min(keys) if keys else None
        return {
            "first": None if first is None else key_start(first).isoformat(),
            **{name: "" if first is None else encode(series, first) for name, series in named.items()},
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        start = aware_utc(raw.get("first"))
        if start is None:
            return
        first = hour_key(start)
        self.pv = decode(raw.get("pv"), first, 0, _KWH_MAX)
        for run in RUNS:
            for name in _WEATHER_FIELDS:
                self.weather[run][name] = decode(raw.get(f"{name}{run}"), first, *_BOUNDS[name])
            self.provider[run] = decode(raw.get(f"p{run}"), first, 0, _KWH_MAX)

    def diagnostics(self) -> dict[str, Any]:
        def span(series: Mapping[int, int]) -> dict[str, Any]:
            return {
                "hours": len(series),
                "oldest": None if not series else key_start(min(series)).isoformat(),
                "newest": None if not series else key_start(max(series)).isoformat(),
            }

        return {
            "pv": span(self.pv),
            **{f"weather_run{run}": span(self.weather[run]["g"]) for run in RUNS},
            **{f"provider_run{run}": span(self.provider[run]) for run in RUNS},
        }


# ------------------------------------------------------------------------------------------------ the review


@dataclass(frozen=True, slots=True)
class DayScore:
    """One candidate's issued day against the measured: hours scored, hourly MAE (kWh), and the day's forecast and
    measured kWh over those hours."""

    hours: int
    mae: float
    forecast_kwh: float
    measured_kwh: float

    @property
    def error_kwh(self) -> float:
        return self.forecast_kwh - self.measured_kwh

    def as_row(self) -> list[Any]:
        return [self.hours, round(self.mae, 3), round(self.forecast_kwh, 2), round(self.measured_kwh, 2)]

    @classmethod
    def from_row(cls, raw: Any) -> DayScore | None:
        if not isinstance(raw, list) or len(raw) != 4:
            return None
        hours = raw[0]
        if isinstance(hours, bool) or not isinstance(hours, int) or hours <= 0:
            return None
        numbers = raw[1:]
        if not all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) for v in numbers):
            return None
        return cls(hours, float(numbers[0]), float(numbers[1]), float(numbers[2]))


def day_score(pairs: list[tuple[float, float]]) -> DayScore | None:
    """The score of `(forecast, measured)` kWh pairs, `None` without any."""
    if not pairs:
        return None
    return DayScore(
        hours=len(pairs),
        mae=sum(abs(f - a) for f, a in pairs) / len(pairs),
        forecast_kwh=sum(f for f, _ in pairs),
        measured_kwh=sum(a for _, a in pairs),
    )


#: (candidate, issue, target local day ISO).
LogKey = tuple[str, str, str]


@dataclass(slots=True)
class IssueLog:
    """The candidates' forecasts as issued and their reviews (see the module), on the clock of `tz`."""

    tz: str
    #: (candidate, issue, day) -> (first hour key, hundredths of a kWh per hour from it).
    forecasts: dict[LogKey, tuple[int, list[int | None]]] = field(default_factory=dict)
    #: (issue, day) -> what the issue was made from, per group of candidates (`own`, `provider`).
    issues: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    reviews: dict[LogKey, DayScore] = field(default_factory=dict)

    def issued(self, issue: str, day: date, group: str) -> bool:
        return group in self.issues.get((issue, day.isoformat()), {})

    def record(
        self,
        issue: str,
        day: date,
        group: str,
        by_candidate: Mapping[str, Mapping[datetime, float]],
        meta: Mapping[str, Any],
    ) -> int:
        """Log one issue of a group of candidates for `day` (first wins); how many candidates were logged."""
        if self.issued(issue, day, group):
            return 0
        self.issues.setdefault((issue, day.isoformat()), {})[group] = dict(meta)
        logged = 0
        for candidate, by_hour in by_candidate.items():
            key = (candidate, issue, day.isoformat())
            hours = {hour_key(start): kwh for start, kwh in by_hour.items() if _int(kwh * 100, 0, _KWH_MAX) is not None}
            if key in self.forecasts or not hours:
                continue
            first = min(hours)
            self.forecasts[key] = (
                first,
                [None if (v := hours.get(k)) is None else round(v * 100) for k in range(first, max(hours) + 1)],
            )
            logged += 1
        return logged

    def review(self, history: SolarHistory, now: datetime) -> list[tuple[LogKey, DayScore]]:
        """Review every issued day that has ended and is measured (see the module); the new reviews."""
        zone = time_zones.require_zone(self.tz)
        today = now.astimezone(zone).date()
        made: list[tuple[LogKey, DayScore]] = []
        for key, (first, values) in sorted(self.forecasts.items()):
            if key in self.reviews:
                continue
            day = date.fromisoformat(key[2])
            if day >= today:
                continue
            last_key = hour_key(datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)) - 1
            if last_key not in history.pv and (today - day).days < REVIEW_WAIT_DAYS:
                continue
            pairs = [
                (value / 100, measured)
                for offset, value in enumerate(values)
                if value is not None and (measured := history.pv_kwh(first + offset)) is not None
            ]
            score = day_score(pairs)
            if score is None:
                continue
            self.reviews[key] = score
            made.append((key, score))
        return made

    def prune(self, now: datetime) -> None:
        zone = time_zones.require_zone(self.tz)
        first_day = (now.astimezone(zone).date() - timedelta(days=KEEP_LOG_DAYS)).isoformat()
        self.forecasts = {key: value for key, value in self.forecasts.items() if key[2] >= first_day}
        self.reviews = {key: value for key, value in self.reviews.items() if key[2] >= first_day}
        self.issues = {key: value for key, value in self.issues.items() if key[1] >= first_day}

    def summary(self, now: datetime, days: int = SUMMARY_DAYS) -> dict[str, dict[str, Any]]:
        """Per candidate and issue, the last `days` reviewed days combined: days, hours, hourly MAE (hour-weighted),
        daily kWh MAE and mean daily error (bias, forecast less measured)."""
        zone = time_zones.require_zone(self.tz)
        first_day = (now.astimezone(zone).date() - timedelta(days=days)).isoformat()
        grouped: dict[tuple[str, str], list[DayScore]] = {}
        for (candidate, issue, day), score in self.reviews.items():
            if day >= first_day:
                grouped.setdefault((candidate, issue), []).append(score)
        out: dict[str, dict[str, Any]] = {}
        for (candidate, issue), scores in sorted(grouped.items()):
            hours = sum(s.hours for s in scores)
            out.setdefault(candidate, {})[issue] = {
                "days": len(scores),
                "hours": hours,
                "hourly_mae_kwh": round(sum(s.mae * s.hours for s in scores) / hours, 3),
                "daily_mae_kwh": round(sum(abs(s.error_kwh) for s in scores) / len(scores), 2),
                "daily_bias_kwh": round(sum(s.error_kwh for s in scores) / len(scores), 2),
                "measured_kwh_per_day": round(sum(s.measured_kwh for s in scores) / len(scores), 2),
            }
        return out

    def recent(self, candidates: Iterable[str], days: int = 7) -> list[dict[str, Any]]:
        """The last `days` reviewed days, per day and issue the candidates' scores (for the diagnostics)."""
        by_day: dict[tuple[str, str], dict[str, Any]] = {}
        wanted = set(candidates)
        for (candidate, issue, day), score in self.reviews.items():
            if candidate in wanted:
                row = by_day.setdefault((day, issue), {"date": day, "issue": issue, "measured_kwh": None})
                row["measured_kwh"] = round(score.measured_kwh, 2)
                row[candidate] = {"mae": round(score.mae, 3), "error_kwh": round(score.error_kwh, 2)}
        kept_days = sorted({day for day, _ in by_day})[-days:]
        return [row for (day, _issue), row in sorted(by_day.items()) if day in kept_days]

    # ---- storage

    def as_dict(self) -> dict[str, Any]:
        return {
            "forecasts": [
                [c, i, d, key_start(first).isoformat(), ",".join("" if v is None else str(v) for v in values)]
                for (c, i, d), (first, values) in sorted(self.forecasts.items())
            ],
            "issues": [[i, d, meta] for (i, d), meta in sorted(self.issues.items())],
            "reviews": [[c, i, d, *score.as_row()] for (c, i, d), score in sorted(self.reviews.items())],
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        for row in raw.get("forecasts") or []:
            if not isinstance(row, list) or len(row) != 5 or not all(isinstance(v, str) for v in row):
                continue
            start = aware_utc(row[3])
            if start is None or not _iso_day(row[2]):
                continue
            first = hour_key(start)
            values: list[int | None] = []
            for part in row[4].split(","):
                try:
                    values.append(int(part) if part else None)
                except ValueError:
                    values.append(None)
            self.forecasts[(row[0], row[1], row[2])] = (first, values)
        for row in raw.get("issues") or []:
            if isinstance(row, list) and len(row) == 3 and isinstance(row[0], str) and _iso_day(row[1]):
                if isinstance(row[2], dict):
                    self.issues[(row[0], row[1])] = row[2]
        for row in raw.get("reviews") or []:
            if not isinstance(row, list) or len(row) != 7 or not all(isinstance(v, str) for v in row[:3]):
                continue
            score = DayScore.from_row(row[3:])
            if score is not None and _iso_day(row[2]):
                self.reviews[(row[0], row[1], row[2])] = score


def _iso_day(raw: Any) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        date.fromisoformat(raw)
    except ValueError:
        return False
    return True


# ------------------------------------------------------------------------------------------------ planes


def planes_as_rows(planes: Iterable[Plane] | None) -> list[list[float]] | None:
    return None if planes is None else [[round(t, 1), round(a, 1), round(k, 3)] for t, a, k in planes]


def planes_from_rows(raw: Any) -> tuple[Plane, ...] | None:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
        return None
    planes: list[Plane] = []
    for row in raw:
        if not isinstance(row, list) or len(row) != 3:
            return None
        if not all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) for v in row):
            return None
        tilt, az, kwp = (float(v) for v in row)
        if not (0 <= tilt <= 90 and 0 <= az < 360 and 0 <= kwp <= 10_000):
            return None
        planes.append((tilt, az, kwp))
    return tuple(planes)
