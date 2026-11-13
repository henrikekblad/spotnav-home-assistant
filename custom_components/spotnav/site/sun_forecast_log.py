"""A minimal forecast log for the sun (SpotNav 3.0, wave 2): each hour's first forecast and its outcome, reviewed
per local day the night after. Pure: the clock is passed in.

It is kept the same shape as the house load forecast's log (`site/forecast_log.py` on the load-forecast branch,
not yet in this one): the same kinds-of-hours store (`version`, `tz`, `kinds` -> `hours` rows
`[start, forecast, made_at, actual]` and `reviews`), the same rules and the same scores, so the two can become one
log without touching what a site has kept. Only what the sun needs is here: no night or evening split (both are
written as `None`, as that log does for a day without such hours).

* **The first forecast** made for each hour is logged, with when it was made; a later forecast never replaces it,
  and only hours that start after it was made are logged.
* **The outcome** is added once the hour has completed; an hour without one stays unscored.
* **The review**: every local day that has ended and has a scored hour is reviewed once: mean absolute error, how
  often the forecast was below the outcome, and by more than `BIG_UNDER`.
* **Bounds**: hours `KEEP_HOURS_DAYS` days and `MAX_HOURS` per kind, reviews `KEEP_REVIEW_DAYS`, `MAX_KINDS` kinds.
  Storage is untrusted: a malformed record is dropped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Final

from .. import time_zones

#: The sun's forecast as SpotNav reads and uses it (kWh in the hour, the sources summed, never corrected).
KIND_SUN: Final = "sun_kwh"
#: The same forecast after the shadow correction (`site/solar_correction.py`), logged beside it for the review.
KIND_SUN_CORRECTED: Final = "sun_kwh_corrected"

KEEP_HOURS_DAYS: Final = 14
MAX_HOURS: Final = (KEEP_HOURS_DAYS + 3) * 24
KEEP_REVIEW_DAYS: Final = 30
MAX_KINDS: Final = 8
#: An under-forecast by more than this (kWh) is a large one.
BIG_UNDER: Final = 0.5
STORE_VERSION: Final = 1


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _instant(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return None if moment.tzinfo is None else moment.astimezone(timezone.utc)


@dataclass(slots=True)
class LoggedHour:
    forecast: float
    made_at: datetime
    actual: float | None = None


@dataclass(frozen=True, slots=True)
class Score:
    hours: int
    mae: float
    under_share: float
    big_under_share: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "mae": round(self.mae, 3),
            "under_share": round(self.under_share, 3),
            "big_under_share": round(self.big_under_share, 3),
        }


def score(pairs: list[tuple[float, float]]) -> Score | None:
    """The score of `(forecast, actual)` pairs, `None` without any."""
    if not pairs:
        return None
    errors = [actual - forecast for forecast, actual in pairs]
    count = len(errors)
    return Score(
        hours=count,
        mae=sum(abs(error) for error in errors) / count,
        under_share=sum(1 for error in errors if error > 0) / count,
        big_under_share=sum(1 for error in errors if error > BIG_UNDER) / count,
    )


def _score_from(raw: Any) -> Score | None:
    if not isinstance(raw, dict):
        return None
    hours = raw.get("hours")
    numbers = [raw.get(key) for key in ("mae", "under_share", "big_under_share")]
    if isinstance(hours, bool) or not isinstance(hours, int) or hours <= 0:
        return None
    if not all(_number(value) and value >= 0 for value in numbers):
        return None
    return Score(hours, float(numbers[0]), float(numbers[1]), float(numbers[2]))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class DayReview:
    day: date
    reviewed_at: datetime
    all: Score

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "reviewed_at": self.reviewed_at.isoformat(),
            "all": self.all.as_dict(),
            "night": None,
            "evening": None,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> DayReview | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("date"), str):
            return None
        try:
            day = date.fromisoformat(raw["date"])
        except ValueError:
            return None
        reviewed_at = _instant(raw.get("reviewed_at"))
        whole = _score_from(raw.get("all"))
        if reviewed_at is None or whole is None:
            return None
        return cls(day, reviewed_at, whole)


@dataclass(slots=True)
class KindLog:
    hours: dict[datetime, LoggedHour] = field(default_factory=dict)
    reviews: list[DayReview] = field(default_factory=list)


@dataclass(slots=True)
class SunForecastLog:
    """The site's sun forecast log (see the module), on the clock of `tz`."""

    tz: str
    kinds: dict[str, KindLog] = field(default_factory=dict)

    def _kind(self, kind: str) -> KindLog | None:
        log = self.kinds.get(kind)
        if log is None and len(self.kinds) < MAX_KINDS:
            log = self.kinds[kind] = KindLog()
        return log

    def record_forecast(self, kind: str, by_hour: Mapping[datetime, float | None], made_at: datetime) -> int:
        """Log the forecast for each hour that starts after `made_at` and has none yet; how many were logged."""
        log = self._kind(kind)
        if log is None:
            return 0
        made_at = made_at.astimezone(timezone.utc)
        logged = 0
        for start, value in by_hour.items():
            if value is None or not _number(value) or start <= made_at or start in log.hours:
                continue
            log.hours[start] = LoggedHour(float(value), made_at)
            logged += 1
        if logged:
            self._prune(log, made_at)
        return logged

    def record_actual(self, kind: str, start: datetime, value: float | None) -> bool:
        """The outcome of the hour starting at `start`; whether it was added (a logged hour without one yet)."""
        log = self.kinds.get(kind)
        hour = None if log is None else log.hours.get(start)
        if hour is None or hour.actual is not None or value is None or not _number(value):
            return False
        hour.actual = float(value)
        return True

    def hours(self, kind: str) -> dict[datetime, LoggedHour]:
        """The kind's logged hours by start (UTC); empty for a kind never logged."""
        log = self.kinds.get(kind)
        return {} if log is None else dict(log.hours)

    def review(self, now: datetime) -> list[tuple[str, DayReview]]:
        """Review every local day that has ended, has a scored hour and has no review yet; the new reviews."""
        zone = time_zones.require_zone(self.tz)
        now = now.astimezone(timezone.utc)
        today = now.astimezone(zone).date()
        made: list[tuple[str, DayReview]] = []
        for kind, log in self.kinds.items():
            reviewed = {review.day for review in log.reviews}
            by_day: dict[date, list[tuple[float, float]]] = {}
            for start, hour in log.hours.items():
                day = start.astimezone(zone).date()
                if hour.actual is None or day >= today:
                    continue
                by_day.setdefault(day, []).append((hour.forecast, hour.actual))
            for day in sorted(by_day):
                whole = score(by_day[day])
                if day in reviewed or whole is None:
                    continue
                review = DayReview(day=day, reviewed_at=now, all=whole)
                log.reviews.append(review)
                made.append((kind, review))
            log.reviews.sort(key=lambda review: review.day)
            self._prune(log, now)
        return made

    def _prune(self, log: KindLog, now: datetime) -> None:
        oldest = now - timedelta(days=KEEP_HOURS_DAYS)
        kept = sorted((start, hour) for start, hour in log.hours.items() if start >= oldest)
        log.hours = dict(kept[-MAX_HOURS:])
        first_day = now.astimezone(time_zones.require_zone(self.tz)).date() - timedelta(days=KEEP_REVIEW_DAYS)
        log.reviews = [review for review in log.reviews if review.day >= first_day][-KEEP_REVIEW_DAYS:]

    def last_week(self, kind: str) -> Score | None:
        """The last seven reviewed days' scores combined, weighted by their hours."""
        log = self.kinds.get(kind)
        if log is None or not log.reviews:
            return None
        days = log.reviews[-7:]
        hours = sum(review.all.hours for review in days)
        return Score(
            hours=hours,
            mae=sum(review.all.mae * review.all.hours for review in days) / hours,
            under_share=sum(review.all.under_share * review.all.hours for review in days) / hours,
            big_under_share=sum(review.all.big_under_share * review.all.hours for review in days) / hours,
        )

    # ---- storage

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "tz": self.tz,
            "kinds": {
                kind: {
                    "hours": [
                        [
                            start.isoformat(),
                            round(hour.forecast, 3),
                            hour.made_at.isoformat(),
                            None if hour.actual is None else round(hour.actual, 3),
                        ]
                        for start, hour in sorted(log.hours.items())
                    ],
                    "reviews": [review.as_dict() for review in log.reviews],
                }
                for kind, log in self.kinds.items()
            },
        }

    def restore(self, raw: Any) -> None:
        """Take what the store kept; a malformed part is dropped."""
        if not isinstance(raw, dict) or not isinstance(raw.get("kinds"), dict):
            return
        kinds: dict[str, KindLog] = {}
        for kind, body in list(raw["kinds"].items())[:MAX_KINDS]:
            if not isinstance(kind, str) or not isinstance(body, dict):
                continue
            log = KindLog()
            for row in body.get("hours") or []:
                parsed = _hour_from_row(row)
                if parsed is not None:
                    log.hours[parsed[0]] = parsed[1]
            for item in body.get("reviews") or []:
                review = DayReview.from_dict(item)
                if review is not None and all(kept.day != review.day for kept in log.reviews):
                    log.reviews.append(review)
            log.reviews.sort(key=lambda review: review.day)
            log.hours = dict(sorted(log.hours.items())[-MAX_HOURS:])
            log.reviews = log.reviews[-KEEP_REVIEW_DAYS:]
            kinds[kind] = log
        self.kinds = kinds

    def diagnostics(self) -> dict[str, Any]:
        return {
            **self.as_dict(),
            "last_week": {
                kind: None if (week := self.last_week(kind)) is None else week.as_dict() for kind in self.kinds
            },
        }


def _hour_from_row(raw: Any) -> tuple[datetime, LoggedHour] | None:
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    start, made_at = _instant(raw[0]), _instant(raw[2])
    forecast, actual = raw[1], raw[3]
    if start is None or made_at is None or not _number(forecast):
        return None
    if actual is not None and not _number(actual):
        return None
    return start, LoggedHour(float(forecast), made_at, None if actual is None else float(actual))
