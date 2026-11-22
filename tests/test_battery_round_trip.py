"""A home battery's round trip learnt from its own meters (`battery/round_trip.py`)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.plan_model import DEFAULT_ROUND_TRIP_EFFICIENCY
from custom_components.spotnav.battery.plan_shadow import BUCKET, Bucket
from custom_components.spotnav.battery.round_trip import (
    MIN_WINDOWS,
    REJECT_DISAGREE,
    REJECT_IMPLAUSIBLE,
    SOURCE_COUNTERS,
    SOURCE_METERS,
    RoundTrip,
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
) -> None:
    """Every quarter hour over `days`: the battery runs it, then the learner looks at its end."""
    moment = start
    end = start + timedelta(days=days)
    learner.observe(moment, battery.soc, _counters(battery, counters, counter_scale), battery.buckets.values())
    while moment < end:
        bucket = battery.quarter(moment)
        moment += BUCKET
        if skip and bucket.start in skip:
            del battery.buckets[bucket.start]
        if reset_at is not None and moment == reset_at:
            battery.charged, battery.discharged = 0.0, 0.0
        learner.observe(moment, battery.soc, _counters(battery, counters, counter_scale), battery.buckets.values())


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
