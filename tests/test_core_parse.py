"""The shared small parsers: each variant's edge cases."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.core.parse import aware_instant, aware_utc, finite_number, is_number, number_from_state


@pytest.mark.parametrize("value", [0, 1, -2.5, 3.0, 10**6])
def test_is_number_accepts_finite_int_and_float(value: float) -> None:
    assert is_number(value) is True
    assert finite_number(value) == float(value)


@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), float("-inf"), "1.5", None, [1], {"a": 1}, b"1"]
)
def test_is_number_rejects_the_rest(value: object) -> None:
    assert is_number(value) is False
    assert finite_number(value) is None


def test_finite_number_returns_a_float_for_an_int() -> None:
    assert isinstance(finite_number(3), float)


@pytest.mark.parametrize("state, expected", [("1.5", 1.5), ("-3", -3.0), (" 7 ", 7.0), ("0", 0.0), ("1e3", 1000.0)])
def test_number_from_state_reads_text(state: str, expected: float) -> None:
    assert number_from_state(state) == expected


@pytest.mark.parametrize("state", [None, "", "unknown", "unavailable", "nan", "inf", "-inf", "1,5"])
def test_number_from_state_rejects_what_is_no_finite_number(state: str | None) -> None:
    assert number_from_state(state) is None


def test_aware_instant_keeps_the_offset() -> None:
    moment = aware_instant("2026-11-19T10:00:00+01:00")
    assert moment == datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)
    assert moment is not None and moment.utcoffset() == timedelta(hours=1)


def test_aware_utc_converts_to_utc() -> None:
    moment = aware_utc("2026-11-19T10:00:00+01:00")
    assert moment == datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)
    assert moment is not None and moment.utcoffset() == timedelta(0)
    assert aware_utc("2026-11-19T09:00:00Z") == moment


@pytest.mark.parametrize("raw", ["2026-11-19T10:00:00", "2026-11-19", "not a time", "", None, 5, 1.5, True, [], datetime(2026, 11, 19, tzinfo=timezone.utc)])
def test_instants_reject_naive_and_non_text(raw: object) -> None:
    assert aware_instant(raw) is None
    assert aware_utc(raw) is None
