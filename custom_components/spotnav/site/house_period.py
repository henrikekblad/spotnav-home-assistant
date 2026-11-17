"""`spotnav/get_house_history`'s answer (API v1): one day, month or year of the house as it went (SpotNav 3.0, shadow).

Pure: the shell (`api/house_history.py`) reads the periods from the recorder and hands them over with the prices,
the battery's reviewed days and the forecasts' daily reviews; nothing is read or written here.

    {api_version: 1, site_id, time_zone, generated_at, period: "day" | "month" | "year", date (the period's first
     day), end (the first day after it), today, complete, money: {currency, major_unit, minor_unit} | null,
     energy_flow: (as `spotnav/get_house`'s, `site/energy_flow.py`) | null,
     buckets: [{key, grid_in, grid_out, solar, battery_in, battery_out, car, heat, house, use, cost, periods,
                battery_saving, battery_days, saving, saving_battery, saving_car, saving_heat, arbitrage, buy,
                sell}],
     cost: {net, bought, sold, kwh_in, kwh_out, periods, priced} | null,
     review: {days, period_days, archived, actual_cost, battery_saving, battery_days, sun_gave, sun_days,
              against_nothing, nothing_days, plan_saving, own_saving, modes: {mode: {saving, days}}, mode,
              by_day: [{date, battery_saving, plan_saving, own_saving, own_share, plan_share, share_note}],
              perfect: (`site/perfect_view.py`) | null} | null,
     forecast: {house, sun, sun_corrected, soc: {mae, days} | null},
     saving: {total, battery, car, heat, battery_days, car_days, heat_days, days} | null }

* **Energy** (`energy_flow`, `buckets`): the Energy dashboard's meters and the chargers' registers per period (a
  day within the recorder's short-term statistics by the quarter hour, else by the hour), allocated period by
  period by the energy flow's one rule and summed over the day, month or year, so a month's flow is the sum of its
  hours' flows, never a month's totals allocated at once (that would let the midday sun charge the night's battery).
  A bucket is an hour of a day, a day of a month or a month of a year (`site/history_period.py`): the meters as read
  (`grid_in`, `grid_out`, `solar`, `battery_in`, `battery_out`) and the uses as the flow allocates them (`house` the
  balance, `heat`, `car`; where the meters disagree the flow takes the shortfall off them), `use` their sum. `null` without the dashboard's grid meter.
  A bucket's `battery_saving` is the battery's money over the reviewed days in it (the review's `battery_saving`,
  what the day cost against the same day with no battery; negative: the battery made it dearer), over
  `battery_days` of them; `null` and 0 where no day of the bucket is reviewed, and always for the hours of a day (the
  review is a whole day's). The card's bar is `cost` filled and `cost + battery_saving` whole. A bucket's
  `arbitrage` is what the battery earned in it, the house card's headline measure (`battery/stored_value.py`, the same
  as the battery's own app shows): an hour of a day from the day's hourly tally (kept 40 days), a day or a month from
  the days' totals (kept 400 days); `null` without a battery or where nothing is kept.
  `buy` and `sell` are the bucket's mean prices over its priced periods (minor unit per kWh, every fee in the buy
  price), weighted by their minutes; `null` where none is priced (the house card's price lines for a past day).
* **Cost** (`cost`): each period's import at its buy price with every fee and its export at its sell price, as the
  battery plan prices a quarter hour (`battery/plan_shadow.price_slots`: the area's held day prices with the fees as
  they are set now, so a corrected fee corrects the history). An hour read by the hour is priced at the mean of its
  slots (the meters do not say when in the hour the energy came). A period with a slot missing is left out of the
  cost and counted (`periods` against `priced`): the card says how much of the period is priced. `null` without a
  price source or fees.
* **The battery's money** (`review`): the battery plan's nightly reviews of the period's days
  (`battery/plan_shadow.DayReview`; kept 30 days, and older ones from the day archive where a day was exported,
  `archived`), summed: what the days cost (`actual_cost`), against the same days with no battery
  (`battery_saving`), what the sun gave, both together, the plan and the battery's own way against plain
  self-consumption, each mode planned beside it. Each sum says over how many days (`*_days`); `days` against
  `period_days` (the period's days up to yesterday) says how much of the period was reviewed; `by_day` has each
  reviewed day (the front's month calendar moved here) with its share of its perfect plan in hindsight (`own_share`,
  `plan_share`; `share_note` says why a day with a perfect plan has none: `partial`, under 90 % of it measured, or
  `small`, the perfect plan saved under 5 kr); `perfect` the period's days against their perfect plans (`site/perfect_view.py`: the shares summed
  over the period, where the day boundary washes out). `null` without a battery or with no reviewed day.
* **What SpotNav saved** (`saving`): the battery, the car and the heat each against its own counterfactual
  (`site/spotnav_saving.py`), per day summed over the period: `{total, battery, car, heat, battery_days, car_days,
  heat_days, days}` (each part over the days that have it, `days` the days with any part); `null` without any. A
  month's or a year's bucket has the same sums (`saving`, `saving_battery`, `saving_car`, `saving_heat`, `null` where
  none of its days has the part); a day's hours have none (the parts are a whole day's).
* **The forecasts** (`forecast`): the mean of each forecast's daily mean absolute error over the period's reviewed
  days (the forecast log keeps 30 days of reviews), with the days counted; `null` per forecast without one.

Money is in the area's minor unit (öre). Shadow: nothing in it controls anything.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Final

from ..battery.plan_model import MODES
from ..battery.plan_shadow import KIND_BATTERY, DayReview, PriceSlot
from .energy_flow import EnergyHour, allocate_hour, energy_flow
from .forecast_log import KIND_HOUSE, KIND_SUN, KIND_SUN_CORRECTED
from .perfect_view import perfect_block
from .spotnav_saving import DaySaving, period_sums
from .history_period import PERIOD_DAY, Period, bucket_key, bucket_keys, local_date, midnight_utc

HOUSE_HISTORY_API_VERSION: Final = 1
HISTORY_KEYS: Final = (
    "api_version",
    "site_id",
    "time_zone",
    "generated_at",
    "period",
    "date",
    "end",
    "today",
    "complete",
    "money",
    "energy_flow",
    "buckets",
    "cost",
    "review",
    "forecast",
    "saving",
)
BUCKET_KEYS: Final = (
    "key",
    "grid_in",
    "grid_out",
    "solar",
    "battery_in",
    "battery_out",
    "car",
    "heat",
    "house",
    "use",
    "cost",
    "periods",
    "battery_saving",
    "battery_days",
    "saving",
    "saving_battery",
    "saving_car",
    "saving_heat",
    "arbitrage",
    "buy",
    "sell",
)
#: The forecast log's kinds the history scores, by the answer's name.
FORECAST_KINDS: Final = (
    ("house", KIND_HOUSE),
    ("sun", KIND_SUN),
    ("sun_corrected", KIND_SUN_CORRECTED),
    ("soc", KIND_BATTERY),
)


@dataclass(frozen=True, slots=True)
class HouseHistoryInputs:
    site_id: str
    now: datetime
    tz: str
    period: Period
    #: The periods read (`EnergyHour`, quarter hours or hours, UTC starts), `None` without the grid meter.
    periods: Sequence[EnergyHour] | None
    has_solar: bool = False
    has_battery: bool = False
    has_heat: bool = False
    #: The heat's sources (the heat pump's meter and each heat zone counted, `site/energy_flow.py`).
    heat_sources: int = 1
    step_min: int = 60
    prices: Sequence[PriceSlot] = ()
    #: The area's money words `(currency, major, minor)`; `None` without a price source.
    money: tuple[str, str | None, str | None] | None = None
    #: The battery's reviewed days (memory and archive), `None` without a battery.
    reviews: Sequence[DayReview] | None = None
    #: The days of `reviews` read from the day archive.
    archived: frozenset[date] = frozenset()
    #: Per forecast kind (`forecast_log`'s), each reviewed day's mean absolute error.
    forecast_mae: Mapping[str, Mapping[date, float]] = field(default_factory=dict)
    #: What SpotNav saved on each of the period's days up to today (`site/spotnav_saving.py`); empty without.
    savings: Mapping[date, DaySaving] = field(default_factory=dict)
    #: The battery's arbitrage per local day and per UTC hour (`battery/stored_value.py`), the house card's headline
    #: measure; empty without a battery.
    arbitrage_days: Mapping[date, float] = field(default_factory=dict)
    arbitrage_hours: Mapping[datetime, float] = field(default_factory=dict)


def _r(value: float, digits: int = 2) -> float:
    return round(value, digits)


def slot_prices(period: EnergyHour, slots: Sequence[PriceSlot]) -> tuple[float, float] | None:
    """The period's buy and sell price (minor per kWh): the slots over it weighted by their overlap; `None` when any
    part of it has no slot."""
    start, end = period.start, period.end
    covered = 0.0
    buy = sell = 0.0
    for slot in slots:
        if slot.end <= start or slot.start >= end:
            continue
        overlap = (min(end, slot.end) - max(start, slot.start)).total_seconds()
        covered += overlap
        buy += slot.buy * overlap
        sell += slot.sell * overlap
    span = (end - start).total_seconds()
    if span <= 0 or covered < span - 1:
        return None
    return buy / covered, sell / covered


def _sorted_slots(slots: Sequence[PriceSlot]) -> list[PriceSlot]:
    return sorted(slots, key=lambda slot: slot.start)


def _slots_near(slots: list[PriceSlot], starts: list[datetime], period: EnergyHour) -> list[PriceSlot]:
    """The slots that may overlap `period` (a binary search on the sorted starts; a slot is at most a day long)."""
    low = bisect_left(starts, period.start - timedelta(days=1))
    high = bisect_right(starts, period.end)
    return slots[low:high]


def _empty_bucket(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "grid_in": 0.0,
        "grid_out": 0.0,
        "solar": None,
        "battery_in": None,
        "battery_out": None,
        "car": 0.0,
        "heat": None,
        "house": 0.0,
        "use": 0.0,
        "cost": None,
        "periods": 0,
        "battery_saving": None,
        "battery_days": 0,
        "saving": None,
        "saving_battery": None,
        "saving_car": None,
        "saving_heat": None,
        "arbitrage": None,
        "buy": None,
        "sell": None,
    }


def _add(bucket: dict[str, Any], key: str, value: float | None) -> None:
    if value is None:
        return
    bucket[key] = (bucket[key] or 0.0) + value


def energy_and_cost(inputs: HouseHistoryInputs) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The buckets (see the module) and the period's cost."""
    period, tz = inputs.period, inputs.tz
    buckets = {key: _empty_bucket(key) for key in bucket_keys(period, tz)}
    slots = _sorted_slots(inputs.prices)
    starts = [slot.start for slot in slots]
    priced_any = inputs.money is not None and bool(slots)
    bought = sold = kwh_in = kwh_out = 0.0
    priced = 0
    count = 0
    # Per bucket: the priced minutes and the prices weighted by them, for its mean buy and sell price.
    weights: dict[str, list[float]] = {}
    for item in inputs.periods or ():
        key = bucket_key(period, item.start, tz)
        bucket = buckets.get(key)
        if bucket is None:
            continue
        count += 1
        flows = allocate_hour(item)

        def used(role: str) -> float:
            return sum(kwh for (_source, use), kwh in flows.items() if use == role)  # noqa: B023 - read at once

        house, heat, car = used("house"), used("heat"), used("car")
        bucket["periods"] += 1
        _add(bucket, "grid_in", item.grid_in)
        _add(bucket, "grid_out", item.grid_out)
        _add(bucket, "solar", item.solar)
        _add(bucket, "battery_in", item.battery_in)
        _add(bucket, "battery_out", item.battery_out)
        _add(bucket, "car", car)
        _add(bucket, "heat", heat if item.heat is not None else None)
        _add(bucket, "house", house)
        _add(bucket, "use", house + heat + car)
        prices = slot_prices(item, _slots_near(slots, starts, item)) if priced_any else None
        if prices is not None:
            cost = item.grid_in * prices[0] - item.grid_out * prices[1]
            bucket["cost"] = (bucket["cost"] or 0.0) + cost
            bought += item.grid_in * prices[0]
            sold += item.grid_out * prices[1]
            kwh_in += item.grid_in
            kwh_out += item.grid_out
            priced += 1
            weight = weights.setdefault(key, [0.0, 0.0, 0.0])
            weight[0] += item.minutes
            weight[1] += prices[0] * item.minutes
            weight[2] += prices[1] * item.minutes
    if period.kind != PERIOD_DAY:
        seen: set[date] = set()
        for review in inputs.reviews or ():
            saving = review.battery_saving
            if saving is None or review.day in seen or not period.contains(review.day):
                continue
            seen.add(review.day)
            target = buckets.get(bucket_key(period, midnight_utc(review.day, tz) + timedelta(hours=12), tz))
            if target is not None:
                _add(target, "battery_saving", saving)
                target["battery_days"] += 1
        for day, day_saving in inputs.savings.items():
            if not period.contains(day):
                continue
            target = buckets.get(bucket_key(period, midnight_utc(day, tz) + timedelta(hours=12), tz))
            if target is not None:
                _add(target, "saving", day_saving.total)
                _add(target, "saving_battery", day_saving.battery)
                _add(target, "saving_car", day_saving.car)
                _add(target, "saving_heat", day_saving.heat)
    # The battery's arbitrage: a day's by its hours, a month's and a year's by their days.
    if period.kind == PERIOD_DAY:
        for hour, value in inputs.arbitrage_hours.items():
            target = buckets.get(bucket_key(period, hour, tz))
            if target is not None:
                _add(target, "arbitrage", value)
    else:
        for day, value in inputs.arbitrage_days.items():
            if period.contains(day):
                target = buckets.get(bucket_key(period, midnight_utc(day, tz) + timedelta(hours=12), tz))
                if target is not None:
                    _add(target, "arbitrage", value)
    for key, (minutes, buy, sell) in weights.items():
        if minutes > 0:
            buckets[key]["buy"] = buy / minutes
            buckets[key]["sell"] = sell / minutes
    rows = []
    for bucket in buckets.values():
        rows.append({key: (_r(value, 3) if isinstance(value, float) else value) for key, value in bucket.items()})
    cost = None
    if inputs.money is not None and inputs.periods is not None:
        cost = {
            "net": _r(bought - sold),
            "bought": _r(bought),
            "sold": _r(sold),
            "kwh_in": _r(kwh_in, 3),
            "kwh_out": _r(kwh_out, 3),
            "periods": count,
            "priced": priced,
        }
    return rows, cost


def review_block(inputs: HouseHistoryInputs) -> dict[str, Any] | None:
    """The battery's money over the period's reviewed days (see the module)."""
    if inputs.reviews is None:
        return None
    period = inputs.period
    days = sorted((review for review in inputs.reviews if period.contains(review.day)), key=lambda review: review.day)
    unique: dict[date, DayReview] = {}
    for review in days:
        unique[review.day] = review
    reviews = list(unique.values())
    if not reviews:
        return None
    today = local_date(inputs.now, inputs.tz)
    period_days = sum(1 for day in period.days() if day < today)
    battery = [review.battery_saving for review in reviews if review.battery_saving is not None]
    sun = [review.sun_value for review in reviews if review.sun_value is not None]
    nothing = [review.against_nothing for review in reviews if review.against_nothing is not None]
    modes: dict[str, Any] = {}
    for mode in MODES:
        ran = [review.mode_savings[mode] for review in reviews if mode in review.modes]
        modes[mode] = {"saving": _r(sum(ran)) if ran else None, "days": len(ran)}
    in_force = [review.mode for review in reviews if review.mode is not None]
    return {
        "days": len(reviews),
        "period_days": period_days,
        "archived": sum(1 for review in reviews if review.day in inputs.archived),
        "actual_cost": _r(sum(review.actual.cost for review in reviews)),
        "battery_saving": _r(sum(battery)) if battery else None,  # type: ignore[arg-type]
        "battery_days": len(battery),
        "sun_gave": _r(sum(sun)) if sun else None,  # type: ignore[arg-type]
        "sun_days": len(sun),
        "against_nothing": _r(sum(nothing)) if nothing else None,  # type: ignore[arg-type]
        "nothing_days": len(nothing),
        "plan_saving": _r(sum(review.saving for review in reviews)),
        "own_saving": _r(sum(review.actual_saving for review in reviews)),
        "modes": modes,
        # The mode in force on most of the reviewed days (the last one on a tie).
        "mode": max(reversed(in_force), key=in_force.count) if in_force else None,
        "by_day": [
            {
                "date": review.day.isoformat(),
                "battery_saving": None if review.battery_saving is None else _r(review.battery_saving),
                "plan_saving": _r(review.saving),
                "own_saving": _r(review.actual_saving),
                **_day_shares(review),
            }
            for review in reviews
        ],
        # The days against their perfect plans in hindsight (`site/perfect_view.py`).
        "perfect": perfect_block(reviews),
    }


#: Why a day with a perfect plan has no share: under 90 % of it measured, or too small a difference to compare.
SHARE_PARTIAL: Final = "partial"
SHARE_SMALL: Final = "small"


def _day_shares(review: DayReview) -> dict[str, Any]:
    """A day's share of its perfect plan: what really happened and the site's mode, and why there is none (`None`
    without a perfect plan)."""
    facit = review.facit
    own = None if facit is None else facit.candidates.get("own")
    plan = None if facit is None or review.mode is None else facit.candidates.get(review.mode)
    note = None
    if facit is not None and facit.saving is not None:
        note = SHARE_PARTIAL if not facit.whole else SHARE_SMALL if own is not None and own.share is None else None
    return {
        "own_share": None if own is None or own.share is None else round(own.share, 1),
        "plan_share": None if plan is None or plan.share is None else round(plan.share, 1),
        "share_note": note,
    }


def forecast_block(inputs: HouseHistoryInputs) -> dict[str, Any]:
    """Each forecast's mean daily error over the period's reviewed days."""
    out: dict[str, Any] = {}
    for name, kind in FORECAST_KINDS:
        days = [mae for day, mae in (inputs.forecast_mae.get(kind) or {}).items() if inputs.period.contains(day)]
        out[name] = {"mae": round(sum(days) / len(days), 3), "days": len(days)} if days else None
    return out


def house_history(inputs: HouseHistoryInputs) -> dict[str, Any]:
    """The whole answer (`HISTORY_KEYS`)."""
    period = inputs.period
    today = local_date(inputs.now, inputs.tz)
    buckets, cost = energy_and_cost(inputs)
    flow = None
    if inputs.periods is not None:
        flow = energy_flow(
            sorted(inputs.periods, key=lambda item: item.start),
            day=period.first,
            has_solar=inputs.has_solar,
            has_battery=inputs.has_battery,
            has_heat=inputs.has_heat,
            heat_sources=inputs.heat_sources,
            step_min=inputs.step_min,
        )
    money = inputs.money
    return {
        "api_version": HOUSE_HISTORY_API_VERSION,
        "site_id": inputs.site_id,
        "time_zone": inputs.tz,
        "generated_at": inputs.now.isoformat(),
        "period": period.kind,
        "date": period.first.isoformat(),
        "end": period.end.isoformat(),
        "today": today.isoformat(),
        "complete": period.end <= today,
        "money": None if money is None else {"currency": money[0], "major_unit": money[1], "minor_unit": money[2]},
        "energy_flow": flow,
        "buckets": buckets if inputs.periods is not None else [],
        "cost": cost,
        "review": review_block(inputs),
        "forecast": forecast_block(inputs),
        "saving": period_sums({day: item for day, item in inputs.savings.items() if period.contains(day)}),
    }
