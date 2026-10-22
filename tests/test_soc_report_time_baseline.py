"""A reading timed by the car's report time is measured from the register's value then, when it posted steadily.

The field night of 21-22 October 2026 (UTC): a HALO OCPP charger and a Kia EV6 (77.4 kWh), target 80 %. The car's
70 % was made at 02:47:54 (its report time) and arrived at 03:02:51. The charger's register posted every 30 s:
6941.645 at 02:47:34, 6941.733 at 02:48:04, 6944.386 at 03:03:04, 6952.909 at 03:51:04. Measured from the value
after the arrival the 2.7 kWh charged between the report and its arrival was left out: the estimate reached 80 %
only at 03:51, when the car read 83 %. Measured from the register's value at the report time it says 83.0 % at
03:51 and 80 % at about 03:35.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.target_stop import SocReading
from custom_components.spotnav.vehicles.soc_estimate import (
    CHARGE_EFFICIENCY,
    register_value_at,
    RegisterSample,
    resolve_soc,
    SocAnchor,
    SocReader,
)

CAPACITY = 77.4
NIGHT = datetime(2026, 10, 22, tzinfo=timezone.utc)
REGISTER = "sensor.halo_charger_connector_1_energy_active_import_register"
CAR = "sensor.ev6_ev_battery_level"
_ENERGY = {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"}


def _at(clock: str) -> datetime:
    hours, minutes, seconds = (int(part) for part in clock.split(":"))
    return NIGHT + timedelta(hours=hours, minutes=minutes, seconds=seconds)


REPORT = _at("02:47:54")
ARRIVAL = _at("03:02:51")
#: The register's values from the night, between which it posted every 30 s at a steady rate.
KNOWN = [("02:47:34", 6941.645), ("02:48:04", 6941.733), ("03:03:04", 6944.386), ("03:51:04", 6952.909)]


def _halo(first: str = "02:30:04", last: str = "03:51:04") -> list[RegisterSample]:
    """The register every 30 s, linear between the known values (and at the first rate before them)."""
    known = [(_at(clock), value) for clock, value in KNOWN]
    samples: list[RegisterSample] = []
    at = _at(first)
    while at <= _at(last):
        if at <= known[1][0]:
            (t0, v0), (t1, v1) = known[0], known[1]
        else:
            (t0, v0), (t1, v1) = next((a, b) for a, b in zip(known, known[1:]) if a[0] <= at <= b[0])
        samples.append((at, v0 + (v1 - v0) * (at - t0).total_seconds() / (t1 - t0).total_seconds()))
        at += timedelta(seconds=30)
    return samples


def _reading(now: datetime, *, report: datetime | None = REPORT, percent: float = 70.0) -> SocReading:
    state_age = (now - ARRIVAL).total_seconds()
    if report is None:
        return SocReading(percent, "vehicle", CAR, "ev6", state_age)
    return SocReading(
        percent, "vehicle", CAR, "ev6", (now - report).total_seconds(),
        time_basis="vehicle_timestamp", state_age_s=state_age,
    )


def _replay(
    samples: list[RegisterSample], *, history: bool = True, report: datetime | None = REPORT, until: str = "03:51:04"
) -> tuple[dict[datetime, float], SocAnchor | None]:
    """The estimate every 10 s from the arrival on, as the stop decision reads it, and the last anchor."""
    anchor: SocAnchor | None = None
    seen: dict[datetime, float] = {}
    moments = [ARRIVAL + timedelta(seconds=10 * step) for step in range(int((_at(until) - ARRIVAL).total_seconds()) // 10 + 1)]
    for now in [*moments, _at(until)]:
        shown = [sample for sample in samples if sample[0] <= now]
        posted_at, kwh = shown[-1]
        result = resolve_soc(
            reading=_reading(now, report=report),
            anchor=anchor,
            register_kwh=kwh,
            register_updated_at=posted_at,
            capacity_kwh=CAPACITY,
            now=now,
            vehicle_id="ev6",
            register_entity_id=REGISTER,
            register_history=shown if history else (),
        )
        anchor = result.anchor
        assert result.reading is not None and result.reading.soc_percent is not None
        seen[now] = result.reading.soc_percent
    return seen, anchor


def _crossing(seen: dict[datetime, float], percent: float) -> datetime:
    return min(at for at, value in seen.items() if value >= percent)


def test_the_field_night_is_measured_from_the_registers_value_at_the_report_time() -> None:
    seen, anchor = _replay(_halo())
    assert anchor is not None and anchor.baseline == "report_time"
    assert anchor.register_kwh == pytest.approx(6941.645 + 0.088 * 20 / 30)
    assert seen[_at("03:51:04")] == pytest.approx(83.0, abs=0.2), "the car read 83 % at 03:51"
    crossed = _crossing(seen, 80.0)
    assert _at("03:34:00") <= crossed <= _at("03:37:00"), crossed
    # Already at the arrival the energy since the report is counted.
    assert seen[ARRIVAL] == pytest.approx(70 + (6944.29 - 6941.70) * CHARGE_EFFICIENCY / CAPACITY * 100, abs=0.05)


def test_the_field_night_without_the_registers_values_keeps_the_value_after_the_arrival() -> None:
    """No values seen (a restart): the 1.15.0 rule, the night as it went."""
    seen, anchor = _replay(_halo(), history=False)
    assert anchor is not None and anchor.baseline == "after_arrival"
    assert anchor.register_kwh == pytest.approx(6944.29, abs=0.01), "the value posted 03:02:34, after the report"
    assert seen[_at("03:51:04")] == pytest.approx(79.9, abs=0.2)


def test_a_register_that_books_in_a_lump_keeps_the_value_after_the_arrival() -> None:
    """An Easee-like register: nothing posted within REGISTER_STEADY_GAP_S of the report time."""
    lump: list[RegisterSample] = [
        (_at("02:10:00"), 1650.0), (_at("03:01:30"), 1652.7), (_at("03:20:00"), 1654.0), (_at("03:51:00"), 1657.0)
    ]
    seen, anchor = _replay(lump)
    assert anchor is not None and anchor.baseline == "after_arrival" and anchor.register_kwh == 1652.7
    assert seen[_at("03:51:04")] == pytest.approx(70 + (1657.0 - 1652.7) * CHARGE_EFFICIENCY / CAPACITY * 100)


def test_a_gap_between_the_report_time_and_the_arrival_keeps_the_value_after_the_arrival() -> None:
    """Steady around the report time, then silent for more than REGISTER_STEADY_GAP_S before the arrival."""
    samples = [sample for sample in _halo() if not _at("02:52:00") < sample[0] < _at("02:55:00")]
    _seen, anchor = _replay(samples)
    assert anchor is not None and anchor.baseline == "after_arrival"


def test_a_reading_without_the_cars_report_time_keeps_the_first_value_after_it() -> None:
    seen, anchor = _replay(_halo(), report=None)
    assert anchor is not None and anchor.time_basis == "last_updated" and anchor.baseline == "after_arrival"
    assert anchor.register_kwh == pytest.approx(6944.386, abs=0.001), "the value posted 03:03:04"


def test_a_register_reset_between_the_report_time_and_the_arrival_keeps_the_value_after_the_arrival() -> None:
    """A session register that started again at 02:55:04: never interpolated across the reset."""
    samples = [
        (at, value if at < _at("02:55:04") else value - 6942.0) for at, value in _halo()
    ]
    _seen, anchor = _replay(samples)
    assert anchor is not None and anchor.baseline == "after_arrival"
    assert anchor.register_kwh == pytest.approx(6944.29 - 6942.0, abs=0.01)


def test_a_value_at_the_report_time_above_the_present_one_is_never_used() -> None:
    """A register that dipped by less than the noise tolerance: the value at the report time would run ahead."""
    samples: list[RegisterSample] = [
        (_at("02:47:34"), 10.00), (_at("02:48:04"), 10.04), *[
            (_at("02:48:04") + timedelta(seconds=30 * step), 10.00) for step in range(1, 30)
        ]
    ]
    shown = [sample for sample in samples if sample[0] <= ARRIVAL]
    result = resolve_soc(
        reading=_reading(ARRIVAL), anchor=None, register_kwh=shown[-1][1], register_updated_at=shown[-1][0],
        capacity_kwh=CAPACITY, now=ARRIVAL, vehicle_id="ev6", register_entity_id=REGISTER, register_history=shown,
    )
    assert result.anchor is not None and result.anchor.baseline == "after_arrival"
    assert result.anchor.register_kwh == 10.00


def test_register_value_at_interpolates_only_within_a_steady_run() -> None:
    samples: list[RegisterSample] = [(_at("02:00:00"), 1.0), (_at("02:01:00"), 2.0), (_at("02:02:00"), 3.0)]
    assert register_value_at(samples, _at("02:00:30"), _at("02:02:30")) == pytest.approx(1.5)
    assert register_value_at(samples, _at("02:01:00"), _at("02:02:00")) == 2.0
    assert register_value_at(samples, _at("01:59:59"), _at("02:02:00")) is None, "nothing posted before"
    assert register_value_at(samples, _at("02:00:30"), _at("02:05:00")) is None, "silent too long before `until`"
    unreadable: list[RegisterSample] = [*samples[:2], (_at("02:01:30"), None), samples[2]]
    assert register_value_at(unreadable, _at("02:00:30"), _at("02:02:30")) is None


def test_the_anchor_keeps_which_rule_gave_its_baseline_across_storage() -> None:
    anchor = SocAnchor(70.0, 6941.7, REPORT, "vehicle", "ev6", REGISTER, time_basis="vehicle_timestamp",
                       baseline="report_time")
    stored = anchor.as_dict()
    assert stored["baseline"] == "report_time"
    again = SocAnchor.from_dict(stored)
    assert again is not None and again.baseline == "report_time"
    assert SocAnchor.from_dict({**stored, "baseline": "made up"}).baseline is None  # type: ignore[union-attr]


# ------------------------------------------------------------------------------- through the reader


def _reader(hass: HomeAssistant, reading: dict[str, Any]) -> SocReader:
    def _raw(vehicle_id: str | None) -> SocReading | None:
        if reading.get("percent") is None:
            return None
        now = datetime.now(timezone.utc)
        return SocReading(
            reading["percent"], "vehicle", CAR, vehicle_id, (now - reading["report"]).total_seconds(),
            time_basis="vehicle_timestamp", state_age_s=(now - reading["arrival"]).total_seconds(),
        )

    return SocReader(
        hass,
        "entry-halo",
        raw_reader=_raw,
        register_entity_id=lambda: REGISTER,
        charge_control=lambda: None,
        remembered_capacity=lambda _vehicle: CAPACITY,
    )


async def test_the_reader_keeps_the_registers_values_and_measures_from_the_report_time(
    hass: HomeAssistant, freezer: Any
) -> None:
    reading: dict[str, Any] = {"percent": None}
    samples = _halo()
    freezer.move_to(samples[0][0])
    hass.states.async_set(REGISTER, str(samples[0][1]), _ENERGY)
    reader = _reader(hass, reading)
    assert reader.read("ev6") is None, "no level yet; the register is watched from here"
    for posted_at, value in samples[1:]:
        if posted_at > ARRIVAL:
            break
        freezer.move_to(posted_at)
        hass.states.async_set(REGISTER, str(value), _ENERGY)
    await hass.async_block_till_done()
    freezer.move_to(ARRIVAL)
    reading.update(percent=70.0, report=REPORT, arrival=ARRIVAL)
    reader.read("ev6")
    anchor = reader.diagnostics()["anchor"]
    assert anchor["baseline"] == "report_time"
    assert anchor["register_kwh"] == pytest.approx(6941.645 + 0.088 * 20 / 30)
    freezer.move_to(_at("03:51:04"))
    hass.states.async_set(REGISTER, "6952.909", _ENERGY)
    estimate = reader.read("ev6")
    assert estimate is not None and estimate.estimated
    assert estimate.soc_percent == pytest.approx(83.0, abs=0.2)
    reader.async_shutdown()


async def test_after_a_restart_with_no_values_kept_the_reading_keeps_the_value_after_the_arrival(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The register's values are kept in memory only: a reader started after the report time has none around it."""
    freezer.move_to(_at("03:02:34"))
    hass.states.async_set(REGISTER, "6944.298", _ENERGY)
    freezer.move_to(ARRIVAL)
    reader = _reader(hass, {"percent": 70.0, "report": REPORT, "arrival": ARRIVAL})
    reader.read("ev6")
    anchor = reader.diagnostics()["anchor"]
    assert anchor["baseline"] == "after_arrival" and anchor["register_kwh"] == 6944.298
    reader.async_shutdown()


def test_a_restored_reading_keeps_the_anchor_and_its_report_time_baseline() -> None:
    """The 1.15.0 restart rule still holds: the car's value set again at a start is the anchor's own reading."""
    anchor = SocAnchor(
        70.0, 6941.704, REPORT, "vehicle", "ev6", REGISTER, time_basis="vehicle_timestamp", baseline="report_time"
    )
    now = _at("03:40:00")
    restart_value = SocReading(70.0, "vehicle", CAR, "ev6", 5.0)
    result = resolve_soc(
        reading=restart_value, anchor=anchor, register_kwh=6950.0, register_updated_at=now - timedelta(seconds=5),
        capacity_kwh=CAPACITY, now=now, vehicle_id="ev6", register_entity_id=REGISTER, since_restart=True,
    )
    assert result.anchor is not None
    assert result.anchor.register_kwh == 6941.704 and result.anchor.baseline == "report_time"
    assert result.reading is not None and result.reading.estimated
    assert result.reading.soc_percent == pytest.approx(70 + (6950.0 - 6941.704) * CHARGE_EFFICIENCY / CAPACITY * 100)
