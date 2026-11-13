"""The defensive battery plan's inputs (`battery/defensive.py`): the house's p90 in the dear hours, the sun's p10."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.defensive import DEAR_SHARE, dear_hours, defensive_inputs
from custom_components.spotnav.battery.plan_shadow import PriceSlot
from custom_components.spotnav.site.forecast_band import FORM_ADDITIVE, FORM_RATIO, POOL_CELL, ErrorBand, Spread

UTC = timezone.utc
TZ = "Europe/Stockholm"
#: Midnight in Stockholm on a winter day (UTC+1).
MIDNIGHT = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)


def _prices(buy: list[float], *, minutes: int = 60) -> list[PriceSlot]:
    step = timedelta(minutes=minutes)
    return [PriceSlot(MIDNIGHT + step * i, MIDNIGHT + step * (i + 1), price, 40.0) for i, price in enumerate(buy)]


def _band(form: str, low: float, high: float) -> ErrorBand:
    cells = {(kind, hour): Spread(low, high, 20, POOL_CELL) for kind in ("weekday", "weekend") for hour in range(24)}
    if form == FORM_RATIO:
        cells = {(None, hour): Spread(low, high, 20, POOL_CELL) for hour in range(24)}
    return ErrorBand(form, TZ, 14, cells, None, form == FORM_ADDITIVE)


def test_the_dear_hours_are_the_dearest_share_of_the_horizon() -> None:
    buy = [float(price) for price in range(1, 25)]
    dear = dear_hours(_prices(buy), MIDNIGHT, TZ)
    assert DEAR_SHARE == 0.5 and len(dear) == 12
    assert min(dear) == MIDNIGHT + timedelta(hours=12)
    # Quarter-hour prices: an hour counts by its mean; hours already gone do not count.
    quarters = _prices([10.0, 10.0, 10.0, 90.0] + [20.0] * 4 + [25.0] * 4, minutes=15)
    assert dear_hours(quarters, MIDNIGHT, TZ, share=1 / 3) == {MIDNIGHT}
    later = MIDNIGHT + timedelta(hours=1, minutes=10)
    assert dear_hours(quarters, later, TZ, share=0.5) == {MIDNIGHT + timedelta(hours=2)}
    assert dear_hours([], MIDNIGHT, TZ) == set()


def test_the_house_takes_its_p90_in_the_dear_hours_only_and_the_sun_its_p10() -> None:
    buy = [50.0] * 12 + [300.0] * 12
    house = {MIDNIGHT + timedelta(hours=h): 1.0 for h in range(24)}
    solar = {MIDNIGHT + timedelta(hours=h): 2000.0 if 8 <= h <= 14 else 0.0 for h in range(24)}
    inputs = defensive_inputs(
        house, solar, _band(FORM_ADDITIVE, -0.2, 0.8), _band(FORM_RATIO, 0.5, 1.3), _prices(buy), MIDNIGHT, TZ
    )
    assert inputs is not None and inputs.house_band and inputs.sun_band
    assert [inputs.house_by_hour[MIDNIGHT + timedelta(hours=h)] for h in (0, 11, 12, 23)] == pytest.approx(
        [1.0, 1.0, 1.8, 1.8]
    )
    assert inputs.solar_wh_by_hour is not None
    assert inputs.solar_wh_by_hour[MIDNIGHT + timedelta(hours=12)] == pytest.approx(1000.0)
    assert inputs.solar_wh_by_hour[MIDNIGHT + timedelta(hours=2)] == 0.0
    assert inputs.dear_hours == 12
    assert inputs.house_kwh_added == pytest.approx(12 * 0.8)
    assert inputs.sun_kwh_removed == pytest.approx(7 * 1.0)
    assert inputs.as_dict()["dear_hours"] == 12
    # The inputs it was given are left as they were.
    assert house[MIDNIGHT + timedelta(hours=12)] == 1.0 and solar[MIDNIGHT + timedelta(hours=12)] == 2000.0


def test_a_side_still_learning_keeps_its_point_forecast() -> None:
    house = {MIDNIGHT + timedelta(hours=h): 1.0 for h in range(24)}
    solar = {MIDNIGHT + timedelta(hours=12): 2000.0}
    only_sun = defensive_inputs(house, solar, None, _band(FORM_RATIO, 0.5, 1.3), _prices([1.0] * 24), MIDNIGHT, TZ)
    assert only_sun is not None and only_sun.house_by_hour == house and only_sun.dear_hours == 0
    assert only_sun.solar_wh_by_hour == {MIDNIGHT + timedelta(hours=12): 1000.0}
    only_house = defensive_inputs(
        house, None, _band(FORM_ADDITIVE, 0.0, 0.5), _band(FORM_RATIO, 0.5, 1.3), _prices([1.0] * 24), MIDNIGHT, TZ
    )
    assert only_house is not None and only_house.solar_wh_by_hour is None and not only_house.sun_kwh_removed


def test_nothing_to_change_gives_no_defensive_inputs() -> None:
    house = {MIDNIGHT: 1.0}
    assert defensive_inputs(house, {MIDNIGHT: 100.0}, None, None, _prices([1.0]), MIDNIGHT, TZ) is None
    # A sun band without a solar forecast changes nothing either.
    assert defensive_inputs(house, None, None, _band(FORM_RATIO, 0.5, 1.0), _prices([1.0]), MIDNIGHT, TZ) is None
