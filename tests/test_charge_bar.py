"""The dashboard's `progress` block, decided once in Home Assistant (`api/charge_bar.py`).

The rules are the app's charge bar (`ChargeBar.kt`): every basis, the owner's cases (a target inside a
window; a person's Start under a schedule pause at 89 % counting against the car's 100 %), solar and
hybrid without an end, missing numbers as `null`, and the end laid along the plan's windows. Where Home
Assistant knows more (a measured power or current, the session's start level and energy) it is used.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.api.charge_bar import (
    BASIS_ENERGY,
    BASIS_OPEN,
    BASIS_TARGET,
    BASIS_VEHICLE_LIMIT,
    ProgressFacts,
    charge_bar,
    percent_of,
)

UTC = timezone.utc
STOCKHOLM = ZoneInfo("Europe/Stockholm")
IN_WINDOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
WINDOW = (datetime(2026, 9, 22, 8, 15, tzinfo=UTC), datetime(2026, 9, 22, 23, 15, tzinfo=UTC))


def ends(bar: dict | None) -> datetime | None:
    assert bar is not None
    return None if bar["ends_at"] is None else datetime.fromisoformat(bar["ends_at"])


def close(expected: datetime, actual: datetime | None) -> None:
    assert actual is not None
    assert abs((actual - expected).total_seconds()) <= 1, (expected, actual)


def after(start: datetime, kwh: float, kw: float) -> datetime:
    return start + timedelta(hours=kwh / kw)


def target(**changes) -> ProgressFacts:
    """A target charge inside its installed window: 75.1 % of 80 %, 4.22 kWh to go at 10 A on one phase."""
    facts = ProgressFacts(
        now=IN_WINDOW,
        charging=True,
        connection="charging",
        driver="target_soc",
        installed_periods=(WINDOW,),
        installed_amps=10,
        installed_phases=1,
        soc_percent=75.1,
        target_percent=80.0,
        need_kwh=4.22,
        room_kwh=20.0,
        capacity_kwh=77.0,
    )
    return replace(facts, **changes)


KWH_NOW = datetime(2026, 9, 22, 7, 0, tzinfo=UTC)


def energy(**changes) -> ProgressFacts:
    """A fixed amount inside its window: 5 of 20 kWh delivered, at 10 A on one phase."""
    facts = ProgressFacts(
        now=KWH_NOW,
        charging=True,
        connection="charging",
        driver="manual_kwh",
        installed_periods=((KWH_NOW - timedelta(hours=1), KWH_NOW + timedelta(hours=12)),),
        installed_amps=10,
        installed_phases=1,
        phases=1,
        plan_delivered_kwh=5.0,
        plan_remaining_kwh=15.0,
        requested_kwh=20.0,
        settings_amps=10,
    )
    return replace(facts, **changes)


def started(**changes) -> ProgressFacts:
    """A person's Start with no plan: Home Assistant charges until the car is full or unplugged."""
    facts = ProgressFacts(
        now=datetime(2026, 9, 22, 6, 0, tzinfo=UTC),
        charging=True,
        connection="charging",
        driver="manual_kwh",
        paused=True,
        person_started=True,
        phases=1,
        settings_amps=10,
        capacity_kwh=77.0,
    )
    return replace(facts, **changes)


# -- a target: the level as a share of the target, the end from the need


def test_a_target_is_the_level_as_a_share_of_the_target() -> None:
    bar = charge_bar(target())
    assert bar is not None
    assert bar["basis"] == BASIS_TARGET
    assert bar["percent"] == 93  # 75.1 of 80, rounded down
    assert bar["moving"] is True
    close(after(IN_WINDOW, 4.22, 2.3), ends(bar))
    assert bar["power_kw"] == pytest.approx(2.3)
    assert bar["power_source"] == "planned"


def test_the_target_inside_a_window_ends_inside_it_and_never_after_the_last() -> None:
    assert ends(charge_bar(target(need_kwh=80.0))) == WINDOW[1]


def test_the_end_skips_the_gaps_between_windows() -> None:
    bar = charge_bar(
        target(
            need_kwh=2.3,
            installed_periods=(
                (IN_WINDOW, IN_WINDOW + timedelta(minutes=30)),
                (IN_WINDOW + timedelta(hours=2), IN_WINDOW + timedelta(hours=3)),
            ),
        )
    )
    assert ends(bar) == IN_WINDOW + timedelta(hours=2, minutes=30)


def test_windows_in_local_offsets_are_compared_as_instants() -> None:
    # The same window written in Stockholm's summer time: 10:15+02:00 is 08:15Z.
    local = (WINDOW[0].astimezone(STOCKHOLM), WINDOW[1].astimezone(STOCKHOLM))
    bar = charge_bar(target(need_kwh=80.0, installed_periods=(local,)))
    assert ends(bar) == WINDOW[1]
    assert bar is not None and bar["ends_at"].endswith("+00:00")


def test_the_end_crosses_the_autumn_shift_as_elapsed_time() -> None:
    # 01:30Z on 25 October is 03:30 CEST, an hour before the clocks go back; two hours later is 03:30Z.
    now = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    bar = charge_bar(target(now=now, installed_periods=(), need_kwh=4.6, installed_amps=None, installed_phases=None, phases=1, settings_amps=10, paused=False))
    # No window holds now, so the target gives way to the car's limit (to 100 %): the end from the room.
    close(after(now, 20.0, 2.3), ends(bar))
    local = ends(bar).astimezone(STOCKHOLM)  # type: ignore[union-attr]
    assert local.utcoffset() == timedelta(hours=1)


def test_a_target_without_a_need_has_a_percent_and_no_end() -> None:
    bar = charge_bar(target(need_kwh=None))
    assert bar is not None and bar["percent"] == 93 and bar["ends_at"] is None


def test_a_level_at_the_target_is_hundred_and_has_no_end() -> None:
    bar = charge_bar(target(soc_percent=85.0, need_kwh=0.0))
    assert bar is not None and bar["percent"] == 100 and bar["ends_at"] is None


def test_the_target_is_never_above_the_cars_limit() -> None:
    bar = charge_bar(target(vehicle_max_percent=75.0, soc_percent=60.0))
    assert bar is not None and bar["percent"] == 80


def test_a_target_without_a_level_or_a_target_has_no_bar() -> None:
    assert charge_bar(target(soc_percent=None)) is None
    assert charge_bar(target(target_percent=None)) is None


# -- a charge the plan does not drive now: the car's own limit


def test_a_persons_start_under_a_scheduled_pause_counts_to_the_cars_limit() -> None:
    # The owner's screen: the schedule paused until tonight, the car charging anyway at 89 % toward its
    # 100 %, which read "96 % of the target" before.
    now = datetime(2026, 9, 22, 7, 0, tzinfo=UTC)
    bar = charge_bar(
        target(now=now, soc_percent=89.0, room_kwh=9.4, need_kwh=0.0, target_percent=93.0, paused=True)
    )
    assert bar is not None
    assert bar["basis"] == BASIS_VEHICLE_LIMIT
    assert bar["percent"] == 89
    close(after(now, 9.4, 2.3), ends(bar))


def test_outside_the_windows_the_target_gives_way_to_the_cars_limit() -> None:
    now = datetime(2026, 9, 22, 7, 0, tzinfo=UTC)
    bar = charge_bar(target(now=now, vehicle_max_percent=90.0, room_kwh=12.0))
    assert bar is not None and bar["basis"] == BASIS_VEHICLE_LIMIT and bar["percent"] == 83
    close(after(now, 12.0, 2.3), ends(bar))


def test_a_paused_schedule_does_not_drive_the_charge_even_inside_a_window() -> None:
    bar = charge_bar(target(soc_percent=60.0, room_kwh=30.0, paused=True))
    assert bar is not None and bar["basis"] == BASIS_VEHICLE_LIMIT and bar["percent"] == 60
    # The straight line from now, not cut at the window's end.
    close(after(IN_WINDOW, 30.0, 2.3), ends(bar))


def test_a_persons_start_counts_to_the_cars_limit_with_the_room_from_the_battery() -> None:
    facts = started(soc_percent=50.0, vehicle_max_percent=80.0, measured_current_a=16.0)
    bar = charge_bar(facts)
    assert bar is not None and bar["basis"] == BASIS_VEHICLE_LIMIT and bar["percent"] == 62
    close(after(facts.now, 77.0 * 30.0 / 100.0 / 0.9, 3.68), ends(bar))
    assert bar["power_source"] == "measured"


def test_a_persons_start_prefers_the_stated_room() -> None:
    facts = started(soc_percent=50.0, room_kwh=10.0)
    close(after(facts.now, 10.0, 2.3), ends(charge_bar(facts)))


def test_a_persons_start_without_a_level_is_the_open_bar_with_its_power() -> None:
    bar = charge_bar(started(measured_current_a=16.0))
    assert bar is not None
    assert bar["basis"] == BASIS_OPEN
    assert bar["percent"] is None and bar["ends_at"] is None
    assert bar["power_kw"] == pytest.approx(3.68)
    assert bar["moving"] is True


def test_a_persons_start_under_solar_still_states_its_end() -> None:
    assert ends(charge_bar(started(strategy="solar", soc_percent=50.0, room_kwh=10.0))) is not None


def test_an_amount_outside_the_plan_with_no_level_is_the_open_bar() -> None:
    bar = charge_bar(energy(installed_periods=()))
    assert bar is not None and bar["basis"] == BASIS_OPEN and bar["percent"] is None and bar["ends_at"] is None


# -- a fixed amount: delivered over the whole amount


def test_an_amount_is_the_delivered_share() -> None:
    bar = charge_bar(energy())
    assert bar is not None and bar["basis"] == BASIS_ENERGY and bar["percent"] == 25
    close(after(KWH_NOW, 15.0, 2.3), ends(bar))


def test_without_a_remainder_the_requested_amount_is_the_whole() -> None:
    bar = charge_bar(energy(plan_remaining_kwh=None))
    assert bar is not None and bar["percent"] == 25
    close(after(KWH_NOW, 15.0, 2.3), ends(bar))


def test_an_amount_with_nothing_delivered_known_has_no_bar() -> None:
    assert charge_bar(energy(plan_delivered_kwh=None)) is None


def test_a_delivered_amount_is_hundred_and_has_no_end() -> None:
    bar = charge_bar(energy(plan_delivered_kwh=20.0, plan_remaining_kwh=0.0))
    assert bar is not None and bar["percent"] == 100 and bar["ends_at"] is None


def test_with_no_power_known_there_is_a_percent_and_no_end() -> None:
    bar = charge_bar(energy(installed_amps=None, settings_amps=None))
    assert bar is not None and bar["percent"] == 25 and bar["ends_at"] is None
    assert bar["power_kw"] is None and bar["power_source"] is None


# -- Fill: the level as a share of the car's limit


def test_fill_is_the_level_as_a_share_of_the_cars_limit() -> None:
    bar = charge_bar(energy(fill_to_limit=True, soc_percent=60.0, vehicle_max_percent=80.0, room_kwh=17.1))
    assert bar is not None and bar["basis"] == BASIS_VEHICLE_LIMIT and bar["percent"] == 75
    close(after(KWH_NOW, 17.1, 2.3), ends(bar))


def test_fill_without_a_level_has_no_bar() -> None:
    assert charge_bar(energy(fill_to_limit=True)) is None


# -- solar and hybrid follow the sun: a percent, never an end


@pytest.mark.parametrize("strategy", ["solar", "hybrid"])
def test_solar_and_hybrid_show_the_percent_without_an_end(strategy: str) -> None:
    bar = charge_bar(energy(strategy=strategy))
    assert bar is not None and bar["percent"] == 25 and bar["ends_at"] is None


@pytest.mark.parametrize("strategy", ["solar", "hybrid"])
def test_a_sun_charge_outside_the_plan_counts_to_the_limit_without_an_end(strategy: str) -> None:
    bar = charge_bar(energy(strategy=strategy, installed_periods=(), soc_percent=40.0, room_kwh=40.0))
    assert bar is not None and bar["basis"] == BASIS_VEHICLE_LIMIT and bar["percent"] == 40
    assert bar["ends_at"] is None


# -- the power: measured first, then what was assigned or planned


def test_a_measured_power_goes_before_everything() -> None:
    bar = charge_bar(energy(measured_power_kw=3.1, measured_current_a=16.0))
    assert bar is not None and bar["power_kw"] == pytest.approx(3.1) and bar["power_source"] == "measured"
    close(after(KWH_NOW, 15.0, 3.1), ends(bar))


def test_a_measured_current_is_power_over_the_phases_the_charge_uses() -> None:
    bar = charge_bar(energy(measured_current_a=16.0, phases=3, installed_phases=3))
    assert bar is not None and bar["power_kw"] == pytest.approx(11.09, abs=0.01)


def test_the_assigned_current_goes_before_the_schedules() -> None:
    bar = charge_bar(energy(assigned_current_a=6.0))
    assert bar is not None and bar["power_kw"] == pytest.approx(1.38) and bar["power_source"] == "planned"


def test_the_schedules_own_power_goes_before_its_current() -> None:
    bar = charge_bar(target(installed_power_kw=2.11))
    close(after(IN_WINDOW, 4.22, 2.11), ends(bar))


def test_three_phases_use_the_voltage_between_phases() -> None:
    bar = charge_bar(energy(installed_amps=16, installed_phases=3, phases=3, voltage_between_phases_v=230.0))
    assert bar is not None and bar["power_kw"] == pytest.approx(6.37, abs=0.01)


# -- visibility and motion


def test_no_bar_while_the_charge_is_off_or_starting_up() -> None:
    assert charge_bar(energy(charging=False)) is None
    assert charge_bar(energy(starting_up=True)) is None


@pytest.mark.parametrize("state", ["disconnected", "finished", "error"])
def test_no_bar_for_no_car_a_full_car_or_an_error(state: str) -> None:
    assert charge_bar(energy(connection=state)) is None


@pytest.mark.parametrize("state", ["paused", "connected"])
def test_a_paused_charge_stands_still_and_states_no_end_or_power(state: str) -> None:
    bar = charge_bar(energy(connection=state))
    assert bar is not None and bar["moving"] is False and bar["percent"] == 25
    assert bar["ends_at"] is None and bar["power_kw"] is None


def test_a_car_that_takes_no_current_stands_still() -> None:
    bar = charge_bar(energy(vehicle_not_requesting=True))
    assert bar is not None and bar["moving"] is False


def test_a_measured_zero_current_stands_still() -> None:
    bar = charge_bar(energy(measured_current_a=0.1))
    assert bar is not None and bar["moving"] is False and bar["ends_at"] is None


def test_with_no_connection_stated_the_charge_moves() -> None:
    bar = charge_bar(energy(connection="unknown"))
    assert bar is not None and bar["moving"] is True


# -- this charge, from the session


def test_the_sessions_start_and_measured_energy_are_carried() -> None:
    start = KWH_NOW - timedelta(minutes=40)
    bar = charge_bar(energy(session_started_at=start, session_start_soc_percent=41.04, session_delivered_kwh=5.126))
    assert bar is not None
    assert datetime.fromisoformat(bar["started_at"]) == start
    assert bar["start_soc_percent"] == 41.0
    assert bar["delivered_kwh"] == 5.13


def test_without_a_session_its_fields_are_null() -> None:
    bar = charge_bar(energy())
    assert bar is not None
    assert bar["started_at"] is None and bar["start_soc_percent"] is None and bar["delivered_kwh"] is None


def test_the_block_has_exactly_its_keys() -> None:
    bar = charge_bar(energy())
    assert bar is not None
    assert set(bar) == {
        "basis", "percent", "ends_at", "power_kw", "power_source", "moving",
        "start_soc_percent", "started_at", "delivered_kwh",
    }


def test_percent_is_rounded_down_and_clamped() -> None:
    assert percent_of(-0.2) == 0
    assert percent_of(0.999) == 99
    assert percent_of(1.0) == 100
    assert percent_of(1.3) == 100
    assert percent_of(0.57) == 57
    assert percent_of(float("nan")) is None
