"""The history profile built in Home Assistant (`pricing/price_profile.py`), against the relay's own.

Every fixture under `fixtures/profile/` holds day files and the profile the relay's `internal/profile`
built from them; the build here must give the same document to the last decimal. The relay's
`profile_test.go` assertions are kept beside it, so a failure names the rule, not only a diff.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.pricing.price_profile import (
    MIN_DAYS,
    _round,
    build_profile,
    profile_window,
)
from custom_components.spotnav.pricing.relay_contract import PriceDocument, PriceProfile, parse_day, parse_profile

FIXTURES = Path(__file__).parent / "fixtures" / "profile"

CASES = sorted(path.stem for path in FIXTURES.glob("*.json"))

STOCKHOLM = ZoneInfo("Europe/Stockholm")


def _case(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _documents(case: dict[str, Any]) -> dict[date, PriceDocument]:
    return {
        date.fromisoformat(key): parse_day(body, area_id=case["area"], day=date.fromisoformat(key))
        for key, body in case["days"].items()
    }


def _build(name: str) -> PriceProfile | None:
    case = _case(name)
    return build_profile(
        case["area"], case["tz"], case["market_tz"], datetime.fromisoformat(case["now"]), _documents(case)
    )


def _profile(name: str) -> PriceProfile:
    profile = _build(name)
    assert profile is not None
    return profile


# ----------------------------------------------------------------------------------------- parity


def test_every_relay_case_is_here() -> None:
    assert len(CASES) == 13


@pytest.mark.parametrize("name", CASES)
def test_the_build_equals_the_relays_profile(name: str) -> None:
    case = _case(name)
    built = _build(name)
    if case["profile"] is None:
        assert built is None
        return
    expected = parse_profile(case["profile"], area_id=case["area"])
    assert built is not None
    assert (built.version, built.area_id, built.tz, built.unit, built.weeks) == (
        expected.version,
        expected.area_id,
        expected.tz,
        expected.unit,
        expected.weeks,
    )
    assert (built.from_date, built.to_date) == (expected.from_date, expected.to_date)
    assert built.generated == expected.generated
    assert built.generated.utcoffset() == expected.generated.utcoffset()
    # Exact float equality: the same rounding, the same arithmetic order.
    assert dict(built.hours) == dict(expected.hours)
    assert list(built.hours) == [(entry["weekday"], entry["hour"]) for entry in case["profile"]["hours"]]


# ------------------------------------------------------------------ the relay's assertions, ported


def test_a_normal_week() -> None:
    profile = _profile("normal_week")
    assert (profile.version, profile.area_id, profile.tz, profile.unit, profile.weeks) == (
        1,
        "SE4",
        "Europe/Stockholm",
        "EUR/kWh",
        4,
    )
    assert (profile.from_date, profile.to_date) == (date(2026, 9, 5), date(2026, 10, 2))
    assert profile.generated.isoformat() == "2026-10-02T14:05:00+02:00"
    assert len(profile.hours) == 7 * 24
    friday = profile.hour(5, 13)
    assert friday is not None and friday.n == 16 and math.isclose(friday.median, 0.135) and friday.std == 0
    assert next(iter(profile.hours)) == (1, 0)


def test_median_and_population_std() -> None:
    entry = _profile("median_and_population_std").hour(1, 3)
    assert entry is not None and entry.n == 16 and entry.median == 1.5
    assert abs(entry.std - math.sqrt(1.25)) <= 1e-5


def test_twenty_one_days_publish_and_twenty_do_not() -> None:
    assert len(_case("seven_days_missing")["days"]) == MIN_DAYS
    assert _profile("seven_days_missing").hours
    assert len(_case("eight_days_missing")["days"]) == MIN_DAYS - 1
    assert _build("eight_days_missing") is None


def test_a_document_with_no_prices_does_not_count() -> None:
    """The relay skips a day file that does not parse; a held document always parses, so the guard
    that remains is the empty one, and it must not count towards the 21 days.
    """
    case = _case("seven_days_missing")
    documents = _documents(case)
    first = min(documents)
    documents[first] = replace(documents[first], prices=())
    now = datetime.fromisoformat(case["now"])
    assert build_profile("SE4", "Europe/Stockholm", "Europe/Stockholm", now, documents) is None


def test_days_outside_the_window_are_ignored() -> None:
    assert date(2026, 10, 3) in _documents(_case("outside_the_window"))
    assert _build("outside_the_window") is None


def test_today_is_in_the_window() -> None:
    entry = _profile("today_in_the_window").hour(5, 0)
    # Fridays 09-18, 09-25 and 10-02, four quarters each.
    assert entry is not None and entry.n == 12


def test_the_sample_threshold() -> None:
    assert _profile("hourly_below_threshold").hours == {}
    entry = _profile("two_fridays_at_threshold").hour(5, 7)
    assert entry is not None and entry.n == 8


def test_the_autumn_change_counts_the_repeated_hour_twice() -> None:
    profile = _profile("dst_autumn")
    two, three = profile.hour(7, 2), profile.hour(7, 3)
    assert two is not None and two.n == 3 * 4 + 8
    assert three is not None and three.n == 16
    assert sum(entry.n for (weekday, _), entry in profile.hours.items() if weekday == 7) == 4 * 96 + 4


def test_the_spring_change_skips_an_hour() -> None:
    profile = _profile("dst_spring")
    two, three = profile.hour(7, 2), profile.hour(7, 3)
    assert two is not None and two.n == 12
    assert three is not None and three.n == 16


def test_market_days_are_grouped_by_the_display_zone() -> None:
    profile = _profile("split_calendar_gb")
    assert profile.tz == "Europe/London"
    assert all(entry.median == hour for (_, hour), entry in profile.hours.items())


# ------------------------------------------------------------------------------- the module's own


def test_the_window_is_the_markets_days() -> None:
    # 23:30 in Lisbon on the 26th is already the 27th in Madrid.
    now = datetime(2026, 10, 26, 23, 30, tzinfo=timezone.utc)
    assert profile_window(now, "Europe/Madrid") == (date(2026, 9, 30), date(2026, 10, 27))
    assert profile_window(now, "Europe/Lisbon") == (date(2026, 9, 29), date(2026, 10, 26))


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError):
        profile_window(datetime(2026, 10, 2, 14), "Europe/Stockholm")


def test_generated_is_whole_seconds_in_the_display_zone() -> None:
    case = _case("normal_week")
    now = datetime(2026, 10, 2, 12, 5, 7, 900_000, tzinfo=timezone.utc)
    profile = build_profile("SE4", "Europe/Stockholm", "Europe/Stockholm", now, _documents(case))
    assert profile is not None
    assert profile.generated.isoformat() == "2026-10-02T14:05:07+02:00"
    assert profile.generated.tzinfo == STOCKHOLM


@pytest.mark.parametrize(
    ("value", "rounded"),
    [
        (0.000005, 0.00001),
        (-0.000005, -0.00001),
        (0.000015, 0.00002),
        (0.000025, 0.00003),
        (0.123444, 0.12344),
        (-0.123446, -0.12345),
        (2.5e-6, 0.0),
        (0.0, 0.0),
        (1.000005, 1.00001),
        (0.012345, 0.01235),
    ],
)
def test_rounding_is_half_away_from_zero(value: float, rounded: float) -> None:
    """Values checked against Go's `math.Round(x*1e5) / 1e5`; Python's `round` gives 0.0, -0.0,
    0.00002, 1.0 and 0.01234 for five of them.
    """
    assert _round(value) == rounded


def test_the_relays_constants() -> None:
    window = profile_window(datetime(2026, 10, 2, 12, tzinfo=timezone.utc), "Europe/Stockholm")
    assert window[1] - window[0] == timedelta(days=27)
