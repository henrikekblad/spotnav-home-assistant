"""Hybrid mode's pure planning core: how much grid energy to buy, and how much
to hold back in the hope of sun, to meet a departure deadline.

Without a forecast, hybrid is plain cheapest (`grid_kwh == remaining_need_kwh`).
With one, it may hold back grid energy for expected sun, bounded by the safety
rule (never late) and the price rule (only when it pays on average). This module
chooses `grid_kwh`; `planning/planner.py` still chooses the hours. Pure: no Home
Assistant import, no I/O, no clock, no state between calls.

Safety rule:
    slack_kwh  = max(0, hours_to_departure x max_charge_kw x feasibility_margin
                        - remaining_need_kwh)
    credit_cap = min(forecast_credit_kwh x confidence, slack_kwh, remaining_need_kwh)
Holding back at most the spare buying capacity leaves time to buy whatever the
sun fails to deliver: hoping for sun can make charging dearer, never late.

Price rule: for a kWh planned at price `p`, falling back to `p_f` if the sun
fails, the expected gain of holding it back is `p - (1 - confidence) x p_f`;
hold it back only when that is at least `min_gain_fraction` of `p`. Per call:
build the full-need plan (cheapest whole slots covering the need); `p_f` is the
cheapest slot outside it from the first creditable forecast hour to departure
(none means hold back nothing); walk the plan most expensive first until a slot
fails. The rule only narrows `credit_cap`.

States, decided in this order:
`satisfied` (need <= 0) > `no_forecast` (`forecast_wh is None`) > `last_call`
(slack <= 0) > `waiting_for_sun` (zero cap, or the price rule held something
back) > `not_worth_it` (`credit_cap > 0` but nothing held back: price rule
rejected, no fallback price, or no price data). A departure at or before `now`
clamps `hours_to_departure` to 0.

Forecast credit per hour: `usable_w = max(0, forecast_wh[h]) - house_baseline_w`;
the car gets `min(usable_w, max_charge_w)` pro-rated by overlap with the window,
only if `usable_w >= solar_start_w` (checked before pro-rating, as the solar
controller cannot start below its start current). `forecast_wh is None` (no
source) differs from `{}` (nothing usable now): `{}` reads `waiting_for_sun`, so
a gap never flips the state. Naive datetimes are refused.

Known simplification: forecast solar is treated as free (`confidence` is the only
discount), which overvalues holding back on sites with a battery or paid export.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Mapping, Sequence


#: The state hybrid is in this call; see the module docstring for precedence.
HybridState = Literal["no_forecast", "waiting_for_sun", "last_call", "satisfied", "not_worth_it"]

#: Every reason a result can carry, one per branch of `plan_hybrid`.
HybridReason = Literal[
    "satisfied_need_met",
    "no_forecast_cheapest",
    "last_call_no_slack",
    "waiting_for_sun_within_slack",
    "no_price_data",
    "holding_back_does_not_pay",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class HybridConfig:
    """Hybrid's tunable parameters.

    `solar_start_w` has no default: only the caller knows the solar
    controller's start power. `confidence` both discounts the forecast and is
    the probability the price rule weighs gain against.
    """

    #: Public solar forecasts run optimistic; trust only half of one.
    confidence: float = 0.5
    #: Derate on the maximum charging power available between now and departure.
    feasibility_margin: float = 0.8
    #: What the house takes before the car sees any sun.
    house_baseline_w: float = 500.0
    #: Sun below this cannot start the car charging.
    solar_start_w: float
    #: Replan once the remaining need has changed by this much.
    replan_step_kwh: float = 1.0
    # Required expected gain as a share of the slot's price: at break-even
    # holding back buys only variance, and float error can pass a strict `>`.
    min_gain_fraction: float = 0.1


@dataclass(frozen=True, slots=True)
class HybridResult:
    """What hybrid mode currently wants, and why.

    `grid_kwh` goes to the cheapest planner in place of the full need.
    `credit_kwh` is what is held back, `forecast_credit_kwh` the raw forecast
    promise, `slack_kwh` the safety ceiling; `credit_kwh` never exceeds
    `min(forecast_credit_kwh * confidence, slack_kwh, remaining_need_kwh)`.
    """

    grid_kwh: float
    credit_kwh: float
    forecast_credit_kwh: float
    slack_kwh: float
    state: HybridState
    reason: HybridReason


@dataclass(frozen=True, slots=True)
class PriceSlot:
    """One priced interval; `price` is per kWh in the planner's unit."""

    start: datetime
    duration_s: float
    price: float


@dataclass(frozen=True, slots=True)
class _PricedCapacity:
    """A `PriceSlot` with its UTC start and the kWh it delivers at `max_charge_kw`."""

    slot: PriceSlot
    start_utc: datetime
    kwh: float


def _as_utc_instant(value: datetime, what: str) -> datetime:
    """Refuse a naive datetime and return the same instant in UTC.

    All arithmetic goes through this: datetimes sharing one `tzinfo` object
    compare by wall-clock fields, which is wrong by an hour across DST.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{what} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class _ForecastCredit:
    """Raw forecast kWh, and the start of the earliest creditable overlap (or `None`)."""

    kwh: float
    first_creditable_start: datetime | None


def _forecast_credit(
    forecast_wh: Mapping[datetime, float],
    *,
    now: datetime,
    departure: datetime,
    config: HybridConfig,
    max_charge_kw: float,
) -> _ForecastCredit:
    """The raw forecast credit for `[now, departure)`, before `confidence` and
    the caps. Every key is checked for timezone-awareness; `now` and `departure`
    arrive as UTC.
    """
    max_charge_w = max_charge_kw * 1000.0
    total_wh = 0.0
    first_creditable_start: datetime | None = None
    for hour_start, wh in forecast_wh.items():
        hour_start_utc = _as_utc_instant(hour_start, "a forecast hour")
        hour_end_utc = hour_start_utc + timedelta(hours=1)
        overlap_start = max(hour_start_utc, now)
        overlap_end = min(hour_end_utc, departure)
        if overlap_end <= overlap_start:
            # Outside the window, or the window is empty.
            continue

        forecast_pv_w = max(0.0, wh)
        usable_w = forecast_pv_w - config.house_baseline_w
        if usable_w < config.solar_start_w:
            continue

        hour_fraction = (overlap_end - overlap_start).total_seconds() / 3600.0
        car_w = min(usable_w, max_charge_w)
        total_wh += car_w * hour_fraction
        if first_creditable_start is None or overlap_start < first_creditable_start:
            first_creditable_start = overlap_start
    return _ForecastCredit(kwh=total_wh / 1000.0, first_creditable_start=first_creditable_start)


def _priced_capacities(
    price_slots: Sequence[PriceSlot],
    *,
    now: datetime,
    departure: datetime,
    max_charge_kw: float,
) -> list[_PricedCapacity]:
    """Price slots windowed to `[now, departure)`; naive starts are always refused."""
    capacities: list[_PricedCapacity] = []
    for slot in price_slots:
        start_utc = _as_utc_instant(slot.start, "a price slot")
        if not (now <= start_utc < departure):
            continue
        capacities.append(
            _PricedCapacity(
                slot=slot,
                start_utc=start_utc,
                kwh=max_charge_kw * (slot.duration_s / 3600.0),
            )
        )
    return capacities


def _cheapest_full_need_plan(
    capacities: list[_PricedCapacity], remaining_need_kwh: float
) -> list[_PricedCapacity]:
    """The cheapest whole slots covering the need (like `planner.slots_needed`).

    If capacity falls short, all of it is in the plan.
    """
    ordered = sorted(capacities, key=lambda c: (c.slot.price, c.start_utc))
    plan: list[_PricedCapacity] = []
    covered = 0.0
    for capacity in ordered:
        if covered >= remaining_need_kwh:
            break
        plan.append(capacity)
        covered += capacity.kwh
    return plan


def _fallback_price(
    capacities: list[_PricedCapacity],
    full_plan: list[_PricedCapacity],
    *,
    first_creditable_start: datetime,
    departure: datetime,
) -> float | None:
    """The cheapest price outside the full-need plan from `first_creditable_start`
    to departure (what a held-back kWh costs if the sun fails), else `None`.
    """
    claimed = {id(capacity) for capacity in full_plan}
    candidates = [
        capacity
        for capacity in capacities
        if id(capacity) not in claimed and first_creditable_start <= capacity.start_utc < departure
    ]
    if not candidates:
        return None
    return min(capacity.slot.price for capacity in candidates)


def _hold_back_from_plan(
    full_plan: list[_PricedCapacity],
    *,
    credit_cap: float,
    fallback_price: float,
    confidence: float,
    min_gain_fraction: float,
) -> float:
    """How much of `credit_cap` is worth holding back, walking the plan most
    expensive first and stopping at the first slot that fails the price test.
    """
    ordered = sorted(full_plan, key=lambda c: (-c.slot.price, c.start_utc))
    held_back = 0.0
    remaining_cap = credit_cap
    for capacity in ordered:
        if remaining_cap <= 0:
            break
        # Expected gain per kWh: p - (1 - c) * p_f.
        price = capacity.slot.price
        gain = price - (1.0 - confidence) * fallback_price
        if gain < min_gain_fraction * price or gain <= 0.0:
            break
        take = min(capacity.kwh, remaining_cap)
        held_back += take
        remaining_cap -= take
    return held_back


def plan_hybrid(
    *,
    now: datetime,
    departure: datetime,
    remaining_need_kwh: float,
    max_charge_kw: float,
    config: HybridConfig,
    price_slots: Sequence[PriceSlot],
    forecast_wh: Mapping[datetime, float] | None = None,
) -> HybridResult:
    """How much grid energy hybrid mode wants right now, and why.

    `price_slots` may be empty (giving `no_price_data`). `forecast_wh` is
    hourly PV Wh keyed by timezone-aware hour start; `None` means no source.
    See the module docstring for the rules.
    """
    now_utc = _as_utc_instant(now, "now")
    departure_utc = _as_utc_instant(departure, "departure")

    if remaining_need_kwh <= 0:
        # Checked first: it outranks every other state.
        return HybridResult(
            grid_kwh=0.0,
            credit_kwh=0.0,
            forecast_credit_kwh=0.0,
            slack_kwh=0.0,
            state="satisfied",
            reason="satisfied_need_met",
        )

    # A passed or equal deadline clamps to zero hours, driving slack to zero.
    # UTC instants, so a DST day counts 23 or 25 real hours.
    hours_to_departure = max(0.0, (departure_utc - now_utc).total_seconds() / 3600.0)
    capacity_kwh = hours_to_departure * max_charge_kw * config.feasibility_margin
    slack_kwh = max(0.0, capacity_kwh - remaining_need_kwh)

    if forecast_wh is None:
        return HybridResult(
            grid_kwh=remaining_need_kwh,
            credit_kwh=0.0,
            forecast_credit_kwh=0.0,
            slack_kwh=slack_kwh,
            state="no_forecast",
            reason="no_forecast_cheapest",
        )

    forecast = _forecast_credit(
        forecast_wh,
        now=now_utc,
        departure=departure_utc,
        config=config,
        max_charge_kw=max_charge_kw,
    )
    forecast_credit_kwh = forecast.kwh

    if slack_kwh <= 0:
        return HybridResult(
            grid_kwh=remaining_need_kwh,
            credit_kwh=0.0,
            forecast_credit_kwh=forecast_credit_kwh,
            slack_kwh=slack_kwh,
            state="last_call",
            reason="last_call_no_slack",
        )

    # The safety ceiling. `max(0.0, ...)` guards only a negative `confidence`.
    credit_cap = max(0.0, min(forecast_credit_kwh * config.confidence, slack_kwh, remaining_need_kwh))

    credit_kwh: float
    state: HybridState
    reason: HybridReason

    if credit_cap <= 0:
        # The forecast contributes nothing, so there was no trade to reject.
        credit_kwh = 0.0
        state, reason = "waiting_for_sun", "waiting_for_sun_within_slack"
    elif not price_slots:
        credit_kwh = 0.0
        state, reason = "not_worth_it", "no_price_data"
    elif forecast.first_creditable_start is None:
        # Only reachable through float edge cases; handled rather than asserted.
        credit_kwh = 0.0
        state, reason = "not_worth_it", "holding_back_does_not_pay"
    else:
        capacities = _priced_capacities(
            price_slots, now=now_utc, departure=departure_utc, max_charge_kw=max_charge_kw
        )
        full_plan = _cheapest_full_need_plan(capacities, remaining_need_kwh)
        fallback_price = _fallback_price(
            capacities,
            full_plan,
            first_creditable_start=forecast.first_creditable_start,
            departure=departure_utc,
        )
        if fallback_price is None:
            credit_kwh = 0.0
        else:
            credit_kwh = _hold_back_from_plan(
                full_plan,
                credit_cap=credit_cap,
                fallback_price=fallback_price,
                confidence=config.confidence,
                min_gain_fraction=config.min_gain_fraction,
            )
        if credit_kwh > 0:
            state, reason = "waiting_for_sun", "waiting_for_sun_within_slack"
        else:
            state, reason = "not_worth_it", "holding_back_does_not_pay"

    grid_kwh = remaining_need_kwh - credit_kwh

    return HybridResult(
        grid_kwh=grid_kwh,
        credit_kwh=credit_kwh,
        forecast_credit_kwh=forecast_credit_kwh,
        slack_kwh=slack_kwh,
        state=state,
        reason=reason,
    )


def needs_replan(previous_remaining_kwh: float, new_remaining_kwh: float, config: HybridConfig) -> bool:
    """Whether a changed remaining need warrants a replan.

    `>=`, not `>`: a change of exactly `replan_step_kwh` triggers.
    """
    return abs(previous_remaining_kwh - new_remaining_kwh) >= config.replan_step_kwh
