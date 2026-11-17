"""The heat card's indoor curve (`heat/indoor_day.py`): the rooms on the house's scale per step up to now, the comfort
band around the target, and the setpoint where the history hands it over."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.spotnav.heat.config import HeatConfig, SensorOffset
from custom_components.spotnav.heat.indoor_day import INDOOR_DAY_KEYS, indoor_day, means_by_start, points

START = datetime(2026, 11, 15, 23, tzinfo=timezone.utc)
END = START + timedelta(days=1)
CONFIG = HeatConfig(
    target_c=21.0,
    indoor_sensors=("sensor.kok", "sensor.uppe"),
    indoor_offsets=(SensorOffset("sensor.uppe", -1.0),),
)


def five(values: dict[int, float]) -> dict[datetime, float]:
    """5-minute means by the minute after `START`."""
    return {START + timedelta(minutes=minute): value for minute, value in values.items()}


def test_rooms_count_less_their_offset_mean_and_coldest_per_step() -> None:
    readings = {
        "sensor.kok": five({0: 21.2, 5: 21.4, 10: 21.3, 15: 21.0}),
        "sensor.uppe": five({0: 20.0, 5: 20.0, 10: 20.0, 15: 19.6}),
    }
    now = START + timedelta(minutes=20)
    out = points(readings, {"sensor.uppe": -1.0}, START, END, now, 15)
    # Upstairs aims a degree lower: its 20.0 is 21.0 on the house's scale.
    assert out == [
        {"start": START.isoformat(), "mean_c": 21.15, "coldest_c": 21.0, "coldest": "sensor.uppe"},
        {"start": (START + timedelta(minutes=15)).isoformat(), "mean_c": 20.8, "coldest_c": 20.6, "coldest": "sensor.uppe"},
    ]


def test_nothing_after_now_nor_an_implausible_reading_and_empty_steps_are_left_out() -> None:
    readings = {"sensor.kok": five({0: 21.0, 30: 3.0, 60: 21.5, 120: 22.0})}
    out = points(readings, {}, START, END, START + timedelta(minutes=90), 15)
    assert [point["start"] for point in out] == [START.isoformat(), (START + timedelta(hours=1)).isoformat()]


def test_the_block_has_the_band_and_no_block_without_sensors() -> None:
    block = indoor_day(CONFIG, {"sensor.kok": five({0: 21.0})}, START, END, START + timedelta(hours=1), 15)
    assert block is not None and tuple(block) == INDOOR_DAY_KEYS
    assert (block["low_c"], block["high_c"], block["step_min"], block["setpoint"]) == (20.5, 21.5, 15, None)
    assert block["points"] == [{"start": START.isoformat(), "mean_c": 21.0, "coldest_c": 21.0, "coldest": "sensor.kok"}]
    # The rooms are named for the chart; a sensor without a name goes by its entity id.
    assert block["rooms"] == [{"entity_id": "sensor.kok", "name": "sensor.kok"}, {"entity_id": "sensor.uppe", "name": "sensor.uppe"}]
    named = indoor_day(CONFIG, {}, START, END, START, 15, names={"sensor.kok": "Kök"})
    assert named is not None and named["rooms"][0] == {"entity_id": "sensor.kok", "name": "Kök"}
    hours = [(START, 21.0, "normal"), (START + timedelta(hours=1), 20.5, "lower")]
    with_setpoint = indoor_day(CONFIG, {}, START, END, START, 60, hours)
    assert with_setpoint is not None and with_setpoint["points"] == []
    assert with_setpoint["setpoint"] == [
        {"start": START.isoformat(), "target_c": 21.0, "action": "normal"},
        {"start": (START + timedelta(hours=1)).isoformat(), "target_c": 20.5, "action": "lower"},
    ]
    assert indoor_day(HeatConfig(), {}, START, END, START, 15) is None


def test_statistics_rows_are_read_by_their_start() -> None:
    rows = [{"start": START.timestamp(), "mean": 21.2}, {"start": START + timedelta(minutes=5), "mean": 21.3}, {"start": 1.0}]
    assert means_by_start(rows) == {START: 21.2, START + timedelta(minutes=5): 21.3}
