"""The house card's history (`site/history_period.py`, `site/house_period.py`): periods on the site's clock, the energy
summed from per-period flows, the cost at the held prices, the battery's reviewed days and the forecasts' errors, and
the contract fixtures the card reads (`tests/fixtures/house_history/`, `SPOTNAV_WRITE_FIXTURES=1` rewrites them)."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from custom_components.spotnav.api.house_history import read_archived_reviews
from custom_components.spotnav.battery.hindsight import CellTemperature
from custom_components.spotnav.battery.plan_shadow import DayReview, PriceSlot, Run
from custom_components.spotnav.day_record import review_part
from custom_components.spotnav.site.energy_flow import EnergyHour
from custom_components.spotnav.site.history_period import (
    PERIOD_DAY,
    PERIOD_MONTH,
    PERIOD_YEAR,
    bucket_key,
    bucket_keys,
    period_of,
    requested_period,
    shifted,
    utc_bounds,
)
from tests.test_house_view import facit_for

from custom_components.spotnav.site.spotnav_saving import DaySaving
from custom_components.spotnav.site.house_period import (
    BUCKET_KEYS,
    HISTORY_KEYS,
    HouseHistoryInputs,
    house_history,
    slot_prices,
)

TZ = "Europe/Stockholm"
FIXTURES = Path(__file__).parent / "fixtures" / "house_history"
NOW = datetime(2026, 11, 16, 13, 20, tzinfo=timezone.utc)
MONEY = ("SEK", "kr", "öre")


def _fixture(name: str, payload: dict[str, Any]) -> None:
    path = FIXTURES / name
    payload = json.loads(json.dumps(payload))
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == payload, f"{name} drifted from the backend"


def hours_of(first: datetime, end: datetime) -> list[datetime]:
    out = []
    moment = first
    while moment < end:
        out.append(moment)
        moment += timedelta(hours=1)
    return out


def villa_hour(start: datetime) -> EnergyHour:
    """An hour of a villa with sun, a battery, a heat pump and an evening car: the sun at midday, the battery charged
    by it and given back in the evening, the car from 22."""
    hour = (start + timedelta(hours=1)).hour  # CET, close enough for the shape
    sun = 1.5 if 10 <= hour < 14 else 0.0
    return EnergyHour(
        start=start,
        grid_in=0.4 if sun else (1.2 if hour < 17 else 0.6),
        grid_out=0.2 if sun else 0.0,
        solar=sun,
        battery_in=0.5 if sun else 0.0,
        battery_out=0.6 if 17 <= hour < 21 else 0.0,
        car=3.0 if hour >= 22 else 0.0,
        heat=0.8,
    )


def flat_slots(first: datetime, end: datetime, buy: float = 150.0, sell: float = 60.0) -> list[PriceSlot]:
    out = []
    moment = first
    while moment < end:
        price = buy + (100.0 if (moment + timedelta(hours=1)).hour in (17, 18, 19) else 0.0)
        out.append(PriceSlot(moment, moment + timedelta(minutes=15), price, sell))
        moment += timedelta(minutes=15)
    return out


def review(day: date, cost: float = 3000.0, **extra: Any) -> DayReview:
    base = dict(
        day=day,
        reviewed_at=datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc),
        quarters=96,
        covered=96,
        unplanned=0,
        start_soc_pct=40.0,
        end_value=120.0,
        usable_kwh=40.0,
        actual=Run(cost, 40.0),
        plan=Run(cost - 100.0, 40.0),
        self_consumption=Run(cost + 50.0, 40.0),
        modes={"balanced": Run(cost - 100.0, 40.0), "max_profit": Run(cost - 150.0, 40.0)},
        mode="balanced",
        no_battery_cost=cost + 400.0,
        sun_gave=700.0,
        sun_quarters=96,
    )
    base.update(extra)
    return DayReview(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ periods


def test_periods_start_and_end_on_local_midnights() -> None:
    day = period_of(PERIOD_DAY, date(2026, 11, 15))
    month = period_of(PERIOD_MONTH, date(2026, 11, 15))
    year = period_of(PERIOD_YEAR, date(2026, 11, 15))
    assert (month.first, month.end) == (date(2026, 11, 1), date(2026, 12, 1))
    assert (year.first, year.end) == (date(2026, 1, 1), date(2027, 1, 1))
    start, end = utc_bounds(day, TZ)
    assert start == datetime(2026, 11, 14, 23, tzinfo=timezone.utc) and end - start == timedelta(hours=24)


def test_dst_days_have_23_and_25_hourly_buckets() -> None:
    autumn = period_of(PERIOD_DAY, date(2026, 10, 25))
    spring = period_of(PERIOD_DAY, date(2026, 3, 29))
    assert len(bucket_keys(autumn, TZ)) == 25 and len(set(bucket_keys(autumn, TZ))) == 25
    assert len(bucket_keys(spring, TZ)) == 23
    # The repeated 02:00 is two buckets, told apart by the offset.
    assert "2026-10-25T02:00:00+02:00" in bucket_keys(autumn, TZ)
    assert "2026-10-25T02:00:00+01:00" in bucket_keys(autumn, TZ)


def test_year_boundary_shifts_and_buckets() -> None:
    january = period_of(PERIOD_MONTH, date(2026, 1, 10))
    assert shifted(january, -1) == period_of(PERIOD_MONTH, date(2025, 12, 1))
    assert shifted(period_of(PERIOD_MONTH, date(2025, 12, 31)), 1) == january
    assert shifted(period_of(PERIOD_YEAR, date(2026, 6, 1)), -1).first == date(2025, 1, 1)
    # New Year's Eve 23:30 local is still December, though it is 22:30 UTC.
    late = datetime(2025, 12, 31, 22, 30, tzinfo=timezone.utc)
    assert bucket_key(period_of(PERIOD_YEAR, date(2025, 1, 1)), late, TZ) == "2025-12"
    assert bucket_key(period_of(PERIOD_MONTH, date(2026, 1, 1)), datetime(2025, 12, 31, 23, 30, tzinfo=timezone.utc), TZ) == "2026-01-01"


def test_requested_period_refuses_what_it_cannot_show() -> None:
    today = date(2026, 1, 5)
    assert requested_period("month", "2025-12-15", today) == period_of(PERIOD_MONTH, date(2025, 12, 1))
    assert requested_period("year", None, today).first == date(2026, 1, 1)
    for kind, raw in (("week", "2026-01-01"), ("day", "2026-01-06"), ("day", "2026-13-01"), ("day", 20260101), ("month", "2015-01-01")):
        with pytest.raises(ValueError):
            requested_period(kind, raw, today)


# ------------------------------------------------------------------------------------------------ energy and cost


def month_inputs(**extra: Any) -> HouseHistoryInputs:
    period = period_of(PERIOD_MONTH, date(2026, 11, 1))
    start, _end = utc_bounds(period, TZ)
    last = datetime(2026, 11, 16, 13, tzinfo=timezone.utc)
    hours = hours_of(start, last)
    base = dict(
        site_id="site_villa",
        now=NOW,
        tz=TZ,
        period=period,
        periods=[villa_hour(hour) for hour in hours],
        has_solar=True,
        has_battery=True,
        has_heat=True,
        prices=flat_slots(start - timedelta(days=1), last + timedelta(days=1)),
        money=MONEY,
        # The car's figure from the 10th; the battery under its own control (no part); the heat lowered on two days.
        savings={
            date(2026, 11, day): DaySaving(
                car=150.0 + 10 * day, heat=60.0 if day in (12, 13) else None, battery_share=0.0
            )
            for day in range(10, 17)
        },
        reviews=[
            review(
                date(2026, 11, day),
                # The 7th is partial (no share); on the 9th the perfect plan saved under 5 kr (too small to compare).
                facit=None
                if day < 6
                else facit_for(date(2026, 11, 9), 420.0, 300.0, 380.0, 360.0)
                if day == 9
                else facit_for(date(2026, 11, day), 900.0 + 20 * day, 700.0 + 15 * day, 820.0, 760.0, provisional=day == 15, measured=80 if day == 7 else 96),
                cell_temperature=None if day < 8 else CellTemperature(19.5 + day * 0.2, 23.0 + day * 0.3, 15 if day == 12 else 0, 1440),
            )
            for day in range(3, 16)
        ],
        archived=frozenset({date(2026, 11, 3)}),
        forecast_mae={"house_kw": {date(2026, 11, 14): 0.4, date(2026, 11, 15): 0.6, date(2026, 10, 31): 9.0}},
    )
    base.update(extra)
    return HouseHistoryInputs(**base)  # type: ignore[arg-type]


def test_month_buckets_sum_the_hours_and_the_flow_is_the_sum_of_the_hours() -> None:
    answer = house_history(month_inputs())
    assert tuple(answer) == HISTORY_KEYS
    buckets = answer["buckets"]
    assert len(buckets) == 30 and all(tuple(bucket) == BUCKET_KEYS for bucket in buckets)
    full = next(bucket for bucket in buckets if bucket["key"] == "2026-11-10")
    assert full["periods"] == 24
    assert full["heat"] <= 24 * 0.8 and full["heat"] == pytest.approx(full["use"] - full["house"] - full["car"], abs=0.01)
    assert full["solar"] == pytest.approx(4 * 1.5)
    # Today has its completed hours only, later days none.
    today = next(bucket for bucket in buckets if bucket["key"] == "2026-11-16")
    assert today["periods"] == 14
    assert next(bucket for bucket in buckets if bucket["key"] == "2026-11-20")["periods"] == 0
    flow = answer["energy_flow"]
    nodes = {node["id"]: node["kwh"] for node in flow["nodes"]}
    assert nodes["source.solar"] == pytest.approx(sum(bucket["solar"] or 0 for bucket in buckets), abs=0.05)
    assert nodes["use.heat"] == pytest.approx(sum(bucket["heat"] or 0 for bucket in buckets), abs=0.05)
    assert nodes["use.house"] == pytest.approx(sum(bucket["house"] for bucket in buckets), abs=0.05)
    # Allocated hour by hour, an hour that imported and exported is netted: never more than the meters.
    assert nodes["source.grid"] <= sum(bucket["grid_in"] for bucket in buckets)
    assert answer["period"] == "month" and answer["complete"] is False and answer["date"] == "2026-11-01"


def test_cost_prices_each_hour_and_counts_the_unpriced() -> None:
    inputs = month_inputs()
    answer = house_history(inputs)
    cost = answer["cost"]
    assert cost["periods"] == cost["priced"] == len(inputs.periods)
    expected = 0.0
    for item in inputs.periods:
        dear = (item.start + timedelta(hours=1)).hour in (17, 18, 19)
        expected += item.grid_in * (250.0 if dear else 150.0) - item.grid_out * 60.0
    assert cost["net"] == pytest.approx(expected, abs=0.05)
    assert sum(bucket["cost"] or 0 for bucket in answer["buckets"]) == pytest.approx(expected, abs=0.5)
    # Prices only from the 10th: the earlier hours are counted, not priced.
    start = datetime(2026, 11, 9, 23, tzinfo=timezone.utc)
    later = house_history(month_inputs(prices=flat_slots(start, NOW + timedelta(days=1))))
    assert later["cost"]["priced"] < later["cost"]["periods"]
    assert next(bucket for bucket in later["buckets"] if bucket["key"] == "2026-11-05")["cost"] is None


def test_an_hour_priced_by_quarters_is_their_mean_and_a_gap_leaves_it_unpriced() -> None:
    start = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
    slots = [PriceSlot(start + timedelta(minutes=15 * q), start + timedelta(minutes=15 * (q + 1)), 100.0 + 40 * q, 50.0) for q in range(4)]
    hour = EnergyHour(start=start, grid_in=1.0, grid_out=0.0)
    assert slot_prices(hour, slots) == pytest.approx((160.0, 50.0))
    assert slot_prices(hour, slots[:3]) is None


def test_missing_meters_and_prices() -> None:
    answer = house_history(month_inputs(periods=None, reviews=None))
    assert answer["energy_flow"] is None and answer["buckets"] == [] and answer["cost"] is None
    assert answer["review"] is None
    unpriced = house_history(month_inputs(money=None, prices=()))
    assert unpriced["cost"] is None and unpriced["money"] is None
    assert all(bucket["cost"] is None for bucket in unpriced["buckets"])


def test_a_dst_day_allocates_all_25_hours() -> None:
    period = period_of(PERIOD_DAY, date(2026, 10, 25))
    start, end = utc_bounds(period, TZ)
    hours = hours_of(start, end)
    answer = house_history(month_inputs(period=period, periods=[villa_hour(hour) for hour in hours], now=end + timedelta(days=2)))
    assert len(answer["buckets"]) == 25
    assert all(bucket["periods"] == 1 for bucket in answer["buckets"])
    assert answer["energy_flow"]["hours"] == 25
    assert answer["complete"] is True


def test_a_year_buckets_by_month_across_new_year() -> None:
    period = period_of(PERIOD_YEAR, date(2025, 6, 1))
    hours = [datetime(2025, 12, 31, 22, tzinfo=timezone.utc), datetime(2025, 12, 31, 23, tzinfo=timezone.utc)]
    answer = house_history(month_inputs(period=period, periods=[villa_hour(hour) for hour in hours], reviews=[]))
    december = next(bucket for bucket in answer["buckets"] if bucket["key"] == "2025-12")
    assert december["periods"] == 1  # 23:00 local; midnight is 2026's
    assert [bucket["key"] for bucket in answer["buckets"]][0] == "2025-01"
    assert answer["review"] is None


# ------------------------------------------------------------------------------------------------ reviews


def test_reviews_sum_over_the_period_and_say_how_many_days() -> None:
    answer = house_history(month_inputs())
    block = answer["review"]
    assert block["days"] == 13 and block["period_days"] == 15 and block["archived"] == 1
    assert block["battery_saving"] == pytest.approx(13 * 400.0)
    assert block["sun_gave"] == pytest.approx(13 * 700.0)
    assert block["against_nothing"] == pytest.approx(13 * 1100.0)
    assert block["actual_cost"] == pytest.approx(13 * 3000.0)
    assert block["plan_saving"] == pytest.approx(13 * 150.0)
    assert block["own_saving"] == pytest.approx(13 * 50.0)
    assert block["modes"]["max_profit"] == {"saving": pytest.approx(13 * 200.0), "days": 13}
    assert block["modes"]["self_consumption"] == {"saving": None, "days": 0}
    assert block["mode"] == "balanced"


def test_reviews_outside_the_period_and_without_figures() -> None:
    reviews = [review(date(2026, 10, 31)), review(date(2026, 11, 2), no_battery_cost=None, sun_quarters=None)]
    block = house_history(month_inputs(reviews=reviews))["review"]
    assert block["days"] == 1
    assert block["battery_saving"] is None and block["battery_days"] == 0
    assert block["sun_gave"] is None and block["against_nothing"] is None


def test_a_bucket_carries_the_battery_money_of_its_reviewed_days() -> None:
    buckets = house_history(month_inputs())["buckets"]
    by_key = {bucket["key"]: bucket for bucket in buckets}
    assert by_key["2026-11-05"]["battery_saving"] == pytest.approx(400.0) and by_key["2026-11-05"]["battery_days"] == 1
    assert by_key["2026-11-02"]["battery_saving"] is None and by_key["2026-11-02"]["battery_days"] == 0
    # A reviewed day that made the battery dearer is kept negative.
    worse = house_history(month_inputs(reviews=[review(date(2026, 11, 5), no_battery_cost=2900.0)]))["buckets"]
    assert {bucket["key"]: bucket for bucket in worse}["2026-11-05"]["battery_saving"] == pytest.approx(-100.0)


def test_a_year_bucket_sums_the_month_and_the_hours_of_a_day_have_none() -> None:
    year = period_of(PERIOD_YEAR, date(2026, 1, 1))
    start, _end = utc_bounds(year, TZ)
    first = house_history(month_inputs(period=year, periods=[], reviews=[review(date(2026, 11, 3)), review(date(2026, 11, 4)), review(date(2026, 10, 30))]))
    by_key = {bucket["key"]: bucket for bucket in first["buckets"]}
    assert by_key["2026-11"]["battery_saving"] == pytest.approx(800.0) and by_key["2026-11"]["battery_days"] == 2
    assert by_key["2026-10"]["battery_days"] == 1 and by_key["2026-01"]["battery_saving"] is None
    day = period_of(PERIOD_DAY, date(2026, 11, 15))
    hourly = house_history(month_inputs(period=day, reviews=[review(date(2026, 11, 15))]))["buckets"]
    assert all(bucket["battery_saving"] is None and bucket["battery_days"] == 0 for bucket in hourly)


def test_forecast_errors_are_the_mean_over_the_period() -> None:
    forecast = house_history(month_inputs())["forecast"]
    assert forecast["house"] == {"mae": 0.5, "days": 2}
    assert forecast["sun"] is None and forecast["soc"] is None


def test_archived_reviews_are_read_from_exported_days(tmp_path: Path) -> None:
    kept = review(date(2026, 9, 3))
    (tmp_path / "2026-09-03.json").write_text(json.dumps({"battery_review": review_part(kept)}), encoding="utf-8")
    (tmp_path / "2026-09-04.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "2026-09-05.json").write_text(json.dumps({"battery_review": None}), encoding="utf-8")
    found = read_archived_reviews(tmp_path, [date(2026, 9, day) for day in range(1, 7)])
    assert list(found) == [date(2026, 9, 3)]
    assert found[date(2026, 9, 3)].battery_saving == pytest.approx(400.0)
    assert read_archived_reviews(tmp_path / "missing", [date(2026, 9, 3)]) == {}


def test_a_day_without_a_share_says_why_and_the_month_counts_whole_days() -> None:
    review = house_history(month_inputs())["review"]
    notes = {row["date"]: row["share_note"] for row in review["by_day"]}
    assert notes["2026-11-07"] == "partial" and notes["2026-11-09"] == "small" and notes["2026-11-05"] is None
    assert notes["2026-11-10"] is None
    shares = {row["date"]: row["own_share"] for row in review["by_day"]}
    assert shares["2026-11-07"] is None and shares["2026-11-09"] is None and shares["2026-11-10"] is not None
    perfect = review["perfect"]
    # Days 6 to 15 have a perfect plan; the 7th is partial and left out, the 9th counts in the sums.
    assert perfect["days"] == 9 and perfect["partial"] == 1 and perfect["shares"]["own"] is not None


# ------------------------------------------------------------------------------------------------ fixtures


def test_contract_fixtures() -> None:
    _fixture("villa_month.json", house_history(month_inputs()))
    day = period_of(PERIOD_DAY, date(2026, 11, 15))
    start, end = utc_bounds(day, TZ)
    quarters = []
    for hour in hours_of(start, end):
        whole = villa_hour(hour)
        for q in range(4):
            quarters.append(
                EnergyHour(
                    start=hour + timedelta(minutes=15 * q),
                    minutes=15,
                    grid_in=whole.grid_in / 4,
                    grid_out=whole.grid_out / 4,
                    solar=(whole.solar or 0) / 4,
                    battery_in=(whole.battery_in or 0) / 4,
                    battery_out=(whole.battery_out or 0) / 4,
                    car=(whole.car or 0) / 4,
                    heat=(whole.heat or 0) / 4,
                )
            )
    _fixture(
        "villa_day.json",
        house_history(
            month_inputs(
                period=day,
                periods=quarters,
                step_min=15,
                forecast_mae={
                    "house_kw": {date(2026, 11, 15): 0.42},
                    "sun_kwh": {date(2026, 11, 15): 0.12},
                    "sun_kwh_corrected": {date(2026, 11, 15): 0.09},
                    "battery": {date(2026, 11, 15): 6.5},
                },
            )
        ),
    )
    year = period_of(PERIOD_YEAR, date(2026, 1, 1))
    first, _ = utc_bounds(year, TZ)
    hours = [first + timedelta(hours=offset) for offset in range(0, 24 * 319, 3)]
    _fixture("house_alone_year.json", house_history(month_inputs(period=year, periods=[EnergyHour(start=h, grid_in=2.4, grid_out=0.0) for h in hours], has_solar=False, has_battery=False, has_heat=False, reviews=None)))
