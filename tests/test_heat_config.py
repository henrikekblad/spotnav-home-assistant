"""The heat card's settings (`heat/config.py`): the stored shape read leniently, every write judged strictly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.heat.config import (
    DEFAULT_TARGET_C,
    HeatConfig,
    Night,
    Zone,
    apply_changes,
    away_target,
    read_config,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
KNOWN = {
    "sensor.kitchen": "indoor",
    "sensor.upstairs": "indoor",
    "sensor.outside": "outdoor",
    "sensor.cpu": "other",
    "climate.floor": "zone",
    "climate.air": "zone",
    "switch.relay": "relay",
    "switch.relay_b": "relay",
    "number.offset": "offset",
    "switch.hot_water": "hot_water",
}


def check(entity_id: str, role: str) -> bool | None:
    kind = KNOWN.get(entity_id)
    if kind is None:
        return None
    if role in ("indoor", "outdoor"):
        return kind in ("indoor", "outdoor")
    return True


def write(changes: dict, current: HeatConfig | None = None):
    return apply_changes(current or HeatConfig(), changes, now=NOW, check=check)


def codes(errors) -> list[tuple[str, str]]:
    return [(error.field, error.code) for error in errors]


def test_a_missing_or_broken_store_reads_as_the_defaults() -> None:
    assert read_config(None) == HeatConfig()
    assert read_config({"target_c": 40, "mode": "away", "night": {"delta_c": 9, "start": "25:00"}}) == HeatConfig()
    stored = HeatConfig(
        target_c=21.5,
        mode="away",
        away_until="2026-11-20T16:00:00+01:00",
        night=Night(True, 1.5, "22:30", "06:00"),
        indoor_sensors=("sensor.kitchen", "sensor.upstairs"),
        outdoor_sensor="sensor.outside",
        control_path="sg_ready",
        control_entity="switch.relay",
        control_entity_b="switch.relay_b",
        hot_water_entity="switch.hot_water",
        zones=(Zone("climate.floor", 0.5), Zone("climate.air", -1.0)),
    )
    assert read_config(stored.as_dict()) == stored
    # Duplicates and wrong domains are dropped, a zone's broken offset reads 0.
    raw = stored.as_dict() | {
        "indoor_sensors": ["sensor.kitchen", "sensor.kitchen", "switch.x"],
        "zones": [{"entity_id": "climate.floor", "offset_c": 0.3}, {"entity_id": "climate.floor"}, {"entity_id": "x"}],
        "control_path": "relay",
    }
    read = read_config(raw)
    assert read.indoor_sensors == ("sensor.kitchen",)
    assert read.zones == (Zone("climate.floor", 0.0),)
    assert (read.control_entity, read.control_entity_b) == ("switch.relay", None)


def test_the_target_is_a_half_degree_from_16_to_25() -> None:
    assert write({"target_c": 21.5})[0].target_c == 21.5
    assert codes(write({"target_c": 25.5})[1]) == [("target_c", "out_of_range")]
    assert codes(write({"target_c": 21.3})[1]) == [("target_c", "not_a_step")]
    assert codes(write({"target_c": True})[1]) == [("target_c", "invalid_value")]
    assert away_target(21.0) == 18.0 and away_target(17.5) == 16.0


def test_away_needs_a_return_time_ahead_and_home_clears_it() -> None:
    until = (NOW + timedelta(days=3)).isoformat()
    away, errors = write({"mode": "away", "away_until": until})
    assert not errors and (away.mode, away.away_until) == ("away", until)
    assert away.away_end() == NOW + timedelta(days=3)
    assert codes(write({"mode": "away"})[1]) == [("away_until", "away_until_required")]
    assert codes(write({"mode": "away", "away_until": (NOW - timedelta(minutes=1)).isoformat()})[1]) == [
        ("away_until", "away_in_past")
    ]
    assert codes(write({"mode": "away", "away_until": (NOW + timedelta(days=91)).isoformat()})[1]) == [
        ("away_until", "away_too_far")
    ]
    assert codes(write({"mode": "away", "away_until": "2026-11-20T16:00"})[1]) == [("away_until", "invalid_value")]
    # Moving the return time alone keeps the mode; home clears it.
    later = (NOW + timedelta(days=5)).isoformat()
    assert write({"away_until": later}, away)[0].away_until == later
    home, errors = write({"mode": "home"}, away)
    assert not errors and (home.mode, home.away_until) == ("home", None)


def test_the_cooler_night() -> None:
    night, errors = write({"night": {"on": True, "delta_c": 2.0}})
    assert not errors and night.night == Night(True, 2.0, "23:00", "06:00")
    assert night.night.covers(23 * 60) and night.night.covers(5 * 60 + 59) and not night.night.covers(6 * 60)
    assert codes(write({"night": {"delta_c": 3.5}})[1]) == [("night", "out_of_range")]
    assert codes(write({"night": {"start": "6:00"}})[1]) == [("night", "invalid_value")]
    assert codes(write({"night": {"start": "06:00", "end": "06:00"}})[1]) == [("night", "invalid_value")]
    assert codes(write({"night": {"on": True, "extra": 1}})[1]) == [("night", "invalid_value")]
    assert write({"night": None}, night)[0].night == Night()


def test_sensors_must_be_temperatures_home_assistant_knows() -> None:
    config, errors = write({"indoor_sensors": ["sensor.kitchen", "sensor.upstairs"], "outdoor_sensor": "sensor.outside"})
    assert not errors and config.indoor_sensors == ("sensor.kitchen", "sensor.upstairs")
    assert codes(write({"indoor_sensors": ["sensor.cpu"]})[1]) == [("indoor_sensors", "wrong_kind")]
    assert codes(write({"indoor_sensors": ["sensor.gone"]})[1]) == [("indoor_sensors", "entity_not_found")]
    assert codes(write({"indoor_sensors": ["switch.relay"]})[1]) == [("indoor_sensors", "wrong_kind")]
    assert codes(write({"indoor_sensors": ["sensor.kitchen"] * 2})[1]) == [("indoor_sensors", "duplicate")]
    assert codes(write({"indoor_sensors": [f"sensor.s{i}" for i in range(9)]})[1]) == [("indoor_sensors", "too_many")]
    assert write({"outdoor_sensor": None}, config)[0].outdoor_sensor is None


def test_the_control_path_and_its_entities() -> None:
    relay, errors = write({"control_path": "relay", "control_entity": "switch.relay"})
    assert not errors and (relay.control_path, relay.control_entity, relay.control_entity_b) == ("relay", "switch.relay", None)
    sg, errors = write({"control_path": "sg_ready", "control_entity_b": "switch.relay_b"}, relay)
    # A new path drops the old entity unless it is named again.
    assert not errors and (sg.control_entity, sg.control_entity_b) == (None, "switch.relay_b")
    assert codes(write({"control_path": "offset", "control_entity": "switch.relay"})[1]) == [("control_entity", "wrong_kind")]
    assert codes(write({"control_path": "sg_ready", "control_entity": "switch.relay", "control_entity_b": "switch.relay"})[1]) == [
        ("control_entity_b", "duplicate")
    ]
    none, errors = write({"control_path": "none"}, relay)
    assert not errors and (none.control_path, none.control_entity) == ("none", None)
    assert codes(write({"control_path": "boost"})[1]) == [("control_path", "invalid_value")]
    assert write({"hot_water_entity": "switch.hot_water"})[0].hot_water_entity == "switch.hot_water"
    assert codes(write({"hot_water_entity": "sensor.kitchen"})[1]) == [("hot_water_entity", "wrong_kind")]


def test_zones_with_offsets() -> None:
    zones, errors = write({"zones": [{"entity_id": "climate.floor", "offset_c": 0.5}, {"entity_id": "climate.air"}]})
    assert not errors and zones.zones == (Zone("climate.floor", 0.5), Zone("climate.air", 0.0))
    assert codes(write({"zones": [{"entity_id": "climate.floor", "offset_c": 3.5}]})[1]) == [("zones", "out_of_range")]
    assert codes(write({"zones": [{"entity_id": "climate.floor", "offset_c": 0.25}]})[1]) == [("zones", "not_a_step")]
    assert codes(write({"zones": [{"entity_id": "climate.floor"}, {"entity_id": "climate.floor"}]})[1]) == [
        ("zones", "duplicate")
    ]
    assert codes(write({"zones": [{"entity_id": "climate.gone"}]})[1]) == [("zones", "entity_not_found")]
    assert codes(write({"zones": [{"entity_id": "climate.floor", "power": 1}]})[1]) == [("zones", "invalid_value")]


def test_nothing_applies_while_any_field_fails() -> None:
    current = HeatConfig(target_c=20.0)
    updated, errors = write({"target_c": 22.0, "zones": "climate.floor"}, current)
    assert updated is current and codes(errors) == [("zones", "invalid_value")]
    assert codes(write({"colour": "red"})[1]) == [("colour", "unknown_field")]


@pytest.mark.parametrize("target", [16.0, 25.0, DEFAULT_TARGET_C])
def test_the_ends_of_the_dial(target: float) -> None:
    assert write({"target_c": target})[0].target_c == target
