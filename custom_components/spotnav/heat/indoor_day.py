"""The heat card's indoor curve for one local day: what a lowering did to the rooms (SpotNav 3.0). Pure.

    indoor_day: {start, end, step_min, low_c, high_c,
                 points: [{start, mean_c, coldest_c}],
                 setpoint: [{start, target_c, action}] | null} | null

* **`points`**: the chosen indoor sensors' measured means per step (`step_min`: 15 on the live card, from the
  recorder's 5-minute statistics; 60 in the history, from its hourly ones), only up to now. Each sensor counts as its
  reading less its offset (its room's own aim against the house target, `heat/config.py`), so every room is on the
  house's scale: `mean_c` is the mean of the sensors that have the step, `coldest_c` the lowest (what the control
  compares with the comfort band; the same as `mean_c` with one sensor). A step without a reading is left out. Every
  chosen sensor counts: the live exclusion of a room warmed by a stove or the sun (`heat/rooms.py`) is not made
  afresh for the past.
* **`low_c`**, **`high_c`**: the comfort band around the target as set now.
* **`setpoint`**: the heat plan's target per hour of the day (`target_c`: the target, less the band in a lowered hour,
  night setback and away included), for the history only; `null` on the live card, whose band hours carry it
  (`band.hours[].target_c`). Where the relay control sets no temperature, a lowered hour's target is the band's
  lower edge: no learned effect in degrees exists to draw instead.

`None` without indoor sensors chosen.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from .config import BAND_C, HeatConfig
from .rooms import plausible

#: The live card's step (min); the history's is an hour.
LIVE_STEP_MIN: Final = 15
HISTORY_STEP_MIN: Final = 60
INDOOR_DAY_KEYS: Final = ("start", "end", "step_min", "low_c", "high_c", "points", "setpoint")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def means_by_start(rows: Iterable[Mapping[str, Any]]) -> dict[datetime, float]:
    """A recorder statistics series' `mean` per period start (UTC); rows without one are skipped."""
    out: dict[datetime, float] = {}
    for row in rows:
        start = row.get("start")
        if isinstance(start, (int, float)) and not isinstance(start, bool):
            start = datetime.fromtimestamp(float(start), timezone.utc)
        mean = _number(row.get("mean"))
        if isinstance(start, datetime) and mean is not None:
            out[start] = mean
    return out


def points(
    readings: Mapping[str, Mapping[datetime, float]],
    offsets: Mapping[str, float],
    start: datetime,
    end: datetime,
    now: datetime,
    step_min: int,
) -> list[dict[str, Any]]:
    """The curve's points (see the module): per step from `start` up to `now` (and before `end`)."""
    step_s = step_min * 60
    last = min(end, now)
    # Per step index, per sensor: its readings' sum and count.
    sums: dict[int, dict[str, list[float]]] = {}
    for entity_id, series in readings.items():
        for at, value in series.items():
            if not start <= at < last or not plausible(value):
                continue
            held = sums.setdefault(int((at - start).total_seconds() // step_s), {}).setdefault(entity_id, [0.0, 0.0])
            held[0] += value
            held[1] += 1
    out: list[dict[str, Any]] = []
    for index in sorted(sums):
        values = [total / count - offsets.get(entity_id, 0.0) for entity_id, (total, count) in sums[index].items()]
        out.append(
            {
                "start": (start + timedelta(seconds=index * step_s)).isoformat(),
                "mean_c": round(sum(values) / len(values), 2),
                "coldest_c": round(min(values), 2),
            }
        )
    return out


def indoor_day(
    config: HeatConfig,
    readings: Mapping[str, Mapping[datetime, float]],
    start: datetime,
    end: datetime,
    now: datetime,
    step_min: int,
    setpoint: Sequence[tuple[datetime, float, str]] | None = None,
) -> dict[str, Any] | None:
    """The block (see the module); `setpoint` as `(hour start, target °C, action)`."""
    if not config.indoor_sensors:
        return None
    chosen = {entity_id: readings.get(entity_id, {}) for entity_id in config.indoor_sensors}
    offsets = {entity_id: config.offset_of(entity_id) for entity_id in config.indoor_sensors}
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "step_min": step_min,
        "low_c": config.target_c - BAND_C,
        "high_c": config.target_c + BAND_C,
        "points": points(chosen, offsets, start, end, now, step_min),
        "setpoint": None
        if setpoint is None
        else [{"start": hour.isoformat(), "target_c": round(target, 2), "action": action} for hour, target, action in setpoint],
    }


__all__: Sequence[str] = (
    "HISTORY_STEP_MIN", "INDOOR_DAY_KEYS", "LIVE_STEP_MIN", "indoor_day", "means_by_start", "points",
)
