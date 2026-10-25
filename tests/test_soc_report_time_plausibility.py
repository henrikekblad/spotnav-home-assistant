"""A car's report time gives the baseline only when its reading agrees with the energy the charger delivered.

The field night of 24-25 October 2026 (UTC): a HALO OCPP charger and a Kia EV6 (77.4 kWh). The car's 53 % was
stamped 00:06:47; its next level, 60 %, arrived at 02:06:51 stamped 00:39:39. The charger delivered nothing until
01:00:45 (the register flat at 6952.909 from 00:30 to 01:00, 6963.009 at 01:59:45): 53 -> 60 % with no energy is
impossible, so the stamp was not when the level was measured. Inside a charge such a stamp would count energy the
reading already holds.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.target_stop import SocReading
from custom_components.spotnav.vehicles.soc_estimate import (
    CHARGE_EFFICIENCY,
    check_report_time,
    REGISTER_HISTORY_S,
    RegisterSample,
    resolve_soc,
    SocAnchor,
    SocReader,
)

CAPACITY = 77.4
NIGHT = datetime(2026, 10, 25, tzinfo=timezone.utc)
REGISTER = "sensor.halo_charger_connector_1_energy_active_import_register"
CAR = "sensor.ev6_ev_battery_level"
_ENERGY = {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"}
POINTS_PER_KWH = CHARGE_EFFICIENCY / CAPACITY * 100


def _at(clock: str) -> datetime:
    hours, minutes, seconds = (int(part) for part in clock.split(":"))
    return NIGHT + timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _reading(now: datetime, percent: float, report: datetime, arrival: datetime) -> SocReading:
    return SocReading(
        percent, "vehicle", CAR, "ev6", (now - report).total_seconds(),
        time_basis="vehicle_timestamp", state_age_s=(now - arrival).total_seconds(),
    )


# ------------------------------------------------------------------------------- the night of 24-25 October


def _idle_then_charging(*, flat_posts: bool = False) -> list[RegisterSample]:
    """Flat at 6952.909 (posted before midnight and at 00:30, or every 30 s), then from 01:00:45 every 30 s at the
    rate that reached 6963.009 at 01:59:45."""
    samples: list[RegisterSample] = [(NIGHT - timedelta(minutes=1), 6952.909)]
    if flat_posts:
        at = NIGHT
        while at < _at("01:00:45"):
            samples.append((at, 6952.909))
            at += timedelta(seconds=30)
    else:
        samples.append((_at("00:30:00"), 6952.909))
    rate = (6963.009 - 6952.909) / (_at("01:59:45") - _at("01:00:45")).total_seconds()
    at = _at("01:00:45")
    while at <= _at("02:10:00"):
        samples.append((at, 6952.909 + rate * (at - _at("01:00:45")).total_seconds()))
        at += timedelta(seconds=30)
    return samples


def _replay_the_night(
    samples: list[RegisterSample], *, until: str = "02:10:00"
) -> tuple[SocAnchor, float, datetime]:
    """The 53 % (arrived 00:07:10) and then the 60 %, read every 30 s as the stop decision reads them, with the
    register's values of the last `REGISTER_HISTORY_S` (as `SocReader` keeps them)."""
    anchor: SocAnchor | None = None
    now = _at("00:07:10")
    estimate, last_read = 0.0, now
    while now <= _at(until):
        if now < _at("02:06:51"):
            reading = _reading(now, 53.0, _at("00:06:47"), _at("00:07:10"))
        else:
            reading = _reading(now, 60.0, _at("00:39:39"), _at("02:06:51"))
        horizon = now - timedelta(seconds=REGISTER_HISTORY_S)
        shown = [sample for sample in samples if horizon <= sample[0] <= now]
        posted_at, kwh = [sample for sample in samples if sample[0] <= now][-1]
        result = resolve_soc(
            reading=reading, anchor=anchor, register_kwh=kwh, register_updated_at=posted_at,
            capacity_kwh=CAPACITY, now=now, vehicle_id="ev6", register_entity_id=REGISTER, register_history=shown,
        )
        anchor = result.anchor
        assert result.reading is not None and result.reading.soc_percent is not None
        estimate, last_read = result.reading.soc_percent, now
        now += timedelta(seconds=30)
    assert anchor is not None
    return anchor, estimate, last_read


def test_the_night_of_25_october_is_flagged_and_measured_from_the_value_after_the_arrival() -> None:
    samples = _idle_then_charging()
    anchor, estimate, last_read = _replay_the_night(samples)
    assert anchor.soc_percent == 60.0 and anchor.read_at == _at("00:39:39")
    assert anchor.baseline_check == "failed"
    assert anchor.baseline == "after_arrival"
    at_arrival = [value for posted_at, value in samples if posted_at <= _at("02:06:51")][-1]
    assert anchor.register_kwh == pytest.approx(at_arrival)
    last = [value for posted_at, value in samples if posted_at <= last_read][-1]
    assert last is not None and at_arrival is not None
    assert estimate == pytest.approx(60 + (last - at_arrival) * POINTS_PER_KWH)


def test_the_night_with_a_register_posting_while_idle_never_takes_the_report_times_value() -> None:
    """A register that posts its flat value every 30 s is steady at 00:39:39: unchecked, the 60 % would have been
    measured from 6952.909 and the whole hour's charge counted on top of it (about 72 % at 02:10)."""
    samples = _idle_then_charging(flat_posts=True)
    anchor, estimate, _last_read = _replay_the_night(samples)
    assert anchor.report_kwh == pytest.approx(6952.909), "the register's value at the stamp is known"
    assert anchor.baseline_check == "failed" and anchor.baseline == "after_arrival"
    assert estimate < 61.0


# ------------------------------------------------------------------------------- the pure check


def _previous(**changes: Any) -> SocAnchor:
    anchor = SocAnchor(
        50.0, 100.0, _at("01:00:00"), "vehicle", "ev6", REGISTER, time_basis="vehicle_timestamp",
        baseline="report_time", report_kwh=100.0, floor_kwh=100.0,
    )
    return replace(anchor, **changes)


def _check(previous: SocAnchor | None, percent: float, *, report_kwh: float | None, **kwargs: Any) -> str:
    return check_report_time(
        previous, soc_percent=percent, read_at=kwargs.pop("read_at", _at("01:30:00")), vehicle_id="ev6",
        register_entity_id=REGISTER, samples=kwargs.pop("samples", ()), report_kwh=report_kwh,
        capacity_kwh=CAPACITY, **kwargs,
    )


def test_a_rise_that_matches_the_energy_passes_and_one_beyond_it_fails() -> None:
    # 5.16 kWh is 6.0 points; the tolerance is 2 points.
    assert _check(_previous(), 56.0, report_kwh=105.16) == "passed"
    assert _check(_previous(), 57.9, report_kwh=105.16) == "passed"
    assert _check(_previous(), 58.5, report_kwh=105.16) == "failed"
    # 25.8 kWh is 30 points; the tolerance is a quarter of that.
    assert _check(_previous(), 87.0, report_kwh=125.8) == "passed"
    assert _check(_previous(), 88.0, report_kwh=125.8) == "failed"


def test_a_rise_with_almost_no_energy_fails() -> None:
    assert _check(_previous(), 51.0, report_kwh=100.1) == "passed"
    assert _check(_previous(), 52.0, report_kwh=100.1) == "failed"


def test_a_level_below_the_energy_is_no_evidence_either_way() -> None:
    """A car driven between the readings, or heating its pack: the report time's baseline can only under-count."""
    assert _check(_previous(), 40.0, report_kwh=100.0) == "inconclusive"
    assert _check(_previous(), 51.0, report_kwh=110.0) == "inconclusive"


def test_a_report_time_before_the_previous_one_fails() -> None:
    assert _check(_previous(), 50.0, report_kwh=100.0, read_at=_at("00:59:00")) == "failed"


def test_the_charge_limit_bounds_the_expected_rise() -> None:
    assert _check(_previous(soc_percent=78.0), 80.0, report_kwh=120.0, ceiling_percent=80.0) == "passed"


def test_without_the_registers_values_at_both_report_times_only_a_rise_beyond_the_most_delivered_fails() -> None:
    previous = _previous(report_kwh=None, floor_kwh=99.0, baseline="after_arrival", register_kwh=101.0)
    later: list[RegisterSample] = [(_at("01:35:00"), 105.0)]
    # At most 6 kWh (from 99 to the 105 posted after the report time): 7.0 points, plus 2.
    assert _check(previous, 58.0, report_kwh=None, samples=later) == "inconclusive"
    assert _check(previous, 60.0, report_kwh=None, samples=later) == "failed"
    assert _check(previous, 58.0, report_kwh=None, samples=()) == "inconclusive", "nothing posted after"


def test_nothing_comparable_is_no_previous() -> None:
    assert _check(None, 60.0, report_kwh=100.0) == "no_previous"
    assert _check(_previous(source="charger"), 60.0, report_kwh=100.0) == "no_previous"
    assert _check(_previous(vehicle_id="other"), 60.0, report_kwh=100.0) == "no_previous"
    assert _check(_previous(time_basis="last_updated"), 60.0, report_kwh=100.0) == "no_previous"
    assert _check(_previous(register_entity_id="sensor.other"), 60.0, report_kwh=100.0) == "no_previous"
    assert _check(_previous(carried_kwh=3.0), 60.0, report_kwh=100.0) == "no_previous"
    assert check_report_time(
        _previous(), soc_percent=60.0, read_at=_at("01:30:00"), vehicle_id="ev6", register_entity_id=REGISTER,
        samples=(), report_kwh=100.0, capacity_kwh=None,
    ) == "no_previous"


def test_the_check_and_its_values_survive_storage() -> None:
    anchor = _previous(baseline_check="failed")
    again = SocAnchor.from_dict(anchor.as_dict())
    assert again is not None
    assert (again.report_kwh, again.floor_kwh, again.baseline_check) == (100.0, 100.0, "failed")
    unnamed = {**anchor.as_dict(), "register_entity_id": None, "baseline_check": "made up"}
    stored = SocAnchor.from_dict(unnamed)
    assert stored is not None and stored.report_kwh is None and stored.floor_kwh is None
    assert stored.baseline_check is None


# ------------------------------------------------------------------------------- with no previous reading


def _steady(start: datetime, end: datetime, *, at_start: float = 1000.0) -> list[RegisterSample]:
    """A register posting every 30 s, 0.088 kWh each (10.56 kWh an hour, 12.3 points an hour)."""
    samples: list[RegisterSample] = []
    at = start
    while at <= end:
        samples.append((at, at_start + 0.088 * (at - start).total_seconds() / 30))
        at += timedelta(seconds=30)
    return samples


@pytest.mark.parametrize(("untrusted", "rule"), [(False, "report_time"), (True, "after_arrival")])
def test_with_no_previous_reading_the_steady_rule_holds_unless_the_car_was_flagged(untrusted: bool, rule: str) -> None:
    samples = _steady(_at("01:00:00"), _at("01:20:00"))
    now = _at("01:20:00")
    result = resolve_soc(
        reading=_reading(now, 50.0, _at("01:05:00"), now), anchor=None, register_kwh=samples[-1][1],
        register_updated_at=samples[-1][0], capacity_kwh=CAPACITY, now=now, vehicle_id="ev6",
        register_entity_id=REGISTER, register_history=samples, report_time_untrusted=untrusted,
    )
    assert result.anchor is not None
    assert result.anchor.baseline_check == "no_previous" and result.anchor.baseline == rule


# ------------------------------------------------------------------------------- through the reader


async def test_a_pair_that_disagrees_flags_the_car_until_a_later_pair_agrees(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Charging steadily at 12.3 points an hour from 50 % at 00:00. The 62 % measured at 01:00 comes stamped 00:30:
    it fails and the car is flagged. The 68 % measured and stamped at 01:30 is lower than the energy since the wrong
    stamp makes (no evidence): still flagged. The 74 % stamped 02:00 agrees with it: trusted again."""
    start = _at("00:00:00")
    samples = _steady(start, _at("02:10:00"))
    reading: dict[str, Any] = {"percent": None}

    def _raw(vehicle_id: str | None) -> SocReading | None:
        if reading["percent"] is None:
            return None
        now = datetime.now(timezone.utc)
        return _reading(now, reading["percent"], reading["report"], reading["arrival"])

    freezer.move_to(samples[0][0])
    hass.states.async_set(REGISTER, str(samples[0][1]), _ENERGY)
    reader = SocReader(
        hass, "entry-halo", raw_reader=_raw, register_entity_id=lambda: REGISTER, charge_control=lambda: None,
        remembered_capacity=lambda _vehicle: CAPACITY,
    )
    assert reader.read("ev6") is None
    pending = iter(samples[1:])

    async def _until(moment: datetime) -> None:
        for posted_at, value in pending:
            freezer.move_to(posted_at)
            hass.states.async_set(REGISTER, str(value), _ENERGY)
            if posted_at >= moment:
                break
        await hass.async_block_till_done()
        freezer.move_to(moment)

    def _value_at(moment: datetime) -> float:
        return next(value for posted_at, value in samples if posted_at == moment)  # type: ignore[misc]

    steps = [
        (50.0, "00:00:00", "00:15:00", "no_previous", "report_time", False),
        (62.0, "00:30:00", "01:05:00", "failed", "after_arrival", True),
        (68.0, "01:30:00", "01:35:00", "inconclusive", "after_arrival", True),
        (74.0, "02:00:00", "02:05:00", "passed", "report_time", False),
    ]
    for percent, report, arrival, check, rule, untrusted in steps:
        await _until(_at(arrival))
        reading.update(percent=percent, report=_at(report), arrival=_at(arrival))
        reader.read("ev6")
        diagnostics = reader.diagnostics()
        anchor = diagnostics["anchor"]
        assert (anchor["soc_percent"], anchor["baseline_check"], anchor["baseline"]) == (percent, check, rule)
        assert diagnostics["report_time_untrusted"] is untrusted, percent
        if rule == "report_time":
            assert anchor["register_kwh"] == pytest.approx(_value_at(_at(report)))
        else:
            assert anchor["register_kwh"] == pytest.approx(_value_at(_at(arrival)))
    reader.async_shutdown()
