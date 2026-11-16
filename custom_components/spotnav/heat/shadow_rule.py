"""The heat card's day band in shadow (plan `plans/v3_heat_card.md`): what SpotNav would aim the house at, hour by
hour, from a simple rule, until the joint plan takes heat in (plan `plans/v3_heating_plan.md` steps 3 and 7).
Nothing is written anywhere: the band is shown and logged, and reviewed later.

Pure. Per local hour of the day, one action and the temperature it aims at:

* `away` (the target less 3 °C, never below 16 °C) until `return` the `RETURN_HOURS` before the return time (the
  target again, so the house is warm when the household is back);
* `night` (the target less the cooler night's degrees) in the cooler night's hours, when it is on;
* `lower` (the comfort band's low edge, the target less 0.5 °C) in the day's dearest hours: price at or above the
  day's `DEAR_QUANTILE` and at least `DEAR_OVER_MEDIAN` × its median, at most `MAX_LOWERED_RUN` hours in a row with
  an hour back to normal after, at most `MAX_LOWERED_DAY` a day (plan section 5's fixed guards), dearest first;
* `preheat` (the band's high edge, the target plus 0.5 °C) the hour before a lowered run, when that hour is at most
  `PREHEAT_SHARE` of the run's mean price (the heat bought there is worth carrying);
* `normal` (the target) otherwise.

Guards that keep the house at `normal` instead of `lower` (and say why): no prices (`no_prices`), outdoors at or
below `NO_LOWER_BELOW_C` (`too_cold`), the coldest room already below the target less `ROOM_COLD_C`
(`room_cold`, for the running hour on). The night and away keep their own aims under the guards: they are what the
household asked for, not a saving SpotNav chose.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from .config import BAND_C, HeatConfig, away_target

DEAR_QUANTILE: Final = 0.75
DEAR_OVER_MEDIAN: Final = 1.15
MAX_LOWERED_RUN: Final = 3
MAX_LOWERED_DAY: Final = 6
PREHEAT_SHARE: Final = 0.85
RETURN_HOURS: Final = 2
NO_LOWER_BELOW_C: Final = -15.0
ROOM_COLD_C: Final = 1.0

ACTION_NORMAL: Final = "normal"
ACTION_LOWER: Final = "lower"
ACTION_PREHEAT: Final = "preheat"
ACTION_NIGHT: Final = "night"
ACTION_AWAY: Final = "away"
ACTION_RETURN: Final = "return"

REASON_DEAR: Final = "dear_hour"
REASON_CHEAP_BEFORE: Final = "cheap_before_dear"
REASON_NIGHT: Final = "night"
REASON_AWAY: Final = "away"
REASON_RETURN: Final = "return"
REASON_NORMAL: Final = "normal"
REASON_NO_PRICES: Final = "no_prices"
REASON_TOO_COLD: Final = "too_cold"
REASON_ROOM_COLD: Final = "room_cold"


@dataclass(frozen=True, slots=True)
class BandHour:
    start: datetime
    action: str
    target_c: float
    reason: str
    price: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "action": self.action,
            "target_c": self.target_c,
            "reason": self.reason,
            "price": None if self.price is None else round(self.price, 2),
        }


@dataclass(frozen=True, slots=True)
class WouldDo:
    """The running hour's action, until when it lasts and why."""

    action: str
    target_c: float
    until: datetime | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "target_c": self.target_c,
            "until": None if self.until is None else self.until.isoformat(),
            "reason": self.reason,
        }


def day_hours(day_start: datetime, zone: ZoneInfo) -> list[datetime]:
    """The local day's hour starts (UTC-aware, 23 to 25 of them)."""
    local = day_start.astimezone(zone)
    midnight = datetime(local.year, local.month, local.day, tzinfo=zone)
    following = midnight.date() + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=zone)
    hours: list[datetime] = []
    moment = midnight.astimezone(ZoneInfo("UTC"))
    while moment < end:
        hours.append(moment)
        moment += timedelta(hours=1)
    return hours


def _quantile(values: Sequence[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * share))]


def dear_hours(prices: Mapping[datetime, float], blocked: set[datetime]) -> set[datetime]:
    """The hours the rule lowers in (see the module), never one in `blocked`."""
    if len(prices) < 6:
        return set()
    values = list(prices.values())
    median = statistics.median(values)
    bar = max(_quantile(values, DEAR_QUANTILE), median * DEAR_OVER_MEDIAN if median > 0 else median)
    starts = sorted(prices)
    index = {start: i for i, start in enumerate(starts)}
    chosen: set[datetime] = set()
    for start in sorted((s for s in starts if prices[s] >= bar and s not in blocked), key=lambda s: (-prices[s], s)):
        if len(chosen) >= MAX_LOWERED_DAY:
            break
        trial = chosen | {start}
        if _longest_run(trial, starts, index) > MAX_LOWERED_RUN:
            continue
        chosen = trial
    return chosen


def _runs(chosen: set[datetime], starts: list[datetime]) -> list[list[datetime]]:
    runs: list[list[datetime]] = []
    for start in starts:
        if start in chosen:
            if runs and runs[-1][-1] + timedelta(hours=1) == start:
                runs[-1].append(start)
            else:
                runs.append([start])
    return runs


def _longest_run(chosen: set[datetime], starts: list[datetime], index: Mapping[datetime, int]) -> int:
    """The longest run of hours in a row among `chosen` (a run ends at the first normal hour)."""
    longest = current = 0
    previous: int | None = None
    for position in sorted(index[start] for start in chosen):
        current = current + 1 if previous is not None and position == previous + 1 else 1
        longest = max(longest, current)
        previous = position
    return longest


def band(
    config: HeatConfig,
    hours: Sequence[datetime],
    prices: Mapping[datetime, float],
    now: datetime,
    zone: ZoneInfo,
    *,
    outdoor_c: float | None = None,
    coldest_c: float | None = None,
) -> tuple[list[BandHour], str | None]:
    """The day's band (see the module) and the guard that stood lowering down (`None` when none did)."""
    target = config.target_c
    away_end = config.away_end()
    current = now.replace(minute=0, second=0, microsecond=0)
    fixed: dict[datetime, tuple[str, float, str]] = {}
    for start in hours:
        local = start.astimezone(zone)
        if away_end is not None and start < away_end:
            if away_end - start <= timedelta(hours=RETURN_HOURS):
                fixed[start] = (ACTION_RETURN, target, REASON_RETURN)
            else:
                fixed[start] = (ACTION_AWAY, away_target(target), REASON_AWAY)
        elif config.night.on and config.night.covers(local.hour * 60 + local.minute):
            fixed[start] = (ACTION_NIGHT, target - config.night.delta_c, REASON_NIGHT)

    day_prices = {start: prices[start] for start in hours if start in prices}
    guard: str | None = None
    if len(day_prices) < len(hours) // 2:
        guard = REASON_NO_PRICES
    elif outdoor_c is not None and outdoor_c <= NO_LOWER_BELOW_C:
        guard = REASON_TOO_COLD
    blocked = set(fixed)
    if guard is None and coldest_c is not None and coldest_c < target - ROOM_COLD_C:
        guard = REASON_ROOM_COLD
        blocked |= {start for start in hours if start >= current}
    lowered = set() if guard in (REASON_NO_PRICES, REASON_TOO_COLD) else dear_hours(day_prices, blocked)

    preheat: set[datetime] = set()
    starts = sorted(hours)
    for run in _runs(lowered, starts):
        before = run[0] - timedelta(hours=1)
        mean = statistics.mean(day_prices[start] for start in run)
        if before in day_prices and before not in fixed and before not in lowered and day_prices[before] <= mean * PREHEAT_SHARE:
            preheat.add(before)

    out: list[BandHour] = []
    for start in starts:
        price = day_prices.get(start)
        if start in fixed:
            action, aim, reason = fixed[start]
        elif start in lowered:
            action, aim, reason = ACTION_LOWER, target - BAND_C, REASON_DEAR
        elif start in preheat:
            action, aim, reason = ACTION_PREHEAT, target + BAND_C, REASON_CHEAP_BEFORE
        else:
            action, aim, reason = ACTION_NORMAL, target, guard if guard is not None and start >= current else REASON_NORMAL
        out.append(BandHour(start, action, aim, reason, price))
    return out, guard


def would_do(hours: Sequence[BandHour], now: datetime) -> WouldDo | None:
    """The running hour's action and until when the same action lasts (`None` past the band's end)."""
    ordered = sorted(hours, key=lambda hour: hour.start)
    for index, hour in enumerate(ordered):
        if hour.start <= now < hour.start + timedelta(hours=1):
            until = hour.start + timedelta(hours=1)
            for later in ordered[index + 1 :]:
                if later.action != hour.action:
                    break
                until = later.start + timedelta(hours=1)
            last = ordered[-1].start + timedelta(hours=1)
            return WouldDo(hour.action, hour.target_c, None if until >= last and hour.action == ACTION_NORMAL else until, hour.reason)
    return None
