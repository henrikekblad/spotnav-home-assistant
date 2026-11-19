"""Level A of the hot water (`heat/dhw_level_a.py`): the heat plan's blocks kept out of the time before each peak
unless the pump already ran, the day's blocked time capped without a tank sensor, the tank rule, the cuts shown and
the cold-shower risk the review counts."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.heat.config import HeatConfig
from custom_components.spotnav.heat.dhw_level_a import (
    CUT_DAY,
    CUT_PEAK,
    MAX_DAY_NO_TANK,
    TankGuard,
    cold_risk,
    cuts,
    guard_hours,
    kept_hours,
    next_hygiene,
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
    kept = kept_hours(guards(PEAKS, [DAY], ZONE, [], now))
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
    kept = kept_hours(guards(PEAKS, [DAY], ZONE, [], now))
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


# ---- the day's review and the level-A gate


def _day_peaks():
    from custom_components.spotnav.heat.dhw_review import DayPeak

    return [DayPeak(local(6), local(8)), DayPeak(local(18), local(21))]


def test_the_review_counts_a_peak_a_block_made_cold() -> None:
    from custom_components.spotnav.heat.dhw_review import review_day

    previous = local(21, day=DAY - timedelta(days=1))
    blocks = [(local(4), local(6, 15)), (local(9), local(11))]
    runs = [run(6, 20), run(18, 30)]
    review = review_day(
        DAY, ZONE, _day_peaks(), [], runs, lambda _q: 100.0, mode="shadow", cutoff=0.2,
        blocks=blocks, previous_until=previous, blocks_cut=2,
    )
    assert review["cold_risks"] == 1 and review["blocks_cut"] == 2 and review["a_mode"] == "shadow"
    assert [item["cold_risk"] for item in review["per_peak"]] == [True, False]
    # The evening peak looks back to the morning one's end: the 09:00 block came after it, with no run held into
    # the evening peak.
    assert review["hits"] == 2


def test_the_level_a_gate_counts_its_own_shadow_days_and_asks_no_saving() -> None:
    from custom_components.spotnav.heat.dhw_review import back_off, gate

    first = DAY
    days = [
        {"date": (first + timedelta(days=i)).isoformat(), "mode": "shadow", "hits": 2, "peaks": 2, "saved": 0.0}
        for i in range(14)
    ]
    assert gate(days, "A")["open"] and not gate(days, "B")["open"]
    # A day level A steered is no shadow day for it, whatever the boost did.
    live = [{**item, "a_mode": "live"} for item in days]
    assert not gate(live, "A")["open"]
    # Three cold risks within a week of steering close it again; a boost's misses do not.
    today = first + timedelta(days=20)
    risky = [
        {"date": (today - timedelta(days=d)).isoformat(), "a_mode": "live", "mode": "shadow", "cold_risks": 1, "misses": 0}
        for d in (1, 2, 4)
    ]
    assert back_off(risky, today, "A") and not back_off(risky, today)
    assert not back_off(risky[:2], today, "A")


# ---- the pump's periodic increase


def _week_of_runs(start_day: date, days: int, hygiene_days: tuple[int, ...], at: tuple[int, int] = (2, 0)) -> list[HotWaterRun]:
    """A morning and an evening run each day, and a long night run (the periodic increase) on the days given."""
    out = []
    for index in range(days):
        day = start_day + timedelta(days=index)
        out += [run(6, 30, 30, day), run(19, 0, 25, day)]
        if index in hygiene_days:
            begin = local(at[0], at[1], day)
            out.append(HotWaterRun(begin, begin + timedelta(minutes=90), 3.5))
    return sorted(out, key=lambda item: item.start)


def _history(days: int, hygiene_every: int | None, at: tuple[int, int] = (2, 0), skip: tuple[int, ...] = ()) -> list[HotWaterRun]:
    """`days` days up to yesterday with the daily runs, and the periodic increase every `hygiene_every` days counted
    back from yesterday (`skip`: those counts left out)."""
    first = DAY - timedelta(days=days)
    marks: tuple[int, ...] = ()
    if hygiene_every is not None:
        marks = tuple(days - 1 - n * hygiene_every for n in range(days // hygiene_every + 1) if n not in skip)
    return _week_of_runs(first, days, tuple(m for m in marks if m >= 0), at)


@pytest.mark.parametrize("period", [7, 10, 14, 28])
def test_the_periodic_increase_seen_three_times_at_equal_intervals_is_kept_free(period: int) -> None:
    runs = _history(62, period)
    found = next_hygiene(runs, ZONE, local(12))
    # Last seen yesterday at 02:00 for 90 minutes: the next is a period after it, kept free from 01:00 to half an hour
    # after its usual end (04:00).
    last = local(2, 0, DAY - timedelta(days=1))
    assert found == (last + timedelta(days=period, hours=-1), last + timedelta(days=period, minutes=120))
    kept = kept_hours([], found)
    assert sorted(kept) == [found[0] + timedelta(hours=index) for index in range(3)]
    assert set(kept.values()) == {("hygiene", None)}


def test_a_shift_within_the_hour_still_counts() -> None:
    runs = [r for r in _history(30, 14) if not (r.kwh == 3.5 and r.start.astimezone(ZONE).date() == DAY - timedelta(days=1))]
    runs += _week_of_runs(DAY - timedelta(days=1), 1, (0,), at=(2, 40))
    found = next_hygiene(sorted(runs, key=lambda item: item.start), ZONE, local(12))
    assert found is not None and found[0] == local(2, 40, DAY + timedelta(days=13)) - timedelta(hours=1)


def test_the_periodic_increase_is_not_guessed() -> None:
    # Seen twice only, or at unequal intervals, or at different times, or in the day: not known.
    assert next_hygiene(_history(20, 14), ZONE, local(12)) is None
    assert next_hygiene(_history(30, 14, skip=(1,)), ZONE, local(12)) is None
    irregular = _week_of_runs(DAY - timedelta(days=30), 30, (0, 10, 29))
    assert next_hygiene(irregular, ZONE, local(12)) is None
    shifted = _week_of_runs(DAY - timedelta(days=30), 30, (1, 15)) + _week_of_runs(DAY - timedelta(days=1), 1, (0,), at=(4, 30))
    assert next_hygiene(sorted(shifted, key=lambda item: item.start), ZONE, local(12)) is None
    assert next_hygiene(_history(62, 14, at=(13, 0)), ZONE, local(12)) is None
    assert next_hygiene([], ZONE, local(12)) is None


def test_a_block_over_the_periodic_increase_is_cut() -> None:
    prices = priced()
    now = local(0, 10)
    config = HeatConfig(target_c=21.0)
    raw, _ = band(config, HOURS, prices, now, ZONE)
    # Say the increase runs at 07:30 (a pump set so): the morning's dear hours are kept free of a block.
    kept = kept_hours([], (local(6, 30), local(9, 30)))
    shaped, _ = band(config, HOURS, prices, now, ZONE, keep_normal=kept)
    assert [(c.start, c.kind) for c in cuts(raw, shaped, kept)] == [(local(7), "hygiene"), (local(8), "hygiene")]


def test_a_standby_reheat_does_not_drop_the_morning_guard() -> None:
    """Review NIT: the owner's small 20:20 run (standby loss) after the evening peak's end does not show the tank was
    filled after its use; a real run does."""
    late = run(21, 20, 10, DAY - timedelta(days=1))
    small = type(late)(late.start, late.end, 0.3)
    assert [g.ready for g in guards(PEAKS, [DAY], ZONE, [small], local(3), cutoff=0.35)] == [local(6), local(18)]
    assert [g.ready for g in guards(PEAKS, [DAY], ZONE, [late], local(3), cutoff=0.35)] == [local(18)]
