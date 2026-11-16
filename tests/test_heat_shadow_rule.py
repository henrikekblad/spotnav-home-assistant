"""The heat card's day band in shadow (`heat/shadow_rule.py`): lowering in the dearest hours under the fixed guards,
pre-heating before them, the cooler night, away and the return."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.spotnav.heat.config import HeatConfig, Night
from custom_components.spotnav.heat.shadow_rule import band, day_hours, dear_hours, would_do

ZONE = ZoneInfo("Europe/Stockholm")
MIDNIGHT = datetime(2026, 11, 16, 0, 0, tzinfo=ZONE).astimezone(timezone.utc)
HOURS = day_hours(MIDNIGHT, ZONE)
#: A November day (öre/kWh with fees): cheap night, morning and evening peaks.
PRICES = [80, 78, 76, 75, 77, 85, 120, 190, 210, 160, 120, 110, 105, 100, 105, 130, 180, 240, 260, 220, 150, 110, 95, 85]


def priced() -> dict[datetime, float]:
    return dict(zip(HOURS, map(float, PRICES), strict=True))


def actions(hours) -> str:
    return "".join({"normal": ".", "lower": "L", "preheat": "P", "night": "N", "away": "A", "return": "R"}[h.action] for h in hours)


def test_a_day_lowers_its_dearest_hours_and_preheats_before_them() -> None:
    now = MIDNIGHT + timedelta(hours=12, minutes=10)
    hours, guard = band(HeatConfig(target_c=21.0), HOURS, priced(), now, ZONE)
    assert guard is None and len(hours) == 24
    assert actions(hours) == "......PLL.......PLLL...."
    lowered = [hour for hour in hours if hour.action == "lower"]
    assert all(hour.target_c == 20.5 for hour in lowered) and len(lowered) <= 6
    assert next(hour for hour in hours if hour.action == "preheat").target_c == 21.5
    would = would_do(hours, now)
    assert would is not None and (would.action, would.until) == ("normal", MIDNIGHT + timedelta(hours=16))


def test_never_more_than_three_hours_in_a_row_nor_six_a_day() -> None:
    flat_peak = {start: (300.0 if 6 <= index <= 13 else 50.0) for index, start in enumerate(HOURS)}
    chosen = sorted(dear_hours(flat_peak, set()))
    indexes = [HOURS.index(start) for start in chosen]
    assert len(chosen) <= 6
    run = longest = 1
    for previous, current in zip(indexes, indexes[1:], strict=False):
        run = run + 1 if current == previous + 1 else 1
        longest = max(longest, run)
    assert longest <= 3
    # A flat day has nothing worth lowering.
    assert dear_hours({start: 100.0 for start in HOURS}, set()) == set()


def test_the_guards() -> None:
    now = MIDNIGHT + timedelta(hours=6)
    hours, guard = band(HeatConfig(), HOURS, {}, now, ZONE)
    assert guard == "no_prices" and set(actions(hours)) == {"."}
    assert hours[10].reason == "no_prices" and hours[2].reason == "normal"
    _, guard = band(HeatConfig(), HOURS, priced(), now, ZONE, outdoor_c=-16.0)
    assert guard == "too_cold"
    # A room already a degree below the target: no lowering from now on; the morning before now keeps its plan.
    later = MIDNIGHT + timedelta(hours=9, minutes=30)
    hours, guard = band(HeatConfig(target_c=21.0), HOURS, priced(), later, ZONE, coldest_c=19.8)
    assert guard == "room_cold"
    assert "L" not in actions(hours)[9:] and actions(hours)[7:9] == "LL"


def test_the_cooler_night_and_away() -> None:
    night = HeatConfig(target_c=21.0, night=Night(True, 1.5, "23:00", "06:00"))
    hours, _ = band(night, HOURS, priced(), MIDNIGHT + timedelta(hours=2), ZONE)
    assert actions(hours)[:6] == "NNNNNN" and actions(hours)[23] == "N"
    assert hours[0].target_c == 19.5
    away = HeatConfig(target_c=21.0, mode="away", away_until=(MIDNIGHT + timedelta(hours=17)).isoformat())
    hours, _ = band(away, HOURS, priced(), MIDNIGHT + timedelta(hours=8), ZONE)
    assert actions(hours)[:17] == "A" * 15 + "RR" and hours[0].target_c == 18.0 and hours[15].target_c == 21.0
    would = would_do(hours, MIDNIGHT + timedelta(hours=8), away, ZONE)
    assert would is not None and (would.action, would.until, would.reason) == ("away", MIDNIGHT + timedelta(hours=15), "away")
    # Away for days: the line follows it past the day's end, to the warm-up before the return.
    long_away = HeatConfig(target_c=21.0, mode="away", away_until=(MIDNIGHT + timedelta(days=4, hours=16)).isoformat())
    hours, _ = band(long_away, HOURS, priced(), MIDNIGHT + timedelta(hours=8), ZONE)
    would = would_do(hours, MIDNIGHT + timedelta(hours=8), long_away, ZONE)
    assert would is not None and would.until == MIDNIGHT + timedelta(days=4, hours=14)
    # The cooler night at 23:30 lasts to 06:00 the next morning.
    hours, _ = band(night, HOURS, priced(), MIDNIGHT + timedelta(hours=23, minutes=30), ZONE)
    would = would_do(hours, MIDNIGHT + timedelta(hours=23, minutes=30), night, ZONE)
    assert would is not None and would.until == MIDNIGHT + timedelta(days=1, hours=6)


def test_a_short_day_at_the_clock_change() -> None:
    spring = datetime(2027, 3, 28, 0, 0, tzinfo=ZONE).astimezone(timezone.utc)
    assert len(day_hours(spring, ZONE)) == 23


def test_a_tight_hour_in_the_dearer_half_is_lowered_first() -> None:
    now = MIDNIGHT + timedelta(hours=5)
    # 15:00 (130 öre, below the dear bar) is tight: the car and the house reach the import cap there.
    tight = {HOURS[15], HOURS[2]}
    hours, _ = band(HeatConfig(target_c=21.0), HOURS, priced(), now, ZONE, tight=tight)
    assert hours[15].action == "lower" and hours[15].reason == "tight_hour"
    # 02:00 is tight too, but in the cheap half: the heat would come back dearer after it.
    assert hours[2].action == "normal"
    assert sum(1 for hour in hours if hour.action == "lower") <= 6


def test_a_cold_forecast_hour_is_not_lowered_and_a_room_below_the_band_stops_it() -> None:
    now = MIDNIGHT + timedelta(hours=5)
    outdoor = {start: (-16.0 if index in (17, 18) else -5.0) for index, start in enumerate(HOURS)}
    hours, guard = band(HeatConfig(target_c=21.0), HOURS, priced(), now, ZONE, outdoor_by_hour=outdoor)
    assert guard is None
    assert hours[17].action == "normal" and hours[17].reason == "too_cold" and hours[16].action == "lower"
    # The coldest room 20.4 °C is below the band (20.5): no lowering from now on.
    _, guard = band(HeatConfig(target_c=21.0), HOURS, priced(), now, ZONE, coldest_c=20.4)
    assert guard == "room_cold"
