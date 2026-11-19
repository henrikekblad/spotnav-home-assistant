"""The household's hot-water peaks: the times of day by which a full tank is wanted, learned from what the tank,
the meter or the pump shows (plan `plans/v3_dhw_plan.md` section 3). Pure.

A **draw** is hot water used: a moment and its size (kWh to reload it). Sources, best first:

* **The tank sensor**: a fall of at least `TANK_FALL_C` within `TANK_FALL_WITHIN` while the pump is not making hot
  water; the reload run that follows gives its size (`DEFAULT_KWH` without one).
* **The meter's runs** (`meter_split.hot_water_runs`): the pump reloads shortly after a draw, so the draw is placed
  `DRAW_LEAD` before the run's start. A run under `STANDBY_KWH` is the tank's standby loss, not a draw.
* **The pump's own state** (hot water as its priority) where nothing else reads: the runs' starts and lengths, sized
  by the boost power.

A run SpotNav started, and any run within `OWN_AFTER` of one, is left out: it was not the household's draw. On days
with such a run the peaks keep their last learned values (`learn_peaks(..., previous=..., hold=True)`).

**The peaks** (`learn_peaks`): the draws of the last `PEAK_DAYS` days into `BIN` bins by local time of day, weighted
by kWh and smoothed over `SMOOTH_BINS` bins each side. A peak is a local maximum whose draws (within `CLUSTER` of it)
hold at least `MIN_SHARE` of the window's draw energy, at most `MAX_PEAKS`, at least `MIN_GAP` apart. `ready_by` is
the 20th percentile of its draws' times rounded down to the quarter (most draws find a full tank), `until` the 80th
percentile rounded up (at least half an hour after `ready_by`), `kwh` the median of its draws per day, `run_at`
when the pump's own reload is expected (the median draw plus `DRAW_LEAD`).

**Warm start**: before `WARM_DAYS` days have been seen, or for a cluster of fewer than `MIN_DRAWS` draws, the
defaults hold (`DEFAULT_PEAKS`: 05–07 ready by 05:00, 17–20 ready by 17:00, `DEFAULT_KWH` each). A learned peak
replaces the default nearest to it, one by one.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Final, Protocol

DRAW_LEAD: Final = timedelta(minutes=20)
STANDBY_KWH: Final = 0.6
PEAK_DAYS: Final = 21
MAX_PEAKS: Final = 3
MIN_SHARE: Final = 0.20
MIN_GAP: Final = timedelta(hours=3)
#: The draws that belong to a peak: within half the gap between two peaks.
CLUSTER: Final = timedelta(minutes=90)
BIN: Final = timedelta(minutes=15)
SMOOTH_BINS: Final = 2
WARM_DAYS: Final = 7
MIN_DRAWS: Final = 5
OWN_AFTER: Final = timedelta(hours=2)
DEFAULT_KWH: Final = 1.0
TANK_FALL_C: Final = 3.0
TANK_FALL_WITHIN: Final = timedelta(minutes=15)
#: A reload run counted as a tank draw's size when it starts this soon after the fall.
TANK_RELOAD_WITHIN: Final = timedelta(hours=2)

_DAY_MIN: Final = 24 * 60
_BINS: Final = _DAY_MIN // 15


class RunLike(Protocol):
    @property
    def start(self) -> datetime: ...

    @property
    def end(self) -> datetime: ...

    @property
    def kwh(self) -> float: ...


@dataclass(frozen=True, slots=True)
class Draw:
    at: datetime
    kwh: float


@dataclass(frozen=True, slots=True)
class Peak:
    """A peak by local time of day (see the module)."""

    ready_by: time
    until: time
    kwh: float
    learned: bool
    run_at: time

    def as_dict(self) -> dict[str, Any]:
        return {"ready_by": self.ready_by.strftime("%H:%M"), "kwh": round(self.kwh, 2), "learned": self.learned}

    def stored(self) -> dict[str, Any]:
        return {
            "ready_by": self.ready_by.strftime("%H:%M"),
            "until": self.until.strftime("%H:%M"),
            "kwh": round(self.kwh, 3),
            "learned": self.learned,
            "run_at": self.run_at.strftime("%H:%M"),
        }

    @classmethod
    def from_stored(cls, raw: Any) -> Peak | None:
        try:
            return cls(
                time.fromisoformat(raw["ready_by"]),
                time.fromisoformat(raw["until"]),
                float(raw["kwh"]),
                raw["learned"] is True,
                time.fromisoformat(raw["run_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


DEFAULT_PEAKS: Final = (
    Peak(time(5, 0), time(7, 0), DEFAULT_KWH, False, time(6, 20)),
    Peak(time(17, 0), time(20, 0), DEFAULT_KWH, False, time(18, 50)),
)


def _minute(moment: datetime, zone: tzinfo) -> int:
    local = moment.astimezone(zone)
    return local.hour * 60 + local.minute


def _time(minute: float) -> time:
    whole = int(minute) % _DAY_MIN
    return time(whole // 60, whole % 60)


def _gap(a: float, b: float) -> float:
    """Minutes between two times of day, around midnight."""
    diff = abs(a - b) % _DAY_MIN
    return min(diff, _DAY_MIN - diff)


def _signed(minute: float, centre: float) -> float:
    """`minute` relative to `centre` in (-720, 720]."""
    diff = (minute - centre) % _DAY_MIN
    return diff - _DAY_MIN if diff > _DAY_MIN / 2 else diff


def standby(run: RunLike) -> bool:
    return run.kwh < STANDBY_KWH


def own_run(run: RunLike, own: Sequence[datetime]) -> bool:
    """Whether a run is SpotNav's own: it starts within `OWN_AFTER` after one of SpotNav's starts."""
    return any(start - timedelta(minutes=5) <= run.start <= start + OWN_AFTER for start in own)


def draws_from_runs(runs: Iterable[RunLike], own: Sequence[datetime] = ()) -> list[Draw]:
    """The draws the meter's runs show: each run that is no standby loss and not SpotNav's own, `DRAW_LEAD` early."""
    return [Draw(run.start - DRAW_LEAD, run.kwh) for run in runs if not standby(run) and not own_run(run, own)]


def draws_from_tank(
    readings: Sequence[tuple[datetime, float]],
    making: Callable[[datetime], bool],
    runs: Sequence[RunLike] = (),
) -> list[Draw]:
    """The draws a tank sensor shows (see the module); `making(moment)` says whether the pump made hot water then."""
    out: list[Draw] = []
    ordered = sorted(readings)
    index = 0
    while index < len(ordered):
        at, value = ordered[index]
        found = None
        for later, other in ordered[index + 1 :]:
            if later - at > TANK_FALL_WITHIN:
                break
            if value - other >= TANK_FALL_C and not making(at) and not making(later):
                found = later
                break
        if found is None:
            index += 1
            continue
        reload = next((run for run in sorted(runs, key=lambda r: r.start) if at <= run.start <= at + TANK_RELOAD_WITHIN), None)
        out.append(Draw(at, DEFAULT_KWH if reload is None else reload.kwh))
        # One draw per fall: skip past the fall.
        while index < len(ordered) and ordered[index][0] <= found:
            index += 1
    return out


def draws_from_state(intervals: Sequence[tuple[datetime, datetime]], boost_kw: float) -> list[Draw]:
    """The draws the pump's own hot-water state shows: each run's start, sized by its length at `boost_kw`."""
    out = []
    for start, end in intervals:
        kwh = boost_kw * (end - start).total_seconds() / 3600.0
        if kwh >= STANDBY_KWH:
            out.append(Draw(start - DRAW_LEAD, kwh))
    return out


def _percentile(values: Sequence[float], share: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = share * (len(ordered) - 1)
    low = math.floor(position)
    high = min(len(ordered) - 1, low + 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _clusters(draws: Sequence[tuple[float, float, date]]) -> list[tuple[float, list[tuple[float, float, date]]]]:
    """The maxima of the smoothed histogram that hold enough of the energy, with their draws (see the module)."""
    total = sum(kwh for _minute, kwh, _day in draws)
    if total <= 0:
        return []
    bins = [0.0] * _BINS
    for minute, kwh, _day in draws:
        bins[int(minute // 15) % _BINS] += kwh
    smooth = [sum(bins[(i + k) % _BINS] for k in range(-SMOOTH_BINS, SMOOTH_BINS + 1)) for i in range(_BINS)]
    maxima = [
        i
        for i in range(_BINS)
        if smooth[i] > 0 and smooth[i] >= smooth[(i - 1) % _BINS] and smooth[i] > smooth[(i + 1) % _BINS]
    ]
    chosen: list[float] = []
    for i in sorted(maxima, key=lambda i: (-smooth[i], i)):
        centre = i * 15 + 7.5
        if all(_gap(centre, other) >= MIN_GAP.total_seconds() / 60 for other in chosen):
            chosen.append(centre)
    out = []
    for centre in chosen:
        members = [
            item
            for item in draws
            if _gap(item[0], centre) <= CLUSTER.total_seconds() / 60
            and min(chosen, key=lambda other: _gap(item[0], other)) == centre
        ]
        if sum(kwh for _m, kwh, _d in members) >= MIN_SHARE * total:
            out.append((centre, members))
    out.sort(key=lambda item: -sum(kwh for _m, kwh, _d in item[1]))
    return out[:MAX_PEAKS]


def _peak_of(centre: float, members: Sequence[tuple[float, float, date]]) -> Peak:
    times = [centre + _signed(minute, centre) for minute, _kwh, _day in members]
    ready = math.floor(_percentile(times, 0.2) / 15) * 15
    until = max(math.ceil(_percentile(times, 0.8) / 15) * 15, ready + 30)
    per_day: dict[date, float] = {}
    for _minute, kwh, day in members:
        per_day[day] = per_day.get(day, 0.0) + kwh
    run_at = statistics.median(times) + DRAW_LEAD.total_seconds() / 60
    return Peak(_time(ready), _time(until), statistics.median(per_day.values()), True, _time(run_at))


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def learn_peaks(
    draws: Sequence[Draw],
    zone: tzinfo,
    days_seen: int,
    *,
    previous: Sequence[Peak] = (),
    hold: bool = False,
) -> list[Peak]:
    """The peaks (see the module), ordered by `ready_by`. `days_seen`: the days of the window with readings;
    `previous` the peaks found before, kept where `hold` (the window had SpotNav's own boosts) and nothing learned
    now stands near them."""
    learned: list[Peak] = []
    if days_seen >= WARM_DAYS:
        items = [(float(_minute(draw.at, zone)), draw.kwh, draw.at.astimezone(zone).date()) for draw in draws if draw.kwh > 0]
        for centre, members in _clusters(items):
            if len(members) >= MIN_DRAWS:
                learned.append(_peak_of(centre, members))
    gap = MIN_GAP.total_seconds() / 60
    if hold:
        for old in previous:
            if old.learned and all(_gap(_minutes(old.ready_by), _minutes(new.ready_by)) >= gap for new in learned):
                learned.append(old)
    learned = sorted(learned, key=lambda peak: _minutes(peak.ready_by))[:MAX_PEAKS]
    defaults = list(DEFAULT_PEAKS)
    for peak in learned:
        if not defaults:
            break
        nearest = min(defaults, key=lambda default: _gap(_minutes(default.ready_by), _minutes(peak.ready_by)))
        defaults.remove(nearest)
    kept = [
        default
        for default in defaults
        if all(_gap(_minutes(default.ready_by), _minutes(peak.ready_by)) >= gap for peak in learned)
    ]
    return sorted([*learned, *kept], key=lambda peak: _minutes(peak.ready_by))[:MAX_PEAKS]


def peak_instant(peak: Peak, day: date, zone: tzinfo) -> tuple[datetime, datetime, datetime]:
    """A peak on a local day: its `ready_by`, `until` and expected own run (`run_at`) as aware datetimes; an
    `until` or `run_at` before `ready_by` by the clock is the next day's."""

    def at(value: time) -> datetime:
        return datetime.combine(day, value, tzinfo=zone)

    ready = at(peak.ready_by)
    until = at(peak.until)
    if until <= ready:
        until += timedelta(days=1)
    run = at(peak.run_at)
    if run < ready:
        run += timedelta(days=1)
    return ready, until, run


__all__ = [
    "DEFAULT_PEAKS",
    "DRAW_LEAD",
    "Draw",
    "Peak",
    "draws_from_runs",
    "draws_from_state",
    "draws_from_tank",
    "learn_peaks",
    "own_run",
    "peak_instant",
    "standby",
]
