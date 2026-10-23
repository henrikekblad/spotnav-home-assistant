"""An electricity price's arithmetic: the hours each day is on, the clock-change days, and the next change."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.spotnav.planning.auto_settings import AreaAutoSettings, FiscalOverride
from custom_components.spotnav.planning.planner import effective_minor_per_kwh
from custom_components.spotnav.pricing.price_points import (
    CHEAP,
    EXPENSIVE,
    fiscal_for,
    next_price_change,
    next_switch,
    overrides_from_data,
    point_at,
    price_points,
    PricePoint,
    rank,
)
from custom_components.spotnav.pricing.relay_contract import parse_catalogue, parse_day

from .relay import fixture

DAY = date(2026, 9, 22)
START = datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)


def points(prices: list[float], *, minutes: int = 15, day: date = DAY, start: datetime = START) -> list[PricePoint]:
    step = timedelta(minutes=minutes)
    return [
        PricePoint(start=start + step * index, end=start + step * (index + 1), day=day, spot=price, price=price)
        for index, price in enumerate(prices)
    ]


def chosen_indexes(items: list[PricePoint], hours: float, kind: str = CHEAP) -> list[int]:
    ranking = rank(items, hours, kind)
    return [index for index, point in enumerate(items) if point.start in ranking.chosen]


def test_the_cheapest_quarters_are_chosen_one_by_one_and_a_tie_goes_to_the_earlier() -> None:
    items = points([5, 1, 3, 1, 2, 1, 4, 9])

    # Four quarters are one hour; three cost 1, then the 2 beats the 3.
    assert chosen_indexes(items, 1.0) == [1, 3, 4, 5]
    # Two of the three equal quarters: the earlier two.
    assert chosen_indexes(items, 0.5) == [1, 3]
    assert rank(items, 0.5, CHEAP).thresholds == {DAY: 1}


def test_expensive_is_the_mirror_and_its_threshold_is_the_cheapest_chosen() -> None:
    items = points([5, 1, 9, 1, 2, 9, 4, 3])

    assert chosen_indexes(items, 0.75, EXPENSIVE) == [0, 2, 5]
    assert chosen_indexes(items, 0.25, EXPENSIVE) == [2]
    assert rank(items, 0.75, EXPENSIVE).thresholds == {DAY: 5}


def test_no_hours_is_never_on_and_24_hours_is_the_whole_day() -> None:
    items = points([float(index % 7) for index in range(96)])

    assert chosen_indexes(items, 0) == []
    assert rank(items, 0, CHEAP).thresholds == {DAY: None}
    assert len(chosen_indexes(items, 24)) == 96
    assert len(chosen_indexes(items, 24, EXPENSIVE)) == 96


def test_fractional_hours_are_quarters_with_quarter_prices() -> None:
    items = points([float(index) for index in range(96)])

    assert chosen_indexes(items, 2.25) == list(range(9))
    assert chosen_indexes(items, 0.25, EXPENSIVE) == [95]


@pytest.mark.parametrize(("hours", "count"), [(0.25, 0), (0.5, 1), (1.25, 1), (1.5, 2), (2.75, 3)])
def test_with_hourly_prices_a_fraction_rounds_to_the_nearest_whole_hour(hours: float, count: int) -> None:
    items = points([float(index) for index in range(24)], minutes=60)

    assert chosen_indexes(items, hours) == list(range(count))


def test_each_day_is_ranked_on_its_own() -> None:
    today = points([1, 2, 3, 4])
    tomorrow = points([0.5, 9, 9, 9], day=DAY + timedelta(days=1), start=START + timedelta(days=1))

    ranking = rank(today + tomorrow, 0.25, CHEAP)

    # Tomorrow's cheap quarter does not take today's place.
    assert ranking.chosen == {today[0].start, tomorrow[0].start}
    assert ranking.thresholds == {DAY: 1, DAY + timedelta(days=1): 0.5}


SE4_FEES = AreaAutoSettings(
    area_id="SE4",
    vat=FiscalOverride(enabled=True),
    tax=FiscalOverride(enabled=True),
    transfer=FiscalOverride(enabled=True, value=12.5),
)


def se4():
    return parse_catalogue(json.loads(fixture("areas.json"))).area("SE4")


@pytest.mark.parametrize(
    ("name", "day", "count"),
    [("day_SE4_2026-03-29_92.json", date(2026, 3, 29), 92), ("day_SE4_2025-10-26_100.json", date(2025, 10, 26), 100)],
)
def test_a_clock_change_day_has_its_own_number_of_quarters(name: str, day: date, count: int) -> None:
    document = parse_day(json.loads(fixture(name)), area_id="SE4", day=day)
    fiscal = fiscal_for(SE4_FEES, se4())
    items = price_points((document,), currency="SEK", fiscal=fiscal)

    assert len(items) == count and {point.day for point in items} == {day}
    assert len(rank(items, 6, CHEAP).chosen) == 24
    assert len(rank(items, 6.25, EXPENSIVE).chosen) == 25
    # 24 hours is the whole day, also when the day has 25 of them; and 23 hours of a 23-hour day too.
    assert len(rank(items, 24, CHEAP).chosen) == count
    assert len(rank(items, 23, CHEAP).chosen) == min(count, 92)


def test_the_price_is_the_planners_own_effective_price_in_the_major_unit() -> None:
    document = parse_day(json.loads(fixture("day_SE4_2026-09-22_96.json")), area_id="SE4", day=DAY)
    fiscal = fiscal_for(SE4_FEES, se4())

    items = price_points((document,), currency="SEK", fiscal=fiscal)

    # The area's suggestions for VAT (25 %) and energy tax (36 öre), and a grid fee of our own.
    assert fiscal is not None and fiscal.vat_percent == 25 and fiscal.tax_minor_per_kwh == 36
    assert fiscal.transfer_minor_per_kwh == 12.5
    first = document.intervals[0].eur_per_kwh * 11.275
    assert items[0].spot == pytest.approx(first)
    assert items[0].price == pytest.approx(effective_minor_per_kwh(first, fiscal) / 100)
    assert items[0].price == pytest.approx((first * 100 + 36 + 12.5) * 1.25 / 100)


def test_a_suggestion_the_area_does_not_publish_leaves_the_fees_unresolved() -> None:
    catalogue = parse_catalogue(json.loads(fixture("areas.json")))
    fees = overrides_from_data("DE-LU", {"vat": {"enabled": True, "value": None}})

    assert fiscal_for(fees, catalogue.area("DE-LU")) is None
    assert fiscal_for(overrides_from_data("DE-LU", {}), catalogue.area("DE-LU")) is not None


def test_the_next_price_change_skips_equal_neighbours() -> None:
    items = points([1, 1, 2, 2, 2])

    assert next_price_change(items, START + timedelta(minutes=1)) == items[2].start
    assert next_price_change(items, START + timedelta(minutes=31)) == items[-1].end
    assert next_price_change(items, START - timedelta(minutes=5)) == START
    assert point_at(items, items[-1].end) is None


def test_the_next_switch_is_where_the_ranking_turns() -> None:
    items = points([1, 1, 5, 5, 1, 5])
    ranking = rank(items, 0.75, CHEAP)

    assert next_switch(items, ranking, START) == items[2].start
    assert next_switch(items, ranking, items[2].start) == items[4].start
    assert next_switch(items, ranking, items[4].start) == items[5].start
    # Off at the last interval and nothing known after it: no change to name.
    assert next_switch(items, ranking, items[5].start) is None
