"""A house warmer than its comfort band lowers the heat whatever the price (`heat/shadow_rule.RoomWarm`, the band's
`room_warm` hours, `heat/rooms.warm_c`), under every fixed guard of the relay's rules (`heat/relay_control.decide`).
Pure: each minute the warm state is stepped on the room's reading, the plan made again and the rules decide."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from custom_components.spotnav.heat.config import HeatConfig
from custom_components.spotnav.heat.relay_control import (
    MAX_WRITES_DAY,
    NO_LOWER_AT_C,
    SAFETY_ROOM_COLD,
    SAFETY_TOO_COLD,
    Situation,
    decide,
    lowered_within,
    planned_run_end,
)
from custom_components.spotnav.heat.rooms import SensorReading, indoor, warm_c
from custom_components.spotnav.heat.shadow_rule import RoomWarm, band, would_do

from .test_heat_shadow_rule import HOURS, MIDNIGHT, ZONE, priced

MIN = timedelta(minutes=1)
H = timedelta(hours=1)
CONFIG = HeatConfig(target_c=21.0)
HIGH = 21.5
LOW = 20.5
#: Flat prices: no hour is dear (the owner's day).
FLAT = {start: 100.0 for start in HOURS}


def simulate(
    room: Callable[[datetime], float | None],
    start: datetime,
    minutes: int,
    *,
    prices: dict[datetime, float] = FLAT,
    outdoor_c: float = 2.0,
    writes_today: int = 0,
) -> tuple[list[tuple[datetime, datetime | None]], list[str], int]:
    """The relay's lowerings, the reasons it opened, and its writes, minute by minute."""
    warm = RoomWarm()
    lowerings: list[tuple[datetime, datetime | None]] = []
    opened: list[str] = []
    held_since: datetime | None = None
    last_end: datetime | None = None
    writes = writes_today
    for minute in range(minutes):
        now = start + minute * MIN
        value = room(now)
        warm.step(now, value, HIGH)
        plan, _guard = band(CONFIG, HOURS, prices, now, ZONE, outdoor_c=outdoor_c, coldest_c=value, room_warm=warm.active)
        safety = None
        if outdoor_c <= NO_LOWER_AT_C:
            safety = SAFETY_TOO_COLD
        elif value is not None and value < LOW:
            safety = SAFETY_ROOM_COLD
        decision = decide(
            Situation(
                now=now,
                plan_until=planned_run_end(plan, now),
                held_since=held_since,
                last_end=last_end,
                lowered_24h=lowered_within(lowerings, now - timedelta(days=1), now),
                writes_today=writes,
                safety=safety,
            )
        )
        if decision.closed and held_since is None:
            held_since, writes = now, writes + 1
            lowerings.append((now, None))
        elif not decision.closed and held_since is not None:
            lowerings[-1] = (held_since, now)
            held_since, last_end, writes = None, now, writes + 1
            opened.append(decision.reason)
    return lowerings, opened, writes


NOON = MIDNIGHT + 12 * H


def test_above_the_band_for_less_than_an_hour_lowers_nothing() -> None:
    lowerings, _opened, writes = simulate(lambda t: 22.3 if t < NOON + 55 * MIN else 21.4, NOON, 180)
    assert lowerings == [] and writes == 0


def test_an_hour_above_the_band_lowers_until_back_inside_with_hysteresis() -> None:
    # 22.3 from noon; at 13:10 21.4 (inside, but not 0.2 inside: still lowered), at 13:20 21.2 (back inside).
    def room(t: datetime) -> float:
        return 22.3 if t < NOON + 70 * MIN else 21.4 if t < NOON + 80 * MIN else 21.2

    lowerings, opened, _writes = simulate(room, NOON, 240)
    # Lowered from 13:00 (an hour above), held the 45 minutes past the room's return, then released.
    assert lowerings == [(NOON + H, NOON + H + 45 * MIN)] and opened == ["normal"]


def test_a_house_that_stays_warm_keeps_every_guard() -> None:
    lowerings, opened, writes = simulate(lambda _t: 22.3, NOON - 3 * H, 12 * 60)
    # Three hours at most in a row, an hour back to normal between, six hours in 24, the writes within the cap.
    assert lowerings[0] == (NOON - 2 * H, NOON + H)
    assert lowerings[1] == (NOON + 2 * H, NOON + 5 * H)
    assert opened == ["max_run", "day_cap"] and len(lowerings) == 2
    assert writes <= MAX_WRITES_DAY


def test_the_days_writes_cap_a_warm_lowering() -> None:
    lowerings, _opened, writes = simulate(lambda _t: 22.3, NOON, 180, writes_today=MAX_WRITES_DAY - 1)
    assert lowerings == [] and writes == MAX_WRITES_DAY - 1


def test_never_at_minus_fifteen() -> None:
    lowerings, _opened, _writes = simulate(lambda _t: 22.3, NOON, 180, outdoor_c=-15.0)
    assert lowerings == []
    # The plan does not lower either.
    warm = RoomWarm(since=NOON - H, active=True)
    plan, guard = band(CONFIG, HOURS, FLAT, NOON, ZONE, outdoor_c=-16.0, room_warm=warm.active)
    assert guard == "too_cold" and all(hour.action == "normal" for hour in plan)


def test_a_room_below_the_band_releases_at_once() -> None:
    lowerings, opened, _writes = simulate(lambda t: 22.3 if t < NOON + 70 * MIN else 20.4, NOON, 180)
    assert lowerings == [(NOON + H, NOON + 70 * MIN)] and opened == ["room_cold"]


def test_the_plan_shows_the_warm_hours_with_their_own_reason_also_in_shadow() -> None:
    now = NOON + 10 * MIN
    plan, guard = band(CONFIG, HOURS, FLAT, now, ZONE, coldest_c=22.3, room_warm=True)
    assert guard is None
    lowered = [hour for hour in plan if hour.action == "lower"]
    assert [hour.start for hour in lowered] == [NOON, NOON + H]
    assert all(hour.reason == "room_warm" and hour.target_c == LOW for hour in lowered)
    # No pre-heat before a warm run.
    assert all(hour.action != "preheat" for hour in plan)
    would = would_do(plan, now)
    assert would is not None and (would.action, would.reason, would.until) == ("lower", "room_warm", NOON + 2 * H)
    # Without prices it lowers all the same.
    plan, guard = band(CONFIG, HOURS, {}, now, ZONE, room_warm=True)
    assert guard == "no_prices" and [hour.reason for hour in plan if hour.action == "lower"] == ["room_warm", "room_warm"]


def test_dear_and_warm_are_one_lowering() -> None:
    # The priced day lowers 17-19 (dear); the house turns too warm at 16:10.
    now = MIDNIGHT + 16 * H + 10 * MIN
    plan, _guard = band(CONFIG, HOURS, priced(), now, ZONE, coldest_c=22.3, room_warm=True)
    by_hour = {HOURS.index(hour.start): hour for hour in plan}
    assert (by_hour[16].action, by_hour[16].reason) == ("lower", "room_warm")
    assert (by_hour[17].action, by_hour[17].reason) == ("lower", "dear_hour")
    lowered = [index for index, hour in by_hour.items() if hour.action == "lower"]
    assert len(lowered) <= 6
    # No run longer than three hours: the dear hours make room for the warm one.
    run = longest = 0
    for index in range(24):
        run = run + 1 if index in lowered else 0
        longest = max(longest, run)
    assert longest <= 3 and by_hour[15].action != "preheat"
    # The relay sees one run: one closing covers both reasons.
    assert planned_run_end(plan, now) == MIDNIGHT + 19 * H


def test_warm_steps_on_and_off() -> None:
    warm = RoomWarm()
    assert not warm.step(NOON, 21.6, HIGH)
    assert not warm.step(NOON + 59 * MIN, 21.6, HIGH)
    assert warm.step(NOON + H, 21.6, HIGH)
    # Within 0.2 of the edge: still warm (no flapping); 0.2 inside: back.
    assert warm.step(NOON + H + MIN, 21.4, HIGH)
    assert not warm.step(NOON + H + 2 * MIN, 21.3, HIGH)
    # A dip to the edge starts the hour again; no reading ends it.
    warm.step(NOON + 2 * H, 21.6, HIGH)
    warm.step(NOON + 2 * H + 30 * MIN, 21.5, HIGH)
    assert not warm.step(NOON + 3 * H + 10 * MIN, 21.6, HIGH) and warm.since == NOON + 3 * H + 10 * MIN
    warm = RoomWarm(since=NOON, active=True)
    assert not warm.step(NOON + H, None, HIGH) and warm.since is None


NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


def _sensor(entity_id: str, now_c: float, half_hour_ago: float, offset: float = 0.0) -> SensorReading:
    return SensorReading(entity_id, entity_id, now_c, ((NOW - timedelta(minutes=35), half_hour_ago),), None, offset)


def test_a_sensor_warmed_by_sun_is_no_warm_reading() -> None:
    # One sensor alone, rising 0.8 °C in half an hour: the rooms rule cannot leave it out, the warm rule does.
    assert warm_c(indoor([_sensor("sensor.kok", 22.3, 21.5)], NOW)) is None
    # Steady, it counts, its offset counted.
    assert warm_c(indoor([_sensor("sensor.kok", 22.3, 22.2)], NOW)) == 22.3
    assert warm_c(indoor([_sensor("sensor.kok", 22.3, 22.2, offset=1.0)], NOW)) == 21.3
    # Two sensors, one warmed by sun while the other is steady: the other rules.
    result = indoor([_sensor("sensor.kok", 23.5, 22.4), _sensor("sensor.hall", 21.9, 21.9)], NOW)
    assert warm_c(result) == 21.9
    # Not known to be steady (no reading before): no warm reading.
    assert warm_c(indoor([SensorReading("sensor.kok", "Kök", 22.3)], NOW)) is None


def test_a_sun_rise_holds_off_the_warm_lowering() -> None:
    # A sensor warmed by sun: 21.4 until 12:00, then up 0.05 °C a minute to 23.0 (rising fast for half an hour after),
    # then flat. The hour above the band starts only once it stops rising.
    history: list[tuple[datetime, float]] = []
    warm = RoomWarm()
    active_at = None
    for minute in range(0, 4 * 60):
        now = NOON - H + minute * MIN
        value = 21.4 if now < NOON else min(23.0, 21.4 + 0.05 * ((now - NOON) / MIN))
        history.append((now, value))
        reading = SensorReading("sensor.kok", "Kök", value, tuple(item for item in history if item[0] >= now - timedelta(minutes=40)))
        if warm.step(now, warm_c(indoor([reading], now)), HIGH) and active_at is None:
            active_at = now
    # Flat at 23.0 from 12:32; rising under 0.6 °C per half hour from 12:51; an hour above after that.
    assert active_at == NOON + H + 51 * MIN
    # Without the exclusion it would have lowered from about 13:03 (an hour after the edge was crossed).
    assert active_at > NOON + H + 10 * MIN
