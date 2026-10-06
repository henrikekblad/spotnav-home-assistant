"""A target charge that delivered what the car's last reading needed is not planned again on that reading.

The field trace: target 93 %, the car last reported 89 % (need 3.44 kWh from the wall), the plan's window
delivered about 5.5 kWh at about 11 kW, and the car's cloud reported nothing new. Where the state of charge
cannot be carried forward by the register, the planner waits for the car's new level instead of installing
the same need again; only measured energy counts, a newer reading, an unplug or a near departure plans
again, the car is asked for a reading once per charge, and a fixed-kWh need is counted as it always was.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.planning.auto_settings import DRIVER_MANUAL_KWH, TargetSocIntent
from custom_components.spotnav.planning.vehicle_update_wait import (
    DEPARTURE_MARGIN_S,
    decide_vehicle_update_wait,
    measured_delivery_since,
)
from custom_components.spotnav.runtime import domain_data, preview_for
from custom_components.spotnav.sessions.model import SOURCE_ESTIMATED, SOURCE_INTEGRATED, SOURCE_REGISTER
from custom_components.spotnav.vehicles.soc_estimate import CHARGE_EFFICIENCY
from tests.relay import serve as serve_prices

from .sessions_helpers import session
from .test_dashboard_api import NOW
from .test_replug import Plug
from .world import CAPACITY, charger_and_car, controller_of, go_auto

pytestmark = pytest.mark.usefixtures("offline_relay")

READ_AT = datetime(2026, 10, 6, 10, 29, 36, tzinfo=timezone.utc)
NEED_KWH = 4.0 * CAPACITY / 100.0 / CHARGE_EFFICIENCY  # 89 % to 93 % of 77 kWh: 3.42 kWh from the wall


def _delivered(start: datetime, kwh: float = 5.5, *, source: str = SOURCE_REGISTER, vehicle_id: str | None = "car"):
    charge = session(start, hours=0.5, energy=kwh, source=source, charger_id="soc_charger")
    charge.vehicle_id = vehicle_id
    return charge


def _decide(sessions: list[Any], *, now: datetime | None = None, **overrides: Any):
    now = now or READ_AT + timedelta(minutes=50)
    facts: dict[str, Any] = dict(
        need_kwh=NEED_KWH,
        reading_age_s=(now - READ_AT).total_seconds(),
        estimated=False,
        sessions=sessions,
        plugged_in_at=READ_AT - timedelta(hours=1),
        connected=True,
        charging=False,
        window_ahead=False,
        departure_at=None,
        power_kw=11.0,
        vehicle_id="car",
        now=now,
    )
    facts.update(overrides)
    return decide_vehicle_update_wait(**facts)


# ----------------------------------------------------------------------------- the pure decision


def test_the_field_trace_waits_on_measured_energy_delivered_after_the_reading() -> None:
    decision = _decide([_delivered(READ_AT + timedelta(minutes=15))])
    assert decision.wait and decision.reason == "waiting" and decision.replan_at is None
    assert decision.delivery is not None and decision.delivery.kwh == 5.5


def test_only_measured_energy_counts() -> None:
    estimated = _decide([_delivered(READ_AT + timedelta(minutes=15), source=SOURCE_ESTIMATED)])
    assert not estimated.wait and estimated.reason == "no_measurement"
    integrated = _decide([_delivered(READ_AT + timedelta(minutes=15), source=SOURCE_INTEGRATED)])
    assert integrated.wait, "a smart plug's integrated power is a measurement"
    assert _decide([]).reason == "no_measurement"


def test_energy_short_of_the_need_plans_again() -> None:
    decision = _decide([_delivered(READ_AT + timedelta(minutes=15), kwh=2.0)])
    assert not decision.wait and decision.reason == "short_of_need"


def test_a_reading_newer_than_the_start_of_the_delivery_plans_from_it() -> None:
    # The reading came during the charge (or after it): it may already show the energy.
    decision = _decide([_delivered(READ_AT - timedelta(minutes=5))])
    assert not decision.wait and decision.reason == "no_measurement"


def test_a_charge_before_this_plug_in_or_for_another_car_does_not_count() -> None:
    before_plug_in = _decide(
        [_delivered(READ_AT + timedelta(minutes=5))], plugged_in_at=READ_AT + timedelta(minutes=10)
    )
    assert not before_plug_in.wait
    other_car = _decide([_delivered(READ_AT + timedelta(minutes=15), vehicle_id="other")])
    assert not other_car.wait
    same_car = _decide([_delivered(READ_AT + timedelta(minutes=15), vehicle_id="car")])
    assert same_car.wait


def test_a_charge_recorded_for_no_car_never_counts() -> None:
    unrecorded = [_delivered(READ_AT + timedelta(minutes=15), vehicle_id=None)]
    assert _decide(unrecorded).reason == "no_measurement"


def test_without_a_known_plug_in_nothing_changes() -> None:
    decision = _decide([_delivered(READ_AT + timedelta(minutes=15))], plugged_in_at=None)
    assert not decision.wait and decision.reason == "no_plug_in"


def test_while_charging_a_window_ahead_an_unplug_or_an_estimate_nothing_changes() -> None:
    sessions = [_delivered(READ_AT + timedelta(minutes=15))]
    assert _decide(sessions, charging=True).reason == "charging"
    assert _decide(sessions, window_ahead=True).reason == "window_ahead"
    assert _decide(sessions, connected=False).reason == "unplugged"
    assert _decide(sessions, connected=None).wait, "a charger that cannot say keeps waiting"
    assert _decide(sessions, estimated=True).reason == "estimated"
    assert _decide(sessions, reading_age_s=None).reason == "no_reading_age"


def test_a_departure_waits_only_until_the_need_would_just_still_fit() -> None:
    sessions = [_delivered(READ_AT + timedelta(minutes=15))]
    now = READ_AT + timedelta(minutes=50)
    departure = now + timedelta(hours=8)
    far = _decide(sessions, departure_at=departure, now=now)
    latest = departure - timedelta(hours=NEED_KWH / 11.0, seconds=DEPARTURE_MARGIN_S)
    assert far.wait and far.replan_at == latest
    close = _decide(sessions, departure_at=latest + timedelta(minutes=1), now=latest + timedelta(minutes=2))
    assert not close.wait and close.reason == "departure_close"
    unknown_power = _decide(sessions, departure_at=departure, now=now, power_kw=None)
    assert not unknown_power.wait and unknown_power.reason == "departure_close"


def test_the_delivery_names_the_last_charge() -> None:
    first = _delivered(READ_AT + timedelta(minutes=15), kwh=2.0)
    second = _delivered(READ_AT + timedelta(minutes=55), kwh=2.0)
    delivery = measured_delivery_since(
        [second, first], read_at=READ_AT, plugged_in_at=READ_AT - timedelta(hours=1), vehicle_id="car"
    )
    assert delivery is not None and delivery.kwh == 4.0
    assert delivery.first_start == first.start and delivery.last_session_id == second.id


# ------------------------------------------------------------------- a real charger, through Home Assistant


async def _charged(hass: HomeAssistant, transport: Any, frozen: Any, **settings: Any) -> dict[str, Any]:
    """The field case: a car at 89 % for a 93 % target, a charger whose state of charge cannot be carried
    forward by a register, and a recorded charge that delivered 5.5 kWh after the car's last reading."""
    serve_prices(transport)
    settings.setdefault("amps", 16)
    settings.setdefault("phases", 3)
    charger, car_id, soc_entity = await charger_and_car(hass, soc_percent="89", **settings)
    controller = controller_of(hass, charger.entry_id)
    controller.energy_register_entity_id = None
    # The charger reports the car plugged in (known unplugged, then plugged in), as it arrives.
    plug = Plug(hass, controller, "switch.wallbox")
    await plug.set(False)
    await plug.set(True)
    assert controller.plugged_in_for_count == dt_util.utcnow()
    if settings.get("driver") != DRIVER_MANUAL_KWH:
        await go_auto(hass, charger.entry_id, target=TargetSocIntent(vehicle_id=car_id, target_percent=93))
    preview = preview_for(hass, charger.entry_id)
    before = await preview.async_recalculate()
    frozen.tick(timedelta(minutes=50))
    store = domain_data(hass).session_store
    charge = _delivered(dt_util.utcnow() - timedelta(minutes=35), vehicle_id=car_id)
    charge.charger_id = charger.entry_id
    store.close(charge, dt_util.utcnow())
    await controller.async_drop_plan()  # the plan's window is over
    await hass.async_block_till_done()
    return {
        "charger": charger, "car": car_id, "soc": soc_entity, "controller": controller,
        "preview": preview, "before": before,
    }


def _status_codes(hass: HomeAssistant, charger: Any) -> list[str]:
    payload = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, charger), can_act=True)
    return [line["code"] for line in payload["status"]["lines"]]


async def test_a_delivered_need_on_a_stale_reading_is_not_planned_again(
    hass: HomeAssistant, transport: Any
) -> None:
    refreshes = async_mock_service(hass, "homeassistant", "update_entity")
    with freeze_time(NOW) as frozen:
        world = await _charged(hass, transport, frozen)
        assert world["before"].proposal is not None, "the need was planned from 89 %"
        assert world["before"].proposal.requested_kwh == pytest.approx(NEED_KWH, abs=0.01)

        waiting = await world["preview"].async_recalculate()
        await hass.async_block_till_done()
        assert (waiting.state, waiting.reason) == ("nothing_to_charge", "waiting_for_vehicle_update")
        assert waiting.proposal is None and world["controller"].plan is None
        assert _status_codes(hass, world["charger"])[0] == "waiting_for_vehicle_update"
        assert len(refreshes) == 1, "the car is asked for its new level at the end of the charge"

        frozen.tick(timedelta(minutes=5))
        again = await world["preview"].async_recalculate()
        await hass.async_block_till_done()
        assert again.reason == "waiting_for_vehicle_update"
        assert len(refreshes) == 1, "once per charge"


async def test_a_newer_reading_plans_from_it(hass: HomeAssistant, transport: Any) -> None:
    async_mock_service(hass, "homeassistant", "update_entity")
    with freeze_time(NOW) as frozen:
        world = await _charged(hass, transport, frozen)
        assert (await world["preview"].async_recalculate()).reason == "waiting_for_vehicle_update"

        # The car really did not take the energy: a fresh 90 % plans the rest at once.
        hass.states.async_set(world["soc"], "90", {"device_class": "battery", "unit_of_measurement": "%"})
        await hass.async_block_till_done()
        snapshot = world["preview"].snapshot()
        assert snapshot.reason != "waiting_for_vehicle_update" and snapshot.proposal is not None
        assert snapshot.proposal.requested_kwh == pytest.approx(3.0 * CAPACITY / 100 / CHARGE_EFFICIENCY, abs=0.01)


async def test_the_same_value_reported_again_is_also_the_answer(hass: HomeAssistant, transport: Any) -> None:
    async_mock_service(hass, "homeassistant", "update_entity")
    with freeze_time(NOW) as frozen:
        world = await _charged(hass, transport, frozen)
        assert (await world["preview"].async_recalculate()).reason == "waiting_for_vehicle_update"
        hass.states.async_set(
            world["soc"], "89", {"device_class": "battery", "unit_of_measurement": "%", "polled": "now"}
        )
        await hass.async_block_till_done()
        snapshot = world["preview"].snapshot()
        assert snapshot.reason != "waiting_for_vehicle_update" and snapshot.proposal is not None


async def test_an_unplug_ends_the_wait(hass: HomeAssistant, transport: Any) -> None:
    async_mock_service(hass, "homeassistant", "update_entity")
    with freeze_time(NOW) as frozen:
        world = await _charged(hass, transport, frozen)
        assert (await world["preview"].async_recalculate()).reason == "waiting_for_vehicle_update"
        world["controller"].adapter.vehicle_connected = lambda: False
        assert (await world["preview"].async_recalculate()).reason != "waiting_for_vehicle_update"


async def test_a_near_departure_plans_as_before(hass: HomeAssistant, transport: Any) -> None:
    async_mock_service(hass, "homeassistant", "update_entity")
    with freeze_time(NOW) as frozen:
        world = await _charged(hass, transport, frozen)
        settings = domain_data(hass).auto_store.settings(world["charger"].entry_id)
        zone = dt_util.get_time_zone("Europe/Stockholm")
        soon = (dt_util.utcnow() + timedelta(minutes=40)).astimezone(zone)
        await go_auto(
            hass, world["charger"].entry_id, departure_enabled=True,
            departure=time(soon.hour, soon.minute), target=settings.target,
        )
        snapshot = await world["preview"].async_recalculate()
        assert snapshot.reason != "waiting_for_vehicle_update"
        assert snapshot.proposal is not None, "the departure's safety wins"


async def test_without_a_measurement_nothing_changes(hass: HomeAssistant, transport: Any) -> None:
    with freeze_time(NOW) as frozen:
        world = await _charged(hass, transport, frozen)
        store = domain_data(hass).session_store
        charger_id = world["charger"].entry_id
        estimated = [
            _delivered(item.start, source=SOURCE_ESTIMATED, vehicle_id=world["car"])
            for item in store.closed_raw(charger_id)
        ]
        for item in estimated:
            item.charger_id = charger_id
        store.replace_closed(charger_id, estimated, dt_util.utcnow())
        snapshot = await world["preview"].async_recalculate()
        assert snapshot.reason != "waiting_for_vehicle_update" and snapshot.proposal is not None


async def test_a_fixed_kwh_need_is_counted_as_before(hass: HomeAssistant, transport: Any) -> None:
    with freeze_time(NOW) as frozen:
        world = await _charged(hass, transport, frozen, driver=DRIVER_MANUAL_KWH, requested_kwh=10.0)
        snapshot = await world["preview"].async_recalculate()
        assert snapshot.reason != "waiting_for_vehicle_update"
        assert snapshot.proposal is not None


async def test_a_car_found_connected_after_a_restart_counts_as_a_new_plug_in(hass: HomeAssistant) -> None:
    """Cars may have been swapped while Home Assistant was down: the plug-in from before is not this car's
    for the wait, and the continuity a restored reading needs is not shown."""
    from custom_components.spotnav.const import CONF_CHARGE_CONTROL
    from custom_components.spotnav.execution.controller import ChargingController

    with freeze_time(NOW) as frozen:
        hass.states.async_set("switch.a", "off")
        controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
        await controller.async_initialize()
        plug = Plug(hass, controller, "switch.a")
        await plug.set(False)
        await plug.set(True)
        seen = dt_util.utcnow()
        assert controller.plugged_in_since == seen and controller.plugged_in_for_count == seen

        frozen.tick(timedelta(hours=3))
        await controller.async_shutdown()
        restarted = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
        restarted.adapter.vehicle_connected = lambda: True  # type: ignore[method-assign]
        await restarted.async_initialize()
        assert restarted.plugged_in_at == seen, "the plug-in is still remembered for the per-plug-in count"
        assert restarted.plugged_in_since is None
        assert restarted.plugged_in_for_count == dt_util.utcnow()

        restarted.adapter.vehicle_connected = lambda: False  # type: ignore[method-assign]
        hass.states.async_set("switch.a", "off", {"plug": False, "stamp": "unplug"})
        await hass.async_block_till_done()
        assert restarted.plugged_in_for_count is None and restarted.plugged_in_since is None
        await restarted.async_shutdown()
