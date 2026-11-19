"""The hot-water planner (`heat/dhw_plan.py`)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.spotnav.heat.dhw_peaks import Peak
from custom_components.spotnav.heat.dhw_plan import (
    QUARTER,
    REASON_CHEAP,
    REASON_SUN,
    PlanInputs,
    hourly_to_quarters,
    plan,
    quarter_of,
    quarters_of,
)

ZONE = ZoneInfo("Europe/Stockholm")
MORNING = Peak(time(6, 0), time(7, 0), 1.5, True, time(6, 30))


def local(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, 20, hh, mm, tzinfo=ZONE).astimezone(timezone.utc) + timedelta(days=day)


def prices(by_hour: dict[int, float], default: float = 100.0) -> dict[datetime, float]:
    """Quarter prices for 2026-11-19 12:00 to 2026-11-21 24:00 local, hour by hour."""
    out = {}
    moment = local(-1, 12)
    while moment < local(1, 23, 45):
        hour = moment.astimezone(ZONE).hour
        day = (moment.astimezone(ZONE).date() - local(0, 12).astimezone(ZONE).date()).days
        out[moment] = by_hour.get(hour, default) if day == 0 else default
        moment += QUARTER
    return out


def inputs(**changes) -> PlanInputs:
    base = PlanInputs(now=local(-1, 22), zone=ZONE, peaks=[MORNING], buy=prices({3: 40.0, 4: 40.0, 1: 60.0}))
    return replace(base, **changes)


def test_cheapest_quarter_before_the_peak() -> None:
    picks = plan(inputs())
    assert len(picks) == 1
    pick = picks[0]
    # 03:00 and 04:00 cost the same: the later start loses less standby heat.
    assert pick.start == local(0, 4, 30)
    assert pick.end == local(0, 5)
    assert pick.peak == local(0, 6)
    assert pick.reason == REASON_CHEAP
    assert pick.price == 40.0
    assert pick.gain > 0


def test_window_bounded_by_max_lead_and_the_previous_peak() -> None:
    evening = Peak(time(17, 0), time(20, 0), 1.0, True, time(18, 0))
    # Cheap at 20:00 the evening before: before the evening peak's end, so not in the morning's window; and 21:00 is
    # more than eight hours before 06:00 too.
    buy = prices({})
    for q, _ in list(buy.items()):
        if q.astimezone(ZONE).hour in (20, 21) and q.astimezone(ZONE).day == 19:
            buy[q] = 1.0
    picks = plan(inputs(peaks=[MORNING, evening], buy=buy, now=local(-1, 19)))
    assert all(pick.start >= local(-1, 22) for pick in picks if pick.peak == local(0, 6))


def test_not_worth_it_when_the_pump_runs_cheap_anyway() -> None:
    assert plan(inputs(buy=prices({}))) == []
    # A gain under the threshold (1.5 kWh x 5 öre) is not worth a boost.
    assert plan(inputs(buy=prices({4: 95.0}))) == []


def test_no_price_at_the_own_run_no_pick() -> None:
    buy = {q: p for q, p in prices({4: 40.0}).items() if not (local(0, 6) <= q < local(0, 7))}
    assert plan(inputs(buy=buy)) == []


def test_fuse_case_with_a_car_and_a_grid_charging_battery() -> None:
    """03–05 is cheapest, but the car charges at 11 kW and the battery grid-charges at 3 kW there under a 16 kW cap
    with the house at 1.5 kW: a 3 kW boost does not fit. It goes to the next-cheapest hour with room."""
    load = {}
    for q in prices({}):
        hour = q.astimezone(ZONE).hour
        load[q] = 1.5 + (11.0 + 3.0 if hour in (3, 4) and q.astimezone(ZONE).day == 20 else 0.0)
    picks = plan(inputs(load_kw=load, cap_kw=16.0))
    assert len(picks) == 1
    assert local(0, 1) <= picks[0].start < local(0, 2)
    # Without the cap the boost would sit in the cheap hours.
    assert local(0, 3) <= plan(inputs(load_kw=load))[0].start < local(0, 5)


def test_blocked_and_unsafe_quarters_are_dropped() -> None:
    cheap = {q for q in prices({}) if local(0, 3) <= q < local(0, 5)}
    assert local(0, 1) <= plan(inputs(blocked=frozenset(cheap)))[0].start < local(0, 2)
    assert local(0, 1) <= plan(inputs(unsafe=frozenset(cheap)))[0].start < local(0, 2)


def test_sun_first() -> None:
    buy = prices({})
    sell = {q: 20.0 for q in buy}
    surplus = {q: 4.0 for q in buy if local(-1, 23) <= q < local(0, 0)}
    pick = plan(inputs(buy=buy, sell=sell, surplus_kw=surplus))[0]
    assert pick.reason == REASON_SUN
    assert pick.price == 20.0
    # A surplus smaller than the boost is no sun.
    small = {q: 1.0 for q in surplus}
    assert plan(inputs(buy=buy, sell=sell, surplus_kw=small)) == []


def test_budget_per_day() -> None:
    evening = Peak(time(17, 0), time(20, 0), 1.5, True, time(18, 0))
    buy = prices({3: 40.0, 4: 40.0, 14: 40.0})
    assert len(plan(inputs(peaks=[MORNING, evening], buy=buy))) == 2
    assert len(plan(inputs(peaks=[MORNING, evening], buy=buy, budget=1))) == 1
    day = local(0, 6).astimezone(ZONE).date()
    assert plan(inputs(peaks=[MORNING, evening], buy=buy, used={day: 3})) == []
    assert len(plan(inputs(peaks=[MORNING, evening], buy=buy, used={day: 2}))) == 1


def test_frozen_pick_is_kept() -> None:
    first = plan(inputs())[0]
    # An hour later 02:00 turns much cheaper, but the pick starts within the freeze.
    later = inputs(now=first.start - timedelta(minutes=50), buy=prices({2: 1.0, 3: 40.0, 4: 40.0}), previous=[first])
    assert plan(later) == [first]


def test_a_pick_moves_once_per_hour_and_only_for_a_real_gain() -> None:
    first = plan(inputs())[0]
    cheaper = prices({1: 60.0, 2: 10.0, 3: 40.0, 4: 40.0})
    moved = plan(inputs(now=local(-1, 23), buy=cheaper, previous=[first]))[0]
    assert local(0, 2) <= moved.start < local(0, 3) and moved.moved_at == local(-1, 23)
    # Even cheaper at 01:00 half an hour later: it stays (moved within the hour).
    cheapest = prices({1: 0.0, 2: 10.0, 3: 40.0, 4: 40.0})
    assert plan(inputs(now=local(-1, 23, 30), buy=cheapest, previous=[moved]))[0].start == moved.start
    # A tiny gain never moves it.
    tiny = prices({1: 60.0, 2: 10.0, 3: 9.99, 4: 40.0})
    assert plan(inputs(now=local(0, 0, 30), buy=tiny, previous=[moved]))[0].start == moved.start


def test_helpers() -> None:
    start = datetime(2026, 11, 20, 3, 7, tzinfo=timezone.utc)
    assert quarter_of(start) == datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
    hour = datetime(2026, 11, 20, 3, tzinfo=timezone.utc)
    assert len(quarters_of([(hour, hour + timedelta(hours=1), 5.0)])) == 4
    assert hourly_to_quarters({hour: 2.0})[hour + 3 * QUARTER] == 2.0
    assert plan(inputs())[0].stored()["reason"] == REASON_CHEAP
