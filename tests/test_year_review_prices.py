"""The year review's prices from the year store's day parts (`compute.PriceDay`): the review worked out from the
parts is the one the whole-year path worked out from the year's slots (`tests/fixtures/year_review_old_path.json`,
written by that path before it was replaced), on the six site shapes and on 15-minute prices across the clock
changes, gaps, part days, ties, zero and negative sell prices and charges across midnight; a part read back from the
store is the same part; a stretch worked out a few days at a time gives the same parts as the whole year at once."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.spotnav.year_review.compute import DayParts, PriceDay, compute_review, price_days_of

from tests.test_year_review import (
    ZONE,
    car_only_site,
    fresh_site,
    full_site,
    nord_pool_site,
    priced,
    sun_battery_site,
)
from tests.year_review_variants import quarter_slots, variants, wavy

OLD_PATH = Path(__file__).parent / "fixtures" / "year_review_old_path.json"


def _old(name: str) -> dict[str, Any]:
    return json.loads(OLD_PATH.read_text(encoding="utf-8"))[name]


def _plain(review: dict[str, Any] | None) -> Any:
    return json.loads(json.dumps(review))


@pytest.mark.parametrize("name", sorted(variants()))
def test_the_review_from_the_day_parts_is_the_whole_year_path_s(name: str) -> None:
    inputs, slots = variants()[name]
    assert _plain(compute_review(priced(inputs, slots))) == _old(name)


@pytest.mark.parametrize(
    ("name", "shape"),
    [
        ("shape_full", full_site),
        ("shape_fresh", fresh_site),
        ("shape_car", car_only_site),
        ("shape_sun", sun_battery_site),
        ("shape_np", nord_pool_site),
    ],
)
def test_the_site_shapes_are_the_whole_year_path_s(name: str, shape: Any) -> None:
    assert _plain(compute_review(shape())) == _old(name)


def test_a_part_read_back_from_the_store_is_the_same_part_and_small() -> None:
    slots = quarter_slots(date(2026, 3, 28), date(2026, 3, 31), wavy, lambda buy: buy * 0.41 - 3.7)
    days = price_days_of(slots, ZONE)
    # The spring clock change: 23 hours, 92 quarters, one run.
    spring = days[date(2026, 3, 29)]
    assert spring.runs == ((0, 900, 92),) and spring.covered_s == 23 * 3600 and len(spring.sells) == 23
    for day, price in days.items():
        stored = json.loads(json.dumps(DayParts(price=price).as_dict()))
        assert DayParts.from_dict(stored) == DayParts(price=price), day
        assert len(json.dumps(stored)) < 400
    assert DayParts.from_dict({"price": {}}) == DayParts(price=PriceDay())
    assert DayParts.from_dict({"price": {"r": "x"}}) == DayParts()
    assert DayParts.from_dict({"price": {"r": [[0, 900, 0]]}}) == DayParts()


def test_a_newer_price_part_replaces_the_held_one_and_a_saving_merge_keeps_it() -> None:
    price = PriceDay(runs=((0, 3600, 24),), cheap=0xFF, sells=(1.0,) * 24, top=(8, 300.0))
    held = DayParts(battery=10.0, price=PriceDay())
    assert held.merged(DayParts(price=price)) == DayParts(battery=10.0, price=price)
    assert DayParts(price=price).merged(DayParts(battery=4.0)) == DayParts(battery=4.0, price=price)
    # A price part alone is no saving day.
    assert not DayParts(price=price).any


def test_a_few_days_at_a_time_gives_the_whole_year_s_parts() -> None:
    first, end = date(2026, 1, 1), date(2026, 12, 15)
    slots = quarter_slots(first - timedelta(days=1), end + timedelta(days=1), wavy, lambda buy: buy - 5.0)
    whole = {day: part for day, part in price_days_of(slots, ZONE).items() if first <= day < end}
    pieces: dict[date, PriceDay] = {}
    day = first
    while day < end:
        stop = min(end, day + timedelta(days=7))
        # As `period_price_slots` reads a stretch: a day either side too.
        window = [slot for slot in slots if day - timedelta(days=1) <= slot.start.astimezone(ZONE).date() <= stop]
        for found, part in price_days_of(window, ZONE).items():
            if day <= found < stop:
                pieces[found] = part
        day = stop
    assert pieces == whole
