"""A state-of-charge value Home Assistant only set again at its start keeps the reading it repeats.

The field night of 13-14 October: a Kia EV6's cloud last reported 43 % in the evening. At 22:50 UTC Home Assistant
restarted and the car's integration wrote 43 % again, stamped with the start. SpotNav took a reading's age from
`last_updated`, so the hours-old 43 % read as fresh, became a new anchor with a new register baseline, and the
evening's charge was forgotten: the estimate said 86 % when the car was full.

Now:

* the first state SpotNav sees of the car's sensor after it starts, with the stored anchor's value, is that
  anchor's reading: its time and baseline stay, and energy delivered before and after the restart counts;
* a value that moved, or a car report time newer than the anchor, is a new reading;
* a car integration's own report-time sensor on the same device (Kia/Hyundai `last_updated_at`, Renault
  `battery_last_activity`, ...) times the reading when there is one;
* a register that falls back across the restart (a per-session register) carries what it counted, never more;
* the wait for the car's level after it ended a plan's charge is not lifted by the restart's re-stamped value.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.execution.target_stop import resolve_soc_reading, SocReading
from custom_components.spotnav.vehicles.report_time import report_time_entity_id
from custom_components.spotnav.vehicles.soc_estimate import (
    CHARGE_EFFICIENCY,
    resolve_soc,
    SocAnchor,
    SocReader,
    track_register,
)

from .helpers import add_percent_battery_sensor
from .relay import FakeScheduler
from .test_car_ended_waits_for_level import _ended, _next_window, TARGET

CAPACITY = 77.4
READ_AT = datetime(2026, 10, 13, 17, 5, tzinfo=timezone.utc)
RESTART = datetime(2026, 10, 13, 22, 50, tzinfo=timezone.utc)
REGISTER = "sensor.halo_energy_active_import_register"


def _expected(soc: float, kwh: float) -> float:
    return soc + kwh * CHARGE_EFFICIENCY / CAPACITY * 100.0


def _reading(value: float, age_s: float, **fields: Any) -> SocReading:
    return SocReading(value, "vehicle", "sensor.ev6_battery", "car", age_s, **fields)


def _resolve(reading: SocReading | None, anchor: SocAnchor | None, register: float | None, now: datetime, **kw: Any):
    return resolve_soc(
        reading=reading, anchor=anchor, register_kwh=register, capacity_kwh=CAPACITY, now=now, vehicle_id="car",
        register_entity_id=REGISTER, **kw,
    )


# ------------------------------------------------------------------------------------------------- the rule


def test_a_restart_with_the_unchanged_value_keeps_the_anchor_and_counts_energy_before_and_after() -> None:
    anchor = SocAnchor(43.0, 6800.0, READ_AT, "vehicle", "car", REGISTER)
    # 20 kWh delivered before the restart; the car's 43 % is written again at 22:50 (its last_updated now).
    restarted = _resolve(_reading(43.0, 2.0), anchor, 6820.0, RESTART + timedelta(seconds=2), since_restart=True)
    assert restarted.anchor == anchor, "the stored reading's time and baseline stay"
    assert restarted.reading is not None and restarted.reading.estimated
    assert restarted.reading.soc_percent == pytest.approx(_expected(43.0, 20.0))
    assert restarted.reading.age_s == pytest.approx((RESTART - READ_AT).total_seconds() + 2.0)
    # 15 kWh more after the restart: both count.
    later = RESTART + timedelta(hours=2)
    after = _resolve(_reading(43.0, 7200.0), restarted.anchor, 6835.0, later, since_restart=True)
    assert after.reading is not None and after.reading.soc_percent == pytest.approx(_expected(43.0, 35.0))


def test_a_restored_value_before_the_register_is_readable_is_not_fresh() -> None:
    anchor = SocAnchor(43.0, 6800.0, READ_AT, "vehicle", "car", REGISTER)
    restarted = _resolve(_reading(43.0, 1.0), anchor, None, RESTART + timedelta(seconds=1), since_restart=True)
    assert restarted.anchor == anchor
    assert restarted.reading is not None and restarted.reading.soc_percent == 43.0
    assert restarted.reading.time_basis == "anchor"
    assert restarted.reading.age_s == pytest.approx((RESTART - READ_AT).total_seconds() + 1.0)


def test_a_changed_value_after_a_restart_is_a_new_reading() -> None:
    anchor = SocAnchor(43.0, 6800.0, READ_AT, "vehicle", "car", REGISTER)
    moved = _resolve(_reading(97.0, 2.0), anchor, 6835.0, RESTART + timedelta(seconds=2), since_restart=True)
    assert moved.anchor is not None and moved.anchor.soc_percent == 97.0 and moved.anchor.register_kwh == 6835.0
    assert moved.reading is not None and moved.reading.soc_percent == 97.0 and not moved.reading.estimated


def test_a_car_report_newer_than_the_anchor_is_a_new_reading_even_with_the_same_value() -> None:
    anchor = SocAnchor(43.0, 6800.0, READ_AT, "vehicle", "car", REGISTER)
    now = RESTART + timedelta(seconds=2)
    report = _reading(43.0, 60.0, time_basis="vehicle_timestamp", state_age_s=2.0)
    read = _resolve(report, anchor, 6800.0, now, since_restart=True)
    assert read.anchor is not None and read.anchor.read_at == now - timedelta(seconds=60)
    assert read.anchor.time_basis == "vehicle_timestamp"


def test_a_car_report_older_than_the_anchor_keeps_the_anchor_with_the_cars_age() -> None:
    anchor = SocAnchor(43.0, 6800.0, READ_AT, "vehicle", "car", REGISTER)
    now = RESTART + timedelta(seconds=2)
    age = (now - READ_AT).total_seconds() + 600.0
    report = _reading(43.0, age, time_basis="vehicle_timestamp", state_age_s=2.0)
    read = _resolve(report, anchor, 6800.0, now, since_restart=True)
    assert read.anchor == anchor
    assert read.reading is not None and read.reading.age_s == pytest.approx(age)


def test_a_newer_car_report_replaces_the_anchor_while_energy_was_delivered() -> None:
    """A stale reading by `last_updated` replaces the anchor only while nothing was delivered; one the car timed
    itself after the anchor is a report it made since, whatever its age."""
    anchor = SocAnchor(43.0, 6800.0, READ_AT, "vehicle", "car", REGISTER)
    now = READ_AT + timedelta(hours=3)
    report = _reading(70.0, 1800.0, time_basis="vehicle_timestamp", state_age_s=5.0)
    read = _resolve(report, anchor, 6825.0, now, register_updated_at=now - timedelta(seconds=30))
    assert read.anchor is not None and read.anchor.soc_percent == 70.0
    assert read.anchor.register_kwh == 6825.0, "the first register value after the report is its baseline"


def test_a_moved_value_whose_car_report_time_did_not_move_is_timed_by_last_updated() -> None:
    anchor = SocAnchor(43.0, 6800.0, READ_AT, "vehicle", "car", REGISTER, time_basis="vehicle_timestamp")
    now = READ_AT + timedelta(hours=1)
    # The value came first, the car's report time is still the anchor's.
    report = _reading(55.0, 3600.0, time_basis="vehicle_timestamp", state_age_s=1.0)
    read = _resolve(report, anchor, 6810.0, now)
    assert read.anchor is not None and read.anchor.soc_percent == 55.0
    assert read.anchor.read_at == now - timedelta(seconds=1)
    assert read.reading is not None and read.reading.time_basis == "last_updated"


def test_without_a_restart_the_same_value_by_last_updated_is_still_a_new_reading() -> None:
    anchor = SocAnchor(43.0, 6800.0, READ_AT, "vehicle", "car", REGISTER)
    read = _resolve(_reading(43.0, 2.0), anchor, 6820.0, RESTART + timedelta(seconds=2))
    assert read.anchor is not None and read.anchor.register_kwh == 6820.0


# ------------------------------------------------------------------------------- a register that falls back


def test_a_session_register_that_starts_again_across_a_restart_carries_what_it_counted() -> None:
    anchor = SocAnchor(43.0, 2.0, READ_AT, "vehicle", "car", REGISTER)
    seen = _resolve(_reading(43.0, 3600.0), anchor, 12.0, READ_AT + timedelta(hours=1))
    assert seen.reading is not None and seen.reading.soc_percent == pytest.approx(_expected(43.0, 10.0))
    # The restart: the session register reads 0, then counts 3 kWh more.
    reset = _resolve(_reading(43.0, 2.0), seen.anchor, 0.0, RESTART, since_restart=True)
    assert reset.reading is not None and reset.reading.soc_percent == pytest.approx(_expected(43.0, 10.0))
    after = _resolve(_reading(43.0, 3600.0), reset.anchor, 3.0, RESTART + timedelta(hours=1), since_restart=True)
    assert after.reading is not None and after.reading.soc_percent == pytest.approx(_expected(43.0, 13.0))


def test_a_register_back_from_a_glitch_is_never_counted_twice() -> None:
    anchor = SocAnchor(43.0, 6800.0, READ_AT, "vehicle", "car", REGISTER)
    anchor = track_register(anchor, 6810.0)
    down = track_register(anchor, 0.0)  # the charger reboots and reads 0 for a while
    assert down is not None and down.carried_kwh == pytest.approx(10.0)
    down = track_register(down, 0.4)
    down = track_register(down, 0.0)
    back = track_register(down, 6811.0)
    assert back is not None and back.register_kwh == 6810.0 and back.carried_kwh == pytest.approx(10.0)
    now = READ_AT + timedelta(hours=2)
    read = _resolve(_reading(43.0, 7200.0), back, 6811.0, now)
    assert read.reading is not None and read.reading.soc_percent == pytest.approx(_expected(43.0, 11.0))


def test_a_session_register_counting_past_the_value_it_fell_from_under_counts() -> None:
    anchor = SocAnchor(43.0, 0.0, READ_AT, "vehicle", "car", REGISTER)
    anchor = track_register(anchor, 8.0)
    anchor = track_register(anchor, 0.0)  # a new session
    anchor = track_register(anchor, 9.0)  # counts past 8: taken as the old count resumed
    assert anchor is not None
    read = _resolve(_reading(43.0, 7200.0), anchor, 9.0, READ_AT + timedelta(hours=2))
    assert read.reading is not None
    assert read.reading.soc_percent <= _expected(43.0, 17.0), "never more than was delivered"
    assert read.reading.soc_percent == pytest.approx(_expected(43.0, 9.0))


def test_what_a_register_carried_is_stored_with_the_anchor() -> None:
    anchor = SocAnchor(43.0, 2.0, READ_AT, "vehicle", "car", REGISTER)
    anchor = track_register(track_register(anchor, 12.0), 0.5)
    assert anchor is not None
    stored = SocAnchor.from_dict(anchor.as_dict())
    assert stored == anchor and stored is not None and stored.peak_kwh == 0.5
    assert SocAnchor.from_dict({**anchor.as_dict(), "carried_kwh": "lots"}) == SocAnchor(
        43.0, 0.5, READ_AT, "vehicle", "car", REGISTER, reset_from_kwh=12.0, resume_carried_kwh=10.0
    )


# ------------------------------------------------------------------------------- the car's own report time


def _car_device(hass: HomeAssistant) -> tuple[str, str]:
    MockConfigEntry(domain="kia_uvo", entry_id="kia_entry").add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id="kia_entry", identifiers={("kia_uvo", "ev6")}, name="EV6"
    )
    soc = add_percent_battery_sensor(hass, device.id, object_id="ev6_ev_battery_level", percent="43")
    return device.id, soc


def _timestamp_sensor(hass: HomeAssistant, device_id: str, key: str, value: str) -> str:
    entry = er.async_get(hass).async_get_or_create(
        "sensor", "kia_uvo", f"vin_{key}", device_id=device_id, suggested_object_id=f"ev6_{key}", translation_key=key
    )
    hass.states.async_set(entry.entity_id, value, {"device_class": "timestamp"})
    return entry.entity_id


async def test_a_car_report_time_older_than_last_updated_gives_the_older_age(hass: HomeAssistant) -> None:
    with freeze_time(RESTART):
        device_id, soc = _car_device(hass)
        stamp = _timestamp_sensor(hass, device_id, "last_updated_at", READ_AT.isoformat())
        _timestamp_sensor(hass, device_id, "last_trip_time", RESTART.isoformat())
        reading = resolve_soc_reading(hass, vehicle_id=device_id, entity_id=soc)
    assert reading is not None and reading.time_basis == "vehicle_timestamp" and reading.time_entity_id == stamp
    assert reading.age_s == pytest.approx((RESTART - READ_AT).total_seconds())
    assert reading.state_age_s == pytest.approx(0.0)


@pytest.mark.parametrize("value", ["unavailable", "unknown", "not a time", "1970-01-01T00:00:00+00:00",
                                   "2026-10-14T05:00:00+00:00"])
async def test_an_unusable_car_report_time_falls_back_to_last_updated(hass: HomeAssistant, value: str) -> None:
    with freeze_time(RESTART):
        device_id, soc = _car_device(hass)
        _timestamp_sensor(hass, device_id, "last_updated_at", value)
        reading = resolve_soc_reading(hass, vehicle_id=device_id, entity_id=soc)
    assert reading is not None and reading.time_basis == "last_updated" and reading.age_s == pytest.approx(0.0)


async def test_two_report_times_that_match_alike_are_never_guessed_between(hass: HomeAssistant) -> None:
    device_id, soc = _car_device(hass)
    _timestamp_sensor(hass, device_id, "last_updated", READ_AT.isoformat())
    _timestamp_sensor(hass, device_id, "last_communication", READ_AT.isoformat())
    assert report_time_entity_id(hass, soc) is None
    battery = _timestamp_sensor(hass, device_id, "battery_last_activity", READ_AT.isoformat())
    assert report_time_entity_id(hass, soc) == battery, "a battery report time is preferred"


# ------------------------------------------------------------------------------- through the reader


_BATTERY = {"device_class": "battery", "unit_of_measurement": "%"}
_ENERGY = {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"}
CAR = "sensor.ev6_battery"


def _reader(hass: HomeAssistant) -> SocReader:
    return SocReader(
        hass,
        "entry-restart",
        raw_reader=lambda vehicle_id: resolve_soc_reading(hass, vehicle_id=vehicle_id, entity_id=CAR),
        register_entity_id=lambda: REGISTER,
        charge_control=lambda: None,
        remembered_capacity=lambda _vehicle: CAPACITY,
    )


async def _restart(hass: HomeAssistant, reader: SocReader, car: str, register: str) -> SocReader:
    """Home Assistant stops (its final write saves what is pending) and starts again: the car's integration and the
    charger's write their last values again, stamped with the start."""
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    reader.async_shutdown()
    hass.states.async_remove(CAR)
    hass.states.async_remove(REGISTER)
    again = _reader(hass)
    await again.async_load()
    hass.states.async_set(REGISTER, register, _ENERGY)
    hass.states.async_set(CAR, car, _BATTERY)
    await hass.async_block_till_done()
    return again


async def test_the_field_night_through_the_reader(hass: HomeAssistant) -> None:
    with freeze_time(READ_AT) as frozen:
        hass.states.async_set(REGISTER, "6800.0", _ENERGY)
        hass.states.async_set(CAR, "43", _BATTERY)
        reader = _reader(hass)
        await reader.async_load()
        assert reader.read(None).soc_percent == 43.0  # type: ignore[union-attr]
        frozen.move_to(RESTART - timedelta(minutes=1))
        hass.states.async_set(REGISTER, "6820.0", _ENERGY)
        before = reader.read(None)
        assert before is not None and before.soc_percent == pytest.approx(_expected(43.0, 20.0))

        frozen.move_to(RESTART)
        again = await _restart(hass, reader, "43", "6820.0")
        restarted = again.read(None)
        assert restarted is not None and restarted.estimated, "the re-stamped 43 % is not a fresh reading"
        assert restarted.soc_percent == pytest.approx(_expected(43.0, 20.0))
        assert restarted.age_s == pytest.approx((RESTART - READ_AT).total_seconds())
        assert again.diagnostics()["last_reading"]["since_restart"] is True

        frozen.tick(timedelta(hours=1))
        hass.states.async_set(REGISTER, "6835.0", _ENERGY)
        after = again.read(None)
        assert after is not None and after.soc_percent == pytest.approx(_expected(43.0, 35.0))

        # The car reports again: a new reading.
        hass.states.async_set(CAR, "100", _BATTERY)
        fresh = again.read(None)
        assert fresh is not None and fresh.soc_percent == 100.0 and not fresh.estimated
        again.async_shutdown()


async def test_a_session_register_reset_by_the_restart_is_carried_through_the_reader(hass: HomeAssistant) -> None:
    with freeze_time(READ_AT) as frozen:
        hass.states.async_set(REGISTER, "2.0", _ENERGY)
        hass.states.async_set(CAR, "43", _BATTERY)
        reader = _reader(hass)
        await reader.async_load()
        reader.read(None)
        frozen.move_to(RESTART - timedelta(minutes=1))
        hass.states.async_set(REGISTER, "12.0", _ENERGY)
        assert reader.read(None).soc_percent == pytest.approx(_expected(43.0, 10.0))  # type: ignore[union-attr]

        frozen.move_to(RESTART)
        again = await _restart(hass, reader, "43", "0.0")
        assert again.read(None).soc_percent == pytest.approx(_expected(43.0, 10.0))  # type: ignore[union-attr]
        frozen.tick(timedelta(hours=1))
        hass.states.async_set(REGISTER, "3.0", _ENERGY)
        assert again.read(None).soc_percent == pytest.approx(_expected(43.0, 13.0))  # type: ignore[union-attr]
        again.async_shutdown()


# ------------------------------------------------------------------------------- the car-ended wait


@pytest.mark.usefixtures("both_restarts")
async def test_the_car_ended_wait_survives_a_restart_that_re_stamps_the_stale_value(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any
) -> None:
    hass.states.async_set(REGISTER, "6800.0", _ENERGY)
    hass.states.async_set(CAR, "43", _BATTERY)
    reader = _reader(hass)
    await reader.async_load()
    assert reader.read(None).soc_percent == 43.0  # type: ignore[union-attr]
    freezer.tick(timedelta(hours=6))
    hass.states.async_set(REGISTER, "6837.0", _ENERGY)
    estimate = reader.read(None)
    assert estimate is not None and estimate.estimated and estimate.soc_percent < TARGET - 3.0

    world = await _ended(hass, timers, car=reader.read)
    assert world.controller.car_ended_wait is not None and world.controller.car_ended_wait["estimated"] is True
    await world.switch("off")

    freezer.tick(timedelta(minutes=20))
    again = await _restart(hass, reader, "43", "6837.0")
    restarted = await world.restart()
    restarted.controller._soc_reader = again.read  # noqa: SLF001
    restarted.controller._car_drawing = lambda: False  # type: ignore[method-assign]  # noqa: SLF001
    reading = again.read(None)
    assert reading is not None and dt_util.utcnow() - timedelta(seconds=reading.age_s or 0.0) < dt_util.utcnow() - (
        timedelta(hours=1)
    ), "the re-stamped value is as old as the reading it repeats"

    starts = len(world.starts)
    await _next_window(restarted, freezer)

    assert len(world.starts) == starts, "a restart re-stamping 43 % lifted the wait"
    assert restarted.controller.car_ended_wait is not None
    again.async_shutdown()
    await restarted.shutdown()
