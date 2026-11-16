"""The house's measured temperature (`heat/rooms.py`): the coldest counted room rules, a local heat source is left
out while it lasts, and a house that warms everywhere is no local source."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.spotnav.heat.rooms import SensorReading, indoor, rise

NOW = datetime(2026, 11, 16, 19, 0, tzinfo=timezone.utc)


def reading(entity_id: str, now_c: float | None, half_hour_ago: float | None, area: str | None = None) -> SensorReading:
    history = () if half_hour_ago is None else ((NOW - timedelta(minutes=40), half_hour_ago),)
    return SensorReading(entity_id, entity_id.split(".")[1].title(), now_c, history, area)


def test_the_coldest_room_rules() -> None:
    result = indoor([reading("sensor.kok", 21.3, 21.2), reading("sensor.norr", 20.1, 20.2), reading("sensor.uppe", 20.6, 20.6)], NOW)
    assert (result.value_c, result.coldest, result.coldest_name) == (20.1, "sensor.norr", "Norr")
    assert [sensor.status for sensor in result.sensors] == ["ok", "ok", "ok"]


def test_a_stove_room_that_warms_fast_is_left_out_while_the_others_stay_flat() -> None:
    stove = reading("sensor.vardagsrum", 20.0, 19.0)
    # The stove room is not the coldest here, but it would be after it cools; while it rises it is not counted.
    result = indoor([stove, reading("sensor.kok", 20.4, 20.4), reading("sensor.norr", 20.3, 20.4)], NOW)
    assert result.coldest == "sensor.norr"
    statuses = {sensor.entity_id: sensor.status for sensor in result.sensors}
    assert statuses["sensor.vardagsrum"] == "local_heat"
    # The coldest room is left out too when it is the one warming fast: the next coldest rules.
    warming = indoor([reading("sensor.hall", 19.5, 18.6), reading("sensor.kok", 20.4, 20.4), reading("sensor.norr", 20.3, 20.3)], NOW)
    assert warming.coldest == "sensor.norr"


def test_a_whole_house_that_warms_is_no_local_source() -> None:
    result = indoor([reading("sensor.a", 20.6, 19.9), reading("sensor.b", 20.8, 20.0)], NOW)
    assert result.coldest == "sensor.a" and all(sensor.status == "ok" for sensor in result.sensors)
    # One sensor alone can never be a local source.
    alone = indoor([reading("sensor.a", 21.0, 19.0)], NOW)
    assert alone.coldest == "sensor.a"


def test_the_room_of_a_running_air_heat_pump_is_left_out() -> None:
    result = indoor(
        [reading("sensor.uppe", 19.0, 19.0, area="linnas_rum"), reading("sensor.kok", 20.4, 20.4, area="kok")],
        NOW,
        heating_areas=frozenset({"linnas_rum"}),
    )
    assert result.coldest == "sensor.kok"
    assert result.sensors[0].status == "air_heat_pump"


def test_unreadable_and_implausible_sensors() -> None:
    result = indoor([reading("sensor.gone", None, 20.0), reading("sensor.glitch", 85.0, 20.0), reading("sensor.kok", 20.9, None)], NOW)
    assert result.coldest == "sensor.kok"
    assert [sensor.status for sensor in result.sensors] == ["unavailable", "unavailable", "ok"]
    assert result.sensors[2].rise_c is None
    assert indoor([], NOW).value_c is None


def test_the_rise_is_against_the_reading_in_force_at_the_windows_start() -> None:
    history = ((NOW - timedelta(hours=2), 18.0), (NOW - timedelta(minutes=35), 19.0), (NOW - timedelta(minutes=10), 19.6))
    assert rise(SensorReading("sensor.a", "A", 20.0, history), NOW) == 1.0
    # Only readings inside the window: the first of them.
    inside = ((NOW - timedelta(minutes=20), 19.5),)
    assert rise(SensorReading("sensor.a", "A", 20.0, inside), NOW) == 0.5
