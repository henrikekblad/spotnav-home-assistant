"""Level A of the hot water (`heat/dhw_level_a.py`): the heat plan's blocks kept out of the time before each peak
unless the pump already ran, the day's blocked time capped without a tank sensor, the tank rule, the cuts shown and
the cold-shower risk the review counts."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.spotnav.heat.config import HeatConfig
from custom_components.spotnav.heat.dhw_level_a import (
    CUT_DAY,
    CUT_PEAK,
    MAX_DAY_NO_TANK,
    TankGuard,
    cold_risk,
    cuts,
    guard_hours,
    guards,
    max_day_hours,
)
from custom_components.spotnav.heat.dhw_peaks import DEFAULT_PEAKS, Peak
from custom_components.spotnav.heat.meter_split import HotWaterRun
from custom_components.spotnav.heat.shadow_rule import band, day_hours

ZONE = ZoneInfo("Europe/Stockholm")
DAY = date(2026, 11, 16)
MIDNIGHT = datetime(2026, 11, 16, tzinfo=ZONE).astimezone(timezone.utc)
HOURS = day_hours(MIDNIGHT, ZONE)
#: A November day: cheap night, a dear morning and a dear evening.
PRICES = [80, 78, 76, 75, 77, 85, 120, 190, 210, 160, 120, 110, 105, 100, 105, 130, 180, 240, 260, 220, 150, 110, 95, 85]
#: The owner's peaks: a shower by 06:00 and an evening one by 18:00.
PEAKS = (
    Peak(time(6, 0), time(8, 0), 0.9, True, time(7, 10)),
    Peak(time(18, 0), time(21, 0), 0.8, True, time(19, 30)),
)


def local(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE).astimezone(timezone.utc)


def run(hour: int, minute: int = 0, minutes: int = 30, day: date = DAY) -> HotWaterRun:
    start = local(hour, minute, day)
    return HotWaterRun(start, start + timedelta(minutes=minutes), 0.8)


def priced() -> dict[datetime, float]:
    return dict(zip(HOURS, map(float, PRICES), strict=True))


def lowered(hours) -> list[int]:
    return [HOURS.index(hour.start) for hour in hours if hour.action == "lower"]


# ---- the time before a peak


def test_each_peak_keeps_the_two_hours_before_it_free() -> None:
    now = local(0, 30)
    found = guards(PEAKS, [DAY], ZONE, [], now)
    assert [(g.start, g.ready) for g in found] == [(local(4), local(6)), (local(16), local(18))]
    assert guard_hours(found) == {local(4): local(6), local(5): local(6), local(16): local(18), local(17): local(18)}


def test_a_ready_by_off_the_hour_keeps_every_hour_it_touches() -> None:
    peak = Peak(time(5, 15), time(7, 0), 1.0, True, time(6, 30))
    found = guards([peak], [DAY], ZONE, [], local(0))
    assert sorted(guard_hours(found)) == [local(3), local(4), local(5)]


def test_no_guard_where_the_pump_already_ran_since_the_last_peak() -> None:
    # Yesterday's evening peak ended 21:00; the pump reheated at 02:10: the tank is full for the morning.
    yesterday = DAY - timedelta(days=1)
    runs = [run(2, 10)]
    found = guards(PEAKS, [DAY], ZONE, runs, local(3))
    assert [g.ready for g in found] == [local(18)]
    # A run before the previous peak's end does not count, nor one still going.
    assert [g.ready for g in guards(PEAKS, [DAY], ZONE, [run(20, 0, day=yesterday)], local(3))] == [local(6), local(18)]
    assert [g.ready for g in guards(PEAKS, [DAY], ZONE, [run(2, 50, 30)], local(3))] == [local(6), local(18)]
    # Later in the day the evening peak looks back to the morning one's end (08:00).
    assert [g.ready for g in guards(PEAKS, [DAY], ZONE, [run(9)], local(12))] == []


def test_no_guard_for_a_peak_a_boost_fills_or_that_has_passed() -> None:
    assert [g.ready for g in guards(PEAKS, [DAY], ZONE, [], local(0), boosted=[local(6)])] == [local(18)]
    assert [g.ready for g in guards(PEAKS, [DAY], ZONE, [], local(7))] == [local(18)]


def test_the_band_keeps_the_hours_before_a_peak() -> None:
    now = local(0, 10)
    config = HeatConfig(target_c=21.0)
    raw, _ = band(config, HOURS, priced(), now, ZONE)
    assert lowered(raw) == [7, 8, 17, 18, 19]
    kept = guard_hours(guards(PEAKS, [DAY], ZONE, [], now))
    shaped, _ = band(config, HOURS, priced(), now, ZONE, keep_normal=kept)
    # 17:00 is the hour before the evening shower: kept normal; no other hour is dear enough, so the block is shorter.
    assert lowered(shaped) == [7, 8, 18, 19]
    assert [(c.start, c.kind, c.ready) for c in cuts(raw, shaped, kept)] == [(local(17), CUT_PEAK, local(18))]
    # Too warm indoors does not lower an hour before a peak either.
    warm, _ = band(config, HOURS, priced(), local(17, 10), ZONE, keep_normal=kept, room_warm=True)
    assert HOURS.index(local(17)) not in lowered(warm)


def test_a_block_kept_from_before_a_peak_moves_to_the_next_dear_hour() -> None:
    prices = priced()
    # Seven hours at or over the bar; the plan lowers six a day at most, so the later of the two cheapest waits.
    prices[local(7)] = 200.0
    prices[local(12)] = 200.0
    prices[local(21)] = 230.0
    now = local(0, 10)
    config = HeatConfig(target_c=21.0)
    kept = guard_hours(guards(PEAKS, [DAY], ZONE, [], now))
    raw, _ = band(config, HOURS, prices, now, ZONE)
    assert lowered(raw) == [7, 8, 17, 18, 19, 21]
    shaped, _ = band(config, HOURS, prices, now, ZONE, keep_normal=kept)
    # With 17:00 kept before the evening shower, 12:00 takes its place.
    assert lowered(shaped) == [7, 8, 12, 18, 19, 21]
    assert [c.start for c in cuts(raw, shaped, kept)] == [local(17)]


def test_without_a_tank_sensor_a_day_lowers_at_most_four_hours() -> None:
    assert max_day_hours(tank_sensor=False) == MAX_DAY_NO_TANK == 4
    assert max_day_hours(tank_sensor=True) is None
    now = local(0, 10)
    config = HeatConfig(target_c=21.0)
    raw, _ = band(config, HOURS, priced(), now, ZONE)
    capped, _ = band(config, HOURS, priced(), now, ZONE, max_day=4)
    # The dearest four stay; the cheapest of the five goes.
    assert lowered(capped) == [8, 17, 18, 19]
    assert [(c.start, c.kind) for c in cuts(raw, capped, {})] == [(local(7), CUT_DAY)]
    # A cap above the plan's own changes nothing.
    assert lowered(band(config, HOURS, priced(), now, ZONE, max_day=9)[0]) == lowered(raw)


def test_the_defaults_before_any_peak_is_learned() -> None:
    found = guards(DEFAULT_PEAKS, [DAY], ZONE, [], local(0))
    assert [(g.start, g.ready) for g in found] == [(local(3), local(5)), (local(15), local(17))]


# ---- the tank rule


def test_the_tank_rule_holds_off_blocks_below_forty_until_forty_five() -> None:
    tank = TankGuard()
    assert tank.step(None) is False
    assert tank.step(48.0) is False
    assert tank.step(39.5) is True
    assert tank.step(42.0) is True
    assert tank.step(None) is True
    assert tank.step(45.0) is False
    assert tank.step(41.0) is False


# ---- the review's cold-shower risk


def test_a_block_that_held_the_run_into_the_peak_is_a_cold_risk() -> None:
    previous = local(21, day=DAY - timedelta(days=1))
    block = [(local(4), local(6, 15))]
    assert cold_risk(local(6), previous, block, [run(6, 20)]) is True
    # A run that started before the peak but ended after it: still not ready by then.
    assert cold_risk(local(6), previous, [(local(4), local(5, 50))], [run(5, 55)]) is True


def test_no_cold_risk_without_a_held_run() -> None:
    previous = local(21, day=DAY - timedelta(days=1))
    # The pump ran at 02:00 after the evening peak: the tank was full before the block.
    assert cold_risk(local(6), previous, [(local(4), local(6, 15))], [run(2), run(6, 20)]) is False
    # The run came long after the block: the tank did not need it.
    assert cold_risk(local(6), previous, [(local(4), local(6, 15))], [run(8)]) is False
    # A block that ended well before the guard, and its run done by the peak.
    assert cold_risk(local(6), previous, [(local(1), local(3))], [run(3, 5)]) is False
    # The run after the block was done before the peak.
    assert cold_risk(local(6), previous, [(local(3), local(4, 30))], [run(4, 40)]) is False
    # No block at all.
    assert cold_risk(local(6), previous, [], [run(6, 20)]) is False
