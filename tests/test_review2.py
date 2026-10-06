"""Adversarial re-review of 99264be.

Inputs adapted to the fix: a reading is `restored` when its entity came back from unavailable (the reader
sees that per entity), and it keeps the anchor only with `plugged_in_throughout` (the charger saw the car
plugged in across the gap); after a restart the controller counts a car found connected from the restart
(`plugged_in_for_count`), not from a plug-in it did not see.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.execution.target_stop import SocReading
from custom_components.spotnav.planning.vehicle_update_wait import decide_vehicle_update_wait
from custom_components.spotnav.sessions.model import ChargeSession, SOURCE_REGISTER
from custom_components.spotnav.vehicles.soc_estimate import CHARGE_EFFICIENCY, resolve_soc, SocAnchor

CAPACITY = 77.0
REG = "sensor.halo_energy_active_import_register"
T0 = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


def _reading(value, age_s, *, restored=False):
    return SocReading(value, "vehicle", "sensor.ev", "car", age_s, restored=restored)


def _resolve(reading, anchor, register, now, *, plugged_in_throughout=False):
    return resolve_soc(reading=reading, anchor=anchor, register_kwh=register, capacity_kwh=CAPACITY, now=now,
                       vehicle_id="car", register_entity_id=REG, plugged_in_throughout=plugged_in_throughout)


def test_new_reading_equal_to_anchor_written_at_start_is_swallowed() -> None:
    # 60 % at 08:00, register 1000; 10 kWh delivered by 10:00 (estimate ~72 %).
    anchor = SocAnchor(60.0, 1000.0, T0, "vehicle", "car", REG)
    # HA is down 12:00-18:00 while the car is driven back down to 60 %. At start the car's integration
    # fetches the cloud: 60 % - a genuinely new reading, written inside the restore window -> restored.
    start = T0 + timedelta(hours=10)
    seen = _resolve(_reading(60.0, 30.0, restored=True), anchor, 1010.0, start + timedelta(seconds=30))
    later = _resolve(_reading(60.0, 3600.0, restored=True), seen.anchor, 1010.0, start + timedelta(hours=1))
    assert later.reading is not None
    assert later.reading.soc_percent == pytest.approx(60.0), "car read ~72 %, really 60 %: undercharged"


def test_car_integration_reload_same_value_drops_the_delivered_energy() -> None:
    # 89 % at 10:29, register 6800; 5.5 kWh delivered 10:45-11:15 (estimate ~95 %).
    anchor = SocAnchor(89.0, 6800.0, T0, "vehicle", "car", REG)
    # 12:00, HA running: the car's integration is reloaded (or loads late, > RESTORE_WINDOW_S after start).
    # Its sensor goes unavailable and back to the same cloud value 89 %: the reader flags the comeback
    # (`restored`), and the charger saw the car plugged in all along.
    now = T0 + timedelta(hours=4)
    seen = _resolve(_reading(89.0, 5.0, restored=True), anchor, 6805.5, now, plugged_in_throughout=True)
    later = _resolve(_reading(89.0, 3600.0), seen.anchor, 6805.5, now + timedelta(hours=1))
    expected = 89.0 + 5.5 * CHARGE_EFFICIENCY / CAPACITY * 100.0
    assert later.reading is not None and later.reading.soc_percent == pytest.approx(expected), (
        "the field case again: 89 % planned for the same 3.44 kWh"
    )


def _session(start, kwh, vehicle_id):
    return ChargeSession(
        id=f"c-{int(start.timestamp())}", charger_id="c", start=start, end=start + timedelta(hours=2),
        energy_kwh=kwh, energy_source=SOURCE_REGISTER, priced_kwh=0.0, cost_minor=None,
        reference_cost_minor=None, area_id=None, currency=None, major_unit=None, minor_unit=None,
        started_by="other", strategy=None, vehicle_id=vehicle_id, vehicle_name=None,
        solar_kwh=0.0, solar_known_kwh=0.0, last_register_kwh=None, last_sample_at=None,
    )


def test_cars_swapped_while_ha_was_down_wait_on_the_other_cars_energy() -> None:
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    # Target vehicle is car B (sessions record the target vehicle, `sessions/inputs.py:76`). Car B's cloud
    # read 50 % at 07:00. Car A plugged in 08:00 (plugged_in_at 08:00) and took 20 kWh, recorded as car B.
    # HA restarts at 11:00 while car A was unplugged and car B plugged in: the first connection after a
    # restart (previous None) does not move plugged_in_at (`controller._connection_changed`).
    car_a = _session(now - timedelta(hours=4), 20.0, "car_b")
    # The controller now counts a car found connected after the restart from the restart (11:00).
    decision = decide_vehicle_update_wait(
        need_kwh=15.0, reading_age_s=5 * 3600.0, estimated=False, sessions=[car_a],
        plugged_in_at=now - timedelta(hours=1), connected=True, charging=False, window_ahead=False,
        departure_at=None, power_kw=None, vehicle_id="car_b", now=now,
    )
    assert not decision.wait, "car B is never charged: it waits on car A's 20 kWh"
