"""The heat pump's relay known to the plan (`heat/relay_effect.py`, `heat/relay_schedule.py`): a lowered block's
measured effect, the learned share and rebound, the forecast's change, and the blueprint's expected closings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.heat.relay_effect import (
    AFTER,
    PRIOR_REBOUND,
    PRIOR_SHARE,
    BlockResult,
    Effect,
    closed_intervals,
    forecast_delta,
    learn,
    measure_block,
)
from custom_components.spotnav.heat.relay_schedule import (
    BLUEPRINT_PATH,
    blueprint_rule,
    expected_closings,
    too_cold,
    unknown_closing,
)

T0 = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)
FIVE = timedelta(minutes=5)


def series(start: datetime, end: datetime, kw: float) -> dict[datetime, float]:
    out = {}
    moment = start
    while moment < end:
        out[moment] = kw
        moment += FIVE
    return out


def owner_block() -> dict[datetime, float]:
    """1.1 kW heating, a 3 h block at 0.5 kW, then 1.5 kW for three hours (a rebound of 1.2 kWh of 1.8 saved)."""
    periods = series(T0 - timedelta(hours=2), T0, 1.1)
    periods |= series(T0, T0 + timedelta(hours=3), 0.5)
    periods |= series(T0 + timedelta(hours=3), T0 + timedelta(hours=6), 1.5)
    return periods


def test_a_block_is_measured_without_its_hot_water_runs() -> None:
    periods = owner_block()
    # A shower's hot-water run inside the block (3 kW for 25 min) is the tank, not the house: left out.
    run = (T0 + timedelta(hours=1), T0 + timedelta(hours=1, minutes=25))
    periods |= series(*run, 3.0)
    result = measure_block((T0, T0 + timedelta(hours=3)), periods, [run])
    assert result is not None
    assert result.baseline_kw == pytest.approx(1.1) and result.during_kw == pytest.approx(0.5)
    assert result.saved_kwh == pytest.approx(1.8) and result.rebound_kwh == pytest.approx(1.2)
    assert result.share() == pytest.approx(0.6 / 1.1) and result.rebound_share() == pytest.approx(1.2 / 1.8)
    assert BlockResult.from_dict(result.as_dict()) == result
    assert BlockResult.from_dict({"start": "x"}) is None


def test_a_block_is_not_measured_too_soon_nor_beside_the_next() -> None:
    periods = owner_block()
    block = (T0, T0 + timedelta(hours=3))
    # The rebound's window not yet full.
    partial = {moment: kw for moment, kw in periods.items() if moment < T0 + timedelta(hours=4)}
    assert measure_block(block, partial, []) is None
    # The next block starts inside the rebound's window.
    assert measure_block(block, periods, [], next_start=T0 + timedelta(hours=4)) is None
    # Too short, and the outdoor temperature moving across it.
    assert measure_block((T0, T0 + timedelta(minutes=20)), periods, []) is None
    outdoor = {T0 - timedelta(hours=2): 8.0, T0 - timedelta(hours=1): 8.0, T0 + timedelta(hours=4): 2.0}
    assert measure_block(block, periods, [], outdoor=outdoor) is None


def test_the_learned_effect_starts_cautious_and_follows_the_blocks() -> None:
    assert learn([]) == Effect(PRIOR_SHARE, PRIOR_REBOUND, 0)
    measured = BlockResult(T0, T0 + timedelta(hours=3), 1.1, 0.5, 1.5)
    five = learn([measured] * 5)
    assert five.blocks == 5
    assert five.lower_share == pytest.approx((5 * 0.6 / 1.1 + 5 * PRIOR_SHARE) / 10)
    assert five.rebound == pytest.approx((5 * (1.2 / 1.8) + 5 * PRIOR_REBOUND) / 10)
    many = learn([measured] * 200)
    assert many.rebound == pytest.approx(1.2 / 1.8, abs=0.02)


def test_the_closed_intervals_from_the_relays_states() -> None:
    states = [(T0, True), (T0 + timedelta(hours=1), False), (T0 + timedelta(hours=3), True)]
    assert closed_intervals(states, T0 + timedelta(hours=4)) == [
        (T0, T0 + timedelta(hours=1)),
        (T0 + timedelta(hours=3), T0 + timedelta(hours=4)),
    ]


def test_the_forecast_loses_the_lowered_heat_and_gets_the_rebound_after() -> None:
    effect = Effect(0.5, 1.0, 0)
    block = [(T0, T0 + timedelta(hours=2))]
    delta = forecast_delta(block, lambda _hour: 1.2, effect, T0)
    assert delta[T0] == pytest.approx(-0.6) and delta[T0 + timedelta(hours=1)] == pytest.approx(-0.6)
    after = [delta[T0 + timedelta(hours=2 + h)] for h in range(3)]
    assert after == pytest.approx([0.4, 0.4, 0.4])
    # Moved, not saved, with the cautious prior: the day's sum is unchanged.
    assert sum(delta.values()) == pytest.approx(0.0)
    # From the running hour on only, a past block's rebound still counted whole.
    later = forecast_delta(block, lambda _hour: 1.2, effect, T0 + timedelta(hours=3))
    assert set(later) == {T0 + timedelta(hours=3), T0 + timedelta(hours=4)}
    assert later[T0 + timedelta(hours=3)] == pytest.approx(0.4)
    # No heat figure for an hour: nothing taken away there.
    assert forecast_delta(block, lambda _hour: None, effect, T0) == {}
    assert AFTER == timedelta(hours=3)


OWNER_CONFIG = {
    "id": "spotnav_varmekurva_dyrt",
    "variables": {
        "relay_mode": "expensive_closed",
        "relay": "switch.alarmsystem_kamrersvagen_varmepanna",
        "cheap_now": "binary_sensor.electricity_price_se4_cheap_now",
        "expensive_now": "binary_sensor.electricity_price_se4_expensive_now",
        "max_on_hours": 3,
        "min_rest_hours": 1,
        "outdoor_sensor": "sensor.utomhus_garage_temperatur",
        "min_outdoor": -15,
    },
}


def test_the_blueprints_rule_is_read_from_its_configuration() -> None:
    rule = blueprint_rule(OWNER_CONFIG)
    assert rule is not None and rule.relay == "switch.alarmsystem_kamrersvagen_varmepanna"
    assert rule.max_on == timedelta(hours=3) and rule.min_rest == timedelta(hours=1)
    assert rule.as_dict()["min_outdoor_c"] == -15.0
    assert blueprint_rule({"variables": {**OWNER_CONFIG["variables"], "relay_mode": "cheap_pulse"}}) is None
    assert blueprint_rule({"triggers": []}) is None and blueprint_rule(None) is None
    assert too_cold(rule, None) and too_cold(rule, -16.0) and not too_cold(rule, 4.0)
    assert BLUEPRINT_PATH == "spotnav/relay_by_price.yaml"


def test_the_blueprints_closings_ahead() -> None:
    rule = blueprint_rule(OWNER_CONFIG)
    assert rule is not None
    # Expensive 16:00-21:00: closed three hours, an hour's rest, then the last hour.
    expensive = [(T0 + timedelta(hours=10), T0 + timedelta(hours=15))]
    now = T0
    closings = expected_closings(now, rule, closed_now=False, changed_at=T0 - timedelta(hours=5), expensive=expensive, cold=False)
    assert closings == [
        (T0 + timedelta(hours=10), T0 + timedelta(hours=13)),
        (T0 + timedelta(hours=14), T0 + timedelta(hours=15)),
    ]
    # Closed since 15:00 now at 17:00: it opens at 18:00 when its three hours are up.
    closed = expected_closings(
        T0 + timedelta(hours=11), rule, closed_now=True, changed_at=T0 + timedelta(hours=9), expensive=expensive, cold=False
    )
    assert closed[0] == (T0 + timedelta(hours=9), T0 + timedelta(hours=12))
    # Too cold: nothing closes, and a closing now ends at once.
    assert expected_closings(now, rule, closed_now=False, changed_at=None, expensive=expensive, cold=True) == []
    # Someone else's closing: at most three hours from its start.
    assert unknown_closing(T0, closed_now=True, changed_at=T0 - timedelta(hours=1)) == [
        (T0 - timedelta(hours=1), T0 + timedelta(hours=2))
    ]
    assert unknown_closing(T0, closed_now=False, changed_at=None) == []
