"""The common forecast shape (`site/forecast_shape.py`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.spotnav.site.forecast_shape import MAX_ROWS, standard_forecast

START = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


def test_rows_are_time_and_value_in_time_order_and_rounded() -> None:
    rows = standard_forecast([(START + timedelta(hours=1), 2.00049), (START, 1.5)])

    assert rows == [
        {"time": "2026-11-14T10:00:00+00:00", "value": 1.5},
        {"time": "2026-11-14T11:00:00+00:00", "value": 2.0},
    ]


def test_rows_without_a_value_or_a_time_zone_are_left_out() -> None:
    rows = standard_forecast([(START, None), (datetime(2026, 11, 14, 12, 0), 1.0), (START, 0.0)])

    assert rows == [{"time": "2026-11-14T10:00:00+00:00", "value": 0.0}]


def test_the_list_is_bounded() -> None:
    rows = standard_forecast((START + timedelta(minutes=15 * i), 1.0) for i in range(MAX_ROWS + 50))

    assert len(rows) == MAX_ROWS
