"""A day's heat review in shadow (plan `plans/v3_heating_plan.md` section 10, steps 2 and 3): what the relay really
did (the SpotNav blueprint or whoever closed it) beside what SpotNav's own plan would have done, each in money and
comfort. Pure; the shell (`heat/runtime.py`) reads the day the night after and keeps the result.

    {date, low_c, effect: {lower_share, rebound, blocks},
     day: {coldest_min_c, minutes_below} | null,
     relay: {hours, value, saved_kwh, measured, coldest_min_c, minutes_below},
     plan: {hours, value, saved_kwh, overlap_hours, coldest_min_c, minutes_below}}

* **SpotNav's plan** counts the hours it lowered for a reason of its own (`plan_lowered_hours`): an hour lowered only
  because the house was already above its band (`room_warm`) is no saving of SpotNav's, so it is neither in the
  plan's hours nor in its value.
* **Money** (`value`, the area's minor unit, positive is saved): per lowered hour the heat taken away at that hour's
  price, less the rebound bought back at the prices of the `AFTER` after the block. The relay's measured blocks
  (`heat/relay_effect.measure_block`) count what the meter showed; an unmeasured block and SpotNav's plan count the
  learned effect (`Effect`) on the hour's heat: where the relay really lowered that hour the meter shows the dip, so
  the heat counted there is the measured block's baseline, else the meter's hour lifted by what the effect took.
  `saved_kwh` is what did not come back (taken away less the rebound); `measured` the relay's blocks the meter
  measured.
* **Comfort** (`coldest_min_c`, `minutes_below`): the coldest chosen room's 5-minute means (the lowest of the rooms
  per period) over each lowered block and the `AFTER` after it: the lowest, and the minutes below the comfort band's
  low edge (`low_c`). For the relay that is what the rooms really did. For SpotNav's plan it is what the rooms did in
  its hours, which shows its own effect only where the relay lowered too (`overlap_hours`); the rest is what the
  house did under the relay's lowering, and the card says so. `day` is the same over the whole day.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Final

from .relay_effect import AFTER, HOUR, BlockResult, Effect, hour_shares
from .shadow_rule import ACTION_LOWER, REASON_ROOM_WARM

FIVE_MINUTES: Final = 5


def blocks_of(hours: Iterable[datetime]) -> list[tuple[datetime, datetime]]:
    """Hour starts as blocks of hours in a row."""
    out: list[tuple[datetime, datetime]] = []
    for start in sorted(hours):
        if out and out[-1][1] == start:
            out[-1] = (out[-1][0], start + HOUR)
        else:
            out.append((start, start + HOUR))
    return out


def _covered(hour: datetime, intervals: Sequence[tuple[datetime, datetime]]) -> float:
    """The share of the hour the intervals cover (0..1)."""
    total = 0.0
    for start, end in intervals:
        overlap = min(end, hour + HOUR) - max(start, hour)
        if overlap > timedelta(0):
            total += overlap.total_seconds() / 3600.0
    return min(1.0, total)


def _measured_in(hour: datetime, measured: Sequence[BlockResult]) -> BlockResult | None:
    return next((block for block in measured if block.start < hour + HOUR and hour < block.end), None)


def heat_base(
    hour: datetime,
    heat_kwh: Mapping[datetime, float],
    relay: Sequence[tuple[datetime, datetime]],
    measured: Sequence[BlockResult],
    effect: Effect,
) -> float | None:
    """The heat the hour would have used unlowered (kWh): a measured block's baseline where the relay lowered it,
    else the meter's hour lifted by what the effect took in the part the relay covered."""
    block = _measured_in(hour, measured)
    if block is not None:
        return block.baseline_kw
    used = heat_kwh.get(hour)
    if used is None:
        return None
    covered = _covered(hour, relay)
    taken = effect.lower_share * covered
    return used / (1.0 - taken) if taken < 0.95 else used


def _comfort(
    coldest: Mapping[datetime, float], windows: Sequence[tuple[datetime, datetime]], low_c: float
) -> tuple[float | None, int]:
    values = [value for moment, value in coldest.items() if any(start <= moment < end for start, end in windows)]
    if not values:
        return None, 0
    return round(min(values), 2), FIVE_MINUTES * sum(1 for value in values if value < low_c)


def _value(
    blocks: Sequence[tuple[datetime, datetime]],
    base: Mapping[datetime, float | None],
    prices: Mapping[datetime, float],
    effect: Effect,
    measured: Sequence[BlockResult] = (),
) -> tuple[float, float, int]:
    """The blocks' value (minor unit), the kWh not coming back, and how many were measured."""
    value = saved = 0.0
    counted = 0
    for start, end in blocks:
        block = next((item for item in measured if item.start == start and item.end == end), None)
        taken_by_hour: list[tuple[datetime, float]] = []
        if block is not None:
            counted += 1
            for hour, share in hour_shares(start, end):
                taken_by_hour.append((hour, (block.baseline_kw - block.during_kw) * share))
            back = block.rebound_kwh
        else:
            for hour, share in hour_shares(start, end):
                kw = base.get(hour)
                if kw is not None:
                    taken_by_hour.append((hour, effect.lower_share * kw * share))
            back = effect.rebound * sum(taken for _hour, taken in taken_by_hour)
        taken_total = sum(taken for _hour, taken in taken_by_hour)
        for hour, taken in taken_by_hour:
            if hour in prices:
                value += taken * prices[hour]
        after = hour_shares(end, end + AFTER)
        priced_after = [(hour, share) for hour, share in after if hour in prices]
        if priced_after:
            weight = sum(share for _hour, share in priced_after)
            value -= back * sum(prices[hour] * share for hour, share in priced_after) / weight
        saved += taken_total - back
    return value, saved, counted


def plan_lowered_hours(kept: Mapping[datetime, tuple[str, str]]) -> list[datetime]:
    """The kept hours SpotNav's plan lowered for a reason of its own (`{hour start: (action, reason)}`): an hour
    lowered only because the house was already too warm (`room_warm`) is what the house did by itself, no value."""
    return [hour for hour, (action, reason) in kept.items() if action == ACTION_LOWER and reason != REASON_ROOM_WARM]


def review_day(
    day: date,
    day_hours: Sequence[datetime],
    *,
    prices: Mapping[datetime, float],
    heat_kwh: Mapping[datetime, float],
    relay: Sequence[tuple[datetime, datetime]],
    plan_lowered: Iterable[datetime],
    measured: Sequence[BlockResult],
    effect: Effect,
    coldest: Mapping[datetime, float],
    low_c: float,
) -> dict[str, Any]:
    """The day's review (see the module). `relay` are the relay's closed intervals that started in the day, `coldest`
    the coldest room's 5-minute means (°C by period start) of the day and the hours after it."""
    plan_blocks = blocks_of(start for start in plan_lowered if start in set(day_hours))
    relay_blocks = sorted(relay)
    hours_seen = {hour for start, end in [*plan_blocks, *relay_blocks] for hour, _ in hour_shares(start, end)}
    base = {hour: heat_base(hour, heat_kwh, relay_blocks, measured, effect) for hour in hours_seen}
    relay_value, relay_saved, counted = _value(relay_blocks, base, prices, effect, measured)
    plan_value, plan_saved, _ = _value(plan_blocks, base, prices, effect)
    after = AFTER
    relay_min, relay_below = _comfort(coldest, [(start, end + after) for start, end in relay_blocks], low_c)
    plan_min, plan_below = _comfort(coldest, [(start, end + after) for start, end in plan_blocks], low_c)
    day_window = [(day_hours[0], day_hours[-1] + HOUR)] if day_hours else []
    day_min, day_below = _comfort(coldest, day_window, low_c)
    overlap = sum(
        _covered(hour, relay_blocks) for start, end in plan_blocks for hour, _share in hour_shares(start, end)
    )
    return {
        "date": day.isoformat(),
        "low_c": low_c,
        "effect": effect.as_dict(),
        "day": None if day_min is None else {"coldest_min_c": day_min, "minutes_below": day_below},
        "relay": {
            "hours": round(sum((end - start).total_seconds() for start, end in relay_blocks) / 3600.0, 2),
            "value": round(relay_value, 2),
            "saved_kwh": round(relay_saved, 3),
            "measured": counted,
            "coldest_min_c": relay_min,
            "minutes_below": relay_below,
        },
        "plan": {
            "hours": float(sum(1 for start, end in plan_blocks for _ in hour_shares(start, end))),
            "value": round(plan_value, 2),
            "saved_kwh": round(plan_saved, 3),
            "overlap_hours": round(overlap, 2),
            "coldest_min_c": plan_min,
            "minutes_below": plan_below,
        },
    }
