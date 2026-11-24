"""`spotnav/get_house_history`'s answer (API v1): one day, month or year of the house as it went (SpotNav 3.0, shadow).

Pure: the shell (`api/house_history.py`) reads the periods from the recorder and hands them over with the prices,
the battery's reviewed days and the forecasts' daily reviews; nothing is read or written here.

    {api_version: 1, site_id, time_zone, generated_at, period: "day" | "month" | "year", date (the period's first
     day), end (the first day after it), today, complete, money: {currency, major_unit, minor_unit} | null,
     energy_flow: (as `spotnav/get_house`'s, `site/energy_flow.py`) | null,
     buckets: [{key, grid_in, grid_out, solar, battery_in, battery_out, car, heat, house, use, cost, periods,
                battery_saving, battery_days, saving, saving_battery, saving_car, saving_heat, arbitrage, buy,
                sell, own_battery, would_battery, sun_gave}],
     cost: {net, bought, sold, kwh_in, kwh_out, periods, priced} | null,
     review: {days, period_days, archived, actual_cost, battery_saving, battery_days, sun_gave, sun_days,
              against_nothing, nothing_days, plan_saving, own_saving, modes: {mode: {saving, days}}, mode,
              by_day: [{date, battery_saving, plan_saving, own_saving, own_share, plan_share, share_note}],
              perfect: (`site/perfect_view.py`) | null} | null,
     forecast: {house, sun, sun_corrected, soc: {mae, days} | null},
     saving: {total, battery, car, heat, battery_days, car_days, heat_days, days} | null,
     own_control: {earned, would, days, would_days} | null,
     sun: {gave, days, period_days, estimated_days} | null }

* **Energy** (`energy_flow`, `buckets`): the Energy dashboard's meters and the chargers' registers per period (a
  day within the recorder's short-term statistics by the quarter hour, else by the hour), allocated period by
  period by the energy flow's one rule and summed over the day, month or year, so a month's flow is the sum of its
  hours' flows, never a month's totals allocated at once (that would let the midday sun charge the night's battery).
  A bucket is an hour of a day, a day of a month or a month of a year (`site/history_period.py`): the meters as read
  (`grid_in`, `grid_out`, `solar`, `battery_in`, `battery_out`; a battery read at the battery as its own counters
  say, so a hybrid inverter's loss and standing use are in the house, and the period's flow says them apart in
  `inverter_kwh`, as today's does, `site/energy_flow.inverter_use`) and the uses as the flow allocates them (`house` the
  balance, `heat`, `car`; where the meters disagree the flow takes the shortfall off them), `use` their sum. `null` without the dashboard's grid meter.
  A bucket's `battery_saving` is the battery's money over the reviewed days in it (the review's `battery_saving`,
  what the day cost against the same day with no battery; negative: the battery made it dearer), over
  `battery_days` of them; `null` and 0 where no day of the bucket is reviewed, and always for the hours of a day (the
  review is a whole day's). The card's bar is `cost` filled and `cost + battery_saving` whole. A bucket's
  `arbitrage` is what the battery earned in it, the house card's headline measure (`battery/stored_value.py`, the same
  as the battery's own app shows): an hour of a day from the day's hourly tally (kept 40 days), a day or a month from
  the days' totals (kept 400 days); `null` without a battery or where nothing is kept.
  A bucket's `battery_flow` says what its arbitrage is made of, for the card's readout: `{discharged, worth, cost,
  charged, charge_cost}` (kWh, and minor units), the energy given back, what it was worth (the buy or sell price),
  what it cost out of the stock (worth less cost is the arbitrage), and the energy charged with its cost (counted as
  it is used, in a later hour's cost); the same retention as `arbitrage` (an hour also where it only charged); `null`
  where not kept, and for a month or year where any of its days with a battery figure lacks it.
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
  `small`, the perfect plan saved under 5 kr; or `loss` beside an `own_share` of 0: the battery's own way cost more
  than no battery that day, which the card says instead of "0 %"); `perfect` the period's days against their perfect plans (`site/perfect_view.py`: the shares summed
  over the period, where the day boundary washes out). `null` without a battery or with no reviewed day.
* **What SpotNav saved** (`saving`): the battery, the car and the heat each against its own counterfactual
  (`site/spotnav_saving.py`), per day summed over the period: `{total, battery, car, heat, battery_days, car_days,
  heat_days, days}` (each part over the days that have it, `days` the days with any part); `null` without any. A
  month's or a year's bucket has the same sums (`saving`, `saving_battery`, `saving_car`, `saving_heat`, `null` where
  none of its days has the part). A day's hours have the battery's part by the hour it was earned in (the hour's
  arbitrage in the day's controlled share; kept 40 days, `null` on an older day) and the car's in the hour its charge
  started; the heat's is a whole day's estimate and stays in the period's sums alone (`saving_heat` `null` on an
  hour), so a day's `saving.total` may hold more than its hours' `saving` add up to.
* **The sun's value** (bucket `sun_gave`): the reviews' `sun_value` (`battery/review_baselines.sun_gave`: the sun's
  energy the house used at the buying price, plus what was sold, over the quarter hours with a production figure)
  over the bucket's reviewed days that have it, for the card's Earnings tab; the period's sum is the review's
  `sun_gave` (the same reviews, so the two never disagree). `null` where no day of the bucket has it, without the
  sun's production, and on a battery site's day's hours (the review is a whole day's). Never inside `saving`: it is
  the sun's, not something SpotNav did.
* **The sun's value without a battery** (`sun`, and bucket `sun_gave`): on a site with the sun and no home battery
  (no battery review) each day before today is valued with the same formula and the battery's term 0
  (`site/sun_value.py`): the nightly sun review's quarter-hour figure where it has the day (`site/sun_review.py`, from
  the 5-minute statistics; its hours kept 40 days, the day 400), else from the periods read here, by the quarter hour
  within the short-term statistics and by the hour past them (an estimate: import and export within an hour net
  against each other; `estimated_days` counts those days). A bucket has the sum of its days, and a day's hours their
  own figure (none where only the day's figure is kept). `sun`: `{gave, days, period_days, estimated_days}` over the
  period's valued days, `period_days` the period's days before today; `null` on a battery site (its `review` has the
  sun's value), without the sun or with no valued day.
* **Before SpotNav controls the battery** (`own_control`, `site/spotnav_saving.own_control`): over the days (or the
  share of a day) the battery ran under its own control, what it earned (`earned`, over `days`) and what SpotNav's
  plan would have earned on the same days (`would`: `earned` plus what the nightly reviews say the plan would have
  done better or worse, known for `would_days` of them; `null` with none reviewed); `null` where SpotNav controlled
  throughout or without a battery. A bucket has the same two (`own_battery`, `would_battery`; a day without its
  review counts as it went in both, so the two are over the same days; an hour of a day has `own_battery` alone, the
  review being a whole day's). Never inside `saving`: nothing was done.
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
from .spotnav_saving import DaySaving, own_control, period_sums
from .sun_value import BASIS_HOUR, SunDay, day_values, period_value
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
    "own_control",
    "sun",
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
    "battery_flow",
    "buy",
    "sell",
    "own_battery",
    "would_battery",
    "sun_gave",
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
    #: What the arbitrage is made of (`battery/stored_value.Flow`), per local day and per UTC hour; a day not kept
    #: with it is missing from `flow_days`.
    flow_days: Mapping[date, Any] = field(default_factory=dict)
    flow_hours: Mapping[datetime, Any] = field(default_factory=dict)
    #: The car's saving per UTC hour its charges started in (`site/spotnav_saving.car_hours`), for a day's hours.
    car_hours: Mapping[datetime, float] = field(default_factory=dict)
    #: On a site with the sun and no home battery, the nightly sun review's days (`site/sun_review.py`; the days
    #: without one are valued from `periods`); `None` elsewhere (no sun value of its own).
    sun_stored: Mapping[date, SunDay] | None = None


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
        "battery_flow": None,
        "buy": None,
        "sell": None,
        "own_battery": None,
        "would_battery": None,
        "sun_gave": None,
    }


def _flow_sum(total: dict[str, float] | None, flow: Any) -> dict[str, float]:
    """`total` with `flow` (`battery/stored_value.Flow`) added, as the bucket's `battery_flow`."""
    out = total or {"discharged": 0.0, "worth": 0.0, "cost": 0.0, "charged": 0.0, "charge_cost": 0.0}
    out["discharged"] += flow.discharged
    out["worth"] += flow.worth
    out["cost"] += flow.cost
    out["charged"] += flow.charged
    out["charge_cost"] += flow.charge_cost
    return out


def _add(bucket: dict[str, Any], key: str, value: float | None) -> None:
    if value is None:
        return
    bucket[key] = (bucket[key] or 0.0) + value


def energy_and_cost(
    inputs: HouseHistoryInputs,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[date, SunDay]]:
    """The buckets (see the module), the period's cost and, without a battery, each day's sun value."""
    period, tz = inputs.period, inputs.tz
    today = local_date(inputs.now, tz)
    valued: list[tuple[EnergyHour, float]] = []
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
            if inputs.sun_stored is not None and local_date(item.start, tz) < today:
                sun_value = period_value(item, prices[0], prices[1])
                if sun_value is not None:
                    valued.append((item, sun_value))
    sun_days = _own_sun_days(inputs, valued)
    _add_sun(inputs, buckets, sun_days)
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
        # The sun's value of each reviewed day (one review per day, as the period's `review` block counts them).
        for day, sun in _sun_days(inputs).items():
            target = buckets.get(bucket_key(period, midnight_utc(day, tz) + timedelta(hours=12), tz))
            if target is not None:
                _add(target, "sun_gave", sun)
        for day, day_saving in inputs.savings.items():
            if not period.contains(day):
                continue
            target = buckets.get(bucket_key(period, midnight_utc(day, tz) + timedelta(hours=12), tz))
            if target is not None:
                _add(target, "saving", day_saving.total)
                _add(target, "saving_battery", day_saving.battery)
                _add(target, "saving_car", day_saving.car)
                _add(target, "saving_heat", day_saving.heat)
    # The battery's arbitrage: a day's by its hours, a month's and a year's by their days. Beside it, the share of it
    # that is SpotNav's doing (a day's hours; a month's and a year's buckets have it above) and the battery's own.
    if inputs.has_battery and period.kind == PERIOD_DAY:
        day_saving = inputs.savings.get(period.first)
        share = None if day_saving is None else day_saving.battery_share
        for hour, value in inputs.arbitrage_hours.items():
            target = buckets.get(bucket_key(period, hour, tz))
            if target is not None:
                _add(target, "arbitrage", value)
                if share is not None and share > 0:
                    _add(target, "saving_battery", value * share)
                    _add(target, "saving", value * share)
                if share is not None and share < 1:
                    _add(target, "own_battery", value * (1.0 - share))
        for hour, flow in inputs.flow_hours.items():
            target = buckets.get(bucket_key(period, hour, tz))
            if target is not None:
                target["battery_flow"] = _flow_sum(target["battery_flow"], flow)
    elif inputs.has_battery:
        broken: set[str] = set()
        for day, value in inputs.arbitrage_days.items():
            if period.contains(day):
                target = buckets.get(bucket_key(period, midnight_utc(day, tz) + timedelta(hours=12), tz))
                if target is not None:
                    _add(target, "arbitrage", value)
                    flow = inputs.flow_days.get(day)
                    if flow is None:
                        broken.add(target["key"])
                    else:
                        target["battery_flow"] = _flow_sum(target["battery_flow"], flow)
        for key in broken:
            buckets[key]["battery_flow"] = None
        # A day without its review counts as it went, so a bucket's two sums are over the same days; a bucket with
        # no reviewed day has no `would_battery`.
        reviewed: set[str] = set()
        for day, (own, would) in own_control_days(inputs).items():
            target = buckets.get(bucket_key(period, midnight_utc(day, tz) + timedelta(hours=12), tz))
            if target is not None:
                _add(target, "own_battery", own)
                target["would_battery"] = (target["would_battery"] or 0.0) + (own if would is None else would)
                if would is not None:
                    reviewed.add(target["key"])
        for bucket in buckets.values():
            if bucket["key"] not in reviewed:
                bucket["would_battery"] = None
    # The car's saving in the hour its charge started (a day's hours; the day must have the car's figure at all).
    if period.kind == PERIOD_DAY:
        day_saving = inputs.savings.get(period.first)
        if day_saving is not None and day_saving.car is not None:
            for hour, value in inputs.car_hours.items():
                target = buckets.get(bucket_key(period, hour, tz))
                if target is not None:
                    _add(target, "saving_car", value)
                    _add(target, "saving", value)
    for key, (minutes, buy, sell) in weights.items():
        if minutes > 0:
            buckets[key]["buy"] = buy / minutes
            buckets[key]["sell"] = sell / minutes
    rows = []
    for bucket in buckets.values():
        row = {key: (_r(value, 3) if isinstance(value, float) else value) for key, value in bucket.items()}
        if row["battery_flow"] is not None:
            row["battery_flow"] = {key: _r(value, 3) for key, value in row["battery_flow"].items()}
        rows.append(row)
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
    return rows, cost, sun_days


def _own_sun_days(inputs: HouseHistoryInputs, valued: Sequence[tuple[EnergyHour, float]]) -> dict[date, SunDay]:
    """Without a battery, each of the period's days before today with a sun value: the nightly review's where kept,
    else from the periods read (see the module)."""
    stored = inputs.sun_stored
    if stored is None:
        return {}
    today = local_date(inputs.now, inputs.tz)
    found = day_values(valued, inputs.tz)
    for day, item in stored.items():
        if inputs.period.contains(day) and day < today:
            found[day] = item
    return {day: found[day] for day in sorted(found) if inputs.period.contains(day)}


def _add_sun(inputs: HouseHistoryInputs, buckets: dict[str, dict[str, Any]], days: Mapping[date, SunDay]) -> None:
    """The days' sun values into their buckets: a day's hours where it has them, a month's and a year's by day."""
    period, tz = inputs.period, inputs.tz
    for day, item in days.items():
        if period.kind == PERIOD_DAY:
            for hour, value in item.hours.items():
                target = buckets.get(bucket_key(period, hour, tz))
                if target is not None:
                    _add(target, "sun_gave", value)
            continue
        target = buckets.get(bucket_key(period, midnight_utc(day, tz) + timedelta(hours=12), tz))
        if target is not None:
            _add(target, "sun_gave", item.value)


def sun_block(inputs: HouseHistoryInputs, days: Mapping[date, SunDay]) -> dict[str, Any] | None:
    """The period's sun value without a battery (see the module); `None` without a valued day."""
    if not days:
        return None
    today = local_date(inputs.now, inputs.tz)
    return {
        "gave": _r(sum(item.value for item in days.values())),
        "days": len(days),
        "period_days": sum(1 for day in inputs.period.days() if day < today),
        "estimated_days": sum(1 for item in days.values() if item.basis == BASIS_HOUR),
    }


def _period_reviews(inputs: HouseHistoryInputs) -> list[DayReview]:
    """The period's reviewed days, one per day (the last one read for a day wins), by day."""
    period = inputs.period
    days = sorted((review for review in inputs.reviews or () if period.contains(review.day)), key=lambda review: review.day)
    unique: dict[date, DayReview] = {}
    for review in days:
        unique[review.day] = review
    return list(unique.values())


def _sun_days(inputs: HouseHistoryInputs) -> dict[date, float]:
    """Each reviewed day's sun value (`DayReview.sun_value`), the days without it left out."""
    return {review.day: review.sun_value for review in _period_reviews(inputs) if review.sun_value is not None}


def own_control_days(inputs: HouseHistoryInputs) -> dict[date, tuple[float, float | None]]:
    """Per day of the period the battery ran (in part) under its own control: what it earned there and what SpotNav's
    plan would have (`site/spotnav_saving.own_control`; `None` without the day's review)."""
    if not inputs.has_battery:
        return {}
    reviews = {review.day: review for review in inputs.reviews or ()}
    out: dict[date, tuple[float, float | None]] = {}
    for day, value in inputs.arbitrage_days.items():
        saving = inputs.savings.get(day)
        if not inputs.period.contains(day) or saving is None:
            continue
        own, would = own_control(value, saving.battery_share, reviews.get(day))
        if own is not None:
            out[day] = (own, would)
    return out


def own_control_block(inputs: HouseHistoryInputs) -> dict[str, Any] | None:
    """The period's `own_control` (see the module); `None` without a day under the battery's own control."""
    days = own_control_days(inputs)
    if not days:
        return None
    known = [would for _own, would in days.values() if would is not None]
    # A day without its review counts as it went, so the two sums are over the same days.
    would = sum(own if would is None else would for own, would in days.values())
    return {
        "earned": _r(sum(own for own, _would in days.values())),
        "would": _r(would) if known else None,
        "days": len(days),
        "would_days": len(known),
    }


def review_block(inputs: HouseHistoryInputs) -> dict[str, Any] | None:
    """The battery's money over the period's reviewed days (see the module)."""
    if inputs.reviews is None:
        return None
    period = inputs.period
    reviews = _period_reviews(inputs)
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
#: The battery's own way cost more than no battery that day: its share stands at 0 %, the card says "loss".
SHARE_LOSS: Final = "loss"


def _day_shares(review: DayReview) -> dict[str, Any]:
    """A day's share of its perfect plan: what really happened and the site's mode, and why there is none (`None`
    without a perfect plan)."""
    facit = review.facit
    own = None if facit is None else facit.candidates.get("own")
    plan = None if facit is None or review.mode is None else facit.candidates.get(review.mode)
    note = None
    if facit is not None and facit.saving is not None:
        note = SHARE_PARTIAL if not facit.whole else SHARE_SMALL if own is not None and own.share is None else None
        if note is None and own is not None and own.share is not None and own.saving < 0:
            note = SHARE_LOSS
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
    buckets, cost, sun_days = energy_and_cost(inputs)
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
        "own_control": own_control_block(inputs),
        "sun": sun_block(inputs, sun_days),
    }
