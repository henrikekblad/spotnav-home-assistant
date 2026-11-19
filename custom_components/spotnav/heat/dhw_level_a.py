"""Level A of the hot water (plan `plans/v3_dhw_plan.md` sections 4, 5.2, 5.3 and 10): the heat plan's blocks made
aware of the hot water on a path that blocks the compressor (`paths.blocks_hot_water`: a relay, SG Ready, the SG Ready
API), where a block also holds the pump's own hot-water run. Pure.

**The plan's rules** (they shape which hours the heat plan lowers; shown in shadow, applied live behind "Styr värmen"
and the level-A gate, `dhw_review.GATES`):

* **Before a peak** (`guards`): no lowered hour in the `PEAK_GUARD` before each learned peak's `ready_by`, so the
  pump's own run that fills the tank for it is never held into the peak. Not where the pump already ran since the
  previous peak's end (`until`): the tank was filled after the last use, so a block there delays nothing the peak
  needs; nor where a planned boost (level B) fills the peak.
* **A day's blocked time** (`max_day_hours`): without a tank sensor nothing shows how the tank stands, so a local day
  lowers at most `MAX_DAY_NO_TANK` hours (the heat plan's own cap is `shadow_rule.MAX_LOWERED_DAY`): the pump gets its
  reheats between the blocks. With a tank sensor the tank rule stands in for it.

* **The pump's periodic increase** (`next_hygiene`, the legionella run NIBE calls "periodisk höjning"): never
  blocked where the meter shows it. It is told from the runs alone: a run at least `HYGIENE_SIZE` times the site's
  median run (energy and length), at night (`HYGIENE_NIGHT`), seen three times at the same time of day
  (`HYGIENE_SAME_TIME`) at equal intervals of a whole number of days (`HYGIENE_PERIODS`, 2 to 30). Its next one (the
  last plus that period) is kept free of blocks from `HYGIENE_BEFORE` before its start to `HYGIENE_AFTER` after its
  usual length. Until it has been seen three times (and
  where the pump runs it at varying times) it is not known, and a block may delay it by up to one block, as any run.

**Back-off** (`dhw_review.back_off`, three cold risks within a week of steering): never back to the plan without
these rules, which protects less. The rules widen instead (`PEAK_GUARD_STRICT`, the day's cap one hour less) until
`CLEAN_DAYS` reviewed days in a row without a cold risk; meanwhile the gate counts no day.

A lowered hour the rules take out is shown as cut (`cuts`): the price rule then lowers the next dearest hour it may,
so a block may move rather than go.

**The rules that only end or hold off a block** are fixed and apply whenever SpotNav steers (`heat/control.py`): no
block starts while the pump makes hot water, a held block ends at the next look once the pump is seen making hot
water under it (a run the closing caught as it began), and with a tank sensor the tank rule (`TankGuard`): below
`TANK_LOW_C` no block starts and a held one ends, until it reads `TANK_BACK_C` again. With the heat plan's own rest
after each block (`relay_control.MIN_NORMAL`), a run is delayed by at most one block.

**The review** (`cold_risk`): a peak whose pump run came late because of a block, the cold-shower risk: a block over
any part of the `PEAK_GUARD` before `ready_by`, no run between the previous peak's end and the block, and the first
run after the block began within `DELAYED_WITHIN` of its end and not done by `ready_by`.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Final

from .dhw_peaks import Peak, RunLike, peak_instant, standby, standby_kwh
from .shadow_rule import ACTION_LOWER, MAX_LOWERED_DAY, BandHour

PEAK_GUARD: Final = timedelta(hours=2)
#: After a back-off (cold risks in a week): the wider guard and one lowered hour less a day, until `CLEAN_DAYS`.
PEAK_GUARD_STRICT: Final = timedelta(hours=3)
CLEAN_DAYS: Final = 7
#: The hours a local day lowers at most on a path that blocks hot water and no tank sensor.
MAX_DAY_NO_TANK: Final = 4
TANK_LOW_C: Final = 40.0
TANK_BACK_C: Final = 45.0
#: A run that starts this soon after a block's end was held by it.
DELAYED_WITHIN: Final = timedelta(minutes=30)
HOUR: Final = timedelta(hours=1)

#: The periodic increase: this many times the median run's energy and length.
HYGIENE_SIZE: Final = 2.0
#: Local hours a periodic increase starts in (from, to): the night.
HYGIENE_NIGHT: Final = (22, 7)
HYGIENE_SAME_TIME: Final = timedelta(minutes=60)
#: The periods in whole days told (NIBE lets the installer set 1-90 days, 14 by default).
HYGIENE_PERIODS: Final = range(2, 31)
#: How long the night's long runs are kept to tell the period: three of the longest period, and a margin.
HYGIENE_KEEP: Final = timedelta(days=64)
HYGIENE_BEFORE: Final = timedelta(hours=1)
HYGIENE_AFTER: Final = timedelta(minutes=30)

CUT_PEAK: Final = "peak"
CUT_DAY: Final = "day_cap"
CUT_HYGIENE: Final = "hygiene"


@dataclass(frozen=True, slots=True)
class Guard:
    """The time before a peak kept free of blocks: `[start, ready)`."""

    start: datetime
    ready: datetime


@dataclass(frozen=True, slots=True)
class Cut:
    """A lowered hour the rules took out of the heat plan: before a peak (`ready`) or over the day's cap."""

    start: datetime
    kind: str
    ready: datetime | None

    def as_dict(self, zone: tzinfo, live: bool) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": (self.start + HOUR).isoformat(),
            "reason": self.kind,
            "peak": None if self.ready is None else self.ready.astimezone(zone).strftime("%H:%M"),
            "live": live,
        }

    def stored(self) -> list[Any]:
        return [self.kind, None if self.ready is None else self.ready.isoformat()]


def _instants(peaks: Sequence[Peak], days: Iterable[date], zone: tzinfo) -> list[tuple[datetime, datetime]]:
    """Each peak's `(ready, until)` on the days given and the day before the first, in time order."""
    wanted = sorted(set(days))
    if not wanted:
        return []
    out = []
    for day in [wanted[0] - timedelta(days=1), *wanted]:
        for peak in peaks:
            ready, until, _run = peak_instant(peak, day, zone)
            out.append((ready, until))
    return sorted(out)


def served(previous_until: datetime | None, ready: datetime, runs: Sequence[RunLike], now: datetime) -> bool:
    """Whether the pump already ran since the previous peak's end (and before `ready`), by `now`: the tank was filled
    after its last use."""
    if previous_until is None or previous_until > now:
        return False
    return any(previous_until <= run.start < ready and run.end <= now for run in runs)


def guards(
    peaks: Sequence[Peak],
    days: Iterable[date],
    zone: tzinfo,
    runs: Sequence[RunLike],
    now: datetime,
    boosted: Iterable[datetime] = (),
    guard: timedelta = PEAK_GUARD,
) -> list[Guard]:
    """The times kept free of blocks on the days given (see the module): one per peak, none for a peak already
    served by a run or filled by a planned boost (`boosted`: their `ready`), none that has passed."""
    wanted = set(days)
    filled = set(boosted)
    instants = _instants(peaks, wanted, zone)
    out = []
    for index, (ready, _until) in enumerate(instants):
        if ready.astimezone(zone).date() not in wanted or ready <= now or ready in filled:
            continue
        previous = instants[index - 1][1] if index > 0 else None
        if served(previous, ready, runs, now):
            continue
        out.append(Guard(ready - guard, ready))
    return out


def guard_hours(items: Sequence[Guard]) -> dict[datetime, datetime]:
    """The plan's hours (UTC hour starts) that touch a guard, each to the peak it keeps (`ready`)."""
    out: dict[datetime, datetime] = {}
    for guard in items:
        hour = guard.start.replace(minute=0, second=0, microsecond=0)
        while hour < guard.ready:
            out.setdefault(hour, guard.ready)
            hour += HOUR
    return out


def max_day_hours(tank_sensor: bool, strict: bool = False) -> int | None:
    """The hours a local day may lower at most by the hot water's rule, `None` where the tank rule stands in; after a
    back-off (`strict`) one hour less, also with a tank sensor (the heat plan's own cap less one)."""
    if not strict:
        return None if tank_sensor else MAX_DAY_NO_TANK
    return (MAX_LOWERED_DAY if tank_sensor else MAX_DAY_NO_TANK) - 1


def is_night(moment: datetime, zone: tzinfo) -> bool:
    hour = moment.astimezone(zone).hour
    first, last = HYGIENE_NIGHT
    return hour >= first or hour < last


def _minute_gap(a: datetime, b: datetime, zone: tzinfo) -> timedelta:
    left, right = a.astimezone(zone), b.astimezone(zone)
    minutes = abs((left.hour * 60 + left.minute) - (right.hour * 60 + right.minute))
    return timedelta(minutes=min(minutes, 24 * 60 - minutes))


def long_night(run: RunLike, zone: tzinfo, kwh: float, seconds: float) -> bool:
    """Whether a run looks like the periodic increase against the site's median run (`kwh`, `seconds`)."""
    return (
        run.kwh >= HYGIENE_SIZE * kwh
        and (run.end - run.start).total_seconds() >= HYGIENE_SIZE * seconds
        and is_night(run.start, zone)
    )


def next_hygiene(
    runs: Sequence[RunLike], zone: tzinfo, now: datetime, candidates: Sequence[RunLike] | None = None
) -> tuple[datetime, datetime] | None:
    """The pump's next periodic increase as the meter has shown it (see the module): `(start, end)` of the time kept
    free, `None` where it is not known. `runs` give the site's median run; `candidates` (the night's long runs kept
    for `HYGIENE_KEEP`, else `runs`) are searched for the last increase and two before it at equal intervals of a whole
    number of days (`HYGIENE_PERIODS`)."""
    real = [run for run in runs if not standby(run, standby_kwh(runs)) and run.end > run.start]
    if len(real) < 3:
        return None
    kwh = statistics.median(run.kwh for run in real)
    seconds = statistics.median((run.end - run.start).total_seconds() for run in real)
    pool = real if candidates is None else [run for run in candidates if run.end > run.start]
    long = sorted((run for run in pool if long_night(run, zone, kwh, seconds)), key=lambda run: run.start)
    if len(long) < 3:
        return None
    last = long[-1]
    last_day = last.start.astimezone(zone).date()

    def seen(day: date) -> bool:
        # By the local clock: the pump keeps its start time over a change to or from summer time.
        return any(
            run.start.astimezone(zone).date() == day and _minute_gap(run.start, last.start, zone) <= HYGIENE_SAME_TIME
            for run in long[:-1]
        )

    for days in HYGIENE_PERIODS:
        period = timedelta(days=days)
        if seen(last_day - period) and seen(last_day - 2 * period):
            span = last.end - last.start
            clock = last.start.astimezone(zone).timetz().replace(tzinfo=None)
            day = last_day + period
            start = datetime.combine(day, clock, tzinfo=zone)
            while start + span + HYGIENE_AFTER <= now:
                day += period
                start = datetime.combine(day, clock, tzinfo=zone)
            return start - HYGIENE_BEFORE, start + span + HYGIENE_AFTER
    return None


def kept_hours(
    items: Sequence[Guard], hygiene: tuple[datetime, datetime] | None = None
) -> dict[datetime, tuple[str, datetime | None]]:
    """The plan's hours (UTC hour starts) kept free of blocks, each with why: before a peak (`ready`), or the pump's
    periodic increase."""
    out: dict[datetime, tuple[str, datetime | None]] = {hour: (CUT_PEAK, ready) for hour, ready in guard_hours(items).items()}
    if hygiene is not None:
        hour = hygiene[0].replace(minute=0, second=0, microsecond=0)
        while hour < hygiene[1]:
            out.setdefault(hour, (CUT_HYGIENE, None))
            hour += HOUR
    return out


def cuts(raw: Sequence[BandHour], shaped: Sequence[BandHour], kept: Mapping[datetime, tuple[str, datetime | None]]) -> list[Cut]:
    """The hours `raw` lowers that `shaped` (the same plan under the rules) does not: the reason the hour is kept
    (`kept_hours`), else over the day's cap."""
    lowered = {hour.start for hour in shaped if hour.action == ACTION_LOWER}
    out = []
    for hour in sorted(raw, key=lambda item: item.start):
        if hour.action != ACTION_LOWER or hour.start in lowered:
            continue
        kind, ready = kept.get(hour.start, (CUT_DAY, None))
        out.append(Cut(hour.start, kind, ready))
    return out


def cold_risk(
    ready: datetime,
    previous_until: datetime | None,
    blocks: Sequence[tuple[datetime, datetime]],
    runs: Sequence[RunLike],
) -> bool:
    """Whether a block held the pump's run for the peak at `ready` into the peak (see the module)."""
    ordered = sorted(runs, key=lambda run: run.start)
    for start, end in sorted(blocks):
        if not (start < ready and end > ready - PEAK_GUARD):
            continue
        if previous_until is not None and any(previous_until <= run.start < start for run in ordered):
            # The tank was filled after the previous peak and before the block.
            continue
        after = next((run for run in ordered if run.start >= start), None)
        if after is not None and after.start <= end + DELAYED_WITHIN and after.end > ready:
            return True
    return False


@dataclass(slots=True)
class TankGuard:
    """The tank rule: low below `TANK_LOW_C` until it reads `TANK_BACK_C` again; a tank that does not read keeps the
    last state (the meter's rules still stand)."""

    low: bool = False

    def step(self, value_c: float | None) -> bool:
        if value_c is None:
            return self.low
        if self.low:
            self.low = value_c < TANK_BACK_C
        else:
            self.low = value_c < TANK_LOW_C
        return self.low


__all__ = [
    "CUT_DAY",
    "CUT_HYGIENE",
    "CUT_PEAK",
    "DELAYED_WITHIN",
    "MAX_DAY_NO_TANK",
    "CLEAN_DAYS",
    "PEAK_GUARD",
    "PEAK_GUARD_STRICT",
    "TANK_BACK_C",
    "TANK_LOW_C",
    "Cut",
    "Guard",
    "TankGuard",
    "cold_risk",
    "cuts",
    "guard_hours",
    "guards",
    "is_night",
    "kept_hours",
    "max_day_hours",
    "long_night",
    "next_hygiene",
    "served",
]
