"""Heat and hot water apart from the heat pump's meter alone (plan `plans/v3_heating_plan.md`, the notes after
section 15): a pump without a network gives no hot-water signal, but its hot-water runs show in its power as a
distinct step, about three times its heating level on the owner's NIBE (≈ 3 kW for 20–30 min against ≈ 1.1 kW).

Pure, on the recorder's 5-minute means (kW). Relative to what the meter itself shows, never fixed watts:

* **Running level**: a period counts as running when its mean is at least `RUN_SHARE` of the window's 90th
  percentile (the idle draw sits far below).
* **Heating baseline**: the median of the running periods of the window (the last day): what the pump draws when it
  heats. On a summer day without heating that is the idle draw, and every run above it is hot water, as it should.
* **Hot water**: at least `MIN_RUN_PERIODS` periods in a row at `HOT_WATER_FACTOR` × the baseline or more, and no
  longer than `MAX_RUN` (a longer high draw is the pump heating hard on a cold day). A lone period (a compressor
  start) is no run. The run's whole energy is hot water: the pump makes hot water instead of heat, not beside it.

What this cannot tell: on a cold day when heating draws as much as a hot-water run, no run stands out and all is
counted as heat (the split then says so through `baseline_kw`, which the view shows).
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

PERIOD: Final = timedelta(minutes=5)
RUN_SHARE: Final = 0.35
HOT_WATER_FACTOR: Final = 1.6
MIN_RUN_PERIODS: Final = 2
MAX_RUN: Final = timedelta(minutes=120)
#: Fewer periods than this in the window: no baseline (too little seen).
MIN_PERIODS: Final = 24

STATE_IDLE: Final = "idle"
STATE_HEATING: Final = "heating"
STATE_HOT_WATER: Final = "hot_water"


@dataclass(frozen=True, slots=True)
class HotWaterRun:
    start: datetime
    end: datetime
    kwh: float

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "kwh": round(self.kwh, 3)}


@dataclass(frozen=True, slots=True)
class Levels:
    """What the window shows: the running level and the heating baseline (kW); `None` without enough periods."""

    running_kw: float
    baseline_kw: float

    @property
    def hot_water_kw(self) -> float:
        return self.baseline_kw * HOT_WATER_FACTOR


def levels(periods: Mapping[datetime, float]) -> Levels | None:
    values = sorted(kw for kw in periods.values() if kw > 0)
    if len(values) < MIN_PERIODS:
        return None
    p90 = values[min(len(values) - 1, int(len(values) * 0.9))]
    running = p90 * RUN_SHARE
    on = [kw for kw in values if kw >= running]
    if not on:
        return None
    baseline = statistics.median(on)
    return Levels(running, baseline) if baseline > 0 else None


def hot_water_runs(periods: Mapping[datetime, float], found: Levels) -> list[HotWaterRun]:
    """The hot-water runs among the 5-minute means (see the module)."""
    runs: list[HotWaterRun] = []
    block: list[tuple[datetime, float]] = []

    def close() -> None:
        if len(block) >= MIN_RUN_PERIODS and len(block) * PERIOD <= MAX_RUN:
            kwh = sum(kw for _, kw in block) * PERIOD.total_seconds() / 3600.0
            runs.append(HotWaterRun(block[0][0], block[-1][0] + PERIOD, kwh))
        block.clear()

    for start in sorted(periods):
        kw = periods[start]
        if kw >= found.hot_water_kw and (not block or start - block[-1][0] == PERIOD):
            block.append((start, kw))
            continue
        close()
        if kw >= found.hot_water_kw:
            block.append((start, kw))
    close()
    return runs


@dataclass(frozen=True, slots=True)
class DaySplit:
    """A day's heat pump energy so far: heat, hot water and the runs (kWh); `hot_water_kwh` is `None` when the
    split cannot be made (no baseline yet), the energy then all heat."""

    total_kwh: float
    heat_kwh: float
    hot_water_kwh: float | None
    runs: tuple[HotWaterRun, ...]
    baseline_kw: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_kwh": round(self.total_kwh, 3),
            "heat_kwh": round(self.heat_kwh, 3),
            "hot_water_kwh": None if self.hot_water_kwh is None else round(self.hot_water_kwh, 3),
            "runs": [run.as_dict() for run in self.runs],
            "baseline_kw": None if self.baseline_kw is None else round(self.baseline_kw, 3),
        }


def split_day(window: Mapping[datetime, float], day_start: datetime, day_end: datetime) -> DaySplit:
    """The day's periods (`day_start`..`day_end`) split, the levels learned on the whole `window` (the last day)."""
    day = {start: kw for start, kw in window.items() if day_start <= start < day_end}
    total = sum(day.values()) * PERIOD.total_seconds() / 3600.0
    found = levels(window)
    if found is None:
        return DaySplit(total, total, None, (), None)
    runs = tuple(run for run in hot_water_runs(window, found) if day_start <= run.start < day_end)
    hot = sum(run.kwh for run in runs)
    return DaySplit(total, max(0.0, total - hot), hot, runs, found.baseline_kw)


def state_now(kw: float | None, found: Levels | None, last_period_kw: float | None = None) -> str | None:
    """What the pump does by its power now: `hot_water` (now and over the last 5-minute period: a compressor start
    is no run), `heating` or `idle`; `None` without a reading or levels."""
    if kw is None or found is None:
        return None
    if kw >= found.hot_water_kw and last_period_kw is not None and last_period_kw >= found.hot_water_kw:
        return STATE_HOT_WATER
    if kw >= found.running_kw:
        return STATE_HEATING
    return STATE_IDLE
