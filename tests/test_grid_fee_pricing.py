"""The grid fee as a percentage of the spot price (A) and the time-of-use grid fee (B), in the planner's arithmetic.

Pure: `effective_minor_per_kwh` with the interval's start, the high-load window on the area's clock (window
edges, the days of the week, the months and their wrap over the new year, windows over midnight, both DST
days, quarter-hour prices) and a plan that steps around the window.
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
WINDOW = HighLoadWindow(days="weekdays", from_month=11, to_month=3, from_hour=7, to_hour=19, tz=TZ)


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
    window = HighLoadWindow(days="all", from_month=1, to_month=12, from_hour=6, to_hour=22, tz=TZ)
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
        # A window that ends where it starts.
        FiscalChoice(
            transfer_high_enabled=True, transfer_high_minor_per_kwh=50,
            high_load_window=HighLoadWindow(from_hour=7, to_hour=7, tz=TZ),
        ),
        # Days that are not one of the three.
        FiscalChoice(
            transfer_high_enabled=True, transfer_high_minor_per_kwh=50,
            high_load_window=HighLoadWindow(days="weekends", tz=TZ),
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


def test_window_fields_are_whole_numbers_in_range() -> None:
    assert high_load_window_problem("weekdays", 11, 3, 7, 19) is None
    assert high_load_window_problem("all", 1, 12, 0, 24) is None
    assert high_load_window_problem("mon_sat", 4, 4, 22, 6) is None  # one month, over midnight
    assert high_load_window_problem("weekdays", 11, 3, 7, 7) is not None  # equal hours
    assert high_load_window_problem("weekdays", 11, 3, -1, 19) is not None
    assert high_load_window_problem("weekdays", 11, 3, 7, 25) is not None
    assert high_load_window_problem("weekdays", 11, 3, 24, 6) is not None
    assert high_load_window_problem("weekdays", 11, 3, 7, 0) is not None
    assert high_load_window_problem("weekdays", 11, 3, 7.5, 19) is not None
    assert high_load_window_problem("weekdays", 11, 3, True, 19) is not None
    assert high_load_window_problem("weekdays", 0, 3, 7, 19) is not None
    assert high_load_window_problem("weekdays", 11, 13, 7, 19) is not None
    assert high_load_window_problem("weekdays", 11.0, 3, 7, 19) is not None
    assert high_load_window_problem("sat_sun", 11, 3, 7, 19) is not None
    assert high_load_window_problem(True, 11, 3, 7, 19) is not None


@pytest.mark.parametrize(
    ("days", "moment", "inside"),
    [
        # Mon-Sat 07-22, November to March (a Finnish seasonal tariff).
        ("mon_sat", _local(2026, 11, 7, 12), True),  # Saturday
        ("mon_sat", _local(2026, 11, 8, 12), False),  # Sunday
        ("mon_sat", _local(2026, 11, 9, 21, 45), True),  # Monday, the last quarter
        ("mon_sat", _local(2026, 11, 9, 22), False),
        ("weekdays", _local(2026, 11, 7, 12), False),  # Saturday
        ("all", _local(2026, 11, 8, 12), True),  # Sunday
    ],
)
def test_the_days_of_the_week(days: str, moment: datetime, inside: bool) -> None:
    window = HighLoadWindow(days=days, from_month=11, to_month=3, from_hour=7, to_hour=22, tz=TZ)
    assert window.contains(moment) is inside


@pytest.mark.parametrize(
    ("from_month", "to_month", "inside_months"),
    [
        (11, 3, {11, 12, 1, 2, 3}),  # over the new year
        (10, 3, {10, 11, 12, 1, 2, 3}),  # Denmark's winter peak
        (4, 9, {4, 5, 6, 7, 8, 9}),
        (6, 6, {6}),  # one month
        (1, 12, set(range(1, 13))),  # all year
        (12, 11, set(range(1, 13))),  # every month too, the other way round
        (12, 1, {12, 1}),
    ],
)
def test_the_months_both_included_and_wrapping_over_the_new_year(
    from_month: int, to_month: int, inside_months: set[int]
) -> None:
    window = HighLoadWindow(days="all", from_month=from_month, to_month=to_month, from_hour=0, to_hour=24, tz=TZ)
    for month in range(1, 13):
        # The first and the last day of each month.
        first = _local(2027, month, 1, 0)
        last = (_local(2027, month % 12 + 1, 1, 12) if month < 12 else _local(2028, 1, 1, 12)) - timedelta(days=1)
        assert window.contains(first) is (month in inside_months), (month, "first")
        assert window.contains(last) is (month in inside_months), (month, "last")


@pytest.mark.parametrize(
    ("moment", "inside"),
    [
        (_local(2026, 11, 2, 21, 45), False),  # Monday evening, before
        (_local(2026, 11, 2, 22), True),  # Monday 22:00
        (_local(2026, 11, 3, 0), True),  # Tuesday 00:00: Monday's night
        (_local(2026, 11, 3, 5, 45), True),  # its last quarter
        (_local(2026, 11, 3, 6), False),
        (_local(2026, 11, 3, 12), False),  # midday is outside
        (_local(2026, 11, 6, 23), True),  # Friday night
        (_local(2026, 11, 7, 3), True),  # Saturday 03:00 is still Friday's night
        (_local(2026, 11, 7, 22), False),  # Saturday night is not a weekday's
        (_local(2026, 11, 8, 23), False),  # nor Sunday's
        (_local(2026, 11, 9, 3), False),  # so Monday 03:00 is outside
        (_local(2026, 11, 9, 22), True),  # Monday night again
        # The months follow the night's first day too.
        (_local(2026, 10, 30, 23), False),  # Friday 30 October: not yet
        (_local(2026, 11, 2, 3), False),  # Monday 2 November 03:00: Sunday's night
        (_local(2027, 3, 31, 23), True),  # Wednesday 31 March, the last night
        (_local(2027, 4, 1, 3), True),  # its morning, in April
        (_local(2027, 4, 1, 23), False),
    ],
)
def test_a_window_over_midnight_belongs_to_the_day_it_starts_on(moment: datetime, inside: bool) -> None:
    window = HighLoadWindow(days="weekdays", from_month=11, to_month=3, from_hour=22, to_hour=6, tz=TZ)
    assert window.contains(moment) is inside
    assert window.contains(moment.astimezone(timezone.utc)) is inside


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
    window = HighLoadWindow(days="all", from_month=1, to_month=12, from_hour=2, to_hour=4, tz=TZ)
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


@pytest.mark.parametrize(
    ("day", "start", "count", "expected"),
    [
        # Spring forward: 00-02 and 03-06 of the night before's window, 22-24 of tonight's: 7 hours.
        ("2027-03-28", "2027-03-28T00:00:00+01:00", 92, 28),
        # Fall back: 00-06 with 02-03 twice, 22-24: 9 hours.
        ("2026-10-25", "2026-10-25T00:00:00+02:00", 100, 36),
    ],
)
def test_a_window_over_midnight_on_both_dst_days(day: str, start: str, count: int, expected: int) -> None:
    window = HighLoadWindow(days="all", from_month=1, to_month=12, from_hour=22, to_hour=6, tz=TZ)
    fiscal = FiscalChoice(transfer_high_enabled=True, transfer_high_minor_per_kwh=50, high_load_window=window).validated()
    documents = parse_documents([_day(day, count=count, start=start, prices=[1.0] * count)])
    rows = chart_intervals(documents, currency="EUR", fiscal=fiscal)
    high = [row for row in rows if row.effective_minor_per_kwh == pytest.approx(150)]
    for row in rows:
        local_hour = row.utc_start.astimezone(STOCKHOLM).hour
        assert (row in high) == (local_hour >= 22 or local_hour < 6)
    assert len(high) == expected


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
