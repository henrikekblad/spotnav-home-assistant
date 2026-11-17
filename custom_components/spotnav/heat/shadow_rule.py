"""SpotNav's own heat plan in shadow, hour by hour (plans `plans/v3_heat_card.md`, `plans/v3_heating_plan.md` steps 2
and 3): what SpotNav would aim the house at, in the joint plan with the cars, the battery and the import cap. Nothing
is written anywhere: the plan is shown, logged, kept per hour and reviewed beside what the relay really did.

Pure. Per local hour of the day, one action and the temperature it aims at:

* `away` (the target less 3 °C, never below 16 °C) until `return` the `RETURN_HOURS` before the return time (the
  target again, so the house is warm when the household is back);
* `night` (the target less the cooler night's degrees) in the cooler night's hours, when it is on;
* `lower` (the comfort band's low edge, the target less 0.5 °C) in the hours worth lowering, at most
  `MAX_LOWERED_RUN` hours in a row with at least an hour back to normal between, at most `MAX_LOWERED_DAY` a day
  (plan section 5's fixed guards). Worth lowering: a **tight** hour first (`REASON_TIGHT`: the joint plan's load,
  the house, the cars' plans and the battery's grid charge, reaches the import cap, the main fuse less its margin
  or the import target's held level, within what lowering the heat frees, so lowering makes room; only in the
  day's dearer half, since in a cheap hour the heat would come back dearer after it), then the day's
  **dearest** hours (`REASON_DEAR`: price at or above the day's `DEAR_QUANTILE` and at least `DEAR_OVER_MEDIAN` x its
  median), dearest first;
  Also **too warm indoors** (`REASON_ROOM_WARM`, whatever the price): while the house is above its comfort band
  (`RoomWarm`: the coldest counted room, its offset counted, above the band's high edge for `ROOM_WARM_AFTER`, until
  it is back `ROOM_WARM_BACK_C` inside it), the running hour and the next (`ROOM_WARM_HOURS`, made again as it
  changes); these hours count in the day's `MAX_LOWERED_DAY` and runs like the others, so an hour both dear and
  warm is one lowered hour (it keeps its price reason);
* `preheat` (the band's high edge, the target plus 0.5 °C) the hour before a lowered run, when that hour is at most
  `PREHEAT_SHARE` of the run's mean price (the heat bought there is worth carrying); never before a run with a warm
  hour;
* `normal` (the target) otherwise.

Guards that keep the house at `normal` instead of `lower` (and say why): no prices (`no_prices`), outdoors at or
below `NO_LOWER_BELOW_C` now (`too_cold`, the whole day) or forecast for the hour (that hour), the coldest counted room
already below the comfort band (the target less the band, `room_cold`, for the running hour and the next
`ROOM_COLD_HOURS - 1`: the plan is made again every hour on the reading then, so one cold evening reading does not
decide the rest of the day). The night and away
keep their own aims under the guards: they are what the household asked for, not a saving SpotNav chose.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
#: The coldest counted room below the target less this: no lowering (the comfort band's low edge).
ROOM_COLD_C: Final = BAND_C
ROOM_COLD_HOURS: Final = 2
#: The house above the band's high edge this long: lowered whatever the price (`RoomWarm`).
ROOM_WARM_AFTER: Final = timedelta(hours=1)
#: How far back inside the band the house must come before the warm lowering ends (no flapping at the edge).
ROOM_WARM_BACK_C: Final = 0.2
#: The hours lowered from the running one on while the house is too warm (the plan is made again as it changes).
ROOM_WARM_HOURS: Final = 2

ACTION_NORMAL: Final = "normal"
ACTION_LOWER: Final = "lower"
ACTION_PREHEAT: Final = "preheat"
ACTION_NIGHT: Final = "night"
ACTION_AWAY: Final = "away"
ACTION_RETURN: Final = "return"

REASON_DEAR: Final = "dear_hour"
REASON_TIGHT: Final = "tight_hour"
REASON_CHEAP_BEFORE: Final = "cheap_before_dear"
REASON_NIGHT: Final = "night"
REASON_AWAY: Final = "away"
REASON_RETURN: Final = "return"
REASON_NORMAL: Final = "normal"
REASON_NO_PRICES: Final = "no_prices"
REASON_TOO_COLD: Final = "too_cold"
REASON_ROOM_COLD: Final = "room_cold"
REASON_ROOM_WARM: Final = "room_warm"


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


@dataclass(slots=True)
class RoomWarm:
    """Whether the house is too warm (see the module), stepped on each look with the coldest counted room less its
    offset (`None`: no trustworthy reading, which ends it) and the band's high edge."""

    #: Since when the reading has been above the high edge (`None` while it is not).
    since: datetime | None = None
    active: bool = False

    def step(self, now: datetime, value_c: float | None, high_c: float) -> bool:
        if value_c is None:
            self.since, self.active = None, False
        elif self.active:
            if value_c <= high_c - ROOM_WARM_BACK_C:
                self.since, self.active = None, False
        elif value_c > high_c:
            if self.since is None:
                self.since = now
            self.active = now - self.since >= ROOM_WARM_AFTER
        else:
            self.since = None
        return self.active


def day_hours(day_start: datetime, zone: ZoneInfo) -> list[datetime]:
    """The local day's hour starts (UTC-aware, 23 to 25 of them)."""
    local = day_start.astimezone(zone)
    midnight = datetime(local.year, local.month, local.day, tzinfo=zone)
    following = midnight.date() + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=zone)
    hours: list[datetime] = []
    moment = midnight.astimezone(timezone.utc)
    while moment < end:
        hours.append(moment)
        moment += timedelta(hours=1)
    return hours


def _quantile(values: Sequence[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * share))]


def dear_hours(
    prices: Mapping[datetime, float],
    blocked: set[datetime],
    tight: Iterable[datetime] = (),
    already: Iterable[datetime] = (),
) -> set[datetime]:
    """The hours the rule lowers in (see the module), never one in `blocked`: `already` (lowered for another reason,
    counted in the day's hours and runs), then the tight hours, then the dear ones, dearest first."""
    chosen: set[datetime] = set(already)
    if len(prices) < 6:
        return chosen
    values = list(prices.values())
    median = statistics.median(values)
    bar = max(_quantile(values, DEAR_QUANTILE), median * DEAR_OVER_MEDIAN if median > 0 else median)
    starts = sorted(prices)
    # The runs are counted over the hours `already` lowered too, priced or not (a warm hour without a price).
    index = {start: i for i, start in enumerate(sorted(set(prices) | chosen))}
    # A tight hour in the cheap half of the day is left alone: the heat would come back dearer after it, and the
    # load there is what the cheap hour is for (a battery's grid charge, the car's night).
    pressed = {start for start in tight if start in prices and prices[start] >= median}
    candidates = [s for s in starts if (prices[s] >= bar or s in pressed) and s not in blocked and s not in chosen]
    for start in sorted(candidates, key=lambda s: (s not in pressed, -prices[s], s)):
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
    tight: Iterable[datetime] = (),
    outdoor_by_hour: Mapping[datetime, float] | None = None,
    room_warm: bool = False,
) -> tuple[list[BandHour], str | None]:
    """The day's band (see the module) and the guard that stood lowering down (`None` when none did). `tight` are the
    hours whose joint load reaches the import cap, `outdoor_by_hour` the outdoor forecast (°C, UTC hour starts),
    `room_warm` whether the house is too warm now (`RoomWarm`)."""
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
    cold_hours = {
        start for start in hours if outdoor_by_hour is not None and outdoor_by_hour.get(start, 99.0) <= NO_LOWER_BELOW_C
    }
    blocked |= cold_hours
    if guard is None and coldest_c is not None and coldest_c < target - ROOM_COLD_C:
        guard = REASON_ROOM_COLD
        blocked |= {start for start in hours if current <= start < current + timedelta(hours=ROOM_COLD_HOURS)}
    pressed = frozenset(start for start in tight if start in day_prices and day_prices[start] >= statistics.median(day_prices.values()))
    warm: set[datetime] = set()
    if room_warm and guard not in (REASON_TOO_COLD, REASON_ROOM_COLD):
        # Too warm lowers whatever the price, with prices or without.
        warm = {start for start in hours if current <= start < current + timedelta(hours=ROOM_WARM_HOURS) and start not in blocked}
    if guard == REASON_TOO_COLD:
        lowered: set[datetime] = set()
    elif guard == REASON_NO_PRICES:
        lowered = set(warm)
    else:
        lowered = dear_hours(day_prices, blocked, pressed, warm)
    # A warm hour the prices alone would lower keeps its price reason.
    priced_low = dear_hours(day_prices, blocked, pressed) if warm and guard is None else set()

    preheat: set[datetime] = set()
    starts = sorted(hours)
    for run in _runs(lowered, starts):
        if warm.intersection(run):
            continue
        before = run[0] - timedelta(hours=1)
        run_prices = [day_prices[start] for start in run if start in day_prices]
        if not run_prices:
            continue
        mean = statistics.mean(run_prices)
        if before in day_prices and before not in fixed and before not in lowered and day_prices[before] <= mean * PREHEAT_SHARE:
            preheat.add(before)

    out: list[BandHour] = []
    for start in starts:
        price = day_prices.get(start)
        if start in fixed:
            action, aim, reason = fixed[start]
        elif start in lowered:
            if start in pressed:
                reason = REASON_TIGHT
            elif start in priced_low or start not in warm:
                reason = REASON_DEAR
            else:
                reason = REASON_ROOM_WARM
            action, aim = ACTION_LOWER, target - BAND_C
        elif start in preheat:
            action, aim, reason = ACTION_PREHEAT, target + BAND_C, REASON_CHEAP_BEFORE
        elif start in cold_hours and start >= current:
            action, aim, reason = ACTION_NORMAL, target, REASON_TOO_COLD
        else:
            action, aim, reason = ACTION_NORMAL, target, guard if guard is not None and start >= current else REASON_NORMAL
        out.append(BandHour(start, action, aim, reason, price))
    return out, guard


def would_do(
    hours: Sequence[BandHour], now: datetime, config: HeatConfig | None = None, zone: ZoneInfo | None = None
) -> WouldDo | None:
    """The running hour's action and until when the same action lasts (`None` past the band's end). An action that
    runs to the band's end is followed past it where its end is known: away to the return's warm-up, the cooler night
    to its end time the next morning; a normal day has no end."""
    ordered = sorted(hours, key=lambda hour: hour.start)
    for index, hour in enumerate(ordered):
        if hour.start <= now < hour.start + timedelta(hours=1):
            until = hour.start + timedelta(hours=1)
            for later in ordered[index + 1 :]:
                if later.action != hour.action:
                    break
                until = later.start + timedelta(hours=1)
            last = ordered[-1].start + timedelta(hours=1)
            if until >= last:
                if hour.action == ACTION_NORMAL:
                    return WouldDo(hour.action, hour.target_c, None, hour.reason)
                if config is not None and hour.action == ACTION_AWAY and (away_end := config.away_end()) is not None:
                    until = max(until, away_end - timedelta(hours=RETURN_HOURS))
                elif config is not None and zone is not None and hour.action == ACTION_NIGHT:
                    local = last.astimezone(zone)
                    end_minutes = config.night.minutes()[1]
                    end = local.replace(hour=end_minutes // 60, minute=end_minutes % 60)
                    until = max(until, end.astimezone(timezone.utc))
            return WouldDo(hour.action, hour.target_c, until, hour.reason)
    return None
