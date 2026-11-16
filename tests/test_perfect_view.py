"""The reviewed days against their perfect plans, summed for a card (`site/perfect_view.py`). Pure."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from custom_components.spotnav.battery.hindsight import CellTemperature
from custom_components.spotnav.site.perfect_view import PERFECT_KEYS, TEMPERATURE_KEYS, WORK_KEYS, WORK_WAYS, perfect_block

from tests.test_house_history import review
from tests.test_house_view import facit_for


def test_days_are_summed_before_the_share_so_a_day_boundary_washes_out() -> None:
    # Day one ended with energy the next day used: low on the first day, high on the second.
    first = review(date(2026, 11, 3), facit=facit_for(date(2026, 11, 3), 1000.0, 300.0, 900.0, 800.0))
    second = review(date(2026, 11, 4), facit=facit_for(date(2026, 11, 4), 1000.0, 1000.0, 900.0, 800.0, provisional=True))
    block = perfect_block([first, second])
    assert block is not None and tuple(block) == PERFECT_KEYS
    assert block["days"] == 2 and block["provisional"] == 1 and block["mode"] == "balanced"
    assert block["saving"] == 2000.0
    assert block["shares"]["own"] == 65.0  # 1300 of 2000, not the mean of 30 % and 100 %
    assert block["savings"]["own"] == 1300.0
    assert set(block["work"]) == set(WORK_WAYS) and all(set(item) == set(WORK_KEYS) for item in block["work"].values())
    assert block["work"]["own"]["switches"] == 8 and block["work"]["perfect"]["discharged_kwh"] == 36.0
    assert block["temperature"] is None and block["clamped"] == []


def test_a_way_above_the_perfect_plan_or_below_no_battery_is_clamped_and_named() -> None:
    above = review(date(2026, 11, 3), facit=facit_for(date(2026, 11, 3), 1000.0, 1040.0, -300.0, 800.0))
    block = perfect_block([above])
    assert block["shares"]["own"] == 100.0 and block["shares"]["balanced"] == 0.0
    assert block["clamped"] == ["own", "balanced"]


def test_no_perfect_plan_and_no_temperature_is_no_block_and_temperature_alone_is_one() -> None:
    assert perfect_block([review(date(2026, 11, 3))]) is None
    assert perfect_block([]) is None
    warm = review(date(2026, 11, 3), cell_temperature=CellTemperature(22.0, 31.0, 20, 1440))
    cool = review(date(2026, 11, 4), cell_temperature=CellTemperature(20.0, 24.0, 0, 720))
    block = perfect_block([warm, cool])
    assert block["days"] == 0 and block["saving"] is None and block["mode"] is None
    assert all(value is None for value in block["shares"].values())
    assert tuple(block["temperature"]) == TEMPERATURE_KEYS
    assert block["temperature"] == {"days": 2, "mean_c": pytest.approx(21.3), "max_c": 31.0, "minutes_above": 20}


def test_a_mode_that_did_not_run_has_no_share() -> None:
    made = facit_for(date(2026, 11, 3), 1000.0, 500.0, 900.0, 800.0)
    made = replace(made, candidates={key: value for key, value in made.candidates.items() if key != "max_profit"})
    block = perfect_block([review(date(2026, 11, 3), facit=made)])
    assert block["shares"]["max_profit"] is None and block["work"]["max_profit"] is None
    assert block["savings"]["max_profit"] is None
