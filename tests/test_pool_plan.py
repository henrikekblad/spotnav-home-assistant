"""The pool's shadow planner (`pool/plan.py`, spec section 5.1)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from custom_components.spotnav.loads.switch_load import RunHistory
from custom_components.spotnav.pool.config import PoolConfig, PoolHeatPump
from custom_components.spotnav.pool.plan import (
    Extra,
    PoolInputs,
    PriceHour,
    filter_need_h,
    heat_need_h,
    learnt_fall,
    learnt_rise,
    plan_pool,
    season_hint,
)

NOW = datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc)
POOL = PoolConfig(heat_pump=PoolHeatPump("climate.poolpump"), circulation_id="switch.c", heat_pump_kw=1.6, circulation_kw=0.4)


def prices(values: list[float], start: datetime = NOW, sell: float = 50.0) -> list[PriceHour]:
    return [PriceHour(start + timedelta(hours=i), value, sell) for i, value in enumerate(values)]


def flat_then(cheap: dict[int, float], hours: int = 36, base: float = 200.0) -> list[PriceHour]:
    return prices([cheap.get(i, base + i) for i in range(hours)])


def history(ran_h: float = 0.0) -> RunHistory:
    # A full need just before now: no deadline inside the first day.
    runs = RunHistory(NOW - timedelta(hours=1))
    if ran_h:
        runs.note(NOW - timedelta(hours=ran_h), NOW)
    return runs


def hours_of(runs) -> set[datetime]:
    out: set[datetime] = set()
    for start, end, _why in runs:
        moment = start
        while moment < end:
            out.add(moment)
            moment += timedelta(hours=1)
    return out


def test_the_need_follows_the_water_over_three_held_between_2_and_12_or_the_persons_hours() -> None:
    assert filter_need_h(POOL, 25.0) == (8.25, "water")
    assert filter_need_h(POOL, 11.0) == (3.75, "water")
    assert filter_need_h(POOL, 4.0) == (2.0, "water")
    assert filter_need_h(POOL, 40.0) == (12.0, "water")
    assert filter_need_h(POOL, None) == (4.0, "default")
    assert filter_need_h(PoolConfig(circulation_id="switch.c", filter_hours=6.5), 25.0) == (6.5, "override")


def test_the_heat_need_and_its_learning() -> None:
    assert heat_need_h(27.0, 25.0, 0.2, 0.5) == 12.5
    assert heat_need_h(27.0, 27.0, 0.2, 0.5) == 0.0 and heat_need_h(27.0, None, 0.2, 0.5) == 0.0
    start = NOW
    runs = [
        (start, start + timedelta(hours=2), 24.0, 24.6),
        (start, start + timedelta(minutes=30), 24.0, 25.0),  # too short
        (start, start + timedelta(hours=4), 24.0, 25.6),
        (start, start + timedelta(hours=1), 24.0, 23.0),  # no rise
    ]
    assert learnt_rise(runs) == (0.35, True)
    assert learnt_rise([]) == (0.2, False)
    assert learnt_fall([(25.0, 24.6, 8.0), (25.0, 24.7, 8.0)]) == (1.05, True)
    assert learnt_fall([]) == (0.5, False)


def test_the_heat_pumps_hours_lie_inside_the_circulations_and_count_toward_the_need() -> None:
    # Water at 26.4: 3 heat pump hours (0.6 + 0.5 fall at 0.4 °C/h); the need 8.75 h.
    plan = plan_pool(
        PoolInputs(NOW, POOL, flat_then({3: 10, 4: 11, 5: 12, 20: 15, 21: 16}), history(), water_c=26.4, day_water_c=26.4, rise_c_h=0.4)
    )
    heat, circ = hours_of(plan.heat), hours_of(plan.circulation)
    assert plan.heat_h == 2.75 and len(heat) == 3
    assert heat <= circ
    assert heat == {NOW + timedelta(hours=h) for h in (3, 4, 5)}
    # The need inside the window: 28-hour rolling deadline, so at most the need's hours, never more.
    assert len(circ) <= 9


def test_sun_surplus_comes_before_the_cheapest_buy_hour() -> None:
    table = prices([100.0] * 24, sell=40.0)
    sun = {NOW + timedelta(hours=13): 3.0, NOW + timedelta(hours=14): 3.0}
    cheap = dict(enumerate([100.0] * 24))
    cheap[2] = 60.0
    table = prices(list(cheap.values()), sell=40.0)
    plan = plan_pool(
        PoolInputs(NOW, POOL, table, history(), water_c=26.0, day_water_c=6.0, rise_c_h=1.0, surplus_kw=sun)
    )
    # 2 heat pump hours (1 °C + 0.5 fall at 1 °C/h): both in the sun, as the sun covers 2.0 kW.
    assert hours_of(plan.heat) == set(sun)
    assert {why for _a, _b, why in plan.heat} == {"sun"}


def test_the_need_is_moved_never_dropped_with_a_rolling_deadline() -> None:
    # Nothing ran for a day: the whole need is due within the window's first hours too, at whatever price.
    runs = RunHistory(NOW - timedelta(hours=30))
    plan = plan_pool(PoolInputs(NOW, PoolConfig(circulation_id="switch.c", filter_hours=4.0), flat_then({30: 1, 31: 1, 32: 1, 33: 1}), runs))
    chosen = hours_of(plan.circulation)
    # Overdue: run at once, whatever the cheap hours later; what the running hour cannot take is said short.
    assert chosen == {NOW + timedelta(hours=h) for h in range(4)}
    assert plan.short_h == 3
    assert {why for _a, _b, why in plan.circulation} == {"need"}


def test_hours_without_room_are_never_chosen() -> None:
    cheap = {2: 1, 3: 1, 4: 1, 5: 1}
    room = {NOW + timedelta(hours=h): 0.1 for h in (2, 3)}
    plan = plan_pool(
        PoolInputs(NOW, PoolConfig(circulation_id="switch.c", filter_hours=2.0, circulation_kw=0.4), flat_then(cheap), RunHistory(NOW - timedelta(hours=27)), room_kw=room)
    )
    assert set(plan.blocked_hours) == set(room)
    assert not hours_of(plan.circulation) & set(room)


def test_runs_are_whole_hours_with_few_starts() -> None:
    # Alternating prices would scatter the hours: the starts are capped (4 a day).
    values = [10.0 if i % 2 == 0 else 11.0 for i in range(24)]
    runs = RunHistory(NOW - timedelta(hours=27))
    plan = plan_pool(PoolInputs(NOW, PoolConfig(circulation_id="switch.c", filter_hours=8.0), prices(values), runs))
    chosen = sorted(hours_of(plan.circulation))
    assert len(chosen) == 8
    starts = sum(1 for i, start in enumerate(chosen) if i == 0 or chosen[i - 1] != start - timedelta(hours=1))
    assert starts <= 4


def test_extra_filtering_is_placed_in_the_cheapest_hours_before_its_end() -> None:
    cheap = {1: 5, 2: 5, 9: 1, 10: 1, 30: 0.5}
    extra = Extra(2, NOW + timedelta(hours=24))
    plan = plan_pool(
        PoolInputs(NOW, PoolConfig(circulation_id="switch.c", filter_hours=2.0), flat_then(cheap), history(2.0), extra=extra)
    )
    extras = {a for a, _b, why in plan.circulation if why == "extra"} | {
        a + timedelta(hours=1) for a, b, why in plan.circulation if why == "extra" and b - a > timedelta(hours=1)
    }
    assert extras <= {NOW + timedelta(hours=h) for h in (1, 2, 9, 10)} and len(hours_of([r for r in plan.circulation if r[2] == "extra"])) == 2
    assert NOW + timedelta(hours=30) not in hours_of([r for r in plan.circulation if r[2] == "extra"])
    assert plan.extra == extra
    gone = plan_pool(PoolInputs(NOW, PoolConfig(circulation_id="switch.c"), flat_then({}), history(), extra=Extra(2, NOW)))
    assert gone.extra is None


def test_a_closed_season_plans_nothing() -> None:
    closed = PoolConfig(heat_pump=PoolHeatPump("climate.p"), circulation_id="switch.c", season_open=False)
    plan = plan_pool(PoolInputs(NOW, closed, flat_then({}), history(), water_c=10.0))
    assert plan.circulation == () and plan.heat == ()


def test_the_season_hint_three_cold_days_once_a_year_not_in_summer() -> None:
    today = date(2026, 9, 20)
    cold = {today - timedelta(days=d): 10.5 for d in (1, 2, 3)}
    assert season_hint(cold, today, season_open=True, hinted_year=None)
    assert not season_hint(cold, today, season_open=True, hinted_year=2026)
    assert not season_hint(cold, today, season_open=False, hinted_year=None)
    assert not season_hint({**cold, today - timedelta(days=2): 12.0}, today, season_open=True, hinted_year=None)
    june = date(2026, 6, 10)
    assert not season_hint({june - timedelta(days=d): 9.0 for d in (1, 2, 3)}, june, season_open=True, hinted_year=None)
