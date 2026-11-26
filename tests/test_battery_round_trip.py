"""A home battery's round trip learnt from its own meters (`battery/round_trip.py`)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.plan_model import DEFAULT_ROUND_TRIP_EFFICIENCY
from custom_components.spotnav.battery.plan_shadow import BUCKET, Bucket
from custom_components.spotnav.battery.round_trip import (
    MIN_WINDOWS,
    PLAN_CAP,
    REJECT_DISAGREE,
    REJECT_IMPLAUSIBLE,
    SOURCE_COUNTERS,
    SOURCE_METERS,
    RoundTrip,
    _raw_counts,
)

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


class Battery:
    """A battery cycled once a day between 10 and 90 % (4 h charging, 4 h discharging, idle between) whose meters
    count at its terminals; `efficiency` is its round trip."""

    def __init__(self, efficiency: float, capacity: float = 20.0, kw: float = 4.0) -> None:
        self.root = efficiency**0.5
        self.capacity = capacity
        self.kw = kw
        self.soc = 10.0
        self.charged = 1000.0
        self.discharged = 900.0
        self.buckets: dict[datetime, Bucket] = {}

    def _action(self, at: datetime) -> float:
        hour = at.hour
        if 1 <= hour < 9 and self.soc < 90.0:
            return self.kw
        if 13 <= hour < 21 and self.soc > 10.0:
            return -self.kw
        return 0.0

    def quarter(self, start: datetime) -> Bucket:
        kw = self._action(start)
        kwh = kw * 0.25
        if kwh > 0:
            stored = min(kwh * self.root, (90.0 - self.soc) / 100.0 * self.capacity)
            kwh = stored / self.root
            self.charged += kwh
        elif kwh < 0:
            stored = max(kwh / self.root, -(self.soc - 10.0) / 100.0 * self.capacity)
            kwh = stored * self.root
            self.discharged -= kwh
        else:
            stored = 0.0
        bucket = Bucket(start=start, battery_kwh=kwh, covered_s=900.0, soc_start=self.soc)
        self.soc += stored / self.capacity * 100.0
        bucket.soc_end = self.soc
        self.buckets[start] = bucket
        return bucket


def _run(
    learner: RoundTrip,
    battery: Battery,
    days: float,
    *,
    start: datetime = T0,
    counters: bool = True,
    counter_scale: float = 1.0,
    reset_at: datetime | None = None,
    skip: set[datetime] | None = None,
    stuck: tuple[float, float] | None = None,
    soc_step: float | None = None,
    partial: set[datetime] | None = None,
) -> None:
    """Every quarter hour over `days`: the battery runs it, then the learner looks at its end."""
    moment = start
    end = start + timedelta(days=days)

    def look() -> None:
        soc = battery.soc if soc_step is None else round(battery.soc / soc_step) * soc_step
        read = stuck if stuck is not None else _counters(battery, counters, counter_scale)
        learner.observe(moment, soc, read, battery.buckets.values())

    look()
    while moment < end:
        bucket = battery.quarter(moment)
        moment += BUCKET
        if skip and bucket.start in skip:
            del battery.buckets[bucket.start]
        if partial and bucket.start in partial:
            bucket.covered_s = 300.0
        if reset_at is not None and moment == reset_at:
            battery.charged, battery.discharged = 0.0, 0.0
        look()


def _counters(battery: Battery, counters: bool, scale: float) -> tuple[float | None, float | None]:
    if not counters:
        return None, None
    return battery.charged * scale, battery.discharged


def test_a_clean_cycle_from_the_counters_teaches_the_round_trip() -> None:
    learner = RoundTrip()

    _run(learner, Battery(0.84), days=12)

    assert len(learner.windows) >= MIN_WINDOWS
    assert all(window.source == SOURCE_COUNTERS for window in learner.windows)
    assert all(window.charged_kwh >= 20.0 for window in learner.windows)
    assert all(window.efficiency == pytest.approx(0.84, abs=0.02) for window in learner.windows)
    assert learner.efficiency == pytest.approx(0.84, abs=0.01)
    assert not learner.rejected


def test_without_counters_the_measured_power_teaches_it() -> None:
    learner = RoundTrip()

    _run(learner, Battery(0.88), days=12, counters=False)

    assert learner.windows and all(window.source == SOURCE_METERS for window in learner.windows)
    assert learner.efficiency == pytest.approx(0.88, abs=0.01)


def test_too_few_windows_leave_it_assumed() -> None:
    learner = RoundTrip()

    _run(learner, Battery(0.84), days=3)

    assert 0 < len(learner.windows) < MIN_WINDOWS
    assert learner.efficiency is None


def test_a_counter_reset_spoils_only_the_window_it_falls_in() -> None:
    learner = RoundTrip()

    _run(learner, Battery(0.84), days=12, reset_at=T0 + timedelta(days=4, hours=3))

    assert all(0.6 <= window.efficiency <= 0.98 for window in learner.windows)
    assert learner.counter_break == T0 + timedelta(days=4, hours=3)
    # The window open across the reset falls back to the measured power; later ones use the counters again.
    sources = [window.source for window in learner.windows]
    assert SOURCE_METERS in sources and sources[-1] == SOURCE_COUNTERS
    assert learner.efficiency == pytest.approx(0.84, abs=0.01)


def test_a_reset_without_measured_power_teaches_nothing_across_it() -> None:
    learner = RoundTrip()
    all_quarters = {T0 + BUCKET * i for i in range(12 * 96)}

    _run(learner, Battery(0.84), days=12, reset_at=T0 + timedelta(days=4, hours=3), skip=all_quarters)

    assert learner.windows
    assert all(window.source == SOURCE_COUNTERS for window in learner.windows)
    assert all(window.start >= T0 + timedelta(days=4, hours=3) or window.end <= T0 + timedelta(days=4, hours=3)
               for window in learner.windows)
    assert learner.efficiency == pytest.approx(0.84, abs=0.01)


def test_a_counter_that_counts_only_part_of_the_charge_is_thrown_away() -> None:
    """A hybrid whose charge counter misses what the panels put in (only the grid's share is counted): it disagrees
    with the measured power and teaches nothing."""
    learner = RoundTrip()

    _run(learner, Battery(0.84), days=12, counter_scale=0.6)

    assert learner.windows == []
    assert learner.rejected and all(window.rejected == REJECT_DISAGREE for window in learner.rejected)
    assert learner.efficiency is None


def test_an_unbelievable_round_trip_is_thrown_away() -> None:
    learner = RoundTrip()

    _run(learner, Battery(0.99), days=12)

    assert learner.windows == []
    assert all(window.rejected == REJECT_IMPLAUSIBLE for window in learner.rejected)
    assert learner.efficiency is None

    low = RoundTrip()
    _run(low, Battery(0.5), days=12, counters=False)
    assert low.efficiency is None


def test_the_value_moves_slowly_and_only_past_the_deadband() -> None:
    learner = RoundTrip()
    battery = Battery(0.92)
    _run(learner, battery, days=12)
    first = learner.efficiency
    assert first == pytest.approx(0.92, abs=0.01)

    # The battery ages to 0.80: the plan's value steps down at most 0.03 a window.
    battery.root = 0.80**0.5
    seen = [first]
    moment = T0 + timedelta(days=12)
    for _ in range(30):
        _run(learner, battery, days=1, start=moment)
        moment += timedelta(days=1)
        seen.append(learner.efficiency)
    steps = [abs(b - a) for a, b in zip(seen, seen[1:]) if a is not None and b is not None]
    assert max(steps) <= 0.03 + 1e-9
    assert learner.efficiency == pytest.approx(0.80, abs=0.02)

    # A window or two a point off moves nothing.
    steady = learner.efficiency
    battery.root = 0.81**0.5
    _run(learner, battery, days=3, start=moment)
    assert learner.efficiency == steady


def test_it_survives_a_restart_with_its_dated_windows() -> None:
    learner = RoundTrip()
    _run(learner, Battery(0.84), days=12)

    restored = RoundTrip.from_dict(learner.as_dict())

    assert restored.as_dict() == learner.as_dict()
    series = restored.series()
    assert len(series) == len(learner.windows)
    assert series[0]["date"] == learner.windows[0].end.isoformat()
    assert restored.view()["windows"][0]["source"] == SOURCE_COUNTERS


@pytest.mark.parametrize("raw", [None, [], "x", {"windows": "x", "efficiency": 3.0, "anchor": {"at": "nope"}}])
def test_a_broken_record_starts_empty(raw: object) -> None:
    learner = RoundTrip.from_dict(raw)
    assert learner.efficiency is None and learner.windows == [] and learner.anchor is None


def test_the_assumed_value_is_still_ninety() -> None:
    assert DEFAULT_ROUND_TRIP_EFFICIENCY == 0.90


def test_a_level_passed_inside_a_quarter_hour_ends_the_window_where_it_was_passed() -> None:
    battery = Battery(0.84)
    moment = T0
    for _ in range(10):  # start mid-charge, at a level no later quarter hour's end lands on
        battery.quarter(moment)
        moment += BUCKET
    learner = RoundTrip()

    _run(learner, battery, days=12, start=moment)

    assert any(window.end.minute % 15 or window.end.second for window in learner.windows)
    assert all(window.efficiency == pytest.approx(0.84, abs=0.005) for window in learner.windows)


def test_a_stored_point_or_window_without_an_offset_is_not_taken() -> None:
    """The pre-release review (2026-11-22): a store edited by hand must never hand a naive datetime to code that
    compares it with an aware one."""
    from custom_components.spotnav.battery.round_trip import Point, Window

    point = {"at": "2026-11-01T00:00:00", "soc": 50, "meter_in": 1.0, "meter_out": 1.0}
    assert Point.from_dict(point) is None
    assert Point.from_dict({**point, "at": "2026-11-01T00:00:00+00:00"}) is not None
    window = {"start": "2026-11-01T00:00:00+00:00", "end": "2026-11-01T08:00:00", "charged_kwh": 10.0,
              "discharged_kwh": 9.0, "efficiency": 0.9, "source": SOURCE_METERS}
    assert Window.from_dict(window) is None
    assert Window.from_dict({**window, "start": "2026-11-01T00:00:00"}) is None
    assert Window.from_dict({**window, "end": "2026-11-01T08:00:00+00:00"}) is not None


def test_a_round_trip_above_the_cap_is_kept_raw_but_the_plan_uses_the_cap() -> None:
    """Counters at the battery miss the inverter's loss: a 97 % DC round trip is taken as a window and shown raw in
    the diagnostics, but the plan never uses more than `PLAN_CAP`."""
    learner = RoundTrip()

    _run(learner, Battery(0.97), days=12)

    assert learner.windows and not learner.rejected
    assert all(window.efficiency == pytest.approx(0.97, abs=0.01) for window in learner.windows)
    assert learner.efficiency == PLAN_CAP == 0.95
    view = learner.view()
    assert view["median"] == pytest.approx(0.97, abs=0.01)
    assert view["capped"] is True and view["cap"] == PLAN_CAP


def test_a_round_trip_under_the_cap_is_not_marked_capped() -> None:
    learner = RoundTrip()
    _run(learner, Battery(0.84), days=12)
    assert learner.view()["capped"] is False


def test_a_value_stored_above_the_cap_is_brought_under_it() -> None:
    learner = RoundTrip.from_dict({"efficiency": 0.97})
    assert learner.efficiency == PLAN_CAP
    assert RoundTrip.from_dict({"efficiency": 0.91}).efficiency == 0.91


class Plant(Battery):
    """A plant whose meters read on the AC side: 78 % AC to AC, a small standby drain while idle (counted as the
    battery's discharge), and a level shown in whole points."""

    STANDBY_KW = 0.03

    def _action(self, at: datetime) -> float:
        kw = super()._action(at)
        return kw if kw or self.soc <= 10.5 else -self.STANDBY_KW


def test_counters_stuck_at_zero_learn_from_the_measured_power() -> None:
    """A BMS whose history counters read 0 (field report: Victron with a Seplos BMS, 78 % AC to AC measured)."""
    learner = RoundTrip()

    _run(learner, Plant(0.78), days=14, stuck=(0.0, 0.0), soc_step=1.0)

    assert len(learner.windows) >= MIN_WINDOWS
    assert all(window.source == SOURCE_METERS for window in learner.windows)
    assert not learner.rejected
    assert learner.efficiency == pytest.approx(0.78, abs=0.015)
    view = learner.view()
    assert view["source"] == "power" and view["counters"] == "stuck"


def test_counters_that_never_move_from_a_value_are_stuck_too() -> None:
    learner = RoundTrip()

    _run(learner, Plant(0.78), days=14, stuck=(1234.5, 1100.0))

    assert learner.windows and all(window.source == SOURCE_METERS for window in learner.windows)
    assert learner.efficiency == pytest.approx(0.78, abs=0.015)
    assert learner.counters_stuck


def test_one_stuck_counter_falls_back_to_the_measured_power() -> None:
    learner = RoundTrip()
    battery = Plant(0.80)
    moment = T0
    learner.observe(moment, battery.soc, (battery.charged, 0.0), battery.buckets.values())
    while moment < T0 + timedelta(days=14):
        battery.quarter(moment)
        moment += BUCKET
        learner.observe(moment, battery.soc, (battery.charged, 0.0), battery.buckets.values())

    assert learner.windows and all(window.source == SOURCE_METERS for window in learner.windows)
    assert learner.efficiency == pytest.approx(0.80, abs=0.015)


def test_working_counters_keep_priority_and_say_so() -> None:
    learner = RoundTrip()

    _run(learner, Battery(0.84), days=12)

    assert all(window.source == SOURCE_COUNTERS for window in learner.windows)
    assert not learner.counters_stuck
    view = learner.view()
    assert view["source"] == "counters" and view["counters"] == "working"
    assert RoundTrip().view()["counters"] == "missing" and RoundTrip().view()["source"] is None


def test_counters_that_start_counting_again_are_used_again() -> None:
    learner = RoundTrip()
    battery = Battery(0.84)

    _run(learner, battery, days=8, stuck=(0.0, 0.0))
    assert learner.counters_stuck
    _run(learner, battery, days=8, start=T0 + timedelta(days=8))

    assert learner.windows[-1].source == SOURCE_COUNTERS and not learner.counters_stuck
    assert learner.efficiency == pytest.approx(0.84, abs=0.01)


def test_stuck_counters_with_gaps_in_the_measured_power_teach_nothing_across_them() -> None:
    """A quarter hour not fully measured, or missing, spoils the window it falls in: no window spans one."""
    learner = RoundTrip()
    gaps = {T0 + timedelta(days=day, hours=3) for day in range(14)}

    _run(learner, Plant(0.78), days=14, stuck=(0.0, 0.0), partial=gaps)

    assert learner.windows == []
    assert learner.efficiency is None

    missing = RoundTrip()
    _run(missing, Plant(0.78), days=14, stuck=(0.0, 0.0), skip=gaps)
    assert missing.windows == [] and missing.efficiency is None


def test_stuck_counters_keep_the_window_rules() -> None:
    """Equal level at both ends, enough energy in, a believable value, the slow median."""
    learner = RoundTrip()

    _run(learner, Plant(0.78), days=14, stuck=(0.0, 0.0), soc_step=1.0)

    assert all(window.charged_kwh >= 20.0 for window in learner.windows)
    assert all(window.efficiency == pytest.approx(0.78, abs=0.03) for window in learner.windows)

    inverted = RoundTrip()  # the power's sign the wrong way round: out over in is far above any battery's
    battery = Plant(0.78)
    moment = T0
    inverted.observe(moment, battery.soc, (0.0, 0.0), battery.buckets.values())
    while moment < T0 + timedelta(days=14):
        bucket = battery.quarter(moment)
        bucket.battery_kwh = -bucket.battery_kwh
        moment += BUCKET
        inverted.observe(moment, battery.soc, (0.0, 0.0), battery.buckets.values())
    assert inverted.windows == [] and inverted.efficiency is None
    assert inverted.rejected and all(window.rejected == REJECT_IMPLAUSIBLE for window in inverted.rejected)


def test_a_counter_counting_only_part_is_not_taken_as_stuck() -> None:
    learner = RoundTrip()

    _run(learner, Battery(0.84), days=12, counter_scale=0.6)

    assert not learner.counters_stuck and learner.windows == []


def test_the_stuck_flag_survives_a_restart() -> None:
    learner = RoundTrip()
    _run(learner, Plant(0.78), days=14, stuck=(0.0, 0.0))

    restored = RoundTrip.from_dict(learner.as_dict())

    assert restored.counters_stuck and restored.as_dict() == learner.as_dict()
    assert _raw_counts(restored.anchor, restored.anchor) == (0.0, 0.0)  # type: ignore[arg-type]
