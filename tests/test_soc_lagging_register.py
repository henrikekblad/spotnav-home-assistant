"""A register that lags: its value posted before a reading is not the reading's baseline.

The field night of 12 October (UTC): an Easee charger and a Mazda MX-30 (35.5 kWh), target 85 %. Charging ran
00:15-02:45 and 03:00-04:14. The car read 53 % until 02:03, 70 % at 02:03, 77 % at 02:48 and 87 % at 04:17. The
charger's lifetime register read 1648.3 kWh until 01:13, then 1650.7 at 01:13, 1653.6 at 02:16, 1655.9 at 03:05,
1658.8 at 04:14 and 1659.4 at 05:07: each value books energy charged well before it was posted.

The 77 % reading was anchored at the 1653.6 then shown (posted 02:16), so the 03:05 step, energy charged before
02:45 and so already in the 77 %, was counted again: 77 + 5.2 x 0.9 / 35.5 x 100 = 90.2 % at 04:14, and the charge
was stopped on "89.98 %" while the car was at about 87 %. Now the baseline is the first value posted at or after the
reading (1655.9): 77 + 2.9 x 0.9 / 35.5 x 100 = 84.4 %, below the car's 87 %.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.target_stop import SocReading
from custom_components.spotnav.vehicles.soc_estimate import CHARGE_EFFICIENCY, SocAnchor, SocReader, resolve_soc

CAPACITY = 35.5
NIGHT = datetime(2026, 10, 12, tzinfo=timezone.utc)
REGISTER = "sensor.easee_driveway_lifetime_energy"


def _at(clock: str) -> datetime:
    hours, minutes = clock.split(":")
    return NIGHT + timedelta(hours=int(hours), minutes=int(minutes))


#: (posted at, kWh): the register as Home Assistant showed it.
REGISTER_STEPS = [("00:00", 1648.3), ("01:13", 1650.7), ("02:16", 1653.6), ("03:05", 1655.9), ("04:14", 1658.8), ("05:07", 1659.4)]
#: (read at, %): the car's readings.
READINGS = [("00:00", 53.0), ("02:03", 70.0), ("02:48", 77.0), ("04:17", 87.0)]


def _last(steps: list[tuple[str, float]], now: datetime) -> tuple[datetime, float]:
    shown = [(_at(at), value) for at, value in steps if _at(at) <= now]
    return shown[-1]


def _resolve(now: datetime, anchor: SocAnchor | None, *, with_time: bool = True) -> Any:
    read_at, percent = _last(READINGS, now)
    posted_at, kwh = _last(REGISTER_STEPS, now)
    return resolve_soc(
        reading=SocReading(percent, "vehicle", "sensor.mx_30_charge_level", "mx30", (now - read_at).total_seconds()),
        anchor=anchor,
        register_kwh=kwh,
        register_updated_at=posted_at if with_time else None,
        capacity_kwh=CAPACITY,
        now=now,
        vehicle_id="mx30",
    )


def _night(*, with_time: bool = True, until: str = "04:16") -> dict[datetime, float | None]:
    """The estimate every minute of the night, as the stop decision would have read it."""
    anchor: SocAnchor | None = None
    seen: dict[datetime, float | None] = {}
    now = _at("00:01")
    while now <= _at(until):
        result = _resolve(now, anchor, with_time=with_time)
        anchor = result.anchor
        seen[now] = None if result.reading is None else result.reading.soc_percent
        now += timedelta(minutes=1)
    return seen


def test_the_field_night_counted_the_0305_step_twice_when_the_baseline_could_predate_the_reading() -> None:
    """The register's posting time not known: the behaviour of before, kept here as the record of the fault."""
    seen = _night(with_time=False)
    assert seen[_at("04:14")] == pytest.approx(77 + (1658.8 - 1653.6) * CHARGE_EFFICIENCY / CAPACITY * 100)
    assert seen[_at("04:14")] > 90


def test_the_field_night_never_estimates_above_what_the_car_read_after() -> None:
    seen = _night()
    at_stop = seen[_at("04:14")]
    assert at_stop == pytest.approx(77 + (1658.8 - 1655.9) * CHARGE_EFFICIENCY / CAPACITY * 100)
    assert at_stop == pytest.approx(84.4, abs=0.1)
    assert max(value for value in seen.values() if value is not None) < 87.0, "the car read 87 % at 04:17"
    # 02:48-03:05: the 77 % reading has no baseline yet and is not carried forward.
    assert seen[_at("03:04")] == 77.0
    # 03:05: the first value posted after the reading is the baseline: still 77 %.
    assert seen[_at("03:05")] == 77.0


def test_each_reading_is_measured_from_the_first_value_posted_after_it() -> None:
    """The 70 % reading at 02:03 (charging, the register last posted at 01:13) is measured from 1653.6 (02:16),
    never from 1650.7: the 02:16 step holds energy charged before 02:03."""
    seen = _night(until="02:47")
    assert seen[_at("02:15")] == 70.0
    assert seen[_at("02:47")] == 70.0, "no value after 02:16 yet: nothing more is counted"


def test_a_stale_reading_that_replaces_the_anchor_takes_its_baseline_by_the_same_rule() -> None:
    """A newer stale reading replaces the anchor when the register shows nothing delivered since; a register that
    lags shows that while energy was charged, so the new anchor's baseline waits for a value posted after it."""
    anchor = SocAnchor(70.0, 1653.6, _at("02:03"), "vehicle", "mx30", None)
    now = _at("03:00")
    stale = SocReading(77.0, "vehicle", "sensor.mx_30_charge_level", "mx30", (now - _at("02:48")).total_seconds())
    result = resolve_soc(
        reading=stale, anchor=anchor, register_kwh=1653.6, register_updated_at=_at("02:16"),
        capacity_kwh=CAPACITY, now=now, vehicle_id="mx30",
    )
    assert result.anchor is not None and result.anchor.soc_percent == 77.0
    assert result.anchor.register_kwh is None, "the 02:16 value predates the reading"
    later = resolve_soc(
        reading=stale, anchor=result.anchor, register_kwh=1655.9, register_updated_at=_at("03:05"),
        capacity_kwh=CAPACITY, now=_at("03:06"), vehicle_id="mx30",
    )
    assert later.anchor is not None and later.anchor.register_kwh == 1655.9
    assert later.reading is not None and later.reading.soc_percent == 77.0


async def test_the_reader_takes_the_registers_posting_time_from_home_assistant(
    hass: HomeAssistant, freezer: Any
) -> None:
    attributes = {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"}
    reading: dict[str, Any] = {"percent": 77.0, "at": _at("02:48")}
    freezer.move_to(_at("02:16"))
    hass.states.async_set(REGISTER, "1653.6", attributes)
    reader = SocReader(
        hass,
        "entry-mx30",
        raw_reader=lambda vehicle_id: SocReading(
            reading["percent"], "vehicle", "sensor.mx_30_charge_level", vehicle_id,
            (datetime.now(timezone.utc) - reading["at"]).total_seconds(),
        ),
        register_entity_id=lambda: REGISTER,
        charge_control=lambda: None,
        remembered_capacity=lambda _vehicle: CAPACITY,
    )
    freezer.move_to(_at("02:48"))
    assert reader.read("mx30").soc_percent == 77.0  # type: ignore[union-attr]
    freezer.move_to(_at("03:05"))
    hass.states.async_set(REGISTER, "1655.9", attributes)
    assert reader.read("mx30").soc_percent == 77.0  # type: ignore[union-attr]
    freezer.move_to(_at("04:14"))
    hass.states.async_set(REGISTER, "1658.8", attributes)
    estimate = reader.read("mx30")
    assert estimate is not None and estimate.estimated
    assert estimate.soc_percent == pytest.approx(84.4, abs=0.1)
    reader.async_shutdown()
