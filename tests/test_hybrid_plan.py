"""The pure hybrid-mode planning core (`planning/hybrid_plan.py`).

No test sleeps: every clock is an explicit `datetime` passed to `plan_hybrid`. The "steep" scenarios
pin the price rule: holding back grid energy whenever the safety rule (time) allows it, regardless
of price, loses money against a Nordic-shaped price curve.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.planning.hybrid_plan import (
    HybridConfig,
    PriceSlot,
    plan_hybrid,
    needs_replan,
)

UTC = timezone.utc
START = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)


def _cfg(**overrides) -> HybridConfig:
    return HybridConfig(solar_start_w=1500.0, **overrides)


def _uniform_price_slots(start: datetime, count: int, *, price: float = 5.0) -> tuple[PriceSlot, ...]:
    """`count` hourly slots from `start`, all at the same `price` -- for
    tests where the price *rule* itself is not what is being tested: with
    every slot tied, the rule passes trivially for any positive confidence
    (see the module docstring's threshold), so the safety-rule numbers these
    tests pin are unaffected by it."""
    return tuple(PriceSlot(start=start + timedelta(hours=h), duration_s=3600.0, price=price) for h in range(count))


# --- the safety rule, as a closed-loop replay -------------------------------
#
# The design's own test: a day, hour by hour. At each hour, `plan_hybrid`
# says how much grid energy is still wanted (`grid_kwh`); that target is
# handed to a tiny stand-in for the existing cheapest planner, which buys it
# in whichever of the *remaining* hours before departure are cheapest right
# now (re-decided every tick, exactly as a real replan would); the car also
# receives whatever real solar is actually available that hour -- which may
# or may not match what was forecast.
#
# Two price shapes, because the price rule's whole point is that the answer
# depends on the *shape* of the curve, not only on whether the forecast is
# right:
#
# * "steep" (night 1.0, dear-evening 10.0, everything else 5.0) is the exact
#   curve the first version of this module was replayed against, and the
#   one that exposed the price rule's necessity: a public forecast's
#   optimism does not by itself justify giving up a cheap night hour for a
#   forecast that might fail and fall back to a dear one.
# * "flat" (night 3.0, day 5.0) has the same qualitative shape but a far
#   smaller spread -- the trade clears the price rule's bar at the same
#   `confidence`, and hybrid does hold back.


MAX_CHARGE_KW = 3.0
INITIAL_NEED_KWH = 9.0

# A forecast promising ~4.1 kW of PV for five midday hours -- comfortably
# above `solar_start_w` once the house's own baseline is subtracted, and
# capped at `MAX_CHARGE_KW` per hour by the credit formula itself.
SUNNY_FORECAST = {START + timedelta(hours=h): 4000.0 for h in range(10, 15)}


def _price_steep(hour_index: int) -> float:
    if hour_index in (2, 3, 4, 5):
        return 1.0
    if hour_index in (22, 23):
        return 10.0
    return 5.0


def _price_flat(hour_index: int) -> float:
    return 3.0 if hour_index < 6 else 5.0


def _real_solar_sunny(hour_index: int) -> float:
    """The sun delivers exactly what the forecast promised: 3.5 kW usable
    for the five forecast hours (capped by the car at `MAX_CHARGE_KW`
    downstream), nothing else."""
    return 3.5 if 10 <= hour_index < 15 else 0.0


def _real_solar_none(hour_index: int) -> float:
    return 0.0


def _replay(*, price_fn, forecast_wh, real_solar_kw_at, confidence: float = 0.5):
    """Run the day, return `(final_remaining_kwh, total_grid_kwh,
    total_cost, states_seen)`.

    `real_solar_kw_at(hour_index)` is the *actual* solar power the car could
    draw that hour (already past the house baseline and the start
    threshold) -- the ground truth, independent of what `forecast_wh`
    promised. Every hour: replan, decide (from the current `grid_kwh`
    target) how many of the cheapest remaining hours are needed and whether
    "now" is one of them, then deliver energy -- full current in a planned
    grid hour (design: "sun or not"), with real solar offsetting only how
    much of that hour's draw is actually bought; opportunistic real solar
    outside a planned hour, with no grid bought at all.
    """
    cfg = _cfg(confidence=confidence)
    prices = {h: price_fn(h) for h in range(24)}
    price_slots = tuple(
        PriceSlot(start=START + timedelta(hours=h), duration_s=3600.0, price=prices[h]) for h in range(24)
    )
    remaining = INITIAL_NEED_KWH
    total_grid_kwh = 0.0
    total_cost = 0.0
    states_seen: set[str] = set()

    for hour_index in range(24):
        if remaining <= 1e-9:
            break
        now = START + timedelta(hours=hour_index)
        departure = START + timedelta(hours=24)
        result = plan_hybrid(
            now=now,
            departure=departure,
            remaining_need_kwh=remaining,
            max_charge_kw=MAX_CHARGE_KW,
            config=cfg,
            price_slots=price_slots,
            forecast_wh=forecast_wh,
        )
        states_seen.add(result.state)
        grid_target = result.grid_kwh
        hours_needed = (
            math.ceil(grid_target / MAX_CHARGE_KW - 1e-9) if grid_target > 1e-9 else 0
        )
        remaining_hours = sorted(range(hour_index, 24), key=lambda h: (prices[h], h))
        selected = set(remaining_hours[:hours_needed])

        real_solar_kw = real_solar_kw_at(hour_index)
        if hour_index in selected:
            deliver = min(MAX_CHARGE_KW, remaining)
            solar_offset = min(real_solar_kw, deliver)
            grid_bought = deliver - solar_offset
            remaining -= deliver
            total_grid_kwh += grid_bought
            total_cost += grid_bought * prices[hour_index]
        else:
            deliver = min(real_solar_kw, MAX_CHARGE_KW, remaining)
            remaining -= deliver

    return remaining, total_grid_kwh, total_cost, states_seen


# --- steep prices: the price rule holds back nothing -------------------------


def test_replay_steep_prices_meets_deadline_in_all_three_scenarios():
    rem_cheapest, *_ = _replay(price_fn=_price_steep, forecast_wh=None, real_solar_kw_at=_real_solar_none)
    rem_sunny, *_ = _replay(price_fn=_price_steep, forecast_wh=SUNNY_FORECAST, real_solar_kw_at=_real_solar_sunny)
    rem_overcast, *_ = _replay(price_fn=_price_steep, forecast_wh=SUNNY_FORECAST, real_solar_kw_at=_real_solar_none)
    for label, remaining in (("cheapest", rem_cheapest), ("sunny", rem_sunny), ("overcast", rem_overcast)):
        assert remaining == pytest.approx(0.0, abs=1e-6), f"{label}: need must be met by departure"


def test_replay_steep_prices_price_rule_holds_back_nothing():
    """The exact scenario that exposed the flaw: night 1.0, dear evening
    10.0, everything else 5.0. Against this curve the price rule rejects
    every hold-back at `confidence = 0.5` (`1.0 <= (1 - 0.5) x 5.0 = 2.5`),
    so hybrid buys exactly what cheapest buys, sun or not."""
    rem_c, grid_c, cost_c, _ = _replay(price_fn=_price_steep, forecast_wh=None, real_solar_kw_at=_real_solar_none)
    rem_s, grid_s, cost_s, states_s = _replay(
        price_fn=_price_steep, forecast_wh=SUNNY_FORECAST, real_solar_kw_at=_real_solar_sunny
    )
    rem_o, grid_o, cost_o, states_o = _replay(
        price_fn=_price_steep, forecast_wh=SUNNY_FORECAST, real_solar_kw_at=_real_solar_none
    )
    print(
        f"steep: cheapest grid={grid_c} cost={cost_c}; sunny grid={grid_s} cost={cost_s}; "
        f"overcast grid={grid_o} cost={cost_o}"
    )
    assert grid_c == pytest.approx(9.0) and cost_c == pytest.approx(9.0)
    assert grid_s == pytest.approx(grid_c), "the price rule holds back nothing, so sunny buys exactly what cheapest buys"
    assert cost_s == pytest.approx(cost_c)
    assert grid_o == pytest.approx(grid_c)
    assert cost_o == pytest.approx(cost_c), "an overcast day costs exactly cheapest's cost -- the design's own required result"
    assert "not_worth_it" in states_s | states_o, "the price rule must actually have been consulted, and rejected"


# --- flat prices: the price rule does hold back -------------------------------


def test_replay_flat_prices_meets_deadline_in_all_three_scenarios():
    rem_cheapest, *_ = _replay(price_fn=_price_flat, forecast_wh=None, real_solar_kw_at=_real_solar_none)
    rem_sunny, *_ = _replay(price_fn=_price_flat, forecast_wh=SUNNY_FORECAST, real_solar_kw_at=_real_solar_sunny)
    rem_overcast, *_ = _replay(price_fn=_price_flat, forecast_wh=SUNNY_FORECAST, real_solar_kw_at=_real_solar_none)
    for label, remaining in (("cheapest", rem_cheapest), ("sunny", rem_sunny), ("overcast", rem_overcast)):
        assert remaining == pytest.approx(0.0, abs=1e-6), f"{label}: need must be met by departure"


def test_replay_flat_prices_sunny_costs_less_than_cheapest():
    _, grid_c, cost_c, _ = _replay(price_fn=_price_flat, forecast_wh=None, real_solar_kw_at=_real_solar_none)
    _, grid_s, cost_s, states_s = _replay(
        price_fn=_price_flat, forecast_wh=SUNNY_FORECAST, real_solar_kw_at=_real_solar_sunny
    )
    print(f"flat: cheapest grid={grid_c} cost={cost_c}; sunny grid={grid_s} cost={cost_s}")
    assert grid_s < grid_c
    assert cost_s < cost_c
    assert "waiting_for_sun" in states_s, "the price rule must actually have held something back"


def test_replay_flat_prices_overcast_is_dearer_but_still_on_time():
    rem_o, _, cost_o, _ = _replay(price_fn=_price_flat, forecast_wh=SUNNY_FORECAST, real_solar_kw_at=_real_solar_none)
    _, _, cost_c, _ = _replay(price_fn=_price_flat, forecast_wh=None, real_solar_kw_at=_real_solar_none)
    assert rem_o == pytest.approx(0.0, abs=1e-6), "holding back for sun that never came must still meet the deadline"
    assert cost_o > cost_c, "but the hours it fell back to buying in were dearer"


# --- the property that matters: confidence matching forecast accuracy --------


def test_mean_cost_does_not_exceed_cheapest_when_confidence_matches_accuracy():
    """The property the price rule exists to guarantee: over a set of days
    where the forecast is right on exactly a fraction `p` of them, and
    `confidence == p`, hybrid's *mean* cost across those days must not
    exceed plain cheapest's -- across both price shapes, even though a
    single overcast day can cost more than cheapest (see the test above).

    `p = 0.5` here: one sunny day, one overcast day, `confidence = 0.5`.

    Numbers (see the printed line when run with `-s`):

    * steep: cheapest 9.0; hybrid sunny 9.0, overcast 9.0 -> mean 9.0 <= 9.0
      (the price rule made hybrid identical to cheapest on this curve, so
      the property holds with equality).
    * flat: cheapest 27.0; hybrid sunny 9.0, overcast 39.0 -> mean 24.0 <
      27.0 (holding back sometimes costs more than cheapest, but pays for
      itself on average at the confidence the caller actually configured).
    """
    for label, price_fn, expected_cheapest, expected_sunny, expected_overcast in (
        ("steep", _price_steep, 9.0, 9.0, 9.0),
        ("flat", _price_flat, 27.0, 9.0, 39.0),
    ):
        _, _, cost_cheapest, _ = _replay(
            price_fn=price_fn, forecast_wh=None, real_solar_kw_at=_real_solar_none, confidence=0.5
        )
        _, _, cost_sunny, _ = _replay(
            price_fn=price_fn, forecast_wh=SUNNY_FORECAST, real_solar_kw_at=_real_solar_sunny, confidence=0.5
        )
        _, _, cost_overcast, _ = _replay(
            price_fn=price_fn, forecast_wh=SUNNY_FORECAST, real_solar_kw_at=_real_solar_none, confidence=0.5
        )
        assert cost_cheapest == pytest.approx(expected_cheapest)
        assert cost_sunny == pytest.approx(expected_sunny)
        assert cost_overcast == pytest.approx(expected_overcast)

        mean_hybrid = (cost_sunny + cost_overcast) / 2  # p = 0.5: one day of each
        print(f"{label}: cheapest={cost_cheapest} hybrid_mean={mean_hybrid} (sunny={cost_sunny}, overcast={cost_overcast})")
        assert mean_hybrid <= cost_cheapest, f"{label}: mean hybrid cost must not exceed cheapest's"


# --- not worth it, and the missing-price case --------------------------------


def test_not_worth_it_when_the_price_rule_rejects_every_slot():
    cfg = _cfg()
    price_slots = tuple(
        PriceSlot(start=START + timedelta(hours=h), duration_s=3600.0, price=_price_steep(h)) for h in range(24)
    )
    result = plan_hybrid(
        now=START,
        departure=START + timedelta(hours=24),
        remaining_need_kwh=9.0,
        max_charge_kw=MAX_CHARGE_KW,
        config=cfg,
        price_slots=price_slots,
        forecast_wh=SUNNY_FORECAST,
    )
    assert result.credit_kwh == 0.0
    assert result.grid_kwh == pytest.approx(9.0)
    assert result.forecast_credit_kwh > 0.0, "the forecast itself is not the reason -- the price rule is"
    assert result.state == "not_worth_it"
    assert result.reason == "holding_back_does_not_pay"


def test_not_worth_it_when_there_is_no_price_data():
    cfg = _cfg()
    result = plan_hybrid(
        now=START,
        departure=START + timedelta(hours=24),
        remaining_need_kwh=9.0,
        max_charge_kw=MAX_CHARGE_KW,
        config=cfg,
        price_slots=(),
        forecast_wh=SUNNY_FORECAST,
    )
    assert result.credit_kwh == 0.0
    assert result.grid_kwh == pytest.approx(9.0)
    assert result.state == "not_worth_it"
    assert result.reason == "no_price_data"


def test_price_slots_entirely_outside_the_window_behave_like_no_fallback():
    """`price_slots` is non-empty, but every slot is before `now` -- after
    windowing there is nothing to evaluate the trade against, the same
    outcome as `no_price_data` in substance, reported through the fallback
    search finding nothing rather than the input being literally empty."""
    cfg = _cfg()
    now = START + timedelta(hours=24)
    departure = now + timedelta(hours=5)
    stale_slots = tuple(
        PriceSlot(start=START + timedelta(hours=h), duration_s=3600.0, price=1.0) for h in range(24)
    )
    forecast = {now + timedelta(hours=h): 4000.0 for h in range(5)}
    result = plan_hybrid(
        now=now,
        departure=departure,
        remaining_need_kwh=5.0,
        max_charge_kw=MAX_CHARGE_KW,
        config=cfg,
        price_slots=stale_slots,
        forecast_wh=forecast,
    )
    assert result.credit_kwh == 0.0
    assert result.state == "not_worth_it"
    assert result.reason == "holding_back_does_not_pay"


def test_naive_price_slot_start_is_refused():
    cfg = _cfg()
    forecast = {START + timedelta(hours=h): 4000.0 for h in range(5)}
    bad_slots = (PriceSlot(start=datetime(2026, 1, 10, 0, 0), duration_s=3600.0, price=5.0),)  # naive
    with pytest.raises(ValueError):
        plan_hybrid(
            now=START,
            departure=START + timedelta(hours=5),
            remaining_need_kwh=5.0,
            max_charge_kw=MAX_CHARGE_KW,
            config=cfg,
            price_slots=bad_slots,
            forecast_wh=forecast,
        )


# --- the start threshold -----------------------------------------------------


def test_thin_forecast_below_start_threshold_credits_nothing():
    cfg = _cfg(house_baseline_w=500.0)
    # 1999 W every hour of a full day: usable_w = 1999 - 500 = 1499 < 1500,
    # every single hour, however many kWh that adds up to over 24 hours.
    forecast = {START + timedelta(hours=h): 1999.0 for h in range(24)}
    result = plan_hybrid(
        now=START,
        departure=START + timedelta(hours=24),
        remaining_need_kwh=5.0,
        max_charge_kw=MAX_CHARGE_KW,
        config=cfg,
        price_slots=(),  # unreached: credit_cap is 0 before price data is ever consulted
        forecast_wh=forecast,
    )
    assert result.forecast_credit_kwh == 0.0
    assert result.credit_kwh == 0.0
    assert result.grid_kwh == pytest.approx(5.0)
    assert result.state == "waiting_for_sun"


# --- slack caps the credit ---------------------------------------------------


def test_huge_forecast_with_little_time_left_credits_no_more_than_slack():
    cfg = _cfg()
    now = START
    departure = START + timedelta(hours=5)
    # An enormous forecast for all five remaining hours -- credit before the
    # slack cap would be 5 hours x 3 kW (the per-hour cap) x 0.5 confidence
    # = 7.5 kWh, far more than the slack below.
    forecast = {START + timedelta(hours=h): 100_000.0 for h in range(5)}
    # A uniform price so the price rule passes trivially (see
    # `_uniform_price_slots`) -- this test is about the slack cap, not price.
    price_slots = _uniform_price_slots(now, 5)
    remaining_need_kwh = 11.5  # capacity = 5 * 3 * 0.8 = 12 -> slack = 0.5
    result = plan_hybrid(
        now=now,
        departure=departure,
        remaining_need_kwh=remaining_need_kwh,
        max_charge_kw=MAX_CHARGE_KW,
        config=cfg,
        price_slots=price_slots,
        forecast_wh=forecast,
    )
    assert result.slack_kwh == pytest.approx(0.5)
    assert result.forecast_credit_kwh * cfg.confidence > result.slack_kwh, "the cap must actually engage"
    assert result.credit_kwh == pytest.approx(0.5)
    assert result.grid_kwh == pytest.approx(11.0)


# --- last call ---------------------------------------------------------------


def test_last_call_when_time_left_barely_covers_the_need():
    cfg = _cfg()
    now = START
    departure = START + timedelta(hours=3)
    # capacity = 3 * 3 * 0.8 = 7.2 -- set the need to exactly that, so slack
    # is precisely zero, with a forecast configured (so this is genuinely
    # `last_call`, not `no_forecast`).
    forecast = {START + timedelta(hours=h): 4000.0 for h in range(3)}
    result = plan_hybrid(
        now=now,
        departure=departure,
        remaining_need_kwh=7.2,
        max_charge_kw=MAX_CHARGE_KW,
        config=cfg,
        price_slots=(),  # unreached: last_call returns before price data is consulted
        forecast_wh=forecast,
    )
    assert result.slack_kwh == pytest.approx(0.0)
    assert result.credit_kwh == 0.0
    assert result.grid_kwh == pytest.approx(7.2)
    assert result.state == "last_call"


# --- no forecast is exactly cheapest -----------------------------------------


def test_no_forecast_is_exactly_cheapest():
    cfg = _cfg()
    result = plan_hybrid(
        now=START,
        departure=START + timedelta(hours=10),
        remaining_need_kwh=6.0,
        max_charge_kw=MAX_CHARGE_KW,
        config=cfg,
        price_slots=(),  # unreached: no forecast means no price rule either
        forecast_wh=None,
    )
    assert result.grid_kwh == pytest.approx(6.0)
    assert result.credit_kwh == 0.0
    assert result.state == "no_forecast"


# --- partial hours, pro-rated at both ends -----------------------------------


def test_partial_hours_prorated_at_both_ends():
    cfg = _cfg()
    now = START + timedelta(minutes=30)  # half of forecast hour 0 remains
    departure = START + timedelta(hours=2, minutes=15)  # a quarter of hour 2
    forecast = {START + timedelta(hours=h): 4000.0 for h in range(3)}
    result = plan_hybrid(
        now=now,
        departure=departure,
        remaining_need_kwh=100.0,  # large, so nothing else caps the credit
        max_charge_kw=MAX_CHARGE_KW,
        config=cfg,
        price_slots=(),  # unreached: a need this large forces last_call before price data is used
        forecast_wh=forecast,
    )
    # usable_w = 4000 - 500 = 3500, capped at max_charge_w = 3000 -> 3 kW
    # every credited hour. Hour 0: 0.5 h (00:30-01:00) -> 1.5 kWh. Hour 1:
    # a full hour (01:00-02:00) -> 3.0 kWh. Hour 2: 0.25 h (02:00-02:15) ->
    # 0.75 kWh. Total 5.25 kWh.
    assert result.forecast_credit_kwh == pytest.approx(5.25)


def test_partial_hour_below_threshold_at_full_power_credits_nothing_even_prorated():
    """The threshold is checked against the un-prorated power level: an hour
    that would only clear it after being shrunk by pro-ration must still
    credit zero."""
    cfg = _cfg(house_baseline_w=500.0)
    now = START + timedelta(minutes=45)  # only 15 minutes of hour 0 remain
    departure = START + timedelta(hours=1)
    # usable_w = 1999 - 500 = 1499 < 1500 -- below threshold at full power,
    # regardless of how little of the hour is left.
    forecast = {START: 1999.0}
    result = plan_hybrid(
        now=now,
        departure=departure,
        remaining_need_kwh=10.0,
        max_charge_kw=MAX_CHARGE_KW,
        config=cfg,
        price_slots=(),
        forecast_wh=forecast,
    )
    assert result.forecast_credit_kwh == 0.0


# --- naive datetimes are refused ---------------------------------------------


def test_naive_now_is_refused():
    with pytest.raises(ValueError):
        plan_hybrid(
            now=datetime(2026, 1, 10, 0, 0),  # naive
            departure=START + timedelta(hours=1),
            remaining_need_kwh=1.0,
            max_charge_kw=MAX_CHARGE_KW,
            config=_cfg(),
            price_slots=(),
        )


def test_naive_departure_is_refused():
    with pytest.raises(ValueError):
        plan_hybrid(
            now=START,
            departure=datetime(2026, 1, 10, 1, 0),  # naive
            remaining_need_kwh=1.0,
            max_charge_kw=MAX_CHARGE_KW,
            config=_cfg(),
            price_slots=(),
        )


def test_naive_forecast_hour_is_refused():
    with pytest.raises(ValueError):
        plan_hybrid(
            now=START,
            departure=START + timedelta(hours=1),
            remaining_need_kwh=1.0,
            max_charge_kw=MAX_CHARGE_KW,
            config=_cfg(),
            price_slots=(),
            forecast_wh={datetime(2026, 1, 10, 0, 0): 4000.0},  # naive key
        )


# --- a DST day counts hours by instant, not wall clock -----------------------


STOCKHOLM = ZoneInfo("Europe/Stockholm")


def test_dst_spring_forward_23_hour_day_counts_by_instant():
    # 2026-03-29: Stockholm's clocks skip 02:00 -> 03:00. Midnight to
    # midnight the next day is 23 real hours, not 24.
    now = datetime(2026, 3, 29, 0, 0, tzinfo=STOCKHOLM)
    departure = datetime(2026, 3, 30, 0, 0, tzinfo=STOCKHOLM)
    assert (departure - now).total_seconds() / 3600 != 23.0, (
        "sanity check: naive subtraction of same-tzinfo datetimes is the "
        "trap this module must not fall into -- see _as_utc_instant"
    )
    cfg = _cfg()
    result = plan_hybrid(
        now=now,
        departure=departure,
        remaining_need_kwh=10.0,
        max_charge_kw=MAX_CHARGE_KW,
        config=cfg,
        price_slots=(),
        forecast_wh=None,
    )
    expected_slack = max(0.0, 23.0 * MAX_CHARGE_KW * cfg.feasibility_margin - 10.0)
    assert result.slack_kwh == pytest.approx(expected_slack)


def test_dst_fall_back_25_hour_day_counts_by_instant():
    # 2026-10-25: Stockholm's clocks repeat 02:00 -> 02:00. Midnight to
    # midnight the next day is 25 real hours, not 24.
    now = datetime(2026, 10, 25, 0, 0, tzinfo=STOCKHOLM)
    departure = datetime(2026, 10, 26, 0, 0, tzinfo=STOCKHOLM)
    cfg = _cfg()
    result = plan_hybrid(
        now=now,
        departure=departure,
        remaining_need_kwh=10.0,
        max_charge_kw=MAX_CHARGE_KW,
        config=cfg,
        price_slots=(),
        forecast_wh=None,
    )
    expected_slack = max(0.0, 25.0 * MAX_CHARGE_KW * cfg.feasibility_margin - 10.0)
    assert result.slack_kwh == pytest.approx(expected_slack)


def test_dst_spring_forward_forecast_overlap_counts_by_instant():
    # The same trap, inside the forecast credit's own overlap arithmetic:
    # an hour straddling the spring-forward gap must be sized by its real
    # 0-minute-or-more overlap with [now, departure), not a wall-clock guess.
    cfg = _cfg()
    now = datetime(2026, 3, 29, 1, 30, tzinfo=STOCKHOLM)  # pre-transition, +01:00
    departure = datetime(2026, 3, 29, 3, 15, tzinfo=STOCKHOLM)  # post-transition, +02:00
    forecast = {now: 4000.0}
    result = plan_hybrid(
        now=now,
        departure=departure,
        remaining_need_kwh=100.0,
        max_charge_kw=MAX_CHARGE_KW,
        config=cfg,
        price_slots=(),
        forecast_wh=forecast,
    )
    # Real elapsed time from 01:30 CET to 03:15 CEST is 45 minutes (the wall
    # clock jumps from 02:00 to 03:00 in between). The forecast hour
    # [now, now+1h) therefore overlaps the window for only 45 real minutes
    # (0.75 h) of usable credit, capped at max_charge_w -> 3 kW * 0.75 h.
    assert result.forecast_credit_kwh == pytest.approx(2.25)


# --- satisfied -----------------------------------------------------------------


def test_satisfied_when_remaining_need_is_zero_or_less():
    cfg = _cfg()
    forecast = {START + timedelta(hours=h): 4000.0 for h in range(3)}
    for remaining in (0.0, -1.5):
        result = plan_hybrid(
            now=START,
            departure=START + timedelta(hours=3),
            remaining_need_kwh=remaining,
            max_charge_kw=MAX_CHARGE_KW,
            config=cfg,
            price_slots=(),
            forecast_wh=forecast,
        )
        assert result.grid_kwh == 0.0
        assert result.credit_kwh == 0.0
        assert result.state == "satisfied"


# --- the replan helper --------------------------------------------------------


def test_replan_triggers_at_the_step_and_not_below_it():
    cfg = _cfg(replan_step_kwh=1.0)
    assert needs_replan(5.0, 4.0, cfg) is True  # exactly at the step
    assert needs_replan(5.0, 4.000001, cfg) is False  # just below it
    assert needs_replan(5.0, 5.999999, cfg) is False  # just below it, rising
    assert needs_replan(5.0, 6.0, cfg) is True  # exactly at the step, rising
    assert needs_replan(5.0, 5.0, cfg) is False  # no change at all


# --- config defaults, pinned (the design's own parameter table) -------------


def test_config_defaults_match_the_design_table():
    cfg = HybridConfig(solar_start_w=4100.0)
    assert cfg.confidence == 0.5
    assert cfg.feasibility_margin == 0.8
    assert cfg.house_baseline_w == 500.0
    assert cfg.solar_start_w == 4100.0
    assert cfg.replan_step_kwh == 1.0


def test_solar_start_w_has_no_default_and_is_required():
    with pytest.raises(TypeError):
        HybridConfig()  # type: ignore[call-arg]


def test_holding_back_at_break_even_is_refused_despite_float_error() -> None:
    """At confidence 0.8 and a fallback five times the night price, holding
    the night back has an expected gain of exactly zero -- it buys variance and
    nothing else. `(1 - 0.8) * 5.0` evaluates to 0.9999999999999998, so a bare
    `p > threshold` passed by a float's width; the minimum-gain margin refuses
    it. A Monte Carlo over 400 days showed the result: hybrid costing 1% more
    than cheapest, calibrated, from a gamble that could not pay."""
    from custom_components.spotnav.planning.hybrid_plan import PriceSlot

    now = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    departure = now + timedelta(hours=24)
    prices = [1.0 if 2 <= h <= 5 else (10.0 if h >= 22 else 5.0) for h in range(24)]
    slots = [PriceSlot(start=now + timedelta(hours=h), duration_s=3600, price=p) for h, p in enumerate(prices)]
    forecast = {now + timedelta(hours=h): 6000.0 for h in range(10, 15)}

    result = plan_hybrid(
        now=now,
        departure=departure,
        remaining_need_kwh=9.0,
        max_charge_kw=3.0,
        config=HybridConfig(solar_start_w=4100, confidence=0.8),
        forecast_wh=forecast,
        price_slots=slots,
    )

    assert result.credit_kwh == 0.0
    assert result.grid_kwh == 9.0
    assert result.state == "not_worth_it"
