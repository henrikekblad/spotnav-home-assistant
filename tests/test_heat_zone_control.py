"""SpotNav setting a zone's thermostat, the pure rules (`heat/zone_control.py`): the setpoint each plan hour asks for,
relative to the person's own value, on the thermostat's step and range; the zone's own comfort; the run rules."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.heat.zone_control import (
    LOWER_C,
    MAX_WRITES_DAY,
    Limits,
    ZoneSituation,
    aim,
    decide,
    fit,
    forecast_delta,
    qualifies,
    read_back,
    same,
    taken,
)

NOW = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
MIN = timedelta(minutes=1)
HEAT = {"supported_features": 1, "temperature": 21.0, "current_temperature": 20.8}


def situation(action: str | None = "lower", **changes) -> ZoneSituation:
    base = ZoneSituation(
        now=NOW,
        action=action,
        plan_until=NOW + 2 * H if action == "lower" else None,
        action_until=NOW + 2 * H,
        home_c=21.0,
        current_c=21.0,
        held=None,
        held_since=None,
        last_end=None,
        lowered_24h=timedelta(0),
        writes_today=0,
        night_delta_c=1.5,
        away_delta_c=3.0,
    )
    return replace(base, **changes)


def test_which_thermostats_qualify() -> None:
    assert qualifies("heat", HEAT) is None
    assert qualifies("unavailable", HEAT) == "unavailable" and qualifies(None, HEAT) == "unavailable"
    # On its own schedule, cooling, both ways or off: left alone.
    for mode in ("auto", "cool", "heat_cool", "off"):
        assert qualifies(mode, HEAT) == "not_heat_mode"
    assert qualifies("heat", {**HEAT, "supported_features": 2}) == "range_only"
    assert qualifies("heat", {**HEAT, "supported_features": 0}) == "no_setpoint"
    assert qualifies("heat", {**HEAT, "temperature": None}) == "no_setpoint"
    assert qualifies("heat", {**HEAT, "current_temperature": "x"}) == "no_temperature"


@pytest.mark.parametrize(
    ("action", "setpoint"),
    [("normal", None), ("lower", 20.0), ("preheat", 21.5), ("return", 21.5), ("night", 19.5), ("away", 18.0)],
)
def test_each_plan_action_s_setpoint_relative_to_the_person_s_value(action: str, setpoint: float | None) -> None:
    assert aim(action, 21.0, Limits(), night_delta_c=1.5, away_delta_c=3.0) == setpoint


def test_a_setpoint_is_on_the_thermostat_s_step_and_within_its_range() -> None:
    assert fit(20.3, Limits(step=0.5)) == 20.5
    assert fit(20.26, Limits(step=0.1)) == 20.3
    assert fit(20.6, Limits(step=1.0)) == 21.0
    assert fit(4.0, Limits(low=5.0, high=35.0)) == 5.0 and fit(40.0, Limits(low=5.0, high=35.0)) == 35.0
    assert Limits.of({"target_temp_step": 0.1, "min_temp": 7, "max_temp": 30}) == Limits(0.1, 7.0, 30.0)
    assert Limits.of({"target_temp_step": 0}) == Limits()
    # Away on a floor thermostat whose lowest value is 18: held at 18.
    assert aim("away", 19.0, Limits(low=18.0), night_delta_c=1.0, away_delta_c=3.0) == 18.0


def test_read_back_is_within_one_step_and_the_same_value_within_half_a_step() -> None:
    limits = Limits(step=0.5)
    assert read_back(20.5, 20.0, limits) and not read_back(21.0, 20.0, limits) and not read_back(None, 20.0, limits)
    assert same(20.0, 20.0, limits) and same(20.2, 20.0, limits) and not same(20.5, 20.0, limits)


def test_a_write_has_taken_only_when_the_thermostat_no_longer_shows_the_value_before_it() -> None:
    half, whole = Limits(0.5, 5.0, 30.0), Limits(1.0, 5.0, 30.0)
    # Two steps apart: the old value is outside the read-back's reach anyway.
    assert taken(21.0, 21.0, 22.0, half) and not taken(22.0, 21.0, 22.0, half)
    # One step apart (a pre-heat handed back, a lowering on a whole-degree thermostat): the old value still showing
    # is within a step of the one written, and is no read-back.
    assert not taken(22.5, 22.0, 22.5, half) and taken(22.0, 22.0, 22.5, half)
    assert not taken(22.0, 21.0, 22.0, whole) and taken(21.0, 21.0, 22.0, whole)
    # A thermostat that rounds to a value of its own, neither the old one nor the one written: taken.
    assert taken(21.5, 21.0, 22.0, half)
    # Already showing the value written, or nothing known of before: the read-back alone.
    assert taken(22.0, 22.0, 22.0, half) and taken(22.5, 22.0, None, half) and not taken(None, 22.0, 22.5, half)


def test_a_planned_lowering_runs_by_the_relay_s_rules() -> None:
    decision = decide(situation())
    assert (decision.action, decision.setpoint_c, decision.reason, decision.until) == ("lower", 21.0 - LOWER_C, "planned", NOW + 2 * H)
    # Held 3 hours: ends (the most in a row), and none starts within the hour after.
    held = situation(held="lower", held_since=NOW - 3 * H, plan_until=NOW + H)
    assert decide(held).setpoint_c is None and decide(held).reason == "max_run"
    assert decide(situation(last_end=NOW - 30 * MIN)).reason == "rest"
    # Six hours in a rolling day at most; a run too short to be worth it does not start.
    assert decide(situation(lowered_24h=timedelta(hours=6))).setpoint_c is None
    assert decide(situation(plan_until=NOW + 30 * MIN)).reason == "too_short"
    # Held, the plan back to normal: kept to the least run, then ended.
    early = situation("normal", held="lower", held_since=NOW - 20 * MIN)
    assert decide(early).action == "lower" and decide(early).reason == "min_lower"
    assert decide(replace(early, held_since=NOW - 50 * MIN)).setpoint_c is None


def test_the_zone_s_own_comfort_releases_a_lowering_and_ends_a_pre_heat() -> None:
    cold = situation(current_c=20.4)
    assert decide(cold).setpoint_c is None and decide(cold).reason == "zone_cold"
    held_cold = situation(current_c=20.4, held="lower", held_since=NOW - 10 * MIN)
    assert decide(held_cold).setpoint_c is None and decide(held_cold).reason == "zone_cold"
    assert decide(situation("preheat", current_c=21.0)).setpoint_c == 21.5
    warm = situation("preheat", current_c=21.5)
    assert decide(warm).setpoint_c is None and decide(warm).reason == "zone_warm"
    # Ended in this hour: not started again in it.
    assert decide(situation("preheat", current_c=21.2, raise_ended=NOW + 10 * MIN, now=NOW + 20 * MIN)).setpoint_c is None
    assert decide(situation("preheat", current_c=21.2, raise_ended=NOW - 10 * MIN)).setpoint_c == 21.5
    # No reading of its own: the plan alone.
    assert decide(situation(current_c=None)).setpoint_c == 20.0


def test_the_night_and_away_are_the_household_s_own_and_never_released() -> None:
    night = decide(situation("night", current_c=19.0))
    assert (night.action, night.setpoint_c, night.reason) == ("night", 19.5, "night")
    away = decide(situation("away", current_c=15.0))
    assert (away.action, away.setpoint_c) == ("away", 18.0)


def test_nothing_while_stale_and_no_lowering_in_the_cold() -> None:
    assert decide(situation(safety="stale")).setpoint_c is None
    assert decide(situation("night", safety="stale")).setpoint_c is None
    assert decide(situation(action=None)).setpoint_c is None
    assert decide(situation(safety="too_cold")).setpoint_c is None
    assert decide(situation(safety="too_cold", held="lower", held_since=NOW - 5 * MIN)).reason == "too_cold"
    # Settling: a held lowering goes on, none starts.
    assert decide(situation(safety="settling")).setpoint_c is None
    assert decide(situation(safety="settling", held="lower", held_since=NOW - 5 * MIN)).setpoint_c == 20.0


def test_no_lowering_starts_without_room_for_its_end_in_the_day_s_writes() -> None:
    assert decide(situation(writes_today=MAX_WRITES_DAY - 1)).reason == "write_cap"
    assert decide(situation(writes_today=MAX_WRITES_DAY - 2)).setpoint_c == 20.0


def test_a_lowered_zone_s_energy_comes_back_after_it() -> None:
    delta = forecast_delta([(NOW, NOW + 2 * H)], 1.0, NOW)
    assert delta == {NOW: -1.0, NOW + H: -1.0, NOW + 2 * H: 1.0, NOW + 3 * H: 1.0}
    assert sum(delta.values()) == 0
    # Its past part is not changed again, its rebound still is; one whose rebound is over is gone.
    later = forecast_delta([(NOW, NOW + 2 * H)], 1.0, NOW + H + 30 * MIN)
    assert later == {NOW + H: -1.0, NOW + 2 * H: 1.0, NOW + 3 * H: 1.0}
    assert forecast_delta([(NOW - 5 * H, NOW - 3 * H)], 1.0, NOW) == {}
    assert forecast_delta([(NOW, NOW + H)], 0.0, NOW) == {}
