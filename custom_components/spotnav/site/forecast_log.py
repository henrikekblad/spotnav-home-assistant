"""The forecast log and its nightly review (SpotNav 3.0): how well each forecast hit, kept short and bounded.

Pure: the clock is passed in. One log per site, any number of *kinds* (today `house_kw`, the house load forecast,
and `house_kw_raw`, the same before its in-day adjustment; later the sun's and the battery plan's), each kept the
same way:

* **The first forecast** made for each hour is logged (`record_forecast`), with when it was made, so its lead is
  known. A later forecast for the same hour never replaces it: the review scores what was known first. Only hours
  that start after the forecast was made are logged (the running hour is partly known already).
* **The outcome** is added when the hour has completed (`record_actual`). An hour without one stays unscored.
* **The review** (`review`): every local day whose hours have all ended and that has at least one scored hour is
  reviewed once, the night after: its mean absolute error, how often the forecast was below the outcome
  (`under_share`, what makes a plan too optimistic) and by more than `BIG_UNDER` (`big_under_share`), over the whole
  day and in the charging hours (22-07 local) and the evening (16-20 local), each with its hour count.
* **Bounds**: hours are kept `KEEP_HOURS_DAYS` days, at most `MAX_HOURS` per kind; reviews `KEEP_REVIEW_DAYS` days, at
  most `KEEP_REVIEW_DAYS` per kind; at most `MAX_KINDS` kinds. Storage is untrusted: a malformed record is dropped.

It decides nothing: it is shown in the diagnostics and the debug bundle (later the house card).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Final

from .. import time_zones

KIND_HOUSE: Final = "house_kw"
#: The same forecast before its in-day adjustment, logged beside it so the review shows what the adjustment earns.
KIND_HOUSE_RAW: Final = "house_kw_raw"

#: The hours of the last this many days are kept (the outcome is compared once the hour has completed).
KEEP_HOURS_DAYS: Final = 14
#: A hard cap per kind beside the age bound: 14 days and the 48 hours ahead, with room for DST.
MAX_HOURS: Final = (KEEP_HOURS_DAYS + 3) * 24
#: The daily reviews of the last this many days are kept.
KEEP_REVIEW_DAYS: Final = 30
#: At most this many kinds are kept (a store that grew kinds by mistake stays small).
MAX_KINDS: Final = 8
#: An under-forecast by more than this (in the kind's unit) is a large one.
BIG_UNDER: Final = 0.5
STORE_VERSION: Final = 1

#: The local hours of the charging night and of the evening, as the backtest reports them.
NIGHT_HOURS: Final = frozenset((22, 23, 0, 1, 2, 3, 4, 5, 6))
EVENING_HOURS: Final = frozenset((16, 17, 18, 19))


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
    """One hour's first forecast, when it was made, and the outcome once known."""

    forecast: float
    made_at: datetime
    actual: float | None = None

    def as_row(self, start: datetime) -> list[Any]:
        return [
            start.isoformat(),
            round(self.forecast, 3),
            self.made_at.isoformat(),
            None if self.actual is None else round(self.actual, 3),
        ]


@dataclass(frozen=True, slots=True)
class Score:
    """How well the forecast hit over some hours."""

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


@dataclass(frozen=True, slots=True)
class DayReview:
    """One local day's review: over the whole day, the charging night (22-07) and the evening (16-20)."""

    day: date
    reviewed_at: datetime
    all: Score
    night: Score | None
    evening: Score | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "reviewed_at": self.reviewed_at.isoformat(),
            "all": self.all.as_dict(),
            "night": None if self.night is None else self.night.as_dict(),
            "evening": None if self.evening is None else self.evening.as_dict(),
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
        return cls(day, reviewed_at, whole, _score_from(raw.get("night")), _score_from(raw.get("evening")))


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


@dataclass(slots=True)
class KindLog:
    """One kind's logged hours (by start, UTC) and its daily reviews (oldest first)."""

    hours: dict[datetime, LoggedHour] = field(default_factory=dict)
    reviews: list[DayReview] = field(default_factory=list)


@dataclass(slots=True)
class ForecastLog:
    """The site's forecast log (see the module), on the clock of `tz`."""

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

    def review(self, now: datetime) -> list[tuple[str, DayReview]]:
        """Review every local day that has ended, has a scored hour and has no review yet (see the module); the new
        reviews. Old hours and reviews are dropped."""
        zone = time_zones.require_zone(self.tz)
        now = now.astimezone(timezone.utc)
        today = now.astimezone(zone).date()
        made: list[tuple[str, DayReview]] = []
        for kind, log in self.kinds.items():
            reviewed = {review.day for review in log.reviews}
            by_day: dict[date, list[tuple[int, float, float]]] = {}
            for start, hour in log.hours.items():
                if hour.actual is None:
                    continue
                local = start.astimezone(zone)
                if local.date() >= today:
                    continue
                by_day.setdefault(local.date(), []).append((local.hour, hour.forecast, hour.actual))
            for day in sorted(by_day):
                if day in reviewed:
                    continue
                rows = by_day[day]
                whole = score([(forecast, actual) for _hour, forecast, actual in rows])
                if whole is None:
                    continue
                review = DayReview(
                    day=day,
                    reviewed_at=now,
                    all=whole,
                    night=score([(f, a) for hour, f, a in rows if hour in NIGHT_HOURS]),
                    evening=score([(f, a) for hour, f, a in rows if hour in EVENING_HOURS]),
                )
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
        """The last seven reviewed days' scores combined, weighted by their hours (the house card's line later)."""
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
                    "hours": [hour.as_row(start) for start, hour in sorted(log.hours.items())],
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
        """The log as kept, and per kind the last week's combined score."""
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
