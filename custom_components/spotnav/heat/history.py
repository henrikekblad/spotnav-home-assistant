"""`spotnav/get_heat_history`'s answer (API v1): one day, month or year of the house's heating (SpotNav 3.0, shadow).

Pure: the shell (`api/heat_history.py`) reads the recorder's statistics and hands them over; nothing is read or
written here.

    {api_version: 1, site_id, time_zone, generated_at, period, date, end, today, complete,
     money: {currency, major, minor} | null,
     meter: {entity_id, name} | null,
     energy: {total_kwh, hours, hot_water_kwh, split_days, days, cost, priced_hours} | null,
     buckets: [{key, kwh, hot_water_kwh, cost, degree_days, outdoor_c, shadow_value}],
     degree_days: {base_c, outdoor: {entity_id, source} | null, sum, days, kwh, kwh_per_degree_day,
                   trend: [{month, kwh, degree_days, kwh_per_degree_day}]} | null,
     comfort: {low_c, sensors, coldest: {entity_id, name, mean_c, min_c} | null, hours, hours_below} | null,
     shadow: {days, lowered_hours, moved_kwh, value, kept_days, lower_share, rebound, blocks} | null,
     immersion: {hours, kwh} | null,
     runs: [{start, end, kwh, cause: "draw" | "standby" | null}],
     review: the day's review (`heat/review.py`, a day only) | null,
     indoor_day: the day's indoor curve by the hour with SpotNav's setpoint per hour (`heat/indoor_day.py`, a day
                 only) | null}

* **Use** (`energy`, `buckets`): the heat pump meter's energy (the recorder's hourly statistics of it, kept for good,
  `site/heat_meter.py`), each hour at its buy price with every fee as the battery plan prices it (the area's held day
  prices, the fees as set now): `cost` over `priced_hours` of `hours`. Hot water is split out per day only where the
  recorder still has the day's 5-minute rows (`heat/meter_split.py`, the levels learned on the day itself):
  `hot_water_kwh` over `split_days` of `days`, `null` with none. A bucket is an hour of a day, a day of a month or a
  month of a year (`site/history_period.py`). A bucket's `shadow_value` is the part of the shadow's `value` that falls
  in its hours (the lowered hour's, the saving counted where the heat was used), `null` where no day of the bucket was
  evaluated: the card's hatched upper bound on top of the bucket's cost.
* **Degree-days** (`degree_days`): per local day `max(0, BASE_C - the day's mean outdoor temperature)`, the mean of
  its hourly means (a day with fewer than `MIN_DAY_HOURS` of them has none). The weather-neutral figure is the meter's
  whole energy (hot water included: older days have no split) over the days that have both, per degree-day:
  `kwh_per_degree_day`, `null` below `MIN_DEGREE_DAYS` (a day: `MIN_DAY_DEGREE_DAYS`). `trend` has the twelve months up to the period's last (a day:
  none), each month's own figure, so a new heat pump, insulation or windows show as a step. The outdoor
  temperature is the heat card's own (`api/heat.read_outdoor`): a sensor's long-term statistics, or, where the
  outdoor figure is a weather entity (which has none), the hours the house forecast recorded (`source`
  `house_forecast`, eight weeks).
* **Comfort** (`comfort`): the indoor sensors chosen in the heat settings, by their hourly statistics: the coldest
  room over the period (the lowest mean) with its mean and its lowest reading, and the hours whose coldest hourly
  mean was below the comfort band's low edge (`low_c`, the target as set now less the band). Every chosen sensor
  counts here; the card's live exclusion of a room warmed by a stove or the sun is not made afresh for the past.
* **What SpotNav would have saved** (`shadow`): SpotNav's own plan on each past day of the period: the hours as it
  planned them when each began (`kept_days` of `days`, kept by the heat shadow, `heat/runtime.py`), else the plan
  (`heat/shadow_rule.band`) run again with that day's prices and the settings as they are now. Against the meter's
  hours, with the lowered block's effect as learned from the relay's measured closings (`lower_share`, `rebound`,
  `blocks`; `heat/relay_effect.py`, a cautious prior until there are some): the heat a lowered hour takes away
  (`moved_kwh`: `lower_share` of the hour's heat) at its price, less the share that comes back (`rebound`) bought in
  the preheat hour before the run, else over the hours after it: `value`. Shadow: the house holding the band is
  assumed; the review (`review`) shows the comfort the relay really gave.
* **Immersion heater** (`immersion`): where the heat pump's integration has its power (`heat/facts.py` role
  `immersion_power`), the hours it ran (hourly mean above `IMMERSION_ON_KW`) and its energy; `null` without.
* **Hot-water runs** (`runs`, a day only): each run of the day (`meter_split.hot_water_runs`) with what most likely
  set it off: `draw` (hot water used: a run at least `DRAW_FACTOR` times the usual small run) or `standby` (the tank
  cooling while nobody drew: about the usual small run), the usual small run being the lower quartile of the runs of
  the week the 5-minute rows hold; `null` with fewer than `MIN_REFERENCE_RUNS` runs to compare with. Without a tank
  sensor this is an estimate from the energy alone, and the card says so.

* **The day's indoor curve** (`indoor_day`, a day only): the indoor sensors' hourly means on the house's scale
  (each less its offset), their mean and the coldest, up to the last hour, beside the comfort band and SpotNav's
  setpoint per hour: the hours as it planned them when each began where the heat shadow kept the day, else the plan
  run again with the day's prices and the settings as they are now (night setback included; away is not known for a
  past day).

Money is in the area's minor unit. Shadow only: nothing here controls a heating device.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Final

from .. import time_zones
from ..site.history_period import (
    PERIOD_DAY,
    PERIOD_MONTH,
    Period,
    bucket_key,
    bucket_keys,
    local_date,
    midnight_utc,
    period_of,
    shifted,
)
from .config import BAND_C, HeatConfig
from .meter_split import HotWaterRun, split_day
from .relay_effect import AFTER, Effect
from .indoor_day import HISTORY_STEP_MIN, indoor_day
from .shadow_rule import ACTION_LOWER, ACTION_PREHEAT, band, day_hours

HEAT_HISTORY_API_VERSION: Final = 1
HISTORY_KEYS: Final = (
    "api_version", "site_id", "time_zone", "generated_at", "period", "date", "end", "today", "complete", "money",
    "meter", "energy", "buckets", "degree_days", "comfort", "shadow", "immersion", "runs", "review", "indoor_day",
)
#: The degree-day base (°C): the outdoor temperature above which a house needs no heating.
BASE_C: Final = 17.0
#: A day needs this many hourly outdoor means for its degree-days.
MIN_DAY_HOURS: Final = 18
#: Fewer degree-days than this: no kWh per degree-day (in a mild month hot water is most of the energy and the figure
#: would jump); a single day needs fewer.
MIN_DEGREE_DAYS: Final = 30.0
MIN_DAY_DEGREE_DAYS: Final = 5.0
TREND_MONTHS: Final = 12
IMMERSION_ON_KW: Final = 0.05
DRAW_FACTOR: Final = 1.4
MIN_REFERENCE_RUNS: Final = 4
CAUSE_DRAW: Final = "draw"
CAUSE_STANDBY: Final = "standby"
OUTDOOR_SENSOR: Final = "sensor"
OUTDOOR_HOUSE_FORECAST: Final = "house_forecast"
FIVE_MIN_HOURS: Final = 5 / 60


@dataclass(frozen=True, slots=True)
class HeatHistoryInputs:
    site_id: str
    now: datetime
    tz: str
    period: Period
    config: HeatConfig
    #: The meter's entity and name; `None` without a meter found or set.
    meter: tuple[str, str | None] | None = None
    #: The meter's energy per UTC hour start (kWh), from the trend's first month to the period's end.
    heat_hours: Mapping[datetime, float] = field(default_factory=dict)
    #: The meter's 5-minute energy (kWh per period start) the recorder still holds, the period's and the week before.
    five: Mapping[datetime, float] = field(default_factory=dict)
    #: Buy price with every fee per slot `(start, end, minor per kWh)`.
    prices: Sequence[tuple[datetime, datetime, float]] = ()
    money: tuple[str, str | None, str | None] | None = None
    #: The outdoor temperature's hourly means (°C, UTC hour starts), from the trend's first month on.
    outdoor: Mapping[datetime, float] = field(default_factory=dict)
    #: `(entity_id, source)` of the outdoor figure, `None` without one.
    outdoor_source: tuple[str | None, str] | None = None
    #: Per chosen indoor sensor: its name and its hourly `(mean, min)` (°C) by UTC hour start.
    indoor: Mapping[str, tuple[str, Mapping[datetime, tuple[float, float]]]] = field(default_factory=dict)
    #: The immersion heater's hourly mean power (kW, UTC hour starts); `None` without its power reading.
    immersion: Mapping[datetime, float] | None = None
    #: SpotNav's plan as kept per day (`heat/runtime.py`): `{day: {hour start: action}}`.
    kept: Mapping[date, Mapping[datetime, str]] = field(default_factory=dict)
    effect: Effect = field(default_factory=Effect)
    #: The heat shadow's day reviews (`heat/review.py`) by day.
    reviews: Mapping[date, Mapping[str, Any]] = field(default_factory=dict)


def _r(value: float, digits: int = 2) -> float:
    return round(value, digits)


def hour_prices(prices: Sequence[tuple[datetime, datetime, float]]) -> dict[datetime, float]:
    """The mean buy price of each UTC clock hour whose slots cover it whole (minor per kWh)."""
    covered: dict[datetime, float] = {}
    sums: dict[datetime, float] = {}
    for start, end, price in prices:
        moment = start
        while moment < end:
            hour = moment.replace(minute=0, second=0, microsecond=0)
            step = min(end, hour + timedelta(hours=1)) - moment
            seconds = step.total_seconds()
            covered[hour] = covered.get(hour, 0.0) + seconds
            sums[hour] = sums.get(hour, 0.0) + price * seconds
            moment += step
    return {hour: sums[hour] / covered[hour] for hour in covered if covered[hour] >= 3599}


def _in(moment: datetime, start: datetime, end: datetime) -> bool:
    return start <= moment < end


def day_outdoor(outdoor: Mapping[datetime, float], tz: str) -> dict[date, float]:
    """Each local day's mean outdoor temperature: the mean of its hourly means, days with `MIN_DAY_HOURS` of them."""
    by_day: dict[date, list[float]] = {}
    for start, value in outdoor.items():
        by_day.setdefault(local_date(start, tz), []).append(value)
    return {day: sum(values) / len(values) for day, values in by_day.items() if len(values) >= MIN_DAY_HOURS}


def degree_days(mean_c: float) -> float:
    return max(0.0, BASE_C - mean_c)


def day_kwh(heat_hours: Mapping[datetime, float], tz: str) -> dict[date, float]:
    out: dict[date, float] = {}
    for start, kwh in heat_hours.items():
        day = local_date(start, tz)
        out[day] = out.get(day, 0.0) + kwh
    return out


def _per_degree_day(kwh: float, dd: float, least: float = MIN_DEGREE_DAYS) -> float | None:
    return _r(kwh / dd, 3) if dd >= least else None


def degree_day_figures(
    days: Sequence[date], kwh_by_day: Mapping[date, float], outdoor_by_day: Mapping[date, float], complete: set[date]
) -> tuple[float, int, float]:
    """The degree-days, the days counted and the meter's energy over `days` that have both a full meter day
    (`complete`) and an outdoor mean."""
    total_dd = 0.0
    total_kwh = 0.0
    counted = 0
    for day in days:
        if day in outdoor_by_day and day in complete:
            total_dd += degree_days(outdoor_by_day[day])
            total_kwh += kwh_by_day.get(day, 0.0)
            counted += 1
    return total_dd, counted, total_kwh


def _complete_days(heat_hours: Mapping[datetime, float], tz: str, today: date) -> set[date]:
    """The past local days whose every hour has a meter figure."""
    counts: dict[date, int] = {}
    for start in heat_hours:
        day = local_date(start, tz)
        counts[day] = counts.get(day, 0) + 1
    out = set()
    for day, count in counts.items():
        if day >= today:
            continue
        hours = int((midnight_utc(day + timedelta(days=1), tz) - midnight_utc(day, tz)).total_seconds() // 3600)
        if count >= hours:
            out.add(day)
    return out


def splits(five: Mapping[datetime, float], days: Sequence[date], tz: str) -> dict[date, tuple[float, list[HotWaterRun]]]:
    """Each day's hot water (kWh) and runs from its 5-minute rows, where the day has its rows and a baseline."""
    out: dict[date, tuple[float, list[HotWaterRun]]] = {}
    for day in days:
        start, end = midnight_utc(day, tz), midnight_utc(day + timedelta(days=1), tz)
        window = {moment: kwh / FIVE_MIN_HOURS for moment, kwh in five.items() if _in(moment, start, end)}
        if not window:
            continue
        split = split_day(window, start, end)
        if split.hot_water_kwh is None:
            continue
        out[day] = (split.hot_water_kwh, list(split.runs))
    return out


def run_causes(runs: Sequence[HotWaterRun], reference: Sequence[HotWaterRun]) -> list[dict[str, Any]]:
    """The runs with their likely cause (see the module)."""
    sizes = sorted(run.kwh for run in reference)
    floor = None
    if len(sizes) >= MIN_REFERENCE_RUNS:
        floor = statistics.quantiles(sizes, n=4)[0] if len(sizes) > 1 else sizes[0]
    out = []
    for run in runs:
        cause = None
        if floor is not None and floor > 0:
            cause = CAUSE_DRAW if run.kwh >= DRAW_FACTOR * floor else CAUSE_STANDBY
        out.append({**run.as_dict(), "cause": cause})
    return out


def shadow_saving(
    config: HeatConfig,
    days: Sequence[date],
    heat_hours: Mapping[datetime, float],
    prices: Mapping[datetime, float],
    outdoor_by_day: Mapping[date, float],
    tz: str,
    by_hour: dict[datetime, float] | None = None,
    *,
    kept: Mapping[date, Mapping[datetime, str]] | None = None,
    effect: Effect | None = None,
) -> dict[str, Any] | None:
    """What SpotNav's plan would have moved and what that was worth (see the module); `None` without a day that has
    its prices and its meter hours. `by_hour`, when given, is filled with each evaluated day's hours and what the
    lowered ones were worth (0 for the others)."""
    zone = time_zones.require_zone(tz)
    effect = effect or Effect()
    # The household's own night and away are no saving SpotNav chose: the rule is run on the band alone.
    rule = replace(config, mode="home", away_until=None)
    counted = kept_days = lowered_hours = 0
    moved = 0.0
    value = 0.0
    for day in days:
        hours = day_hours(midnight_utc(day, tz), zone)
        if not all(hour in heat_hours for hour in hours):
            continue
        day_prices = {hour: prices[hour] for hour in hours if hour in prices}
        if len(day_prices) < len(hours):
            continue
        counted += 1
        if by_hour is not None:
            by_hour.update({hour: 0.0 for hour in hours})
        planned = (kept or {}).get(day)
        if planned:
            kept_days += 1
            actions = dict(planned)
        else:
            end = hours[-1] + timedelta(hours=1)
            banded, _guard = band(rule, hours, day_prices, end, zone, outdoor_c=outdoor_by_day.get(day))
            actions = {item.start: item.action for item in banded}
        run: list[datetime] = []
        for hour in [*hours, None]:
            if hour is not None and actions.get(hour) == ACTION_LOWER:
                run.append(hour)
                continue
            if run:
                before = run[0] - timedelta(hours=1)
                after = [run[-1] + timedelta(hours=1 + offset) for offset in range(int(AFTER.total_seconds() // 3600))]
                back_hours = [before] if actions.get(before) == ACTION_PREHEAT else [h for h in after if h in prices]
                if back_hours:
                    back_price = sum(prices[h] for h in back_hours) / len(back_hours)
                    for lowered in run:
                        taken = effect.lower_share * heat_hours[lowered]
                        moved += taken
                        worth = taken * (day_prices[lowered] - effect.rebound * back_price)
                        value += worth
                        if by_hour is not None:
                            by_hour[lowered] = worth
                        lowered_hours += 1
                run = []
    if counted == 0:
        return None
    return {
        "days": counted,
        "lowered_hours": lowered_hours,
        "moved_kwh": _r(moved, 3),
        "value": _r(value),
        "kept_days": kept_days,
        **{key: item for key, item in effect.as_dict().items()},
    }


def comfort_block(inputs: HeatHistoryInputs, start: datetime, end: datetime) -> dict[str, Any] | None:
    if not inputs.indoor:
        return None
    low = inputs.config.target_c - BAND_C
    means: dict[str, list[float]] = {}
    lows: dict[str, float] = {}
    coldest_by_hour: dict[datetime, float] = {}
    for entity_id, (_name, hours) in inputs.indoor.items():
        # Each room against its own aim: its reading less its offset is what the house's band is compared with.
        offset = inputs.config.offset_of(entity_id)
        for moment, (mean, minimum) in hours.items():
            if not _in(moment, start, end):
                continue
            means.setdefault(entity_id, []).append(mean)
            lows[entity_id] = min(lows.get(entity_id, minimum), minimum)
            coldest_by_hour[moment] = min(coldest_by_hour.get(moment, mean - offset), mean - offset)
    coldest = None
    if means:
        entity_id = min(means, key=lambda key: (sum(means[key]) / len(means[key]) - inputs.config.offset_of(key), key))
        coldest = {
            "entity_id": entity_id,
            "name": inputs.indoor[entity_id][0],
            "mean_c": _r(sum(means[entity_id]) / len(means[entity_id]), 1),
            "min_c": _r(lows[entity_id], 1),
        }
    return {
        "low_c": low,
        "sensors": len(inputs.indoor),
        "coldest": coldest,
        "hours": len(coldest_by_hour),
        "hours_below": sum(1 for value in coldest_by_hour.values() if value < low),
    }


def day_setpoint(inputs: HeatHistoryInputs, day: date, outdoor_c: float | None) -> list[tuple[datetime, float, str]]:
    """SpotNav's target per hour of `day` (see the module): the kept plan's actions where the day was kept, else the
    plan run again; a lowered hour aims at the band's lower edge, a preheat at its upper."""
    zone = time_zones.require_zone(inputs.tz)
    hours = day_hours(midnight_utc(day, inputs.tz), zone)
    rule = replace(inputs.config, mode="home", away_until=None)
    priced = hour_prices(inputs.prices)
    end = hours[-1] + timedelta(hours=1)
    banded, _guard = band(rule, hours, {hour: priced[hour] for hour in hours if hour in priced}, min(end, inputs.now), zone, outdoor_c=outdoor_c)
    kept = inputs.kept.get(day) or {}
    target = rule.target_c
    out: list[tuple[datetime, float, str]] = []
    for hour in banded:
        action = kept.get(hour.start, hour.action)
        aim = {ACTION_LOWER: target - BAND_C, ACTION_PREHEAT: target + BAND_C}.get(action, hour.target_c)
        out.append((hour.start, aim, action))
    return out


def day_indoor(inputs: HeatHistoryInputs, outdoor_by_day: Mapping[date, float]) -> dict[str, Any] | None:
    """The `indoor_day` block of a day (see the module), `None` for a month or a year and without indoor sensors."""
    period = inputs.period
    if period.kind != PERIOD_DAY:
        return None
    readings = {entity_id: {moment: values[0] for moment, values in hours.items()} for entity_id, (_name, hours) in inputs.indoor.items()}
    start, end = midnight_utc(period.first, inputs.tz), midnight_utc(period.end, inputs.tz)
    return indoor_day(
        inputs.config,
        readings,
        start,
        end,
        inputs.now,
        HISTORY_STEP_MIN,
        day_setpoint(inputs, period.first, outdoor_by_day.get(period.first)),
    )


def heat_history(inputs: HeatHistoryInputs) -> dict[str, Any]:
    """The whole answer (`HISTORY_KEYS`)."""
    period, tz = inputs.period, inputs.tz
    today = local_date(inputs.now, tz)
    start, end = midnight_utc(period.first, tz), midnight_utc(period.end, tz)
    priced = hour_prices(inputs.prices)
    in_period = {moment: kwh for moment, kwh in inputs.heat_hours.items() if _in(moment, start, end)}
    days = [day for day in period.days() if day <= today]
    split = splits(inputs.five, days, tz)
    outdoor_by_day = day_outdoor(inputs.outdoor, tz)
    kwh_by_day = day_kwh(inputs.heat_hours, tz)
    complete = _complete_days(inputs.heat_hours, tz, today)

    buckets = {key: {"key": key, "kwh": None, "hot_water_kwh": None, "cost": None, "degree_days": None, "outdoor_c": None, "shadow_value": None} for key in bucket_keys(period, tz)}
    cost = 0.0
    priced_hours = 0
    for moment, kwh in in_period.items():
        bucket = buckets.get(bucket_key(period, moment, tz))
        if bucket is None:
            continue
        bucket["kwh"] = (bucket["kwh"] or 0.0) + kwh
        price = priced.get(moment)
        if price is not None:
            bucket["cost"] = (bucket["cost"] or 0.0) + kwh * price
            cost += kwh * price
            priced_hours += 1
    outdoor_groups: dict[str, list[float]] = {}
    if period.kind == PERIOD_DAY:
        for moment, value in inputs.outdoor.items():
            if _in(moment, start, end):
                outdoor_groups.setdefault(bucket_key(period, moment, tz), []).append(value)
    else:
        for day in days:
            key = bucket_key(period, midnight_utc(day, tz) + timedelta(hours=12), tz)
            if day in split:
                buckets[key]["hot_water_kwh"] = (buckets[key]["hot_water_kwh"] or 0.0) + split[day][0]
            if day in outdoor_by_day:
                outdoor_groups.setdefault(key, []).append(outdoor_by_day[day])
                if day in complete:
                    buckets[key]["degree_days"] = (buckets[key]["degree_days"] or 0.0) + degree_days(outdoor_by_day[day])
    for key, values in outdoor_groups.items():
        buckets[key]["outdoor_c"] = sum(values) / len(values)
    shadow_hours: dict[datetime, float] = {}
    shadow = (
        None
        if inputs.meter is None
        else shadow_saving(
            inputs.config,
            [day for day in days if day < today],
            inputs.heat_hours,
            priced,
            outdoor_by_day,
            tz,
            shadow_hours,
            kept=inputs.kept,
            effect=inputs.effect,
        )
    )
    for moment, worth in shadow_hours.items():
        bucket = buckets.get(bucket_key(period, moment, tz))
        if bucket is not None:
            bucket["shadow_value"] = (bucket["shadow_value"] or 0.0) + worth
    rows = [{key: (_r(value, 3) if isinstance(value, float) else value) for key, value in bucket.items()} for bucket in buckets.values()]

    energy = None
    if inputs.meter is not None:
        hot = [split[day][0] for day in days if day in split]
        energy = {
            "total_kwh": _r(sum(in_period.values()), 3),
            "hours": len(in_period),
            "hot_water_kwh": _r(sum(hot), 3) if hot else None,
            "split_days": len(hot),
            "days": len({local_date(moment, tz) for moment in in_period}),
            "cost": _r(cost) if inputs.money is not None and priced_hours > 0 else None,
            "priced_hours": priced_hours,
        }

    degree = None
    if inputs.outdoor_source is not None:
        dd, counted, kwh = degree_day_figures(days, kwh_by_day, outdoor_by_day, complete)
        trend = []
        if period.kind != PERIOD_DAY:
            last = period_of(PERIOD_MONTH, period.end - timedelta(days=1))
            for offset in range(TREND_MONTHS - 1, -1, -1):
                month = shifted(last, -offset)
                month_days = [day for day in month.days() if day < today]
                month_dd, month_counted, month_kwh = degree_day_figures(month_days, kwh_by_day, outdoor_by_day, complete)
                trend.append(
                    {
                        "month": f"{month.first.year:04d}-{month.first.month:02d}",
                        "days": month_counted,
                        "kwh": _r(month_kwh, 3) if month_counted else None,
                        "degree_days": _r(month_dd, 1) if month_counted else None,
                        "kwh_per_degree_day": _per_degree_day(month_kwh, month_dd) if month_counted else None,
                    }
                )
        degree = {
            "base_c": BASE_C,
            "outdoor": {"entity_id": inputs.outdoor_source[0], "source": inputs.outdoor_source[1]},
            "sum": _r(dd, 1),
            "days": counted,
            "kwh": _r(kwh, 3),
            "kwh_per_degree_day": _per_degree_day(kwh, dd, MIN_DAY_DEGREE_DAYS if period.kind == PERIOD_DAY else MIN_DEGREE_DAYS)
            if counted
            else None,
            "trend": trend,
        }

    immersion = None
    if inputs.immersion is not None:
        hours = [kw for moment, kw in inputs.immersion.items() if _in(moment, start, end)]
        immersion = {"hours": sum(1 for kw in hours if kw > IMMERSION_ON_KW), "kwh": _r(sum(max(0.0, kw) for kw in hours), 3)}

    runs: list[dict[str, Any]] = []
    if period.kind == PERIOD_DAY and period.first in split:
        reference_days = [period.first - timedelta(days=offset) for offset in range(7)]
        reference = [run for day, (_kwh, day_runs) in splits(inputs.five, reference_days, tz).items() for run in day_runs]
        runs = run_causes(split[period.first][1], reference)

    money = inputs.money
    return {
        "api_version": HEAT_HISTORY_API_VERSION,
        "site_id": inputs.site_id,
        "time_zone": tz,
        "generated_at": inputs.now.isoformat(),
        "period": period.kind,
        "date": period.first.isoformat(),
        "end": period.end.isoformat(),
        "today": today.isoformat(),
        "complete": period.end <= today,
        "money": None if money is None else {"currency": money[0], "major": money[1], "minor": money[2]},
        "meter": None if inputs.meter is None else {"entity_id": inputs.meter[0], "name": inputs.meter[1]},
        "energy": energy,
        "buckets": rows if inputs.meter is not None else [],
        "degree_days": degree,
        "comfort": comfort_block(inputs, start, end),
        "shadow": shadow,
        "immersion": immersion,
        "runs": runs,
        "review": dict(inputs.reviews[period.first]) if period.kind == PERIOD_DAY and period.first in inputs.reviews else None,
        "indoor_day": day_indoor(inputs, outdoor_by_day),
    }
