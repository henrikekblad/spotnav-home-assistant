"""The price maths of a charge session: energy per interval times the interval's effective price."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from custom_components.spotnav.planning.planner import FiscalChoice, chart_intervals, effective_minor_per_kwh
from custom_components.spotnav.sessions.costing import cost_of, day_averages, NO_COST

from .sessions_helpers import day_intervals, STOCKHOLM, UTC

DAY = date(2026, 9, 22)
MIDNIGHT = datetime(2026, 9, 22, tzinfo=STOCKHOLM).astimezone(UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return MIDNIGHT + timedelta(hours=hour, minutes=minute)


def test_a_session_inside_one_interval_costs_energy_times_that_price() -> None:
    rows = day_intervals(DAY, [100.0] * 8 + [40.0] + [100.0] * 15)

    costing = cost_of(rows, at(8, 10), at(8, 40), 3.0)

    assert costing.priced_kwh == pytest.approx(3.0)
    assert costing.cost_minor == pytest.approx(3.0 * 40.0)


def test_a_price_change_inside_the_span_splits_the_energy_by_time() -> None:
    """Half an hour at 40 and half an hour at 100: the energy splits by the overlap with each."""
    rows = day_intervals(DAY, [100.0] * 8 + [40.0, 100.0] + [100.0] * 14)

    costing = cost_of(rows, at(8, 30), at(9, 30), 6.0)

    assert costing.priced_kwh == pytest.approx(6.0)
    assert costing.cost_minor == pytest.approx(3.0 * 40.0 + 3.0 * 100.0)


def test_a_sixty_minute_and_a_fifteen_minute_series_price_the_same_charge_alike() -> None:
    hourly = day_intervals(DAY, [10.0 * h for h in range(24)], minutes=60)
    quarters = day_intervals(DAY, [10.0 * (i // 4) for i in range(96)], minutes=15)

    a = cost_of(hourly, at(5, 20), at(7, 50), 5.0)
    b = cost_of(quarters, at(5, 20), at(7, 50), 5.0)

    assert a.cost_minor == pytest.approx(b.cost_minor)
    assert a.reference_cost_minor == pytest.approx(b.reference_cost_minor)


def test_the_effective_price_is_the_planners_own_with_vat_tax_and_grid_fee() -> None:
    """The session is priced like the plan: price, plus energy tax and grid fee, times VAT."""
    fiscal = FiscalChoice(
        tax_enabled=True, tax_minor_per_kwh=36.0,
        transfer_enabled=True, transfer_minor_per_kwh=25.0,
        vat_enabled=True, vat_percent=25.0,
    )
    from custom_components.spotnav.pricing.relay_contract import parse_day  # noqa: PLC0415
    from tests.relay import day_body, SE4  # noqa: PLC0415

    import json  # noqa: PLC0415

    document = parse_day(json.loads(day_body(SE4, DAY)), area_id=SE4, day=DAY)
    rows = chart_intervals((document,), currency="SEK", fiscal=fiscal)
    first = rows[0]
    expected = effective_minor_per_kwh(first.raw_minor_per_kwh / 100, fiscal)

    costing = cost_of(rows, first.utc_start, first.utc_end, 2.0)

    assert costing.cost_minor == pytest.approx(2.0 * expected)
    assert expected == pytest.approx((first.raw_minor_per_kwh + 36.0 + 25.0) * 1.25)


def test_energy_outside_every_published_interval_is_unpriced_not_guessed() -> None:
    rows = day_intervals(DAY, [50.0] * 24)

    # Half the span is past the end of the published day.
    costing = cost_of(rows, at(23, 30), at(24, 30), 4.0)

    assert costing.priced_kwh == pytest.approx(2.0)
    assert costing.cost_minor == pytest.approx(100.0)
    assert cost_of([], at(1), at(2), 4.0) == NO_COST
    assert cost_of(rows, at(30), at(31), 4.0) == NO_COST


def test_no_energy_costs_nothing_and_a_zero_length_span_takes_the_price_at_that_instant() -> None:
    rows = day_intervals(DAY, [10.0 * h for h in range(24)])

    assert cost_of(rows, at(3), at(4), 0.0) == NO_COST
    point = cost_of(rows, at(3, 30), at(3, 30), 2.0)
    assert point.cost_minor == pytest.approx(2.0 * 30.0)


def test_the_reference_is_the_same_energy_at_the_days_average_price() -> None:
    rows = day_intervals(DAY, [20.0] * 12 + [60.0] * 12)  # average 40

    cheap = cost_of(rows, at(2), at(3), 10.0)
    dear = cost_of(rows, at(14), at(15), 10.0)

    assert cheap.cost_minor == pytest.approx(200.0)
    assert cheap.reference_cost_minor == pytest.approx(400.0)
    assert dear.reference_cost_minor == pytest.approx(400.0)
    assert dear.cost_minor - dear.reference_cost_minor == pytest.approx(200.0)


def test_the_days_average_is_weighted_by_interval_length_across_mixed_resolutions() -> None:
    hour = day_intervals(DAY, [30.0])  # one hour at 30
    quarters = day_intervals(date(2026, 9, 22), [90.0] * 4, minutes=15)  # an hour at 90, same day
    shifted = [
        row.__class__(
            start=row.start + timedelta(hours=1), end=row.end + timedelta(hours=1),
            utc_start=row.utc_start + timedelta(hours=1), utc_end=row.utc_end + timedelta(hours=1),
            day=row.day, raw_minor_per_kwh=row.raw_minor_per_kwh,
            effective_minor_per_kwh=row.effective_minor_per_kwh,
        )
        for row in quarters
    ]

    averages = day_averages(hour + shifted)

    assert averages[DAY] == pytest.approx(60.0)


def test_a_dst_day_is_priced_by_the_clock_not_the_wall_time() -> None:
    """25 hours on the autumn night: an hour's charge costs one hour's price whichever 02:00 it was."""
    autumn = date(2026, 10, 25)
    rows = day_intervals(autumn, [float(h) for h in range(25)])
    assert len(rows) == 25
    start = rows[3].utc_start  # the second 02:00-03:00 of the night

    costing = cost_of(rows, start, start + timedelta(hours=1), 1.0)

    assert costing.cost_minor == pytest.approx(3.0)
    assert day_averages(rows)[autumn] == pytest.approx(12.0)
