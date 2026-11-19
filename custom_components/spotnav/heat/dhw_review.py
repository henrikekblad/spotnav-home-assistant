"""The hot-water plan's day review and the gate to live (plan `plans/v3_dhw_plan.md` section 10). Pure.

**A day's review** (`review_day`), once the day is over: each peak of the day against the pump's own runs (the meter's
runs that are no standby loss and that neither SpotNav nor the Boost button started):

* **hit**: a draw (a run's start less `DRAW_LEAD`) within `HIT_WINDOW` of the peak (`ready_by` to `until`);
* the **pump's run** a pick would have replaced: the first such run starting between the pick's start and the peak's
  `until` plus `MISS_AFTER`;
* **saving**: per replaced run, its kWh at the price where it ran less the same kWh at the pick's price and the
  tank's standby loss from the pick to the peak;
* **miss** (a live day): a reload run starting within `MISS_AFTER` after a peak that had a live boost: the boost
  came too small or too early.

**The gate** (`gate`), per level (`GATES`, one place: a sub-function may get a longer gate): the shadow days since
the gate last closed must number at least `min_days` and, where `all_weekdays`, hold each weekday once; at least
`min_peaks` peaks seen, `min_hit` of them hit, and a saving above zero. **Back off** (`back_off`): `MISS_LIMIT`
misses within `MISS_DAYS` live days close the gate again (back to shadow).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Final

from .dhw_peaks import DRAW_LEAD, RunLike, standby
from .dhw_plan import LOSS_KWH_H, Pick, quarter_of

HIT_WINDOW: Final = timedelta(minutes=45)
MISS_AFTER: Final = timedelta(minutes=90)
MISS_LIMIT: Final = 3
MISS_DAYS: Final = 7

MODE_SHADOW: Final = "shadow"
MODE_LIVE: Final = "live"

LEVEL_A: Final = "A"
LEVEL_B: Final = "B"


@dataclass(frozen=True, slots=True)
class GateRule:
    min_days: int
    all_weekdays: bool
    min_peaks: int
    min_hit: float


#: Level B (the boost) goes live after a week of shadow with each weekday seen; level A (blocking the pump's own
#: runs) touches comfort more and waits two weeks.
GATES: Final[Mapping[str, GateRule]] = {
    LEVEL_B: GateRule(min_days=7, all_weekdays=True, min_peaks=10, min_hit=0.70),
    LEVEL_A: GateRule(min_days=14, all_weekdays=False, min_peaks=10, min_hit=0.70),
}


@dataclass(frozen=True, slots=True)
class DayPeak:
    ready: datetime
    until: datetime


def review_day(
    day: date,
    zone: tzinfo,
    peaks: Sequence[DayPeak],
    picks: Sequence[Pick],
    runs: Sequence[RunLike],
    price: Callable[[datetime], float | None],
    *,
    mode: str,
    live_peaks: frozenset[datetime] = frozenset(),
) -> dict[str, Any]:
    """The day's review (see the module): `runs` are the pump's own runs (SpotNav's and the button's left out),
    `price(quarter start)` the buy price, `live_peaks` the peaks a live boost filled."""
    own = sorted((run for run in runs if not standby(run)), key=lambda run: run.start)
    per_peak = []
    hits = misses = 0
    would = actual = saved = moved = 0.0
    for peak in sorted(peaks, key=lambda item: item.ready):
        hit = any(peak.ready - HIT_WINDOW <= run.start - DRAW_LEAD <= peak.until + HIT_WINDOW for run in own)
        hits += hit
        pick = next((item for item in picks if item.peak == peak.ready), None)
        pump_run = None
        if pick is not None:
            pump_run = next((run for run in own if pick.start <= run.start <= peak.until + MISS_AFTER), None)
            ran_at = None if pump_run is None else price(quarter_of(pump_run.start))
            if pump_run is not None and ran_at is not None:
                loss = LOSS_KWH_H * max(0.0, (peak.ready - pick.end).total_seconds() / 3600.0) * pick.price
                cost_then = pump_run.kwh * ran_at
                cost_pick = pump_run.kwh * pick.price + loss
                actual += cost_then
                would += cost_pick
                saved += cost_then - cost_pick
                moved += pump_run.kwh
        miss = peak.ready in live_peaks and any(peak.ready <= run.start <= peak.ready + MISS_AFTER for run in own)
        misses += miss
        per_peak.append(
            {
                "peak": peak.ready.astimezone(zone).strftime("%H:%M"),
                "hit": hit,
                "pick": None if pick is None else pick.start.isoformat(),
                "pump_run": None if pump_run is None else pump_run.start.isoformat(),
                "miss": miss,
            }
        )
    return {
        "date": day.isoformat(),
        "mode": mode,
        "would_cost": round(would, 2),
        "actual_cost": round(actual, 2),
        "saved": round(saved, 2),
        "moved_kwh": round(moved, 3),
        "hits": hits,
        "peaks": len(per_peak),
        "misses": misses,
        "per_peak": per_peak,
    }


def gate(reviews: Sequence[Mapping[str, Any]], level: str = LEVEL_B) -> dict[str, Any]:
    """Whether the level may go live on the shadow days' reviews since the gate last closed (see the module)."""
    rule = GATES[level]
    shadow = [review for review in reviews if review.get("mode") == MODE_SHADOW]
    weekdays = {date.fromisoformat(review["date"]).weekday() for review in shadow}
    peaks = sum(int(review.get("peaks", 0)) for review in shadow)
    hits = sum(int(review.get("hits", 0)) for review in shadow)
    saved = sum(float(review.get("saved", 0.0)) for review in shadow)
    days_ok = len(shadow) >= rule.min_days and (not rule.all_weekdays or len(weekdays) == 7)
    open_ = days_ok and peaks >= rule.min_peaks and hits >= rule.min_hit * peaks and saved > 0
    return {
        "level": level,
        "days": len(shadow),
        "days_needed": rule.min_days,
        "weekdays": len(weekdays),
        "peaks": peaks,
        "peaks_needed": rule.min_peaks,
        "hits": hits,
        "hit_needed": rule.min_hit,
        "saved": round(saved, 2),
        "open": open_,
    }


def back_off(reviews: Sequence[Mapping[str, Any]], today: date) -> bool:
    """Whether the live days' misses of the last `MISS_DAYS` reach `MISS_LIMIT`."""
    first = today - timedelta(days=MISS_DAYS)
    recent = [
        review
        for review in reviews
        if review.get("mode") == MODE_LIVE and first <= date.fromisoformat(review["date"]) < today
    ]
    return sum(int(review.get("misses", 0)) for review in recent) >= MISS_LIMIT


__all__ = [
    "GATES",
    "HIT_WINDOW",
    "LEVEL_A",
    "LEVEL_B",
    "MISS_AFTER",
    "MODE_LIVE",
    "MODE_SHADOW",
    "DayPeak",
    "GateRule",
    "back_off",
    "gate",
    "review_day",
]
