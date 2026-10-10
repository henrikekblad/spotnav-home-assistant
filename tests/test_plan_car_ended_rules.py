"""The car ending a plan's charge by itself, and a stale reading after it: the pure rules.

The field night of 2026-10-09/10: an OCPP HALO and a Kia EV6 at 72 % (a cloud reading that never moved), a target at
the car's own 100 % limit. 24.26 kWh went in by 02:26, at 02:35 the connector said `SuspendedEV`, and SpotNav kept
planning a window every half hour until the departure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.core import events as ev
from custom_components.spotnav.core.ownership import decide
from custom_components.spotnav.core.session import ChargeSession, ManualPause
from custom_components.spotnav.execution.plan_car_ended import PLAN_CAR_IDLE_S, PlanChargeWatch
from custom_components.spotnav.execution.target_stop import (
    CAR_ENDED_ESTIMATE_MARGIN_PERCENT,
    car_ended_full,
    SocReading,
)
from custom_components.spotnav.planning.vehicle_update_wait import decide_vehicle_update_wait
from custom_components.spotnav.sessions.model import ChargeSession as RecordedCharge, SOURCE_REGISTER
from custom_components.spotnav.vehicles.soc_estimate import CHARGE_EFFICIENCY, resolve_soc, SocAnchor

T0 = datetime(2026, 10, 10, 2, 35, 19, tzinfo=timezone.utc)
PLUG_IN = datetime(2026, 10, 9, 20, 47, 46, tzinfo=timezone.utc)
READ_AT = datetime(2026, 10, 9, 20, 50, 47, tzinfo=timezone.utc)
CAPACITY = 77.4


# ---------------------------------------------------------------------------------------------- the watch


def _look(watch: PlanChargeWatch, at: datetime, **facts: object) -> bool:
    values: dict[str, object] = {
        "now": at,
        "plan_charge": True,
        "control_on": True,
        "drawing": False,
        "connector_status": "SuspendedEV",
        "held": False,
        "balancing_paused": False,
        "start_pending": False,
    }
    values.update(facts)
    return watch.observe(**values)  # type: ignore[arg-type]


def test_a_car_at_suspended_ev_for_five_minutes_in_a_window_ended_the_charge() -> None:
    watch = PlanChargeWatch()
    assert not _look(watch, T0)
    assert not _look(watch, T0 + timedelta(seconds=PLAN_CAR_IDLE_S - 1))
    assert _look(watch, T0 + timedelta(seconds=PLAN_CAR_IDLE_S))
    assert not _look(watch, T0 + timedelta(seconds=PLAN_CAR_IDLE_S + 30)), "told once"


def test_a_current_near_zero_with_the_charge_on_is_the_car_ending_it_too() -> None:
    watch = PlanChargeWatch()
    # 02:24-02:30 the EV6 drew 0.57 A with the connector still saying `Charging`: not drawing by the measurement.
    assert not _look(watch, T0, connector_status="Charging", drawing=False)
    assert _look(watch, T0 + timedelta(seconds=PLAN_CAR_IDLE_S), connector_status="Charging", drawing=False)


@pytest.mark.parametrize(
    "facts",
    [
        {"drawing": True},
        {"drawing": None, "connector_status": None},
        {"balancing_paused": True},
        {"held": True},
        {"start_pending": True},
        {"control_on": False},
        {"plan_charge": False},
        # The charger holds the car back (its own limit), not the car itself.
        {"connector_status": "SuspendedEVSE"},
    ],
)
def test_what_is_not_the_car_ending_the_charge_starts_the_clock_again(facts: dict[str, object]) -> None:
    watch = PlanChargeWatch()
    assert not _look(watch, T0)
    assert not _look(watch, T0 + timedelta(seconds=200), **facts)
    assert not _look(watch, T0 + timedelta(seconds=PLAN_CAR_IDLE_S + 10)), "the clock started again"
    assert _look(watch, T0 + timedelta(seconds=200 + 2 * PLAN_CAR_IDLE_S))


def test_suspended_ev_with_no_current_reading_is_not_drawing() -> None:
    watch = PlanChargeWatch()
    assert not _look(watch, T0, drawing=None)
    assert _look(watch, T0 + timedelta(seconds=PLAN_CAR_IDLE_S), drawing=None)


# ---------------------------------------------------------------------------------------------- full or not


def _reading(percent: float | None, *, estimated: bool) -> SocReading:
    return SocReading(percent, "vehicle", "sensor.ev", "car", 3600.0, estimated=estimated)


def test_a_car_that_ended_the_charge_at_its_limit_by_the_estimate_is_full() -> None:
    assert car_ended_full(_reading(100.0, estimated=True), target_soc_percent=100.0, vehicle_max_percent=100.0)
    # The pack size or the charging loss a little off: within the margin, the car that stopped by itself is full.
    near = 100.0 - CAR_ENDED_ESTIMATE_MARGIN_PERCENT
    assert car_ended_full(_reading(near, estimated=True), target_soc_percent=100.0, vehicle_max_percent=100.0)
    assert not car_ended_full(_reading(near - 0.1, estimated=True), target_soc_percent=100.0, vehicle_max_percent=100.0)
    # A target above the car's own limit is the limit.
    assert car_ended_full(_reading(80.0, estimated=True), target_soc_percent=100.0, vehicle_max_percent=80.0)
    # A target below it is the target.
    assert car_ended_full(_reading(79.0, estimated=False), target_soc_percent=80.0, vehicle_max_percent=100.0)
    assert not car_ended_full(_reading(78.0, estimated=False), target_soc_percent=80.0, vehicle_max_percent=100.0)
    # No target (a manual amount to the car's limit): the limit, else 100.
    assert car_ended_full(_reading(99.0, estimated=False), target_soc_percent=None, vehicle_max_percent=None)


def test_a_stale_reading_from_before_the_charge_says_nothing_about_full() -> None:
    assert not car_ended_full(_reading(72.0, estimated=False), target_soc_percent=100.0, vehicle_max_percent=100.0)
    assert not car_ended_full(_reading(None, estimated=False), target_soc_percent=100.0, vehicle_max_percent=100.0)
    assert not car_ended_full(None, target_soc_percent=100.0, vehicle_max_percent=100.0)


# ---------------------------------------------------------------------------------------------- the estimate


def test_the_estimate_is_bounded_by_the_cars_own_limit() -> None:
    anchor = SocAnchor(72.0, 6821.568, READ_AT, "vehicle", "car", "sensor.register")
    stale = SocReading(72.0, "vehicle", "sensor.ev", "car", (T0 - READ_AT).total_seconds())
    seen = resolve_soc(
        reading=stale, anchor=anchor, register_kwh=6845.825, capacity_kwh=CAPACITY, now=T0, vehicle_id="car",
        register_entity_id="sensor.register", ceiling_percent=100.0,
    )
    assert seen.reading is not None and seen.reading.estimated
    assert 72.0 + 24.257 * CHARGE_EFFICIENCY / CAPACITY * 100.0 > 100.0
    assert seen.reading.soc_percent == 100.0
    capped = resolve_soc(
        reading=stale, anchor=anchor, register_kwh=6840.0, capacity_kwh=CAPACITY, now=T0, vehicle_id="car",
        register_entity_id="sensor.register", ceiling_percent=90.0,
    )
    assert capped.reading is not None and capped.reading.soc_percent == 90.0
    # Never below the reading it was carried from, whatever the limit says now.
    above = resolve_soc(
        reading=stale, anchor=anchor, register_kwh=6825.0, capacity_kwh=CAPACITY, now=T0, vehicle_id="car",
        register_entity_id="sensor.register", ceiling_percent=60.0,
    )
    assert above.reading is not None and above.reading.soc_percent == 72.0


# ---------------------------------------------------------------------------------------------- the wait for the car


def _charge(kwh: float) -> RecordedCharge:
    start = datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc)
    return RecordedCharge(
        id="night", charger_id="halo", start=start, end=start + timedelta(hours=3), energy_kwh=kwh,
        energy_source=SOURCE_REGISTER, priced_kwh=0.0, cost_minor=None, reference_cost_minor=None, area_id=None,
        currency=None, major_unit=None, minor_unit=None, started_by="auto", strategy=None, vehicle_id="car",
        vehicle_name=None, solar_kwh=0.0, solar_known_kwh=0.0, last_register_kwh=None, last_sample_at=None,
    )


def _wait(**facts: object):
    values: dict[str, object] = {
        "need_kwh": 24.08,
        "reading_age_s": (T0 - READ_AT).total_seconds(),
        "estimated": False,
        "sessions": [_charge(24.26)],
        "plugged_in_at": PLUG_IN,
        "connected": True,
        "charging": True,
        "window_ahead": True,
        "departure_at": None,
        "power_kw": 11.0,
        "vehicle_id": "car",
        "now": T0 + timedelta(minutes=10),
        "car_ended_at": T0 + timedelta(minutes=5),
        "car_full": False,
    }
    values.update(facts)
    return decide_vehicle_update_wait(**values)  # type: ignore[arg-type]


def test_after_the_car_ended_the_charge_its_stale_reading_is_not_planned_again() -> None:
    """The measured energy covers what the 72 % reading needed, and the car stopped drawing: a window ahead or a
    charge control still on no longer means the charge goes on."""
    decision = _wait()
    assert decision.wait and decision.reason == "waiting"
    # Before this fix, and still without the car ending it: the open plan keeps planning.
    assert _wait(car_ended_at=None).reason == "charging"
    assert _wait(car_ended_at=None, charging=False).reason == "window_ahead"
    # A car that ended a charge before the reading was taken says nothing about this one.
    assert _wait(car_ended_at=READ_AT - timedelta(minutes=1)).reason == "charging"


def test_the_measured_energy_still_has_to_cover_the_need() -> None:
    assert _wait(sessions=[_charge(10.0)]).reason == "short_of_need", "a car that stopped short is planned again"


def test_an_estimate_at_full_waits_for_the_car_once_it_ended_the_charge() -> None:
    decision = _wait(estimated=True, need_kwh=1.9, car_full=True, sessions=[])
    assert decision.wait and decision.reason == "car_ended"
    assert _wait(estimated=True, need_kwh=1.9, car_full=False, sessions=[]).reason == "estimated"
    assert _wait(estimated=True, need_kwh=1.9, car_full=True, car_ended_at=None, sessions=[]).reason == "estimated"


def test_a_departure_still_bounds_the_wait_after_the_car_ended() -> None:
    now = T0 + timedelta(minutes=10)
    decision = _wait(estimated=True, need_kwh=1.9, car_full=True, sessions=[], departure_at=now + timedelta(hours=3))
    assert decision.wait and decision.replan_at is not None and decision.replan_at < now + timedelta(hours=3)
    close = _wait(estimated=True, need_kwh=1.9, car_full=True, sessions=[], departure_at=now + timedelta(minutes=20))
    assert not close.wait and close.reason == "departure_close"


# ---------------------------------------------------------------------------------------------- the core


def test_the_car_ending_a_plan_charge_starts_the_car_ended_record() -> None:
    session, commands = decide(ChargeSession(owner="plan"), ev.CarEnded(plan=True), T0)
    assert session.car_ended_at == T0 and commands == decide(ChargeSession(), ev.CarEnded(plan=True), T0)[1]
    # Not the plan's charge (a person's, the sun's): this rule leaves it.
    session, _ = decide(ChargeSession(owner="solar"), ev.CarEnded(plan=True), T0)
    assert session.car_ended_at is None
    # A person's Start under a manual pause: as before.
    start = ManualPause("start", "plug_in")
    session, _ = decide(ChargeSession(manual=start, owner="person"), ev.CarEnded(), T0)
    assert session.manual is None and session.car_ended_at == T0
    assert ev.CarEnded(plan=True).to_dict() == {"kind": "car_ended", "plan": True}
