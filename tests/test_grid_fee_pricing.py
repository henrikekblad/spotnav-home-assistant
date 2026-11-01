"""The grid fee as a percentage of the spot price (A) and the time-of-use grid fee (B), in the planner's arithmetic.

Pure: `effective_minor_per_kwh` with the interval's start, the high-load window on the area's clock (window
edges, weekends, the winter months, both DST days, quarter-hour prices) and a plan that steps around the window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.planning.planner import (
    FiscalChoice,
    HighLoadWindow,
    PlanRequest,
    PlannerInputError,
    calculate_plan,
    chart_intervals,
    effective_minor_per_kwh,
    high_load_window_problem,
    planning_slots,
)
from .planning import parse_documents

STOCKHOLM = ZoneInfo("Europe/Stockholm")
TZ = "Europe/Stockholm"

#: Weekdays 07-19, November to March: the common Swedish time-of-use window.
WINDOW = HighLoadWindow(weekdays_only=True, from_hour=7, to_hour=19, winter_only=True, tz=TZ)


def _local(*args: int) -> datetime:
    return datetime(*args, tzinfo=STOCKHOLM)


def _owner(**extra: object) -> FiscalChoice:
    """The owner's tariff: energy tax 36 öre, a fixed grid fee of 16 öre ex VAT plus 5 % of spot, VAT 25 %."""
    return FiscalChoice(
        tax_enabled=True,
        tax_minor_per_kwh=36,
        transfer_enabled=True,
        transfer_minor_per_kwh=16,
        vat_enabled=True,
        vat_percent=25,
        transfer_percent_enabled=True,
        transfer_percent=5,
        **extra,  # type: ignore[arg-type]
    ).validated()


def test_the_percentage_part_is_added_before_vat_as_the_owners_bill_shows() -> None:
    fiscal = _owner()
    # 1.00 kr spot: (1.05 x 100 + 36 + 16) x 1.25 = 196.25 öre.
    assert effective_minor_per_kwh(1.0, fiscal) == pytest.approx((1.05 * 100 + 36 + 16) * 1.25)
    assert effective_minor_per_kwh(1.0, fiscal) == pytest.approx(196.25)


def test_the_percentage_part_follows_a_negative_spot_price() -> None:
    fiscal = _owner()
    # -0.20 kr spot: the 5 % part is -1 öre, not clamped at zero: (1.05 x -20 + 36 + 16) x 1.25 = 38.75.
    assert effective_minor_per_kwh(-0.2, fiscal) == pytest.approx((1.05 * -20 + 36 + 16) * 1.25)
    assert effective_minor_per_kwh(-0.2, fiscal) == pytest.approx(38.75)
    # Zero spot adds nothing for the percentage.
    assert effective_minor_per_kwh(0.0, fiscal) == pytest.approx((36 + 16) * 1.25)


def test_without_the_new_parts_the_price_is_the_same_float_as_before() -> None:
    old = FiscalChoice(
        tax_enabled=True, tax_minor_per_kwh=36, transfer_enabled=True, transfer_minor_per_kwh=16,
        vat_enabled=True, vat_percent=25,
    ).validated()
    for spot in (-0.3, 0.0, 0.123456, 1.0, 3.7):
        expected = spot * 100
        expected += 36
        expected += 16
        expected *= 1 + 25 / 100
        # Exactly equal, with or without a start: nothing moved for a tariff without the new parts.
        assert effective_minor_per_kwh(spot, old) == expected
        assert effective_minor_per_kwh(spot, old, _local(2026, 11, 4, 8)) == expected


def test_a_percentage_switched_off_keeps_its_figure_and_adds_nothing() -> None:
    fiscal = FiscalChoice(transfer_percent_enabled=False, transfer_percent=5).validated()
    assert fiscal.transfer_percent is None
    assert effective_minor_per_kwh(1.0, fiscal) == 100.0


@pytest.mark.parametrize(
    ("moment", "high"),
    [
        (_local(2026, 11, 4, 6, 45), False),  # Wednesday, the last quarter before the window
        (_local(2026, 11, 4, 7, 0), True),  # the window's first quarter
        (_local(2026, 11, 4, 18, 45), True),  # its last quarter
        (_local(2026, 11, 4, 19, 0), False),  # the first quarter after it
        (_local(2026, 11, 7, 12, 0), False),  # Saturday
        (_local(2026, 11, 8, 12, 0), False),  # Sunday
        (_local(2026, 10, 30, 12, 0), False),  # Friday 30 October: not winter yet
        (_local(2026, 11, 2, 12, 0), True),  # Monday 2 November: winter
        (_local(2027, 3, 31, 12, 0), True),  # Wednesday 31 March: the last winter day
        (_local(2027, 4, 1, 12, 0), False),  # Thursday 1 April: summer
    ],
)
def test_the_high_load_fee_applies_inside_the_window_on_the_areas_clock(moment: datetime, high: bool) -> None:
    fiscal = FiscalChoice(
        transfer_enabled=True, transfer_minor_per_kwh=10,
        transfer_high_enabled=True, transfer_high_minor_per_kwh=50, high_load_window=WINDOW,
    ).validated()
    expected = 100 + (50 if high else 10)
    assert effective_minor_per_kwh(1.0, fiscal, moment) == pytest.approx(expected)
    # The same instant in UTC is judged on the area's clock all the same.
    assert effective_minor_per_kwh(1.0, fiscal, moment.astimezone(timezone.utc)) == pytest.approx(expected)


def test_every_day_all_year_window() -> None:
    window = HighLoadWindow(weekdays_only=False, from_hour=6, to_hour=22, winter_only=False, tz=TZ)
    fiscal = FiscalChoice(transfer_high_enabled=True, transfer_high_minor_per_kwh=40, high_load_window=window).validated()
    # A Sunday in July: inside.
    assert effective_minor_per_kwh(1.0, fiscal, _local(2026, 7, 12, 12)) == pytest.approx(140)
    # Outside the window with the transfer fee off: nothing is added.
    assert effective_minor_per_kwh(1.0, fiscal, _local(2026, 7, 12, 23)) == pytest.approx(100)


def test_the_high_fee_and_the_percentage_together_with_vat() -> None:
    fiscal = _owner(transfer_high_enabled=True, transfer_high_minor_per_kwh=60, high_load_window=WINDOW)
    inside = _local(2026, 11, 4, 8)
    outside = _local(2026, 11, 4, 20)
    assert effective_minor_per_kwh(1.0, fiscal, inside) == pytest.approx((105 + 36 + 60) * 1.25)
    assert effective_minor_per_kwh(1.0, fiscal, outside) == pytest.approx((105 + 36 + 16) * 1.25)


def test_a_window_without_the_intervals_start_is_a_refusal_not_the_low_fee() -> None:
    fiscal = FiscalChoice(
        transfer_enabled=True, transfer_minor_per_kwh=10,
        transfer_high_enabled=True, transfer_high_minor_per_kwh=50, high_load_window=WINDOW,
    ).validated()
    with pytest.raises(PlannerInputError) as refused:
        effective_minor_per_kwh(1.0, fiscal)
    assert refused.value.code == "invalid_fiscal"


@pytest.mark.parametrize(
    "fiscal",
    [
        # The high fee with no window.
        FiscalChoice(transfer_high_enabled=True, transfer_high_minor_per_kwh=50),
        # A window with no high fee.
        FiscalChoice(high_load_window=WINDOW),
        # A window without the area's zone.
        FiscalChoice(
            transfer_high_enabled=True, transfer_high_minor_per_kwh=50,
            high_load_window=HighLoadWindow(tz=None),
        ),
        # A window that ends before it starts.
        FiscalChoice(
            transfer_high_enabled=True, transfer_high_minor_per_kwh=50,
            high_load_window=HighLoadWindow(from_hour=19, to_hour=7, tz=TZ),
        ),
        # On with no figure.
        FiscalChoice(transfer_percent_enabled=True),
        FiscalChoice(transfer_high_enabled=True, high_load_window=WINDOW),
        # Negative figures.
        FiscalChoice(transfer_percent_enabled=True, transfer_percent=-5),
        FiscalChoice(transfer_high_enabled=True, transfer_high_minor_per_kwh=-1, high_load_window=WINDOW),
    ],
)
def test_validation_refuses_what_cannot_be_priced(fiscal: FiscalChoice) -> None:
    with pytest.raises(PlannerInputError) as refused:
        fiscal.validated()
    assert refused.value.code == "invalid_fiscal"


def test_window_fields_are_whole_hours_in_range() -> None:
    assert high_load_window_problem(True, 7, 19, True) is None
    assert high_load_window_problem(False, 0, 24, False) is None
    assert high_load_window_problem(True, 7, 7, True) is not None
    assert high_load_window_problem(True, -1, 19, True) is not None
    assert high_load_window_problem(True, 7, 25, True) is not None
    assert high_load_window_problem(True, 7.5, 19, True) is not None
    assert high_load_window_problem(True, True, 19, True) is not None
    assert high_load_window_problem("yes", 7, 19, True) is not None


def _day(day: str, *, count: int, start: str, prices: list[float], res: int = 15) -> dict[str, object]:
    assert len(prices) == count
    return {
        "area": "SE4", "date": day, "tz": TZ, "res": res, "start": start, "unit": "EUR/kWh", "prices": prices,
    }


@pytest.mark.parametrize(
    ("day", "start", "count"),
    [
        # Spring forward 2027-03-28: 23 hours, 92 quarters; 02:00-03:00 does not exist.
        ("2027-03-28", "2027-03-28T00:00:00+01:00", 92),
        # Fall back 2026-10-25: 25 hours, 100 quarters; 02:00-03:00 twice.
        ("2026-10-25", "2026-10-25T00:00:00+02:00", 100),
    ],
)
def test_the_window_on_both_dst_days_by_the_local_hour(day: str, start: str, count: int) -> None:
    window = HighLoadWindow(weekdays_only=False, from_hour=2, to_hour=4, winter_only=False, tz=TZ)
    fiscal = FiscalChoice(transfer_high_enabled=True, transfer_high_minor_per_kwh=50, high_load_window=window).validated()
    documents = parse_documents([_day(day, count=count, start=start, prices=[1.0] * count)])
    rows = chart_intervals(documents, currency="EUR", fiscal=fiscal)
    assert len(rows) == count
    high = [row for row in rows if row.effective_minor_per_kwh == pytest.approx(150)]
    for row in rows:
        local_hour = row.utc_start.astimezone(STOCKHOLM).hour
        assert (row in high) == (2 <= local_hour < 4)
    # 02:00-04:00 local: one hour on the short day (02-03 is skipped), three on the long one (02-03 twice).
    assert len(high) == (4 if count == 92 else 12)


def test_hourly_prices_take_the_fee_of_each_quarter_and_each_hour() -> None:
    """An hourly document is charted per hour and planned per quarter; both judge each interval at its start."""
    prices = [1.0] * 24
    documents = parse_documents([_day("2026-11-04", count=24, start="2026-11-04T00:00:00+01:00", prices=prices, res=60)])
    fiscal = FiscalChoice(
        transfer_enabled=True, transfer_minor_per_kwh=10,
        transfer_high_enabled=True, transfer_high_minor_per_kwh=50, high_load_window=WINDOW,
    ).validated()
    rows = chart_intervals(documents, currency="EUR", fiscal=fiscal)
    assert [row.effective_minor_per_kwh for row in rows] == [110.0] * 7 + [150.0] * 12 + [110.0] * 5
    slots = planning_slots(documents, currency="EUR", timezone=TZ)
    assert len(slots) == 96
    priced = [effective_minor_per_kwh(slot.local_major_per_kwh, fiscal, slot.start) for slot in slots]
    assert priced == [110.0] * 28 + [150.0] * 48 + [110.0] * 20


def test_a_plan_steps_around_the_high_load_window_on_quarter_hour_prices() -> None:
    # Wednesday 2026-11-04, quarter-hour prices: 08:00-09:00 is the cheapest spot hour (0.90), but inside the
    # window; 20:00-21:00 costs 1.00. With a 50-cent high fee and no low fee, the evening wins.
    prices = [2.0] * 96
    for index in range(32, 36):
        prices[index] = 0.9
    for index in range(80, 84):
        prices[index] = 1.0
    documents = parse_documents([_day("2026-11-04", count=96, start="2026-11-04T00:00:00+01:00", prices=prices)])

    def plan(fiscal: FiscalChoice) -> list[datetime]:
        request = PlanRequest(
            area_id="SE4", timezone=TZ, currency="EUR", major_unit="€", minor_unit="cent",
            documents=documents, now=_local(2026, 11, 4, 0), phases=1, amps=10, requested_kwh=2.0,
            consumption_kwh_per_10km=2.0, max_periods=1, departure=None, fiscal=fiscal,
        ).validated()
        result = calculate_plan(request)
        assert result.reason is None
        return [slot.start.astimezone(STOCKHOLM) for slot in result.slots]

    without = plan(FiscalChoice())
    assert without[0] == _local(2026, 11, 4, 8)
    with_high = plan(
        FiscalChoice(transfer_high_enabled=True, transfer_high_minor_per_kwh=50, high_load_window=WINDOW)
    )
    assert with_high[0] == _local(2026, 11, 4, 20)
    assert all(not WINDOW.contains(start) for start in with_high)
    assert with_high[-1] - with_high[0] < timedelta(hours=1)
