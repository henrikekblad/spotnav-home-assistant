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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Final

from .dhw_peaks import Peak, RunLike, peak_instant
from .shadow_rule import ACTION_LOWER, BandHour

PEAK_GUARD: Final = timedelta(hours=2)
#: The hours a local day lowers at most on a path that blocks hot water and no tank sensor.
MAX_DAY_NO_TANK: Final = 4
TANK_LOW_C: Final = 40.0
TANK_BACK_C: Final = 45.0
#: A run that starts this soon after a block's end was held by it.
DELAYED_WITHIN: Final = timedelta(minutes=30)
HOUR: Final = timedelta(hours=1)

CUT_PEAK: Final = "peak"
CUT_DAY: Final = "day_cap"


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
        out.append(Guard(ready - PEAK_GUARD, ready))
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


def max_day_hours(tank_sensor: bool) -> int | None:
    """The hours a local day may lower at most by the hot water's rule, `None` where the tank rule stands in."""
    return None if tank_sensor else MAX_DAY_NO_TANK


def cuts(raw: Sequence[BandHour], shaped: Sequence[BandHour], kept: Mapping[datetime, datetime]) -> list[Cut]:
    """The hours `raw` lowers that `shaped` (the same plan under the rules) does not: before a peak where the hour
    touches a guard (`kept`, from `guard_hours`), else over the day's cap."""
    lowered = {hour.start for hour in shaped if hour.action == ACTION_LOWER}
    out = []
    for hour in sorted(raw, key=lambda item: item.start):
        if hour.action != ACTION_LOWER or hour.start in lowered:
            continue
        ready = kept.get(hour.start)
        out.append(Cut(hour.start, CUT_PEAK if ready is not None else CUT_DAY, ready))
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
    "CUT_PEAK",
    "DELAYED_WITHIN",
    "MAX_DAY_NO_TANK",
    "PEAK_GUARD",
    "TANK_BACK_C",
    "TANK_LOW_C",
    "Cut",
    "Guard",
    "TankGuard",
    "cold_risk",
    "cuts",
    "guard_hours",
    "guards",
    "max_day_hours",
    "served",
]
