"""Heat and hot water apart from the heat pump's meter (`heat/meter_split.py`), on a day shaped like the owner's
first night with his meter: idle ~0.15 kW, heating ~1.1 kW cycling, a compressor start at 1.7 kW, and hot-water
plateaus at ~3 kW of 10 to 20 minutes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.spotnav.heat.meter_split import (
    HOT_WATER_FACTOR,
    hot_water_runs,
    levels,
    split_day,
    state_now,
)

DAY = datetime(2026, 11, 15, 23, 0, tzinfo=timezone.utc)  # local midnight, Stockholm
FIVE = timedelta(minutes=5)


def villa_day(scale: float = 1.0) -> dict[datetime, float]:
    """288 five-minute means (kW): heating cycles of 35 min on, 15 min off, two morning showers and an evening run."""
    out: dict[datetime, float] = {}
    for index in range(288):
        start = DAY + index * FIVE
        out[start] = (1.12 if index % 10 < 7 else 0.15) * scale
    for hour, minute, values in ((5, 15, (1.86, 2.84, 2.97, 2.28)), (7, 25, (2.5, 2.77, 2.92, 1.91)), (19, 25, (2.69, 2.14))):
        first = DAY + timedelta(hours=hour, minutes=minute)
        for offset, kw in enumerate(values):
            out[first + offset * FIVE] = kw * scale
    out[DAY + timedelta(hours=4)] = 1.7 * scale  # a compressor start: one period
    return out


def test_the_runs_are_found_relative_to_the_heating_level() -> None:
    day = villa_day()
    found = levels(day)
    assert found is not None and abs(found.baseline_kw - 1.12) < 0.05
    runs = hot_water_runs(day, found)
    assert [(run.start - DAY, round(run.kwh, 2)) for run in runs] == [
        (timedelta(hours=5, minutes=15), 0.83),
        (timedelta(hours=7, minutes=25), 0.84),
        (timedelta(hours=19, minutes=25), 0.4),
    ]
    # The same day at twice the power (a bigger pump) finds the same runs: nothing is fixed watts.
    assert [run.start for run in hot_water_runs(villa_day(2.0), levels(villa_day(2.0)))] == [run.start for run in runs]


def test_the_day_split_and_its_edges() -> None:
    day = villa_day()
    split = split_day(day, DAY, DAY + timedelta(days=1))
    assert split.hot_water_kwh is not None and abs(split.hot_water_kwh - 2.07) < 0.01
    assert abs(split.heat_kwh + split.hot_water_kwh - split.total_kwh) < 1e-9
    # Too little seen: all is heat, and the split says it could not be made.
    early = dict(list(day.items())[:12])
    assert split_day(early, DAY, DAY + timedelta(days=1)).hot_water_kwh is None
    # A long high draw (a cold day's heating) is no hot-water run.
    cold = {start: 1.0 for start in day} | {DAY + timedelta(hours=10) + i * FIVE: 3.0 for i in range(30)}
    assert hot_water_runs(cold, levels(cold)) == []


def test_a_summer_day_counts_every_run_above_the_idle_draw() -> None:
    summer = {DAY + i * FIVE: 0.15 for i in range(288)} | {DAY + timedelta(hours=6) + i * FIVE: 2.9 for i in range(5)}
    found = levels(summer)
    assert found is not None and found.baseline_kw == 0.15
    assert len(hot_water_runs(summer, found)) == 1


def test_the_state_now() -> None:
    found = levels(villa_day())
    assert found is not None
    level = found.baseline_kw * HOT_WATER_FACTOR
    assert state_now(2.9, found, 2.8) == "hot_water"
    # A start spike over the hot-water level without the last period there is heating.
    assert state_now(level + 0.1, found, 1.1) == "heating"
    assert state_now(1.1, found, 1.1) == "heating"
    assert state_now(0.15, found, 0.15) == "idle"
    assert state_now(None, found) is None and state_now(1.0, None) is None
