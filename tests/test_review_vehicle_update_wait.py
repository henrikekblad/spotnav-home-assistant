"""Adversarial review of a435aa6: the wait for the car's new level."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.spotnav.planning.vehicle_update_wait import decide_vehicle_update_wait
from custom_components.spotnav.execution.target_stop import SocReading
from custom_components.spotnav.sessions.model import ChargeSession, SOURCE_INTEGRATED
from custom_components.spotnav.vehicles.soc_estimate import resolve_soc, SocAnchor

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _session(start: datetime, kwh: float, vehicle_id: str | None) -> ChargeSession:
    return ChargeSession(
        id=f"c-{int(start.timestamp())}", charger_id="c", start=start, end=start + timedelta(hours=1),
        energy_kwh=kwh, energy_source=SOURCE_INTEGRATED, priced_kwh=0.0, cost_minor=None,
        reference_cost_minor=None, area_id=None, currency=None, major_unit=None, minor_unit=None,
        started_by="other", strategy=None, vehicle_id=vehicle_id, vehicle_name=None,
        solar_kwh=0.0, solar_known_kwh=0.0, last_register_kwh=None, last_sample_at=None,
    )


def _decide(age_s: float, sessions, plugged_in_at):
    return decide_vehicle_update_wait(
        need_kwh=15.0, reading_age_s=age_s, estimated=False, sessions=sessions, plugged_in_at=plugged_in_at,
        connected=None, charging=False, window_ahead=False, departure_at=None, power_kw=None,
        vehicle_id="car_b", now=NOW,
    )


def test_wait_ends_on_restart_and_the_same_need_is_planned_again() -> None:
    # The field case without a register: 5.5 kWh measured 10:45-11:15 after the 10:29 reading -> waits.
    charge = _session(NOW - timedelta(hours=1, minutes=15), 16.0, "car_b")
    assert _decide(91 * 60.0, [charge], NOW - timedelta(hours=2)).wait
    # 11:58 (re-review: a reload of the car's integration; after a Home Assistant restart the car is not
    # shown to have stayed plugged in, and counts as a new plug-in): the value comes back, age 120 s. The
    # reader flags it `restored`; equal to the kept anchor, with the car plugged in throughout, it is the
    # anchor's reading, with its age.
    anchor = SocAnchor(89.0, None, NOW - timedelta(minutes=91), "vehicle", "car_b")
    restored = SocReading(89.0, "vehicle", "sensor.ev", "car_b", 120.0, restored=True)
    seen = resolve_soc(
        reading=restored, anchor=anchor, register_kwh=None, capacity_kwh=77.0, now=NOW, vehicle_id="car_b",
        plugged_in_throughout=True,
    )
    assert seen.anchor == anchor and seen.reading is not None and not seen.reading.estimated
    assert seen.reading.age_s == 91 * 60.0
    assert _decide(seen.reading.age_s, [charge], NOW - timedelta(hours=2)).wait, "restart ended the wait"


def test_another_cars_energy_on_a_plug_without_connection_state_stops_this_car() -> None:
    # A smart-plug charger (integrated energy, no connection sensor -> plugged_in_at None, connected None).
    # Car B's cloud last read 50 % at 07:00. Car A charged 16 kWh 08:00-09:00 on the plug; the session
    # carries no vehicle (or the charger's one configured vehicle). Car B plugs in at 11:00.
    other = _session(NOW - timedelta(hours=4), 16.0, None)
    decision = _decide(5 * 3600.0, [other], None)
    assert not decision.wait, "car B waits forever on energy that went into car A"
