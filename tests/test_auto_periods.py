"""Automatic charge periods: the planner weighs a start cost per period and keeps every block at least 30 minutes.

Pure arithmetic over `cheapest_slots` and `calculate_plan`; nothing here touches Home Assistant.
"""

from __future__ import annotations

import itertools
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from custom_components.spotnav.planning.planner import (
    AUTO_PERIODS,
    SHORTEST_BLOCK_SLOTS,
    START_COST_KWH,
    FiscalChoice,
    PlanningSlot,
    calculate_plan,
    cheapest_slots,
    energy_per_slot_kwh,
    start_cost_minor,
)

from .planning import build_request, parse_documents

START = datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc)
#: One quarter-hour at 16 A on three phases at 400 V: about 2.77 kWh.
PER_SLOT_11KW = energy_per_slot_kwh(16, 3)


def slots_of(prices: list[float]) -> list[PlanningSlot]:
    return [PlanningSlot(START + timedelta(minutes=15 * index), price, "d") for index, price in enumerate(prices)]


def indices(slots: list[PlanningSlot], chosen: tuple[PlanningSlot, ...] | None) -> tuple[int, ...] | None:
    return None if chosen is None else tuple(slots.index(slot) for slot in chosen)


def runs_of(chosen: tuple[int, ...]) -> list[int]:
    """The lengths of the contiguous runs in chosen indices."""
    lengths: list[int] = []
    for position, index in enumerate(chosen):
        if position and index == chosen[position - 1] + 1:
            lengths[-1] += 1
        else:
            lengths.append(1)
    return lengths


def test_the_constants_are_what_the_docs_say() -> None:
    assert AUTO_PERIODS is None
    assert START_COST_KWH == 0.25
    assert SHORTEST_BLOCK_SLOTS == 2


def test_the_start_cost_follows_the_price_level_of_the_window() -> None:
    """A quarter of a kWh at the window's mean effective price, in the area's minor unit: the same rule in every market."""
    slots = slots_of([1.0, 2.0, 3.0])
    assert start_cost_minor(slots, FiscalChoice()) == pytest.approx(0.25 * 200)
    # Ten times the price level (a currency with a smaller unit) is ten times the start cost.
    assert start_cost_minor(slots_of([10.0, 20.0, 30.0]), FiscalChoice()) == pytest.approx(0.25 * 2000)
    # Negative prices do not make a start pay: the level is the mean of the magnitudes.
    assert start_cost_minor(slots_of([-1.0, 1.0]), FiscalChoice()) == pytest.approx(0.25 * 100)
    # Tax and VAT are part of what a start costs.
    taxed = FiscalChoice(tax_enabled=True, tax_minor_per_kwh=100.0)
    assert start_cost_minor(slots_of([1.0]), taxed) == pytest.approx(0.25 * 200)


def test_two_blocks_split_by_a_slightly_dearer_quarter_hour_merge_into_one() -> None:
    """5 % dearer in the middle costs less than a second start: one block, the latest of the equal ones."""
    prices = [2.0, 2.0, 1.0, 1.0, 1.05, 1.0, 1.0, 2.0, 2.0]
    slots = slots_of(prices)
    auto = indices(slots, cheapest_slots(slots, 4, PER_SLOT_11KW, AUTO_PERIODS, FiscalChoice(), None))
    assert auto == (3, 4, 5, 6)
    assert runs_of(auto) == [4]
    # A number keeps today's hard cap with no start cost: two blocks, the four cheapest quarter-hours.
    capped = indices(slots, cheapest_slots(slots, 4, PER_SLOT_11KW, 2, FiscalChoice(), None))
    assert capped == (2, 3, 5, 6)


def test_a_clearly_expensive_peak_keeps_two_blocks() -> None:
    """Half an hour at three times the price is far more than one start: two blocks around it."""
    prices = [2.0, 2.0, 1.0, 1.0, 3.0, 3.0, 1.0, 1.0, 2.0, 2.0]
    slots = slots_of(prices)
    auto = indices(slots, cheapest_slots(slots, 4, PER_SLOT_11KW, AUTO_PERIODS, FiscalChoice(), None))
    assert auto == (2, 3, 6, 7)
    assert runs_of(auto) == [2, 2]


def test_no_block_is_shorter_than_half_an_hour() -> None:
    """Single cheap quarter-hours between dear ones: auto takes whole half hours, a number may not."""
    prices = [1.0, 3.0, 1.0, 3.0, 1.0, 3.0, 1.0, 3.0]
    slots = slots_of(prices)
    auto = indices(slots, cheapest_slots(slots, 2, PER_SLOT_11KW, AUTO_PERIODS, FiscalChoice(), None))
    assert auto is not None and min(runs_of(auto)) >= SHORTEST_BLOCK_SLOTS
    assert indices(slots, cheapest_slots(slots, 2, PER_SLOT_11KW, 8, FiscalChoice(), None)) == (4, 6)
    # Three quarter-hours cannot be two blocks of at least two: one block of three.
    three = indices(slots, cheapest_slots(slots, 3, PER_SLOT_11KW, AUTO_PERIODS, FiscalChoice(), None))
    assert three is not None and runs_of(three) == [3]


def test_a_need_of_one_quarter_hour_is_one_quarter_hour() -> None:
    """The shortest block gives way when the whole need is smaller than it."""
    prices = [3.0, 1.0, 3.0, 2.0, 2.0]
    slots = slots_of(prices)
    assert indices(slots, cheapest_slots(slots, 1, PER_SLOT_11KW, AUTO_PERIODS, FiscalChoice(), None)) == (1,)


def test_equal_costs_go_to_the_latest_slots_before_the_departure() -> None:
    """Flat prices and a departure at 07:00: one block ending exactly at the departure."""
    day = "2026-09-13"
    documents = parse_documents(
        [
            {
                "area": "SE4", "date": "2026-09-12", "tz": "Europe/Stockholm", "res": 15,
                "start": "2026-09-12T00:00:00+02:00", "unit": "EUR/kWh", "prices": [1.0] * 96,
            },
            {
                "area": "SE4", "date": day, "tz": "Europe/Stockholm", "res": 15,
                "start": f"{day}T00:00:00+02:00", "unit": "EUR/kWh", "prices": [1.0] * 96,
            },
        ]
    )
    request = build_request(
        {
            "area_id": "SE4", "timezone": "Europe/Stockholm", "currency": "EUR", "now": "2026-09-12T20:00:00+02:00",
            "phases": 3, "amps": 16, "requested_kwh": 10.0, "consumption_kwh_per_10km": 2.0,
            "max_periods": AUTO_PERIODS, "departure": "07:00",
        },
        documents,
    )
    result = calculate_plan(request)
    assert result.reason is None
    assert len(result.periods) == 1
    assert result.periods[0][1] == datetime.fromisoformat(f"{day}T07:00:00+02:00")


def _cost(prices: list[float], chosen: tuple[int, ...], per_slot: float) -> float:
    total = 0.0
    for index in chosen:
        total += per_slot * prices[index] * 100
    return total


@pytest.mark.parametrize("seed", range(4))
def test_auto_is_the_cheapest_plan_counting_each_start(seed: int) -> None:
    """Against a brute force: lowest cost plus a start cost per block, every block at least two quarter-hours
    (or the whole need), ties to the latest slots. So auto never costs more than any other plan plus its starts."""
    rng = random.Random(seed)
    for _ in range(150):
        count = rng.randint(3, 10)
        prices = [rng.choice([0.9, 1.0, 1.0, 1.05, 1.5, 3.0]) for _ in range(count)]
        slots = slots_of(prices)
        needed = rng.randint(1, count)
        per_slot = rng.choice([0.92, 1.84, PER_SLOT_11KW])
        start = start_cost_minor(slots, FiscalChoice())
        shortest = min(SHORTEST_BLOCK_SLOTS, needed)

        def feasible(chosen: tuple[int, ...]) -> bool:
            return min(runs_of(chosen)) >= shortest

        def objective(chosen: tuple[int, ...]) -> float:
            return _cost(prices, chosen, per_slot) + start * len(runs_of(chosen))

        plans = [c for c in itertools.combinations(range(count), needed) if feasible(c)]
        best = min(plans, key=lambda c: (round(objective(c), 9), tuple(-i for i in reversed(c))))
        found = indices(slots, cheapest_slots(slots, needed, per_slot, AUTO_PERIODS, FiscalChoice(), None))
        assert found is not None, (prices, needed)
        assert objective(found) == pytest.approx(objective(best)), (prices, needed, per_slot)
        assert min(runs_of(found)) >= shortest
        # The spec's bound: never dearer than the best plan of any number, plus that plan's starts.
        for cap in range(1, 9):
            capped = indices(slots, cheapest_slots(slots, needed, per_slot, cap, FiscalChoice(), None))
            if capped is not None and feasible(capped):
                assert _cost(prices, found, per_slot) <= _cost(prices, capped, per_slot) + start * len(
                    runs_of(capped)
                ) + 1e-9


def test_numbers_one_to_eight_keep_their_hard_cap() -> None:
    """A number is the old behaviour: the cheapest slots in at most that many blocks, no start cost, no shortest block."""
    prices: list[Any] = [1.0, 3.0, 1.0, 3.0, 1.0, 3.0, 1.0, 3.0, 1.0]
    slots = slots_of(prices)
    for cap in range(1, 9):
        chosen = indices(slots, cheapest_slots(slots, 3, PER_SLOT_11KW, cap, FiscalChoice(), None))
        assert chosen is not None
        assert len(runs_of(chosen)) <= cap
    assert indices(slots, cheapest_slots(slots, 3, PER_SLOT_11KW, 3, FiscalChoice(), None)) == (4, 6, 8)
