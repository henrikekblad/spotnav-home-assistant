"""Charge to a target state of charge.

Closed loops first (a simulated car and charger, the real estimate and the real stop decision),
then the pieces around them: the persisted anchor across a restart, the planner's need with the
charging loss, a missing capacity or source named, and the target need going through the
price wait.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, callback
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.planning.auto_controller import LiveVehicleFacts
from custom_components.spotnav.planning.auto_settings import DRIVER_TARGET_SOC, TargetSocIntent
from custom_components.spotnav.vehicles.soc_estimate import (
    CHARGE_EFFICIENCY,
    SocAnchor,
    SocReader,
    resolve_soc,
    target_need_kwh,
)
from custom_components.spotnav.execution.target_stop import (
    ESTIMATE_STOP_MARGIN_PERCENT,
    SOC_FRESH_MAX_AGE_S,
    SocReading,
    decide_target_stop,
)
from tests.harness import Harness, assert_nothing_executed
from tests.relay import cheap_night_day, serve
from tests.relay import TODAY
from tests.relay import SE4

T0 = datetime(2026, 9, 29, 22, 0, tzinfo=timezone.utc)
@dataclass
class Car:
    """A battery and the wall energy it has taken; the truth the estimate is judged against."""

    soc: float
    efficiency: float = CHARGE_EFFICIENCY
    register: float = 1234.5

    def charge(self, seconds: float, kw: float) -> None:
        energy = kw * seconds / 3600.0
        self.register += energy
        self.soc += energy * self.efficiency / CAPACITY * 100.0


def _reading(value: float | None, age_s: float, source: str = "vehicle") -> SocReading:
    return SocReading(
        soc_percent=value, source=source, entity_id="sensor.car_soc", vehicle_id="car", age_s=age_s
    )  # type: ignore[arg-type]


def run_charge(
    *,
    start: float,
    target: float,
    poll_every_s: float | None,
    poll_offset_s: float = 1000.0,
    kw: float = 11.0,
    efficiency: float = CHARGE_EFFICIENCY,
    step_s: float = 30.0,
) -> tuple[Car, float, str, int]:
    """Charge until the real decision says stop. `poll_every_s=None` is a charger that reports
    the state of charge itself, every step. Returns (car, minutes, stop reason, fresh anchors)."""
    car = Car(soc=start, efficiency=efficiency)
    # The car was last polled hours ago, at the state it was left in.
    last_poll_value, last_poll_at = start, -3 * 3600.0
    anchor: SocAnchor | None = None
    t = 0.0
    anchors = 0
    while t < 12 * 3600:
        now = T0 + timedelta(seconds=t)
        if poll_every_s is None:
            last_poll_value, last_poll_at = car.soc, t
        elif t >= poll_offset_s and (t - poll_offset_s) % poll_every_s < step_s:
            last_poll_value, last_poll_at = car.soc, t - (t - poll_offset_s) % poll_every_s
        result = resolve_soc(
            reading=_reading(last_poll_value, t - last_poll_at),
            anchor=anchor,
            register_kwh=car.register,
            capacity_kwh=CAPACITY,
            now=now,
            vehicle_id="car",
        )
        if result.anchor != anchor:
            anchors += 1
        anchor = result.anchor
        decision = decide_target_stop(target_soc_percent=target, reading=result.reading)
        if decision.stop:
            return car, t / 60.0, decision.reason, anchors
        car.charge(step_s, kw)
        t += step_s
    raise AssertionError("the charge never stopped")


def test_a_stale_poll_car_stops_within_two_percent_over_its_target() -> None:
    """The Kia case: the sensor answers once an hour, 40 % to 80 % of a 77 kWh pack at 11 kW."""
    car, minutes, reason, anchors = run_charge(start=40.0, target=80.0, poll_every_s=3600.0)
    assert reason == "reached_estimate" or reason == "reached"
    assert 80.0 <= car.soc <= 82.0, car.soc
    assert anchors >= 2, "the hourly polls each replaced the estimate"
    assert 185 < minutes < 200


def test_a_stale_poll_that_never_answers_still_stops_on_the_estimate_alone() -> None:
    car, _, reason, _ = run_charge(start=40.0, target=80.0, poll_every_s=10 * 3600.0)
    assert reason == "reached_estimate"
    assert 80.0 + ESTIMATE_STOP_MARGIN_PERCENT <= car.soc <= 82.0, car.soc


@pytest.mark.parametrize("efficiency", [0.85, 0.95])
def test_an_efficiency_that_is_off_by_five_points_is_bounded_by_the_polls(efficiency: float) -> None:
    """With an hourly poll the error is corrected every hour: the miss stays small either way."""
    car, _, _, _ = run_charge(start=40.0, target=80.0, poll_every_s=3600.0, efficiency=efficiency)
    assert 78.5 <= car.soc <= 83.0, car.soc


def test_a_charger_that_reports_the_state_of_charge_stops_at_the_target() -> None:
    car, _, reason, _ = run_charge(start=40.0, target=80.0, poll_every_s=None)
    assert reason == "reached"
    assert 80.0 <= car.soc <= 80.3, car.soc


def test_a_fresh_reading_always_replaces_the_estimate() -> None:
    anchor = SocAnchor(50.0, 100.0, T0, "vehicle", "car")
    now = T0 + timedelta(hours=1)
    # 10 kWh delivered: the estimate would say 50 + 10 * 0.9 / 77 * 100 = 61.7.
    estimated = resolve_soc(
        reading=_reading(50.0, 3600.0), anchor=anchor, register_kwh=110.0,
        capacity_kwh=CAPACITY, now=now, vehicle_id="car",
    )
    assert estimated.reading is not None and estimated.reading.estimated
    assert estimated.reading.soc_percent == pytest.approx(50.0 + 10.0 * 0.9 / 77.0 * 100.0)
    assert estimated.reading.age_s == 3600.0
    fresh = resolve_soc(
        reading=_reading(58.0, SOC_FRESH_MAX_AGE_S - 1), anchor=anchor, register_kwh=110.0,
        capacity_kwh=CAPACITY, now=now, vehicle_id="car",
    )
    assert fresh.reading is not None and not fresh.reading.estimated
    assert fresh.reading.soc_percent == 58.0
    assert fresh.anchor is not None and fresh.anchor.soc_percent == 58.0
    assert fresh.anchor.register_kwh == 110.0


def test_no_estimate_without_a_register_a_capacity_or_after_a_meter_reset() -> None:
    anchor = SocAnchor(50.0, 100.0, T0, "vehicle", "car")
    now = T0 + timedelta(hours=1)
    stale = _reading(50.0, 3600.0)
    for register, capacity in ((None, CAPACITY), (110.0, None), (20.0, CAPACITY)):
        result = resolve_soc(
            reading=stale, anchor=anchor, register_kwh=register, capacity_kwh=capacity,
            now=now, vehicle_id="car",
        )
        assert result.reading == stale, (register, capacity)


def test_a_sleeping_car_is_estimated_through_its_unavailable_sensor() -> None:
    anchor = SocAnchor(50.0, 100.0, T0, "vehicle", "car")
    result = resolve_soc(
        reading=_reading(None, 7200.0), anchor=anchor, register_kwh=107.7,
        capacity_kwh=CAPACITY, now=T0 + timedelta(hours=2), vehicle_id="car",
    )
    assert result.reading is not None and result.reading.estimated
    assert result.reading.soc_percent == pytest.approx(50.0 + 7.7 * 0.9 / 77.0 * 100.0)


def test_an_estimate_stops_only_above_the_target_plus_margin() -> None:
    def estimated(value: float) -> SocReading:
        return SocReading(value, "vehicle", None, "car", 3600.0, estimated=True)

    assert not decide_target_stop(target_soc_percent=80.0, reading=estimated(80.9)).stop
    stop = decide_target_stop(target_soc_percent=80.0, reading=estimated(81.0))
    assert stop.stop and stop.reason == "reached_estimate"
    # A reading has no margin, and a target of 100 is still reachable by an estimate.
    assert decide_target_stop(target_soc_percent=80.0, reading=_reading(80.0, 5.0)).reason == "reached"
    assert decide_target_stop(target_soc_percent=100.0, reading=estimated(100.0)).stop


def test_the_need_includes_the_charging_loss() -> None:
    reason, wall = target_need_kwh(soc_percent=40.0, capacity_kwh=77.0, target_percent=80.0)
    assert reason == "ok" and wall == pytest.approx(0.4 * 77.0 / 0.9)
    assert target_need_kwh(soc_percent=40.0, capacity_kwh=None, target_percent=80.0) == ("unknown_capacity", None)
    assert target_need_kwh(soc_percent=90.0, capacity_kwh=77.0, target_percent=80.0)[0] == "already_at_target"


# ---------------------------------------------------------------- the persisted anchor


def _register(hass: HomeAssistant, value: float) -> None:
    hass.states.async_set(
        "sensor.wallbox_energy",
        str(value),
        {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"},
    )


def _make_reader(hass: HomeAssistant, raw: dict[str, Any], clock: dict[str, datetime]) -> SocReader:
    return SocReader(
        hass,
        "entry-a",
        raw_reader=lambda vehicle_id: raw["reading"],
        register_entity_id=lambda: "sensor.wallbox_energy",
        charge_control=lambda: None,
        remembered_capacity=lambda _vehicle: CAPACITY,
        now=lambda: clock["now"],
    )


async def test_a_restart_in_the_middle_of_a_charge_keeps_the_estimate(hass: HomeAssistant) -> None:
    clock = {"now": T0}
    raw: dict[str, Any] = {"reading": _reading(40.0, 5.0)}
    _register(hass, 500.0)
    first = _make_reader(hass, raw, clock)
    await first.async_load()
    assert first.read("car").soc_percent == 40.0  # type: ignore[union-attr]

    # The charge runs; the cloud sensor goes quiet. Flush the delayed save, then "restart".
    async_fire_time_changed(hass, datetime.now(timezone.utc) + timedelta(seconds=5))
    await hass.async_block_till_done()
    first.async_shutdown()

    clock["now"] = T0 + timedelta(hours=1)
    _register(hass, 500.0 + 15.0)
    raw["reading"] = _reading(None, 3600.0)  # the car is asleep after the restart, too
    second = _make_reader(hass, raw, clock)
    await second.async_load()
    reading = second.read("car")
    assert reading is not None and reading.estimated
    assert reading.soc_percent == pytest.approx(40.0 + 15.0 * 0.9 / 77.0 * 100.0)
    assert reading.age_s == pytest.approx(3600.0, abs=10)

    # Without the persisted anchor there would be nothing to estimate from.
    await SocReader.async_remove_stored(hass, "entry-a")
    third = _make_reader(hass, raw, clock)
    await third.async_load()
    assert third.read("car") is not None and third.read("car").soc_percent is None  # type: ignore[union-attr]


# ------------------------------------------------------------- planning: gaps and price wait


async def test_a_missing_source_and_a_missing_capacity_are_named(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    serve(harness.transport)
    harness.vehicle_reader = lambda vehicle_id: LiveVehicleFacts(vehicle_id=vehicle_id)
    controller = await harness.auto(
        driver=DRIVER_TARGET_SOC, target=TargetSocIntent(vehicle_id="car", target_percent=80)
    )
    snapshot = controller.snapshot()
    assert (snapshot.state, snapshot.reason) == ("incomplete_settings", "target_soc_unknown")
    assert snapshot.missing == ("live_soc",)

    assert_nothing_executed(no_execution)


async def test_a_missing_capacity_is_named_when_the_source_reads(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    serve(harness.transport)
    harness.vehicle_reader = lambda vehicle_id: LiveVehicleFacts(vehicle_id=vehicle_id, soc_percent=40.0)
    controller = await harness.auto(
        driver=DRIVER_TARGET_SOC, target=TargetSocIntent(vehicle_id="car", target_percent=80)
    )
    snapshot = controller.snapshot()
    assert (snapshot.state, snapshot.reason) == ("incomplete_settings", "target_capacity_unknown")
    assert snapshot.missing == ("capacity",)
    assert_nothing_executed(no_execution)


async def test_a_target_need_goes_through_the_price_wait_with_the_wall_energy(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """Deadline 08:00 tomorrow, tomorrow's prices not out: 60 % to 80 % of 77 kWh (17.1 kWh from the
    wall) still fits after the publication, so it waits; from 40 % (34.2 kWh) only the remainder
    that cannot wait is bought now, from known prices."""
    serve(harness.transport, days=(TODAY,), listed=(TODAY,))
    harness.transport.serve(
        harness.transport.day_path(SE4, TODAY), 200, cheap_night_day(SE4, TODAY)
    )
    soc = {"value": 60.0}
    harness.vehicle_reader = lambda vehicle_id: LiveVehicleFacts(
        vehicle_id=vehicle_id, soc_percent=soc["value"], reported_capacity_kwh=CAPACITY
    )
    controller = await harness.auto(
        driver=DRIVER_TARGET_SOC,
        target=TargetSocIntent(vehicle_id="car", target_percent=80),
        departure=time(8, 0),
    )
    waiting = controller.snapshot()
    assert waiting.state == "waiting_for_publication" and waiting.price_wait == "waiting"

    soc["value"] = 40.0
    buying = await controller.async_recalculate()
    assert buying.reason == "buying_before_publication" and buying.price_wait == "buy_now"
    assert buying.must_buy_kwh == pytest.approx(0.4 * CAPACITY / CHARGE_EFFICIENCY - 18.25 * 2.3 * 0.8, abs=0.05)
    assert buying.proposal is not None and buying.proposal.unpriced_slots == 0
    assert_nothing_executed(no_execution)


# ------------------------------------------------- the real charger, controller and dashboard

from freezegun import freeze_time  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.spotnav.vehicles import vehicle_properties  # noqa: E402
from custom_components.spotnav.api import dashboard as dashboard_api
from tests.test_dashboard_api import NOW

@freeze_time(NOW)
async def test_the_soc_block_and_the_capability_follow_the_reader(
    hass: HomeAssistant, offline_relay: None
) -> None:
    charger, car_id, soc_entity = await charger_and_car(hass)

    payload = dashboard_api.serialize_dashboard(
        dashboard_api.capture_dashboard(hass, charger), can_act=True
    )
    assert payload["charger"]["capabilities"]["target_soc"] is True
    assert payload["soc"] == {
        "value": 40.0, "age_s": 0, "source": "vehicle", "estimated": False,
        "target_percent": 80.0, "need_kwh": 34.22, "capacity_kwh": CAPACITY, "vehicle_name": "EV6",
        "vehicle_id": car_id, "vehicles": [], "missing": [],
        "vehicle_max_percent": None, "efficiency": 0.9,
    }

    # Two hours on, 30 kWh through the meter and no new poll: an estimate, marked as one.
    with freeze_time(NOW) as frozen:
        frozen.tick(timedelta(hours=2))
        hass.states.async_set(
            REGISTER, "1030.0",
            {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
        )
        estimated = dashboard_api.serialize_dashboard(
            dashboard_api.capture_dashboard(hass, charger), can_act=True
        )["soc"]
    assert estimated["estimated"] is True and estimated["source"] == "vehicle"
    assert estimated["value"] == pytest.approx(40.0 + 30.0 * 0.9 / 77.0 * 100.0, abs=0.1)
    assert estimated["age_s"] >= 7000


@freeze_time(NOW)
async def test_without_a_battery_size_the_capability_is_off_and_the_block_says_so(
    hass: HomeAssistant, offline_relay: None
) -> None:
    charger, car_id, _ = await charger_and_car(hass, capacity=None)
    payload = dashboard_api.serialize_dashboard(
        dashboard_api.capture_dashboard(hass, charger), can_act=True
    )
    assert payload["charger"]["capabilities"]["target_soc"] is False
    assert payload["soc"]["capacity_kwh"] is None and payload["soc"]["need_kwh"] is None
    assert payload["soc"]["missing"] == ["capacity"], "the card can see what to ask for"
    assert payload["planning"]["reason"] == "target_capacity_unknown"

    # ... and the vehicle's own properties (`update_vehicle`) supply it.
    await vehicle_properties.async_update_vehicle_properties(
        hass, domain_data(hass).decision_store, car_id, {vehicle_properties.KEY_CAPACITY: CAPACITY}
    )
    await preview_for(hass, charger.entry_id).async_recalculate()
    await hass.async_block_till_done()
    payload = dashboard_api.serialize_dashboard(
        dashboard_api.capture_dashboard(hass, charger), can_act=True
    )
    assert payload["charger"]["capabilities"]["target_soc"] is True
    assert payload["soc"]["missing"] == [] and payload["soc"]["capacity_kwh"] == CAPACITY
    assert payload["planning"]["reason"] != "target_capacity_unknown"


async def test_the_real_controller_stops_on_the_estimate_and_records_which(
    hass: HomeAssistant, offline_relay: None
) -> None:
    """A plan carrying a target, a car that never polls again: the meter alone ends the charge."""
    with freeze_time(NOW) as frozen:
        await real_controller_stop(hass, frozen)


# ------------------------- a shrinking need never interrupts the charge that is running

from custom_components.spotnav.planning.auto_settings import DRIVER_MANUAL_KWH  # noqa: E402
from tests.relay import serve as serve_prices
from .world import CAPACITY, REGISTER, add_car, charger_and_car, real_controller_stop
from .world import controller_of
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.runtime import preview_for
from custom_components.spotnav.runtime import charger_data

pytestmark = pytest.mark.usefixtures("offline_relay")


def _record_charger_commands(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Every service call the charger receives (seen on the bus, whoever handles it), with the
    switch following it the way a real one would."""
    calls: list[tuple[str, str]] = []

    @callback
    def on_call(event: Any) -> None:
        data = event.data
        service, service_data = data["service"], data.get("service_data", {})
        if service in ("turn_on", "turn_off"):
            targets = service_data.get("entity_id", [])
            for entity_id in [targets] if isinstance(targets, str) else targets:
                calls.append((service, entity_id))
                hass.states.async_set(entity_id, "on" if service == "turn_on" else "off")
        elif service == "set_value":
            calls.append(("set_value", str(service_data)))

    hass.bus.async_listen("call_service", on_call)
    return calls


async def _run_window(
    hass: HomeAssistant, transport: Any, *, driver: str, hourly_poll: bool
) -> tuple[list[tuple[str, str]], Any, Any, list[float]]:
    """Charge through one planned window, recalculating every five minutes as the register moves."""
    serve_prices(transport, rising=True)
    extra: dict[str, Any] = {"amps": 16, "phases": 3}
    if driver == DRIVER_MANUAL_KWH:
        extra.update(driver=DRIVER_MANUAL_KWH, requested_kwh=34.0, target=TargetSocIntent())
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        charger, _, soc_entity = await charger_and_car(hass, **extra)
        controller = controller_of(hass, charger.entry_id)
        preview = preview_for(hass, charger.entry_id)
        plan = controller.plan
        assert plan is not None and plan.auto_owned, "Auto installed a plan"
        first_start, first_end = plan.windows[0]
        planned_end = plan.windows[-1][1]
        assert first_start <= dt_util.utcnow() < first_end, "the window is running from the first moment"
        await hass.async_block_till_done()
        assert any(c[0] == "turn_on" for c in calls), calls
        turned_on_at = len(calls)

        needs: list[float] = []
        true_soc = 40.0
        register = 1000.0
        for step in range(1, 60):
            frozen.tick(timedelta(minutes=5))
            delivered = 16 * 230 * 3 / 1000 * 5 / 60
            register += delivered
            true_soc += delivered * 0.9 / CAPACITY * 100
            hass.states.async_set(
                REGISTER, f"{register:.3f}",
                {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
            )
            if hourly_poll and step % 12 == 0:
                hass.states.async_set(soc_entity, f"{true_soc:.0f}", {"device_class": "battery", "unit_of_measurement": "%"})
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
            await preview.async_recalculate()
            await hass.async_block_till_done()
            snapshot = preview.snapshot()
            if snapshot.proposal is not None and snapshot.proposal.requested_kwh:
                needs.append(snapshot.proposal.requested_kwh)
            if controller.plan is None:
                assert dt_util.utcnow() <= planned_end
                if driver == DRIVER_MANUAL_KWH:
                    assert dt_util.utcnow() == planned_end, "only the planned end ended it"
                break
            assert [c for c in calls[turned_on_at:] if c[0] == "turn_off"] == [], f"step {step}: charge cut short"
            assert controller.plan.windows[0][0] == first_start, "the running window was reinstalled"
            assert controller.plan.windows[-1][1] == planned_end
        return calls, controller, snapshot, needs


@pytest.mark.parametrize(
    ("driver", "hourly_poll"),
    [(DRIVER_TARGET_SOC, False), (DRIVER_TARGET_SOC, True), (DRIVER_MANUAL_KWH, False)],
    ids=["target-estimate", "target-with-hourly-polls", "manual-kwh-baseline"],
)
async def test_a_shrinking_need_never_interrupts_the_running_window(
    hass: HomeAssistant, transport: Any, offline_relay: None, driver: str, hourly_poll: bool
) -> None:
    calls, controller, _snapshot, needs = await _run_window(
        hass, transport, driver=driver, hourly_poll=hourly_poll
    )
    assert len(needs) > 5 and needs[0] > needs[-1], "the need really shrank while charging"
    turn_offs = [c for c in calls if c[0] == "turn_off"]
    turn_ons = [c for c in calls if c[0] == "turn_on"]
    if driver == DRIVER_TARGET_SOC:
        assert controller.plan is None and controller.target_stop_record is not None
        assert len(turn_offs) == 1, calls  # the target stop itself, once
    assert len(turn_ons) == 1, calls  # never a start after the start
    assert ("set_value", "") not in calls


# ------------------------------------------------ which vehicle a charger's target is for


_NO_VEHICLE_YET = TargetSocIntent(vehicle_id=None, target_percent=None)


@freeze_time(NOW)
async def test_the_only_vehicle_is_the_target_when_none_is_stored(
    hass: HomeAssistant, offline_relay: None
) -> None:
    from custom_components.spotnav.planning.auto_controller import live_vehicle_facts

    charger, car_id, _ = await charger_and_car(
        hass, driver=DRIVER_MANUAL_KWH, target=_NO_VEHICLE_YET
    )
    assert domain_data(hass).auto_store.settings(charger.entry_id).target.vehicle_id is None
    payload = dashboard_api.serialize_dashboard(
        dashboard_api.capture_dashboard(hass, charger), can_act=True
    )
    assert payload["charger"]["capabilities"]["target_soc"] is True
    assert payload["soc"]["vehicle_id"] == car_id and payload["soc"]["vehicle_name"] == "EV6"
    assert payload["soc"]["value"] == 40.0 and payload["soc"]["vehicles"] == []
    assert payload["soc"]["missing"] == []
    listed = dashboard_api.serialize_charger_list(dashboard_api.capture_chargers(hass))
    assert listed["chargers"][0]["capabilities"]["target_soc"] is True
    # The planner's reader resolves the same vehicle, so a target plan is made against its charge.
    facts = live_vehicle_facts(hass, charger_data(hass, charger.entry_id).soc_reader, "")
    assert facts is not None and facts.vehicle_id == car_id and facts.soc_percent == 40.0


@freeze_time(NOW)
async def test_several_vehicles_and_none_chosen_asks_for_one(
    hass: HomeAssistant, offline_relay: None
) -> None:
    from custom_components.spotnav.planning.auto_controller import live_vehicle_facts

    charger, ev6, _ = await charger_and_car(hass, driver=DRIVER_MANUAL_KWH, target=_NO_VEHICLE_YET)
    other = add_car(hass, "Niro")
    payload = dashboard_api.serialize_dashboard(
        dashboard_api.capture_dashboard(hass, charger), can_act=True
    )
    soc = payload["soc"]
    assert soc is not None and soc["vehicle_id"] is None
    assert "vehicle" in soc["missing"]
    assert sorted((v["id"], v["name"]) for v in soc["vehicles"]) == sorted(
        [(ev6, "EV6"), (other, "Niro")]
    )
    facts = live_vehicle_facts(hass, charger_data(hass, charger.entry_id).soc_reader, "")
    assert facts is not None and facts.soc_percent is None


@freeze_time(NOW)
async def test_a_stored_vehicle_wins_over_the_only_other_candidate(
    hass: HomeAssistant, offline_relay: None
) -> None:
    charger, ev6, _ = await charger_and_car(hass)  # stores the EV6
    add_car(hass, "Niro")
    soc = dashboard_api.serialize_dashboard(
        dashboard_api.capture_dashboard(hass, charger), can_act=True
    )["soc"]
    # Two vehicles exist: the list is always offered now, the chosen one is not "missing".
    assert soc["vehicle_id"] == ev6 and soc["missing"] == []
    assert len(soc["vehicles"]) == 2 and ev6 in [v["id"] for v in soc["vehicles"]]
    assert soc["value"] == 40.0
