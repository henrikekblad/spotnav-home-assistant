"""The month's free level lifts the import target (plan §19): `effective_target` over what each demand-charge part
bills so far (`planning/demand_charge.component_month`), and the site's hold, status and planning with it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.planning.auto_settings import DemandComponent
from custom_components.spotnav.planning.demand_charge import component_month, HourMean
from custom_components.spotnav.planning.planner import HighLoadWindow
from custom_components.spotnav.site.import_target import effective_target
from custom_components.spotnav.site.site_capacity_controller import LIMIT_CAUSE_IMPORT_TARGET

from .test_import_target_hold import _charging, _HOUR, _plan, _tight_night, _told, _V, _world

TZ = "Europe/Stockholm"
DAY = HighLoadWindow(days="all", from_month=1, to_month=12, from_hour=6, to_hour=23)
NIGHT = HighLoadWindow(days="all", from_month=1, to_month=12, from_hour=23, to_hour=6)
#: 2026-11-10 13:00 in Stockholm.
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _hour(day: int, hour_utc: int, kw: float, *, incomplete: bool = False) -> HourMean:
    return HourMean(start=datetime(2026, 11, day, hour_utc, tzinfo=timezone.utc), kw=kw, incomplete=incomplete)


def _lifted(free: float) -> float:
    """The month's free level less its margin, once (`TARGET_MARGIN_KW`, `TARGET_MARGIN_SHARE`)."""
    return free - max(0.1, 0.02 * free)


def _levels(components, hours, now=NOW):
    return [component_month(component, hours, TZ, now).free_kw for component in components]


# -- the level, pure


def test_a_highest_hour_part_lifts_the_target_to_the_months_peak() -> None:
    sauna = [_hour(3, 17, 6.6), _hour(4, 17, 4.0)]
    found = effective_target(6.0, _levels([DemandComponent(top_n=1)], sauna))
    assert found.free_kw == 6.6 and found.lifted
    # Shown at the month's peak, held under it less its margin, once.
    assert found.kw == 6.6
    assert found.hold_kw == pytest.approx(_lifted(6.6))
    # Below the target nothing changes; an incomplete hour never sets a peak.
    assert effective_target(6.0, _levels([DemandComponent(top_n=1)], [_hour(3, 17, 5.0)])).kw == 6.0
    assert effective_target(6.0, _levels([DemandComponent(top_n=1)], [_hour(3, 17, 9.0, incomplete=True)])).kw == 6.0


def test_a_top_three_part_with_fewer_than_three_days_does_not_lift_it() -> None:
    hours = [_hour(3, 17, 8.0), _hour(4, 17, 7.0)]
    found = effective_target(6.0, _levels([DemandComponent(top_n=3)], hours))
    assert found.free_kw == 0.0 and found.kw == 6.0


def test_a_top_three_part_with_today_counted_lifts_it_to_todays_own_peak() -> None:
    # Three days counted and today (the 10th) among them: today's own peak is free.
    hours = [_hour(3, 17, 9.0), _hour(4, 17, 8.0), _hour(10, 10, 7.0), _hour(5, 17, 5.0)]
    assert effective_target(6.0, _levels([DemandComponent(top_n=3)], hours)).hold_kw == pytest.approx(_lifted(7.0))
    # Today not among them: the third highest day.
    hours = [_hour(3, 17, 9.0), _hour(4, 17, 8.0), _hour(5, 17, 6.8), _hour(10, 10, 5.0)]
    assert effective_target(6.0, _levels([DemandComponent(top_n=3)], hours)).hold_kw == pytest.approx(_lifted(6.8))


def test_two_parts_counting_the_same_hour_lift_it_to_the_lower() -> None:
    hours = [_hour(3, 17, 9.0), _hour(4, 17, 6.6)]
    parts = [DemandComponent(top_n=1), DemandComponent(top_n=1, window=DAY)]
    hours_day_only = [*hours, _hour(5, 3, 3.0)]
    assert effective_target(6.0, _levels(parts, hours_day_only)).hold_kw == pytest.approx(_lifted(9.0))
    # A day part whose own peak is lower holds the hour to it.
    parts = [DemandComponent(top_n=1), DemandComponent(top_n=2, window=DAY)]
    assert effective_target(6.0, _levels(parts, hours)).hold_kw == pytest.approx(_lifted(6.6))


def test_an_hour_no_part_counts_keeps_the_target() -> None:
    # 13:00 in Stockholm: only a night part, which does not count it.
    levels = _levels([DemandComponent(top_n=1, window=NIGHT)], [_hour(3, 23, 9.0)])
    assert levels == [None]
    found = effective_target(6.0, levels)
    assert (found.kw, found.free_kw, found.lifted) == (6.0, None, False)


def test_the_margin_never_takes_the_level_below_the_targets_own_hold() -> None:
    # A peak a hair above the target lifts the level to it, and its margin comes off once: never below what the
    # target as set is held under.
    found = effective_target(6.0, _levels([DemandComponent(top_n=1)], [_hour(3, 17, 6.05)]))
    assert found.free_kw == 6.05 and found.kw == 6.05 and found.lifted
    assert found.hold_kw == pytest.approx(6.05 - 0.121)
    assert found.hold_kw >= effective_target(6.0, ()).hold_kw == pytest.approx(5.88)


def test_an_hour_with_a_gap_frees_no_more_than_its_import_over_the_whole_hour() -> None:
    # 9.0 kW over the 52 minutes measured, 8 missing: the floor counts them as no import, 7.8 kW.
    tracker_hour = HourMean(start=datetime(2026, 11, 3, 17, tzinfo=timezone.utc), kw=9.0, floor_kw=7.8)
    floor_hour = HourMean(start=tracker_hour.start, kw=min(tracker_hour.kw, tracker_hour.floor_kw))
    assert effective_target(6.0, _levels([DemandComponent(top_n=1)], [floor_hour])).free_kw == 7.8


def test_a_new_month_starts_from_the_target() -> None:
    # The first hour of December: the month's hours are none yet, so nothing is free.
    december = datetime(2026, 11, 30, 23, 30, tzinfo=timezone.utc)
    assert effective_target(6.0, _levels([DemandComponent(top_n=1)], [], december)).kw == 6.0


# -- the site's hold with the month's peak


def _parts(monkeypatch, *components: DemandComponent) -> None:
    monkeypatch.setattr(
        "custom_components.spotnav.pricing.price_source.site_demand_components",
        lambda _hass, _entry_id: tuple(components),
    )


def _peak(world, kw: float, *, days_ago: int = 0) -> None:
    """Earlier this month the house's sauna made the month's peak hour (complete): today, or `days_ago`."""
    tracker = world.halo.controller.demand.tracker
    start = _HOUR - timedelta(days=days_ago, hours=3)
    tracker.hours.append(HourMean(start=start, kw=kw))


async def test_the_hold_lets_the_hour_reach_the_months_peak_and_says_so(hass: HomeAssistant, monkeypatch) -> None:
    _parts(monkeypatch, DemandComponent(top_n=1))
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "peak")
        await world.step()
        _peak(world, 7.0)
        await _charging(world)
        await world.run(20)
        # 7.0 kW less its margin (6.86 kW), less the house's 1 kW: 8 A, not the 7 A the 6.0 kW target alone gives.
        assert world.car_a == 8.0
        controller = world.halo.controller
        charger = world.halo.charger.entry_id
        status = controller.import_target_status(charger)
        # Shown at the person's numbers: the month's peak and the target as set, not the margined level.
        assert status is not None and status.target_kw == 7.0 and status.set_kw == 6.0
        assert controller.load_balancing_limit(charger) == (8.0, LIMIT_CAUSE_IMPORT_TARGET)
        assert controller.import_target_snapshot()["effective"] == {
            "kw": pytest.approx(7.0), "free_kw": pytest.approx(7.0), "hold_kw": pytest.approx(_lifted(7.0)),
        }
        assert controller.import_target_snapshot()["budget"]["hold_kw"] == pytest.approx(_lifted(7.0))
        # The plan counts on the target as set less its margin (5.88 kW) less the house: the lift is the running
        # hour's hold only.
        assert controller.import_target_plan_kw(charger) == pytest.approx(4.88, abs=0.05)
        for hour in world.complete_hours():
            assert world.hour_kw(hour) <= 7.0


async def test_without_a_peak_above_it_the_target_holds_as_set(hass: HomeAssistant, monkeypatch) -> None:
    _parts(monkeypatch, DemandComponent(top_n=3))
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "nopeak")
        await world.step()
        _peak(world, 9.0, days_ago=1)  # one day only, not today: a top-3 part frees nothing yet
        await _charging(world)
        await world.run(5)
        assert world.car_a == 7.0
        status = world.halo.controller.import_target_status(world.halo.charger.entry_id)
        assert status is not None and status.target_kw == 6.0 == status.set_kw


async def test_the_fuse_still_wins_below_the_months_peak(hass: HomeAssistant, monkeypatch) -> None:
    _parts(monkeypatch, DemandComponent(top_n=1))
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        # A 20 kW peak paid for; a house of 12 A a phase on the 20 A fuse leaves the car 8 A.
        world = await _world(hass, monkeypatch, frozen, "peakfuse", house_w=12 * 3 * _V)
        await world.step()
        _peak(world, 20.0)
        await _charging(world)
        await world.run(5)
        assert world.car_a <= 8.0
        assert world.halo.controller.load_balancing_limit(world.halo.charger.entry_id)[1] != LIMIT_CAUSE_IMPORT_TARGET


async def test_a_departure_at_risk_is_told_at_the_months_peak(hass: HomeAssistant, monkeypatch) -> None:
    _parts(monkeypatch, DemandComponent(top_n=1))
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "peakrisk")
        _peak(world, 7.0)
        await world.run(10)
        told = _told(world)
        # The month's peak and the import target as set, both named.
        assert told and "the month's peak of 7.0 kW (import target 6.0 kW)" in told[-1]["message"], told
        status = world.halo.controller.import_target_status(world.halo.charger.entry_id)
        assert status is not None and status.at_risk is not None
        assert status.at_risk.target_kw == 7.0 and status.at_risk.set_kw == 6.0
        assert dt_util.utcnow() < _HOUR + timedelta(hours=1)
        assert _plan is not None



# -- the lift is the running hour's alone (review)

DAY_ONLY = HighLoadWindow(days="all", from_month=1, to_month=12, from_hour=6, to_hour=23)


def _hour_at(world, start_utc: datetime, kw: float) -> None:
    world.halo.controller.demand.tracker.hours.append(HourMean(start=start_utc, kw=kw))


async def test_a_plan_at_night_never_counts_on_a_day_parts_peak(hass: HomeAssistant, monkeypatch) -> None:
    """22:00 on the site's clock (Home Assistant's zone in tests, Pacific), a day-only part (06-23) with a 9.0 kW peak
    at 10:00: the evening hour is lifted, the plan (whose night hours no part lifts) is not."""
    _parts(monkeypatch, DemandComponent(top_n=1, window=DAY_ONLY))
    at = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)  # 22:00 Pacific
    with freeze_time(at + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "daylift")
        await world.step()
        _hour_at(world, datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc), 9.0)  # 10:00 Pacific
        await _charging(world)
        await world.run(5)
        controller = world.halo.controller
        charger = world.halo.charger.entry_id
        assert controller.import_target_snapshot()["effective"]["kw"] > 8.0
        assert controller.import_target_plan_kw(charger) == pytest.approx(4.88, abs=0.1)


async def test_a_top_three_parts_own_peak_today_is_not_planned_on(hass: HomeAssistant, monkeypatch) -> None:
    _parts(monkeypatch, DemandComponent(top_n=3))
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "top3lift")
        await world.step()
        _peak(world, 8.0)  # today's own peak: free this hour, gone at midnight
        await _charging(world)
        await world.run(5)
        controller = world.halo.controller
        assert controller.import_target_snapshot()["effective"]["kw"] > 7.0
        assert controller.import_target_plan_kw(world.halo.charger.entry_id) == pytest.approx(4.88, abs=0.1)


async def test_the_departures_later_hours_are_judged_at_the_target_as_set(hass: HomeAssistant, monkeypatch) -> None:
    """22 kWh by a departure three hours on, with a 9.0 kW day peak lifting only this hour: at the lifted level it
    would make it, at the 5 kW the target leaves the later hours it is short. Said now, not an hour late."""
    _parts(monkeypatch, DemandComponent(top_n=1, window=DAY_ONLY))
    at = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)  # 22:00 Pacific
    with freeze_time(at + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "dayrisk")
        _hour_at(world, datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc), 9.0)
        now = dt_util.utcnow()
        departure = now + timedelta(hours=3)
        plan = _plan(now, departure, [(now, departure)])
        plan.energy_kwh = 22.0
        world.halo.cc.plan = plan
        world.departure = departure
        await world.run(10)
        status = world.halo.controller.import_target_status(world.halo.charger.entry_id)
        assert dt_util.utcnow() < at + timedelta(hours=1)
        assert status is not None and status.at_risk is not None
        assert _told(world)
