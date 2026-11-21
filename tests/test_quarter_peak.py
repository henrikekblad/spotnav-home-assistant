"""Quarter-hour peaks as a grid company's rule (Austria from 2027/2028: the month's single highest 15-minute mean, any
hour, at least 2 kW billed, the part above 10 kW at 1.5 times the price): the tariff's data, the tracker's quarter
hours, the month's peaks and the soft ceiling on them, the review's cost, and the import target holding the running
quarter hour. Pure, no Home Assistant."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.demand_plan import DemandBasis, day_demand_cost
from custom_components.spotnav.planning.auto_settings import (
    AutoSettingsError,
    DemandComponent,
    PowerLevel,
)
from custom_components.spotnav.planning.demand_charge import component_month, HourMean, period_kw
from custom_components.spotnav.planning.demand_cost import level_count, part_cost
from custom_components.spotnav.site.demand_tracker import HourTracker, QUARTER_S, RunningHour
from custom_components.spotnav.site.import_target import hour_budget, hour_end_lock_a, TargetMember

TZ = "Europe/Vienna"
UTC = timezone.utc
AUSTRIA = DemandComponent(
    top_n=1, price_winter=2.46, price_summer=2.46, period_min=15, min_kw=2.0, step_kw=10.0, step_factor=1.5
)
HOURLY = DemandComponent(top_n=1, price_winter=2.46, price_summer=2.46)
#: 10:00 CET on Tuesday 12 January 2027.
TEN = datetime(2027, 1, 12, 9, 0, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ the tariff's data


def test_the_peak_rules_are_data_in_the_part_written_only_where_they_differ() -> None:
    stored = AUSTRIA.as_dict()
    assert stored["peak"] == {"period_min": 15, "min_kw": 2.0, "step_kw": 10.0, "step_factor": 1.5}
    assert DemandComponent.from_dict(stored) == AUSTRIA
    # An hourly part billed as measured is written exactly as before the rules.
    assert set(HOURLY.as_dict()) == {"window", "top_n", "price_per_kw"}
    assert DemandComponent.from_dict(HOURLY.as_dict()) == HOURLY
    for bad in (
        {"period_min": 30, "min_kw": 0, "step_kw": None, "step_factor": 1},
        {"period_min": 15, "min_kw": -1, "step_kw": None, "step_factor": 1},
        {"period_min": 15, "min_kw": 0, "step_kw": 0, "step_factor": 1},
        {"period_min": 15, "min_kw": 0, "step_kw": 10, "step_factor": 0.5},
        {"period_min": 15, "min_kw": 0, "step_kw": None},
    ):
        with pytest.raises(AutoSettingsError):
            DemandComponent.from_dict({**HOURLY.as_dict(), "peak": bad})
    level = PowerLevel(level_kw=6.0, period_min=15)
    assert PowerLevel.from_dict(level.as_dict()) == level
    assert "period_min" not in PowerLevel(level_kw=6.0).as_dict()
    with pytest.raises(AutoSettingsError):
        PowerLevel.from_dict({**PowerLevel(level_kw=6.0).as_dict(), "period_min": 30})


def test_the_bill_has_its_minimum_and_its_step() -> None:
    # Wien's draft prices, per kW and month: 2 kW at the least, the part above 10 kW at 1.5 times.
    assert AUSTRIA.billed(1.0, 2.46) == pytest.approx(4.92)
    assert AUSTRIA.billed(4.0, 2.46) == pytest.approx(9.84)
    assert AUSTRIA.billed(11.0, 2.46) == pytest.approx(10 * 2.46 + 1 * 3.69)
    assert [AUSTRIA.price_factor_at(kw) for kw in (1.0, 2.0, 9.9, 10.0)] == [0.0, 1.0, 1.0, 1.5]
    assert HOURLY.billed(11.0, 2.46) == pytest.approx(11 * 2.46)


# ------------------------------------------------------------------------------------------------ the quarter hours


def _observe_quarters(tracker: HourTracker, start: datetime, kw_by_quarter: list[float], step_s: int = 30) -> None:
    moment = start
    for kw in kw_by_quarter:
        for _ in range(int(QUARTER_S // step_s)):
            tracker.observe(moment, kw * 1000.0, 0.0)
            moment += timedelta(seconds=step_s)
    tracker.observe(moment, 0.0, 0.0)


def test_the_tracker_keeps_each_hours_highest_quarter_and_reads_it_back() -> None:
    tracker = HourTracker(tz=TZ)
    # 2, 2, 8, 2 kW: the hour's mean is 3.5 kW, its highest quarter 8 kW.
    _observe_quarters(tracker, TEN, [2.0, 2.0, 8.0, 2.0])
    [hour] = tracker.hours
    assert hour.kw == pytest.approx(3.5, abs=0.01)
    assert hour.quarter_kw == pytest.approx(8.0, abs=0.01)
    assert period_kw(AUSTRIA, hour) == hour.quarter_kw and period_kw(HOURLY, hour) == hour.kw
    again = HourTracker(tz=TZ)
    again.restore(tracker.month_dict(), None)
    assert again.hours[0].quarter_kw == hour.quarter_kw


def test_a_quarter_with_too_long_a_gap_has_no_value_of_its_own() -> None:
    tracker = HourTracker(tz=TZ)
    moment = TEN
    # Readings every 30 s, but six minutes without any in the third quarter, at 9 kW around it.
    while moment < TEN + timedelta(hours=1):
        if not TEN + timedelta(minutes=33) <= moment < TEN + timedelta(minutes=39):
            tracker.observe(moment, 9000.0 if 30 <= (moment - TEN).total_seconds() / 60 < 45 else 1000.0, 0.0)
        moment += timedelta(seconds=30)
    tracker.observe(moment, 0.0, 0.0)
    [hour] = tracker.hours
    assert hour.quarter_kw == pytest.approx(1.0, abs=0.01)


def test_the_running_quarter_is_kept_over_a_restart() -> None:
    running = RunningHour(start=TEN, quarter_start=TEN + timedelta(minutes=15), quarter_wh=100.0, quarter_covered_s=120.0,
                          quarter_peak_kw=4.0)
    read = RunningHour.from_dict(running.as_dict())
    assert read is not None and read.quarter_start == running.quarter_start and read.quarter_peak_kw == 4.0
    stray = {**running.as_dict(), "quarter_start": (TEN + timedelta(hours=2)).isoformat()}
    assert RunningHour.from_dict(stray).quarter_start is None


# ------------------------------------------------------------------------------------------------ the month's peaks


def test_the_months_peak_is_its_highest_quarter_and_the_ceiling_keeps_the_minimum_and_the_step() -> None:
    hours = [
        HourMean(start=TEN - timedelta(days=1), kw=3.0, floor_kw=3.0, quarter_kw=7.0),
        HourMean(start=TEN - timedelta(days=2), kw=5.0, floor_kw=5.0, quarter_kw=5.5),
    ]
    month = component_month(AUSTRIA, hours, TZ, TEN)
    assert month.bar_kw == pytest.approx(7.0) and month.free_kw == pytest.approx(7.0)
    assert component_month(HOURLY, hours, TZ, TEN).bar_kw == pytest.approx(5.0)
    cost = part_cost(AUSTRIA, hours, {}, [], TEN, TZ)
    assert cost.threshold_kw == pytest.approx(7.0) and cost.marginal == pytest.approx(2.46)
    # A month that has reached the step prices a kW more at the price above it.
    high = [HourMean(start=TEN - timedelta(days=1), kw=8.0, floor_kw=8.0, quarter_kw=11.0)]
    assert part_cost(AUSTRIA, high, {}, [], TEN, TZ).marginal == pytest.approx(2.46 * 1.5)
    # A month below the minimum: the ceiling is the minimum, already billed.
    low = [HourMean(start=TEN - timedelta(days=1), kw=0.5, floor_kw=0.5, quarter_kw=1.0)]
    assert part_cost(AUSTRIA, low, {}, [], TEN, TZ).threshold_kw == pytest.approx(2.0)


def test_a_quarter_hour_level_counts_an_hour_whose_quarter_went_over() -> None:
    hours = [HourMean(start=TEN - timedelta(days=1), kw=5.0, floor_kw=5.0, quarter_kw=7.0)]
    assert level_count(hours, 6.0, lambda _start: True, TEN, TZ).used == 0
    assert level_count(hours, 6.0, lambda _start: True, TEN, TZ, quarter=True).used == 1


def test_the_reviews_cost_is_on_the_runs_highest_quarter_with_the_step() -> None:
    basis = DemandBasis(TZ, ((AUSTRIA, 2.46 * 100, {}),))
    quarters = [(TEN + timedelta(minutes=15 * index), kw) for index, kw in enumerate((4.0, 4.0, 12.0, 4.0))]
    # No earlier day: the month's charge is the run's own quarter peak, 12 kW, against the 2 kW minimum.
    assert day_demand_cost(basis, quarters) == pytest.approx(100 * (AUSTRIA.billed(12.0, 2.46) - AUSTRIA.billed(0, 2.46)))
    hourly = DemandBasis(TZ, ((HOURLY, 2.46 * 100, {}),))
    assert day_demand_cost(hourly, quarters) == pytest.approx(100 * 2.46 * 6.0)


# ------------------------------------------------------------------------------------------------ the import target


def _running(minutes: float, quarter_kwh: float, hour_kwh: float) -> RunningHour:
    now = TEN + timedelta(minutes=minutes)
    quarter = TEN + timedelta(minutes=15 * int(minutes // 15))
    return RunningHour(
        start=TEN,
        import_wh=hour_kwh * 1000.0,
        covered_s=minutes * 60.0,
        last_at=now,
        last_grid_w=1000.0,
        quarter_start=quarter,
        quarter_wh=quarter_kwh * 1000.0,
        quarter_covered_s=(now - quarter).total_seconds(),
    )


def test_a_quarter_hour_tariff_holds_the_running_quarter() -> None:
    # 20 minutes in: 3 kWh this hour, 0.5 kWh in the running quarter (5 minutes of it at 6 kW).
    running = _running(20.0, 0.5, 3.0)
    now = TEN + timedelta(minutes=20)
    hourly, _ = hour_budget(target_kw=6.0, running=running, now=now, house_w=1000.0, ceiling_w=20000.0)
    quarter, reason = hour_budget(
        target_kw=6.0, running=running, now=now, house_w=1000.0, ceiling_w=20000.0, quarter=True
    )
    assert reason is None and quarter is not None and hourly is not None
    assert quarter.hour_start == TEN + timedelta(minutes=15) and quarter.hour_end == TEN + timedelta(minutes=30)
    assert quarter.period_s == QUARTER_S and quarter.remaining_s == pytest.approx(600.0)
    # The quarter's budget: 5.88 kW x 15 min = 1.47 kWh, 0.5 spent, 0.97 over 10 minutes = 5.82 kW, less the house.
    assert quarter.rate_w == pytest.approx((5.88 * 250 - 500) * 6, rel=1e-6)
    assert quarter.allowed_w == pytest.approx(quarter.rate_w - 1000.0)
    # The hour's budget is another figure altogether.
    assert hourly.allowed_w != pytest.approx(quarter.allowed_w)


def test_a_quarter_that_cannot_be_measured_holds_the_hour() -> None:
    running = _running(20.0, 0.1, 2.0)
    running.quarter_covered_s = 60.0  # four of its five minutes without a reading
    budget, reason = hour_budget(
        target_kw=6.0, running=running, now=TEN + timedelta(minutes=20), house_w=1000.0, ceiling_w=20000.0,
        quarter=True,
    )
    assert reason is None and budget is not None and budget.period_s == 3600.0


def test_the_quarters_end_lock_is_a_quarter_of_the_hours() -> None:
    member = TargetMember("car", phases=3, min_a=6.0, wants_a=16.0)
    # 200 s before the end: inside the hour's ten-minute lock, outside the quarter's 150 s.
    assert hour_end_lock_a(member, held_cap_a=10.0, held_last_pass=True, remaining_s=200.0) == 10.0
    assert hour_end_lock_a(member, held_cap_a=10.0, held_last_pass=True, remaining_s=200.0, period_s=QUARTER_S) is None
    assert hour_end_lock_a(member, held_cap_a=10.0, held_last_pass=True, remaining_s=100.0, period_s=QUARTER_S) == 10.0


def test_the_site_holds_the_quarter_where_a_part_or_the_level_counting_the_hour_bills_quarters(monkeypatch) -> None:
    from types import SimpleNamespace

    from custom_components.spotnav.planning.planner import HighLoadWindow
    from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController

    parts: list[DemandComponent] = []
    monkeypatch.setattr(
        "custom_components.spotnav.pricing.price_source.site_demand_components", lambda _hass, _entry: tuple(parts)
    )
    level: list[PowerLevel | None] = [None]
    site = SimpleNamespace(
        hass=None, entry_id="site", demand=SimpleNamespace(tracker=SimpleNamespace(tz=TZ)), _power_level=lambda: level[0]
    )
    quarter = SiteCapacityController.target_quarter
    assert not quarter(site, TEN)
    parts.append(HOURLY)
    assert not quarter(site, TEN)
    # A quarter-hour part that counts only the night: not this hour.
    night = HighLoadWindow(days="all", from_month=1, to_month=12, from_hour=22, to_hour=6)
    parts.append(DemandComponent(top_n=1, window=night, period_min=15))
    assert not quarter(site, TEN)
    parts.append(AUSTRIA)
    assert quarter(site, TEN)
    parts[:] = [HOURLY]
    level[0] = PowerLevel(level_kw=6.0, period_min=15)
    assert quarter(site, TEN)
