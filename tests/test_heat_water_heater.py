"""A plain water heater switched by SpotNav, the pure rules (`heat/water_heater.py`): legionella first. Full heat seen
on the meter or the tank sensor, held on from 20 hours after it, never off longer than eight hours, on whenever a
reading or the plan is missing; the cheap windows inside those rules. The property tests drive the rules with a tank
model through any prices and any loss of the meter, the tank sensor and the plan."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.spotnav.heat.water_heater import (
    FULL_EVERY,
    GIVE_UP_AFTER,
    MAX_OFF,
    MAX_PLANNED_OFF,
    MAX_SWITCHINGS_DAY,
    MODE_METERED,
    MODE_TANK,
    MODE_UNMETERED,
    WINDOW_CHEAP,
    WINDOW_FULL_HEAT,
    WINDOW_MAX_OFF,
    Kept,
    Rules,
    Sizes,
    Window,
    cheapest_block,
    daily_kwh,
    element_kw,
    fill_gaps,
    forecast_delta,
    mode_of,
    need_kwh,
    plan_windows,
    sizes,
)

ZONE = ZoneInfo("Europe/Stockholm")
H = timedelta(hours=1)
MIN = timedelta(minutes=1)
# 2026-11-20 12:00 local (CET, UTC+1).
NOON = datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc)
DAY = "2026-11-20"


def local(day: int, hour: int) -> datetime:
    """Local `hour` on November `day` 2026 as UTC."""
    return datetime(2026, 11, day, hour, tzinfo=ZONE).astimezone(timezone.utc)


def flat_prices(start: datetime, hours: int, price: float = 100.0, cheap: dict[datetime, float] | None = None) -> dict[datetime, float]:
    out = {start + i * H: price for i in range(hours)}
    out.update(cheap or {})
    return out


UNMETERED = Sizes(5, 3, 7, True)


# ------------------------------------------------------------------------------------------------ full heat


def test_full_heat_is_the_element_drawing_and_then_idle_for_fifteen_minutes_while_on() -> None:
    rules = Rules()
    look = dict(metered=True, tank_c=None, tank=False)
    assert not rules.observe(NOON, on=True, power_w=2900.0, **look)
    for minute in range(1, 15):
        assert not rules.observe(NOON + minute * MIN, on=True, power_w=5.0, **look)
    assert rules.observe(NOON + 16 * MIN, on=True, power_w=5.0, **look)
    assert rules.kept.last_full == NOON + 16 * MIN
    # Idle without having drawn is no full heat (a dead element reads idle too).
    rules = Rules()
    for minute in range(0, 40):
        assert not rules.observe(NOON + minute * MIN, on=True, power_w=5.0, **look)
    # Switched off before the thermostat was satisfied: it must draw again.
    rules = Rules()
    rules.observe(NOON, on=True, power_w=2900.0, **look)
    rules.observe(NOON + MIN, on=False, power_w=0.0, **look)
    for minute in range(2, 30):
        assert not rules.observe(NOON + minute * MIN, on=True, power_w=5.0, **look)
    # A reading between the two levels starts the idle stretch again.
    rules = Rules()
    rules.observe(NOON, on=True, power_w=2900.0, **look)
    rules.observe(NOON + MIN, on=True, power_w=5.0, **look)
    rules.observe(NOON + 10 * MIN, on=True, power_w=200.0, **look)
    assert not rules.observe(NOON + 17 * MIN, on=True, power_w=5.0, **look)
    assert rules.observe(NOON + 32 * MIN, on=True, power_w=5.0, **look)


def test_with_a_tank_sensor_full_heat_is_sixty_degrees_for_ten_minutes_and_a_cold_tank_heats_at_once() -> None:
    rules = Rules(Kept(last_full=NOON - H))
    look = dict(on=True, power_w=None, metered=False, tank=True)
    rules.observe(NOON, tank_c=61.0, **look)
    assert not rules.observe(NOON + 9 * MIN, tank_c=60.5, **look)
    assert rules.observe(NOON + 10 * MIN, tank_c=60.5, **look)
    rules.observe(NOON + 20 * MIN, tank_c=39.0, **look)
    want = rules.want(NOON + 20 * MIN, mode=MODE_TANK, windows=[], plan_fresh=True, day=DAY)
    assert (want.on, want.reason) == (True, "tank_low")
    rules.observe(NOON + 30 * MIN, tank_c=44.0, **look)
    assert rules.want(NOON + 30 * MIN, mode=MODE_TANK, windows=[], plan_fresh=True, day=DAY).reason == "tank_low"
    rules.observe(NOON + 40 * MIN, tank_c=45.0, **look)
    assert rules.want(NOON + 40 * MIN, mode=MODE_TANK, windows=[], plan_fresh=True, day=DAY).on is False


def test_the_mode_is_the_tank_sensor_else_the_meter_else_none() -> None:
    assert mode_of(True, True) == MODE_TANK and mode_of(True, False) == MODE_METERED and mode_of(False, False) == MODE_UNMETERED


# ------------------------------------------------------------------------------------------------ the hard rules


def _want(rules: Rules, now: datetime, mode: str = MODE_METERED, windows: list[Window] | None = None, fresh: bool = True):
    return rules.want(now, mode=mode, windows=windows or [], plan_fresh=fresh, day=now.astimezone(ZONE).date().isoformat())


def test_h1_holds_the_switch_on_from_twenty_hours_after_full_heat_whatever_the_price() -> None:
    rules = Rules(Kept(last_full=NOON - FULL_EVERY + MIN))
    assert _want(rules, NOON).on is False
    want = _want(rules, NOON + MIN)
    assert (want.on, want.reason) == (True, "full_heat")
    # Held until full heat is seen again, through any hour.
    assert _want(rules, NOON + 10 * H).reason == "full_heat"
    rules.kept.last_full = NOON + 10 * H
    assert _want(rules, NOON + 10 * H + MIN).on is False
    # None known (a first start): on until it is seen.
    assert _want(Rules(), NOON).reason == "full_heat"
    assert _want(Rules(Kept(last_full=NOON - FULL_EVERY + MIN)), NOON, mode=MODE_TANK).on is False


def test_h2_an_off_stretch_never_reaches_eight_hours_and_the_switch_stays_on_an_hour() -> None:
    rules = Rules(Kept(last_full=NOON, off_since=NOON))
    assert _want(rules, NOON + 7 * H + 54 * MIN).on is False
    want = _want(rules, NOON + 7 * H + 55 * MIN)
    assert (want.on, want.reason) == (True, "max_off") and want.until == NOON + 8 * H + 55 * MIN
    rules.switched(NOON + 7 * H + 55 * MIN, True, day=DAY, planned=False)
    assert _want(rules, NOON + 8 * H + 50 * MIN).on is True
    assert _want(rules, NOON + 8 * H + 56 * MIN).on is False
    # The off verdict says when it must end at the latest.
    assert _want(Rules(Kept(last_full=NOON, off_since=NOON)), NOON + H).until == NOON + MAX_OFF - 5 * MIN


def test_h3_on_without_a_fresh_plan_or_a_reading_for_ten_minutes() -> None:
    rules = Rules(Kept(last_full=NOON))
    assert _want(rules, NOON + H, fresh=False).reason == "no_plan"
    rules.observe(NOON + H, on=False, power_w=None, metered=True, tank_c=None, tank=False)
    assert _want(rules, NOON + H + 9 * MIN).on is False
    assert _want(rules, NOON + H + 10 * MIN).reason == "sensor_lost"
    rules.observe(NOON + H + 11 * MIN, on=True, power_w=3.0, metered=True, tank_c=None, tank=False)
    assert _want(rules, NOON + H + 11 * MIN).on is False
    tank = Rules(Kept(last_full=NOON))
    tank.observe(NOON, on=False, power_w=None, metered=False, tank_c=None, tank=True)
    assert _want(tank, NOON + 10 * MIN, mode=MODE_TANK).reason == "sensor_lost"


def test_inside_a_window_on_outside_off_and_the_day_s_switchings_capped() -> None:
    rules = Rules(Kept(last_full=NOON))
    window = Window(NOON + H, NOON + 2 * H, WINDOW_CHEAP, "day:x")
    assert _want(rules, NOON + 90 * MIN, windows=[window]).on is True
    off = _want(rules, NOON + 30 * MIN, windows=[window])
    assert (off.on, off.reason, off.until) == (False, "planned_off", NOON + H)
    rules.kept.switchings[DAY] = MAX_SWITCHINGS_DAY - 1
    assert _want(rules, NOON + 30 * MIN).reason == "write_cap"
    # Already off: the cap never keeps it off, and the next on is never capped.
    rules.kept.off_since = NOON
    assert _want(rules, NOON + 30 * MIN).on is False
    assert _want(rules, NOON + 90 * MIN, windows=[window]).on is True


def test_control_stops_with_one_note_when_full_heat_is_not_seen_for_two_days() -> None:
    rules = Rules(Kept(last_full=NOON - 3 * GIVE_UP_AFTER))
    rules.start(NOON)
    assert not rules.gave_up(NOON + GIVE_UP_AFTER - MIN, MODE_METERED)
    assert rules.gave_up(NOON + GIVE_UP_AFTER, MODE_METERED) and rules.kept.stopped
    # It clears itself on the next full heat.
    rules.observe(NOON + 50 * H, on=True, power_w=3000.0, metered=True, tank_c=None, tank=False)
    rules.observe(NOON + 51 * H, on=True, power_w=1.0, metered=True, tank_c=None, tank=False)
    assert rules.observe(NOON + 51 * H + 15 * MIN, on=True, power_w=1.0, metered=True, tank_c=None, tank=False)
    assert not rules.kept.stopped and not rules.gave_up(NOON + 52 * H, MODE_METERED)
    # Unmetered there is nothing to see: never.
    unmetered = Rules()
    unmetered.start(NOON)
    assert not unmetered.gave_up(NOON + 10 * GIVE_UP_AFTER, MODE_UNMETERED)


def test_the_kept_state_reads_back_and_a_broken_one_reads_empty() -> None:
    kept = Kept(NOON, NOON, NOON, NOON + H, True, False, {DAY: 3}, [Window(NOON, NOON + H, WINDOW_CHEAP, "day:x", 1.5)])
    assert Kept.from_dict(kept.as_dict()) == kept
    assert Kept.from_dict(None) == Kept() and Kept.from_dict({"windows": [{"start": 1}], "switchings": [1]}) == Kept()


# ------------------------------------------------------------------------------------------------ the windows


def test_the_need_and_the_windows_lengths() -> None:
    assert need_kwh([5.0, 6.0]) is None
    assert need_kwh([5.0, 6.0, 10.0, 4.0, 7.0]) == 7.0 * 1.2
    assert sizes(MODE_UNMETERED, [5.0] * 10, 3.0) == UNMETERED
    assert sizes(MODE_METERED, [5.0], 3.0) == Sizes(5, 3, 7, False)
    # 6 kWh * 1.2 on a 3 kW element: ceil(2.4) + 1 = 4 hours, 3 at night (by 05:00) and 1 by day.
    metered = sizes(MODE_METERED, [6.0] * 5, 3.0)
    assert (metered.night_hours, metered.day_hours, metered.night_end, metered.hard_night) == (3, 1, 5, False)
    assert round(metered.night_kwh + metered.day_kwh, 6) == 7.2
    assert sizes(MODE_METERED, [1.0] * 5, 3.0).night_hours + sizes(MODE_METERED, [1.0] * 5, 3.0).day_hours == 3
    assert element_kw([0.0, 2950.0, 3050.0, 3000.0, 10.0]) == 3.0 and element_kw([5.0]) is None
    hourly = {local(18, 0) + i * H: 0.25 for i in range(48)} | {local(20, 0): 0.5}
    assert daily_kwh(hourly, ZONE, NOON) == [6.0, 6.0]


def test_the_cheapest_block_avoids_tight_hours_where_one_fits() -> None:
    hours = [NOON + i * H for i in range(6)]
    prices = {hour: 100.0 for hour in hours} | {NOON + 2 * H: 10.0, NOON + 3 * H: 10.0}
    assert cheapest_block(hours, 2, prices) == [NOON + 2 * H, NOON + 3 * H]
    assert cheapest_block(hours, 2, prices, tight=[NOON + 3 * H]) != [NOON + 2 * H, NOON + 3 * H]
    assert cheapest_block(hours, 2, prices, tight=hours) == [NOON + 2 * H, NOON + 3 * H]
    assert cheapest_block(hours, 2, {}) is None and cheapest_block(hours, 9, prices) is None


def test_the_unmetered_night_window_is_the_cheapest_five_hours_between_ten_and_seven() -> None:
    prices = flat_prices(local(20, 0), 72, cheap={local(21, h): 10.0 for h in (2, 3, 4, 5, 6)})
    windows = plan_windows(NOON, ZONE, prices, UNMETERED)
    night = next(window for window in windows if window.key == "night:2026-11-21")
    assert (night.start, night.end, night.reason) == (local(21, 2), local(21, 7), WINDOW_FULL_HEAT)
    assert next(window for window in windows if window.key == "day:2026-11-20").end <= local(20, 17)
    # No prices for the night: still five hours, at 01:00-06:00.
    windows = plan_windows(NOON, ZONE, flat_prices(local(20, 0), 18), UNMETERED)
    night = next(window for window in windows if window.key == "night:2026-11-21")
    assert (night.start, night.end) == (local(21, 1), local(21, 6))
    # Too little of the span left: its full length from the running hour on.
    late = local(21, 4) + 10 * MIN
    windows = plan_windows(late, ZONE, prices, UNMETERED)
    night = next(window for window in windows if window.key == "night:2026-11-21")
    assert night.end - night.start == 5 * H and night.start == local(21, 4)


def test_the_metered_night_window_ends_by_five() -> None:
    size = sizes(MODE_METERED, [6.0] * 5, 3.0)
    prices = flat_prices(local(20, 0), 72, cheap={local(21, 6): 1.0})
    night = next(window for window in plan_windows(NOON, ZONE, prices, size) if window.key == "night:2026-11-21")
    assert night.end <= local(21, 5) and night.reason == WINDOW_CHEAP and night.kwh is not None


def test_a_started_window_is_never_shortened_or_planned_again() -> None:
    running = Window(local(21, 1), local(21, 6), WINDOW_FULL_HEAT, "night:2026-11-21")
    prices = flat_prices(local(20, 0), 72, cheap={local(21, h): 1.0 for h in (2, 3, 4, 5, 6)})
    windows = plan_windows(local(21, 2), ZONE, prices, UNMETERED, kept=[running])
    assert running in windows and [window.key for window in windows].count("night:2026-11-21") == 1
    # A window kept but not started yet is chosen again.
    later = Window(local(21, 1), local(21, 6), WINDOW_FULL_HEAT, "night:2026-11-21")
    assert later not in plan_windows(NOON, ZONE, prices, UNMETERED, kept=[later])


def test_off_gaps_are_filled_so_none_is_longer_than_seven_hours() -> None:
    windows = [Window(NOON, NOON + H, WINDOW_CHEAP, "a"), Window(NOON + 20 * H, NOON + 21 * H, WINDOW_CHEAP, "b")]
    prices = flat_prices(NOON, 30, cheap={NOON + 5 * H: 1.0})
    added = fill_gaps(windows, NOON, prices)
    assert all(window.reason == WINDOW_MAX_OFF and window.end - window.start == H for window in added)
    assert NOON + 5 * H in [window.start for window in added]
    edges = sorted([(w.start, w.end) for w in windows + added])
    assert all(b[0] - a[1] <= MAX_PLANNED_OFF for a, b in zip(edges, edges[1:]))
    # From an off stretch already running: the first one comes soon enough, never in the past.
    added = fill_gaps([Window(NOON + 10 * H, NOON + 11 * H, WINDOW_CHEAP, "c")], NOON - 6 * H, {}, not_before=NOON)
    assert NOON <= added[0].start and added[0].start - (NOON - 6 * H) <= MAX_PLANNED_OFF


def test_every_planned_day_keeps_its_gaps_within_seven_hours() -> None:
    prices = flat_prices(local(20, 0), 72)
    windows = plan_windows(NOON, ZONE, prices, UNMETERED)
    edges = sorted((w.start, w.end) for w in windows if w.end > NOON)
    gaps = [b[0] - max(a[1], NOON) for a, b in zip([(NOON, NOON)] + edges, edges)]
    assert max(gaps) <= MAX_PLANNED_OFF


def test_the_windows_energy_is_moved_into_them_from_the_rest_of_the_day() -> None:
    window = Window(local(21, 2), local(21, 4), WINDOW_CHEAP, "night:x", 4.0)
    delta = forecast_delta([window], NOON, ZONE)
    assert delta[local(21, 2)] == 2.0 and delta[local(21, 3)] == 2.0
    assert abs(sum(delta.values())) < 0.01
    assert forecast_delta([Window(NOON, NOON + H, WINDOW_CHEAP, "x")], NOON, ZONE) == {}


# ------------------------------------------------------------------------------------------------ the properties


class Tank:
    """A tank with a 65 °C thermostat (5 °C hysteresis) and a 3 kW element heating it 15 °C an hour."""

    def __init__(self, temp: float) -> None:
        self.temp = temp
        self.calling = temp < 60.0
        self.full_at: datetime | None = None
        #: The last moment the tank was at 60 °C or more.
        self.hot_at: datetime | None = None

    def run(self, start: datetime, seconds: float, switch_on: bool, loss_c_h: float) -> None:
        hours = seconds / 3600
        if self.temp < 60.0:
            self.calling = True
        heating = switch_on and self.calling
        self.temp += (15.0 if heating else 0.0) * hours - loss_c_h * hours
        self.temp = max(5.0, self.temp)
        if self.temp >= 65.0:
            self.temp, self.calling = 65.0, False
            self.full_at = start + timedelta(seconds=seconds)
        if self.temp >= 60.0:
            self.hot_at = start + timedelta(seconds=seconds)

    def power_w(self, switch_on: bool) -> float:
        return 3000.0 if switch_on and self.calling else 2.0


def simulate(mode: str, seed: int, outages: list[tuple[int, int, str]], days: int = 5) -> dict[str, object]:
    """Run the rules for `days` with looks 30 to 300 s apart, random prices and tight hours, the losses given
    (`(start minute, minutes, what)`, what being meter, tank or plan), replanning every hour, and a switch that
    always takes. Returns the longest off stretch, the full-heat gaps, the nights' longest on stretch."""
    rng = random.Random(seed)
    start = local(19, 12)
    end = start + days * 24 * H
    prices = {start - 48 * H + i * H: rng.uniform(5, 300) for i in range((days + 4) * 24)}
    tight = {hour for hour in prices if rng.random() < 0.1}
    metered = mode in (MODE_METERED, MODE_TANK) and mode == MODE_METERED
    tank_sensor = mode == MODE_TANK
    rules = Rules()
    tank = Tank(rng.uniform(20, 65))
    switch_on = True
    now = start
    windows: list[Window] = []
    planned_at: datetime | None = None
    off_start: datetime | None = None
    longest_off = timedelta(0)
    fulls: list[datetime] = [start]
    hots: list[datetime] = [start]
    on_start: datetime | None = start
    on_spans: list[tuple[datetime, datetime]] = []
    loss = rng.uniform(0.5, 3.0)
    rules.start(now)

    def lost(what: str) -> bool:
        minute = (now - start).total_seconds() / 60
        return any(kind == what and begin <= minute < begin + length for begin, length, kind in outages)

    while now < end:
        if not lost("plan") and (planned_at is None or now - planned_at >= H):
            size = sizes(mode, [6.0] * 5 if mode != MODE_UNMETERED else [], 3.0)
            windows = plan_windows(
                now, ZONE, prices, size, tight=tight, kept=[w for w in windows if w.start <= now], off_since=rules.kept.off_since
            )
            planned_at = now
        fresh = planned_at is not None and now - planned_at <= 2 * H
        rules.observe(
            now,
            on=switch_on,
            power_w=None if lost("meter") else tank.power_w(switch_on),
            metered=metered,
            tank_c=None if lost("tank") else tank.temp,
            tank=tank_sensor,
        )
        rules.gave_up(now, mode)
        want = rules.want(now, mode=mode, windows=windows, plan_fresh=fresh, day=now.astimezone(ZONE).date().isoformat())
        if want.on != switch_on:
            switch_on = want.on
            rules.switched(now, want.on, day=now.astimezone(ZONE).date().isoformat(), planned=(not want.on) or want.reason == WINDOW_CHEAP)
            if switch_on:
                on_start = now
                if off_start is not None:
                    longest_off = max(longest_off, now - off_start)
                    off_start = None
            else:
                off_start = now
                if on_start is not None:
                    on_spans.append((on_start, now))
                    on_start = None
        step = rng.uniform(30, 300)
        before = tank.full_at
        tank.run(now, step, switch_on, loss)
        if tank.full_at is not None and tank.full_at != before:
            fulls.append(tank.full_at)
        if tank.hot_at is not None and tank.hot_at != hots[-1]:
            hots.append(tank.hot_at)
        now += timedelta(seconds=step)
    if off_start is not None:
        longest_off = max(longest_off, now - off_start)
    if on_start is not None:
        on_spans.append((on_start, now))
    gaps = [b - a for a, b in zip(fulls, fulls[1:])] + [now - fulls[-1]]
    hot_gaps = [b - a for a, b in zip(hots, hots[1:])] + [now - hots[-1]]
    return {
        "longest_off": longest_off,
        "full_gap": max(gaps),
        "hot_gap": max(hot_gaps),
        "on_spans": on_spans,
        "start": start,
        "end": now,
    }


OUTAGES = st.lists(
    st.tuples(st.integers(0, 5 * 24 * 60), st.integers(1, 600), st.sampled_from(["meter", "tank", "plan"])), max_size=6
)


@settings(max_examples=150, deadline=None)
@given(seed=st.integers(0, 10**6), outages=OUTAGES, mode=st.sampled_from([MODE_METERED, MODE_TANK, MODE_UNMETERED]))
def test_no_prices_or_loss_ever_keep_the_switch_off_over_eight_hours(seed: int, outages, mode: str) -> None:
    result = simulate(mode, seed, outages)
    assert result["longest_off"] <= MAX_OFF, result["longest_off"]


@settings(max_examples=150, deadline=None)
@given(seed=st.integers(0, 10**6), outages=OUTAGES, mode=st.sampled_from([MODE_METERED, MODE_TANK]))
def test_where_full_heat_is_seen_the_tank_reaches_its_thermostat_within_every_24_hours(seed: int, outages, mode: str) -> None:
    """Metered: the thermostat itself is satisfied (the tank at its 65 °C) within every 24 hours. With a tank sensor,
    full heat is the tank at 60 °C: it is there within every 24 hours."""
    result = simulate(mode, seed, outages)
    gap = result["full_gap"] if mode == MODE_METERED else result["hot_gap"]
    assert gap <= 24 * H, gap


@settings(max_examples=100, deadline=None)
@given(seed=st.integers(0, 10**6), outages=OUTAGES)
def test_unmetered_every_night_has_five_hours_on_in_a_row(seed: int, outages) -> None:
    result = simulate(MODE_UNMETERED, seed, outages)
    spans = result["on_spans"]
    first = result["start"].astimezone(ZONE).date() + timedelta(days=1)
    last = result["end"].astimezone(ZONE).date()
    day = first
    while day < last:
        night_start, night_end = local(day.day - 1 if day.day > 1 else day.day, 22), local(day.day, 7)
        # The window lies in 22:00-07:00, or later where it started late; five hours on in a row that night (less the
        # looks' own spacing at its two ends, up to five minutes each here).
        assert any(b - a >= 5 * H - 10 * MIN and a < night_end + 5 * H and b > night_start for a, b in spans), day
        day += timedelta(days=1)
