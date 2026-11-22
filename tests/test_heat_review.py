"""A day's heat review (`heat/review.py`): the relay's real lowering and SpotNav's plan side by side, money and
comfort."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.spotnav.heat.relay_effect import BlockResult, Effect
from custom_components.spotnav.heat.review import blocks_of, heat_base, plan_lowered_hours, review_day

DAY = date(2026, 11, 17)
MIDNIGHT = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)
HOURS = [MIDNIGHT + timedelta(hours=h) for h in range(24)]
H = HOURS


def prices() -> dict[datetime, float]:
    """Öre/kWh: 100 all day, 300 in 16-19 local (15-18 UTC)."""
    return {hour: (300.0 if 16 <= index <= 18 else 100.0) for index, hour in enumerate(HOURS)}


def coldest(low_from: datetime | None = None) -> dict[datetime, float]:
    out = {}
    moment = MIDNIGHT
    while moment < MIDNIGHT + timedelta(hours=27):
        out[moment] = 20.3 if low_from is not None and low_from <= moment < low_from + timedelta(minutes=30) else 20.8
        moment += timedelta(minutes=5)
    return out


def test_blocks_of_hours_in_a_row() -> None:
    assert blocks_of([H[3], H[1], H[2], H[7]]) == [(H[1], H[4]), (H[7], H[8])]


def test_the_relays_measured_block_and_the_plan_beside_it() -> None:
    effect = Effect(0.5, 1.0, 0)
    relay = [(H[16], H[19])]
    measured = [BlockResult(H[16], H[19], 1.2, 0.6, 1.8)]
    heat = {hour: 1.2 for hour in HOURS} | {H[16]: 0.6, H[17]: 0.6, H[18]: 0.6}
    review = review_day(
        DAY,
        HOURS,
        prices=prices(),
        heat_kwh=heat,
        relay=relay,
        plan_lowered=[H[16], H[17], H[18]],
        measured=measured,
        effect=effect,
        coldest=coldest(H[18]),
        low_c=20.5,
    )
    # The relay: 1.8 kWh taken at 300 öre, 1.8 kWh back at 100 öre: 360 öre saved, nothing saved in energy.
    assert review["relay"]["value"] == pytest.approx(360.0) and review["relay"]["saved_kwh"] == pytest.approx(0.0)
    assert review["relay"]["measured"] == 1 and review["relay"]["hours"] == 3.0
    # The plan on the same hours counts the measured baseline, not the dip: half of 1.2 kW, all back.
    assert review["plan"]["value"] == pytest.approx(3 * 0.6 * 300 - 1.8 * 100)
    assert review["plan"]["overlap_hours"] == 3.0
    assert review["relay"]["coldest_min_c"] == 20.3 and review["relay"]["minutes_below"] == 30
    assert review["day"] == {"coldest_min_c": 20.3, "minutes_below": 30}
    assert review["effect"] == {"lower_share": 0.5, "rebound": 1.0, "blocks": 0}


def test_an_unmeasured_hour_is_lifted_by_what_the_relay_took() -> None:
    effect = Effect(0.5, 1.0, 0)
    heat = {H[5]: 0.6}
    assert heat_base(H[5], heat, [(H[5], H[6])], [], effect) == pytest.approx(1.2)
    assert heat_base(H[5], heat, [], [], effect) == pytest.approx(0.6)
    assert heat_base(H[6], heat, [], [], effect) is None


def test_a_plan_alone_on_a_day_the_relay_stayed_open() -> None:
    effect = Effect(0.4, 0.5, 6)
    review = review_day(
        DAY,
        HOURS,
        prices=prices(),
        heat_kwh={hour: 1.0 for hour in HOURS},
        relay=[],
        plan_lowered=[H[16], H[17]],
        measured=[],
        effect=effect,
        coldest={},
        low_c=20.5,
    )
    # 0.8 kWh taken at 300, 0.4 back at 300/100/100 weighted over the three hours after (17:00, 18:00 local dear).
    back_price = (300 + 100 + 100) / 3
    assert review["plan"]["value"] == pytest.approx(0.8 * 300 - 0.4 * back_price, abs=0.01)
    assert review["plan"]["saved_kwh"] == pytest.approx(0.4)
    assert review["relay"] == {"hours": 0.0, "value": 0.0, "saved_kwh": 0.0, "measured": 0, "coldest_min_c": None, "minutes_below": 0}
    assert review["day"] is None


def test_hours_lowered_only_because_the_house_was_already_warm_are_no_plan_value() -> None:
    """SN-V34E: 18 of 24 hours kept as lowered for `room_warm` counted as SpotNav's saving."""
    kept = {
        H[2]: ("lower", "room_warm"),
        H[3]: ("lower", "room_warm"),
        H[16]: ("lower", "dear_hour"),
        H[17]: ("lower", "tight_hour"),
        H[18]: ("normal", "normal"),
    }
    assert plan_lowered_hours(kept) == [H[16], H[17]]

    review = review_day(
        DAY,
        HOURS,
        prices=prices(),
        heat_kwh={hour: 1.0 for hour in HOURS},
        relay=[],
        plan_lowered=plan_lowered_hours(kept),
        measured=[],
        effect=Effect(0.4, 0.5, 6),
        coldest={},
        low_c=20.5,
    )
    assert review["plan"]["hours"] == 2.0
