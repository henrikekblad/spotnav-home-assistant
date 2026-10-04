"""Plug-in, unplug and departure: Auto plans again, an open window starts at plug-in, a met need ends the
plan, and a manual need is counted per departure or per plug-in without ever buying a partial charge twice.

The first part drives a bare `ChargingController` (the plug-in seen, the open window started, the safety
rules kept); the second a real charger entry with Auto, prices and an energy register, through Home
Assistant itself, so the wiring in `__init__` is what is tested.
"""

from __future__ import annotations

import itertools
from datetime import datetime, time, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import callback, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.const import CONF_CHARGE_CONTROL
from custom_components.spotnav.execution.controller import (
    CONNECTION_PLUGGED_IN,
    CONNECTION_UNPLUGGED,
    ChargingController,
)
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.planning.auto_controller import advance_register
from custom_components.spotnav.planning.auto_settings import (
    DRIVER_MANUAL_KWH,
    EnergyBaseline,
    PAUSE_UNTIL_RESUMED,
    TargetSocIntent,
)
from custom_components.spotnav.planning.status_compose import compose_status, PlanningFacts, StatusFacts
from custom_components.spotnav.runtime import charger_data, domain_data, preview_for
from tests.relay import serve as serve_prices

from .charger_helpers import detected_config
from .charger_shapes import register_shape, SHAPES
from .helpers import install_schedule
from .test_dashboard_api import NOW
from .world import charger_and_car, controller_of, REGISTER

pytestmark = pytest.mark.usefixtures("offline_relay")

_STAMP = itertools.count()


# --------------------------------------------------------------------------- a bare controller


def _open_window(minutes_in: int = 10, minutes_left: int = 50) -> dict[str, Any]:
    now = dt_util.utcnow()
    return {
        "start": (now - timedelta(minutes=minutes_in)).isoformat(),
        "end": (now + timedelta(minutes=minutes_left)).isoformat(),
        "amps": 10,
    }


def _later_window() -> dict[str, Any]:
    start = dt_util.utcnow() + timedelta(hours=2)
    return {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "amps": 10}


class Plug:
    """A vehicle at a plain switch charger: what the charger says about it, reported on the charge
    control (the entity the controller watches), as a charger reports a plug-in on its status."""

    def __init__(self, hass: HomeAssistant, controller: ChargingController, control: str) -> None:
        self.hass = hass
        self.control = control
        self.connected: bool | None = None
        controller.adapter.vehicle_connected = lambda: self.connected  # type: ignore[method-assign]

    async def set(self, connected: bool | None, *, control: str | None = None) -> None:
        self.connected = connected
        state = self.hass.states.get(self.control)
        self.hass.states.async_set(
            self.control,
            control or (state.state if state is not None else "off"),
            {"plug": connected, "stamp": next(_STAMP)},
        )
        await self.hass.async_block_till_done()


def _obedient_switch(hass: HomeAssistant) -> tuple[list[Any], list[Any]]:
    """`switch.turn_on`/`turn_off`, recorded, with the switch following them as a real one does."""
    starts: list[Any] = []
    stops: list[Any] = []

    def handler(calls: list[Any], state: str):
        async def handle(call: Any) -> None:
            calls.append(call)
            targets = call.data.get("entity_id", [])
            for entity_id in [targets] if isinstance(targets, str) else targets:
                current = hass.states.get(entity_id)
                hass.states.async_set(entity_id, state, None if current is None else current.attributes)

        return handle

    hass.services.async_register("switch", "turn_on", handler(starts, "on"))
    hass.services.async_register("switch", "turn_off", handler(stops, "off"))
    return starts, stops


async def _switch_controller(hass: HomeAssistant, plan: dict[str, Any] | None = None):
    hass.states.async_set("switch.a", "off")
    starts, stops = _obedient_switch(hass)
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()
    plug = Plug(hass, controller, "switch.a")
    await plug.set(False)  # known unplugged: the next "plugged in" is a plug-in
    if plan is not None:
        await install_schedule(controller, plan)
    return controller, plug, starts, stops


async def test_a_plug_in_is_a_change_between_two_known_states_and_is_remembered(hass: HomeAssistant) -> None:
    controller, plug, _, _ = await _switch_controller(hass)
    heard: list[str] = []
    controller.set_connection_handler(lambda event: heard.append(event) or True)

    await plug.set(True)
    assert heard == [CONNECTION_PLUGGED_IN]
    plugged_in_at = controller.plugged_in_at
    assert plugged_in_at is not None

    await plug.set(None)  # the status blinks through unavailable
    await plug.set(True)
    assert heard == [CONNECTION_PLUGGED_IN], "a status that comes back is not a second plug-in"
    await plug.set(False)
    assert heard == [CONNECTION_PLUGGED_IN, CONNECTION_UNPLUGGED]
    assert controller.plugged_in_at == plugged_in_at, "an unplug keeps the plug-in it ended"

    # Remembered across a restart: a need counted per plug-in must not start counting again.
    await controller.async_shutdown()
    restarted = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await restarted.async_initialize()
    assert restarted.plugged_in_at == plugged_in_at
    await restarted.async_shutdown()


async def test_an_easee_status_tells_the_plug_in_and_the_unplug(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    config = detected_config(detect_charger(hass, ids["device_id"]))
    hass.states.async_set("sensor.easee_status", "disconnected")
    async_mock_service(hass, "easee", "action_command")
    controller = ChargingController(hass, "entry_easee", config)
    await controller.async_initialize()
    heard: list[str] = []
    controller.set_connection_handler(lambda event: heard.append(event) or True)

    hass.states.async_set("sensor.easee_status", "awaiting_start")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.easee_status", "disconnected")
    await hass.async_block_till_done()

    assert heard == [CONNECTION_PLUGGED_IN, CONNECTION_UNPLUGGED]
    await controller.async_shutdown()


async def test_a_plug_in_inside_an_open_window_starts_it(hass: HomeAssistant) -> None:
    controller, plug, starts, stops = await _switch_controller(hass, _open_window())
    # Re-arming inside the window already started it once; the car then left and the charge ended.
    await plug.set(False, control="off")
    starts.clear()

    await plug.set(True)

    assert len(starts) == 1 and stops == []
    assert controller.charge_origin == "plan_window"
    await controller.async_shutdown()


async def test_a_handler_that_replans_first_starts_the_window_itself(hass: HomeAssistant) -> None:
    controller, plug, starts, _ = await _switch_controller(hass, _open_window())
    await plug.set(False, control="off")
    starts.clear()
    controller.set_connection_handler(lambda _event: True)

    await plug.set(True)

    assert starts == [], "the handler starts it after replanning, not the controller at once"
    assert await controller.async_start_on_plug_in() is True
    assert len(starts) == 1
    await controller.async_shutdown()


async def test_a_plug_in_outside_every_window_starts_nothing_and_a_self_start_is_held(
    hass: HomeAssistant,
) -> None:
    controller, plug, starts, stops = await _switch_controller(hass, _later_window())

    await plug.set(True)
    assert starts == []
    await plug.set(True, control="on")  # the car starts by itself at plug-in

    assert starts == [] and len(stops) == 1, "the hold stops it once, as before"
    await controller.async_shutdown()


async def test_a_pause_or_solar_owning_the_charger_starts_nothing_at_plug_in(hass: HomeAssistant) -> None:
    controller, plug, starts, _ = await _switch_controller(hass, _open_window())
    await plug.set(False, control="off")
    starts.clear()
    controller.set_hold_guard(lambda: True)

    await plug.set(True)

    assert starts == []
    await controller.async_shutdown()


async def test_load_balancing_without_headroom_starts_nothing_and_remembers_the_wish(
    hass: HomeAssistant,
) -> None:
    controller, plug, starts, _ = await _switch_controller(hass, _open_window())
    await plug.set(False, control="off")
    starts.clear()
    controller.set_start_cap(lambda: 3.0)  # below the six-ampere floor

    await plug.set(True)

    assert starts == []
    assert controller.paused_by_balancing is True, "the regulator resumes it when headroom returns"
    await controller.async_shutdown()


async def test_a_plug_in_with_the_target_reached_ends_the_plan_instead(hass: HomeAssistant) -> None:
    from custom_components.spotnav.execution.target_stop import SocReading

    soc = {"percent": 50.0}
    hass.states.async_set("switch.a", "off")
    controller = ChargingController(
        hass,
        "entry_a",
        {CONF_CHARGE_CONTROL: "switch.a"},
        soc_reader=lambda _vehicle: SocReading(
            soc_percent=soc["percent"], source="vehicle", entity_id=None, vehicle_id="car", age_s=0.0
        ),
    )
    starts, _ = _obedient_switch(hass)
    await controller.async_initialize()
    plug = Plug(hass, controller, "switch.a")
    await plug.set(True)
    await install_schedule(controller, {**_open_window(), "target_soc_percent": 80.0, "vehicle_id": "car"})
    assert len(starts) == 1, "half full: the window starts"
    await plug.set(False, control="off")
    soc["percent"] = 85.0  # charged elsewhere meanwhile

    await plug.set(True)

    assert len(starts) == 1
    assert controller.plan is None and controller.target_stop_record is not None
    await controller.async_shutdown()


async def test_a_met_need_stops_the_plans_charge_and_clears_the_windows_ahead(hass: HomeAssistant) -> None:
    controller, plug, starts, stops = await _switch_controller(hass, _open_window())
    await plug.set(True, control="on")
    assert controller.charge_origin == "plan_window"

    assert await controller.async_end_plan_need_met() is True

    assert controller.plan is None and len(stops) == 1
    await controller.async_shutdown()


async def test_a_met_need_leaves_a_persons_charge_running(hass: HomeAssistant) -> None:
    controller, plug, starts, stops = await _switch_controller(hass, _later_window())
    await controller.async_start(manual=True)
    await plug.set(True, control="on")

    assert await controller.async_end_plan_need_met() is True

    assert controller.plan is None, "the windows ahead are gone"
    assert stops == [], "a person's Charge now is theirs to stop"
    await controller.async_shutdown()


# ------------------------------------------------------------------ counting a manual need


_T0 = datetime(2026, 9, 22, 6, tzinfo=dt_util.UTC)


def _step(baseline: EnergyBaseline, reading: float, seconds: float, *, plugged_in_at: datetime | None = None):
    # Charging the whole time since `_T0`, the last believed reading's instant in these baselines.
    return advance_register(
        baseline, reading, _T0 + timedelta(seconds=seconds), max_kw=22.0, plugged_in_at=plugged_in_at,
        charged_s=seconds,
    )


def test_a_register_that_counts_per_plug_in_is_carried_right_after_a_plug_in() -> None:
    baseline = EnergyBaseline(register_kwh=7.0, departure_key="k", last_register_kwh=10.0, last_register_at=_T0)
    step = _step(baseline, 0.2, 60, plugged_in_at=_T0)
    assert step.accepted and step.delivered_kwh == pytest.approx(3.2), "3 kWh before it started again, 0.2 since"
    assert step.baseline.register_kwh == 0.0 and step.baseline.carried_kwh == pytest.approx(3.0)
    assert _step(step.baseline, 1.0, 300).delivered_kwh == pytest.approx(4.0)


def test_a_lifetime_register_that_reads_zero_for_a_moment_is_a_glitch() -> None:
    """A charger that reboots: its lifetime register reads 0, then its true value again."""
    baseline = EnergyBaseline(register_kwh=1000.0, departure_key="k", last_register_kwh=1000.2, last_register_at=_T0)
    dip = _step(baseline, 0.0, 30)
    assert not dip.accepted and dip.delivered_kwh == pytest.approx(0.2), "nothing moves on one low reading"
    back = _step(dip.baseline, 1000.3, 60)
    assert back.accepted and back.delivered_kwh == pytest.approx(0.3)
    assert back.baseline.register_kwh == 1000.0 and back.baseline.carried_kwh == 0.0
    assert back.baseline.pending_drop_at is None


def test_a_per_plug_in_register_that_reads_zero_mid_charge_is_not_counted_twice() -> None:
    baseline = EnergyBaseline(register_kwh=0.0, departure_key="k", last_register_kwh=7.0, last_register_at=_T0)
    dip = _step(baseline, 0.0, 30, plugged_in_at=_T0 - timedelta(hours=2))
    assert not dip.accepted and dip.delivered_kwh == pytest.approx(7.0)
    back = _step(dip.baseline, 7.1, 60)
    assert back.accepted and back.delivered_kwh == pytest.approx(7.1)


def test_a_drop_that_holds_is_a_register_that_started_again() -> None:
    baseline = EnergyBaseline(register_kwh=50.0, departure_key="k", last_register_kwh=53.0, last_register_at=_T0)
    first = _step(baseline, 20.0, 10)
    assert not first.accepted
    too_soon = _step(first.baseline, 20.0, 60)
    assert not too_soon.accepted, "two readings, but not yet two minutes"
    held = _step(too_soon.baseline, 20.1, 200)
    assert held.accepted and held.delivered_kwh == pytest.approx(3.0)
    assert held.baseline.register_kwh == 20.1 and held.baseline.carried_kwh == pytest.approx(3.0)
    assert _step(held.baseline, 21.6, 400).delivered_kwh == pytest.approx(4.5)


def test_a_reading_that_climbs_faster_than_the_charger_can_deliver_is_not_believed() -> None:
    baseline = EnergyBaseline(register_kwh=1000.0, departure_key="k", last_register_kwh=1000.2, last_register_at=_T0)
    spike = _step(baseline, 1100.0, 60)  # 100 kWh in a minute
    assert not spike.accepted and spike.delivered_kwh == pytest.approx(0.2)
    assert spike.baseline.last_register_kwh == 1000.2, "a reading that did not hold is never carried"
    fell_back = _step(spike.baseline, 1000.3, 90)
    assert fell_back.accepted and fell_back.delivered_kwh == pytest.approx(0.3)
    assert fell_back.baseline.rejected_kwh is None
    # A second reading at or above one that climbed too fast holds: a faster charger, or a meter catching up.
    held = _step(spike.baseline, 1100.5, 120)
    assert held.accepted and held.delivered_kwh == pytest.approx(100.5)
    real = _step(baseline, 1000.5, 120)
    assert real.accepted and real.delivered_kwh == pytest.approx(0.5)
    # An hour at 22 kW is believable.
    assert _step(baseline, 1021.0, 3600).accepted


def test_a_high_reading_that_falls_back_is_dropped_not_carried() -> None:
    baseline = EnergyBaseline(
        register_kwh=1000.0, departure_key="k", last_register_kwh=1004.0, last_register_at=_T0,
        previous_register_kwh=1000.5,
    )
    back = _step(baseline, 1001.0, 30)
    assert back.accepted and back.delivered_kwh == pytest.approx(1.0) and back.baseline.carried_kwh == 0.0


def test_noise_below_the_last_reading_is_not_a_restart() -> None:
    baseline = EnergyBaseline(register_kwh=100.0, departure_key="k", last_register_kwh=104.0, last_register_at=_T0)
    step = _step(baseline, 103.98, 30)
    assert step.delivered_kwh == pytest.approx(3.98) and step.baseline.register_kwh == 100.0


def test_a_stored_baseline_reads_back_with_and_without_the_later_fields() -> None:
    old = EnergyBaseline.from_stored({"register_kwh": 1.0, "departure_key": "no_deadline"})
    assert old.started_at is None and old.remaining_kwh is None and old.carried_kwh == 0.0
    full = EnergyBaseline(
        register_kwh=1.0,
        departure_key="plugin:2026-09-22T06:00:00+00:00",
        started_at=datetime(2026, 9, 22, 6, tzinfo=dt_util.UTC),
        last_register_kwh=3.0,
        carried_kwh=1.5,
        remaining_kwh=4.0,
        last_register_at=datetime(2026, 9, 22, 7, tzinfo=dt_util.UTC),
        pending_drop_kwh=0.0,
        pending_drop_at=datetime(2026, 9, 22, 7, 1, tzinfo=dt_util.UTC),
        pending_drop_count=1,
        delivered_kwh=6.0,
        met_at=datetime(2026, 9, 22, 8, tzinfo=dt_util.UTC),
    )
    assert EnergyBaseline.from_stored(full.as_dict()) == full


def test_the_status_says_when_the_need_is_counted_without_the_register() -> None:
    now = dt_util.utcnow()
    for basis, shown in (("kept", True), ("sessions", True), ("register", False), ("requested", False)):
        status = compose_status(
            StatusFacts(
                now=now,
                has_settings=True,
                planning=PlanningFacts(
                    state="proposal_ready", reason="ready", energy_basis=basis, remaining_kwh=6.43
                ),
            )
        )
        lines = [line for line in status["lines"] if line["code"] == "remaining_need_estimated"]
        assert bool(lines) is shown, basis
        if shown:
            assert lines[0]["params"] == {"kwh": 6.4, "basis": basis}
            assert status["tone"] == "notice"


# ------------------------------------------------------------- a real charger entry with Auto


def _record_charger_commands(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Every start and stop the charger receives, with the switch following it as a real one would."""
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

    hass.bus.async_listen("call_service", on_call)
    return calls


def _meter(hass: HomeAssistant, kwh: float | None) -> None:
    if kwh is None:
        hass.states.async_set(REGISTER, "unavailable")
        return
    hass.states.async_set(
        REGISTER,
        f"{kwh:.3f}",
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    )


class Car:
    """One charger entry with Auto, its plug and its energy register, at a frozen clock."""

    def __init__(self, hass: HomeAssistant, frozen: Any, charger: Any) -> None:
        self.hass = hass
        self.frozen = frozen
        self.charger = charger
        self.controller = controller_of(hass, charger.entry_id)
        self.preview = preview_for(hass, charger.entry_id)
        self.executor = charger_data(hass, charger.entry_id).executor
        self.plug = Plug(hass, self.controller, "switch.wallbox")

    async def settle(self, seconds: float = 6.0) -> None:
        """Let a plug-in or unplug settle and the replan run."""
        self.frozen.tick(timedelta(seconds=seconds))
        async_fire_time_changed(self.hass, dt_util.utcnow())
        await self.hass.async_block_till_done()

    async def unplug(self) -> None:
        await self.plug.set(False, control="off")
        await self.settle()

    async def replug(self) -> None:
        await self.plug.set(True)
        await self.settle()

    def baseline(self) -> EnergyBaseline:
        stored = domain_data(self.hass).auto_store.energy_baseline(self.charger.entry_id)
        assert stored is not None
        return stored

    def requested(self) -> float:
        proposal = self.preview.snapshot().proposal
        assert proposal is not None
        return proposal.requested_kwh

    def dashboard(self) -> dict[str, Any]:
        return dashboard_api.serialize_dashboard(
            dashboard_api.capture_dashboard(self.hass, self.charger), can_act=True
        )


async def _car(hass: HomeAssistant, frozen: Any, *, kwh: float = 10.0, **settings: Any) -> Car:
    settings.setdefault("amps", 16)
    settings.setdefault("phases", 3)
    charger, _, _ = await charger_and_car(
        hass, driver=DRIVER_MANUAL_KWH, requested_kwh=kwh, target=TargetSocIntent(), **settings
    )
    car = Car(hass, frozen, charger)
    await car.plug.set(True)  # the car is there from the start
    return car


async def _delivered(hass: HomeAssistant, frozen: Any, kwh: float, *, minutes: float = 10.0) -> None:
    """The register reads `kwh` after `minutes` of charging: time passes as the energy is delivered."""
    frozen.tick(timedelta(minutes=minutes))
    _meter(hass, kwh)
    await hass.async_block_till_done()


def _turn_ons(calls: list[tuple[str, str]]) -> int:
    return len([call for call in calls if call[0] == "turn_on"])


def _turn_offs(calls: list[tuple[str, str]]) -> int:
    return len([call for call in calls if call[0] == "turn_off"])


async def test_replug_in_an_open_window_starts_it_and_counts_a_new_plug_in_afresh(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen)
        assert car.executor.window_charging_now(), "the window is running from the first moment"
        assert _turn_ons(calls) == 1
        first_key = car.baseline().departure_key

        frozen.tick(timedelta(minutes=10))
        _meter(hass, 1001.5)  # 1.5 kWh delivered in this plug-in
        await car.unplug()
        await car.replug()

        assert _turn_ons(calls) == 2, "the open window starts again at plug-in"
        assert car.controller.charge_origin == "plan_window"
        baseline = car.baseline()
        assert baseline.departure_key.startswith("plugin:") and baseline.departure_key != first_key
        assert baseline.register_kwh == pytest.approx(1001.5), "counted from this plug-in"
        assert car.preview.snapshot().remaining_kwh == pytest.approx(10.0), "no departure: a new plug-in, a new need"
        payload = car.dashboard()
        assert payload["plan"]["remaining_kwh"] == pytest.approx(10.0)
        assert payload["plan"]["delivered_kwh"] == pytest.approx(0.0)


async def test_replug_in_the_same_departure_counts_on_and_never_buys_the_charge_twice(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, departure=time(20, 0), departure_enabled=True)
        key = car.baseline().departure_key

        frozen.tick(timedelta(minutes=20))
        _meter(hass, 1004.0)
        await car.unplug()
        await car.replug()

        assert car.baseline().departure_key == key, "one departure, one count"
        assert car.preview.snapshot().remaining_kwh == pytest.approx(6.0)
        assert car.dashboard()["plan"]["remaining_kwh"] == pytest.approx(6.0)
        assert _turn_ons(calls) == 2


async def test_a_register_that_starts_again_at_plug_in_is_counted_on(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, departure=time(20, 0), departure_enabled=True)
        await car.unplug()
        await car.replug()
        _meter(hass, 0.0)  # a register that counts per plug-in: zero at the plug-in
        await car.preview.async_recalculate()
        await _delivered(hass, frozen, 4.0, minutes=20)
        await car.preview.async_recalculate()
        await car.unplug()

        await car.replug()
        _meter(hass, 0.0)  # zero again at the next plug-in
        await hass.async_block_till_done()
        await _delivered(hass, frozen, 1.0, minutes=5)
        await car.preview.async_recalculate()

        assert car.preview.snapshot().remaining_kwh == pytest.approx(5.0), "4 before, 1 since"
        assert car.baseline().carried_kwh == pytest.approx(4.0)


async def test_an_unreadable_register_keeps_the_last_remainder_and_says_so(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, departure=time(20, 0), departure_enabled=True)
        await _delivered(hass, frozen, 1003.5, minutes=20)
        await car.preview.async_recalculate()
        _meter(hass, None)
        await car.unplug()
        await car.replug()

        snapshot = car.preview.snapshot()
        assert snapshot.energy_basis == "kept"
        assert snapshot.remaining_kwh == pytest.approx(6.5), "never the full ten again"
        lines = car.dashboard()["status"]["lines"]
        assert {"code": "remaining_need_estimated", "params": {"kwh": 6.5, "basis": "kept"}} in lines


async def test_without_a_register_the_recorded_sessions_are_counted(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, departure=time(20, 0), departure_enabled=True)
        # A charger with no energy register at all, from this plug-in on.
        car.controller.energy_register_entity_id = None
        hass.states.async_remove(REGISTER)
        await domain_data(hass).auto_store.async_update(car.charger.entry_id, clear_energy_baseline=True)
        await car.unplug()
        await car.replug()
        assert _turn_ons(calls) == 2
        recorder = charger_data(hass, car.charger.entry_id).sessions
        assert recorder is not None and recorder.session is not None and recorder.session.estimated
        for _ in range(20):  # twenty minutes, estimated from the current asked for
            frozen.tick(timedelta(minutes=1))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
        delivered = recorder.session.energy_kwh
        assert delivered > 1.0

        await car.preview.async_recalculate()

        snapshot = car.preview.snapshot()
        assert snapshot.energy_basis == "sessions"
        assert snapshot.remaining_kwh == pytest.approx(10.0 - delivered)
        line = next(line for line in car.dashboard()["status"]["lines"] if line["code"] == "remaining_need_estimated")
        assert line["params"]["basis"] == "sessions"


async def test_replug_after_the_last_window_is_held_for_the_next_plan_at_once(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, flat=True)  # equal prices plan the latest hours before the departure
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:  # 08:00 in Stockholm, departure at noon there
        car = await _car(hass, frozen, kwh=3.0, departure=time(12, 0), departure_enabled=True)
        plan = car.controller.plan
        assert plan is not None and not car.executor.window_charging_now()
        await car.unplug()
        # The car was away through the whole plan; the departure has passed.
        frozen.move_to(dt_util.parse_datetime("2026-09-22T10:30:00+00:00"))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        stops = _turn_offs(calls)

        await car.plug.set(True, control="on")  # back, and the car starts by itself
        await car.settle()

        new_plan = car.controller.plan
        assert new_plan is not None and new_plan.windows[0][0] > dt_util.utcnow(), "tomorrow's plan, at once"
        assert car.preview.snapshot().remaining_kwh == pytest.approx(3.0), "a new departure, a new need"
        assert _turn_offs(calls) == stops + 1, "held for its window, not charging unheld"
        assert car.controller.hold_until == new_plan.windows[0][0]


async def test_the_departure_passing_plans_the_next_one_with_the_need_counted_afresh(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:  # 08:00 in Stockholm
        car = await _car(hass, frozen, departure=time(12, 0), departure_enabled=True)
        key = car.baseline().departure_key
        await _delivered(hass, frozen, 1004.0, minutes=20)
        await car.preview.async_recalculate()
        assert car.preview.snapshot().remaining_kwh == pytest.approx(6.0)

        frozen.move_to(dt_util.parse_datetime("2026-09-22T10:00:02+00:00"))  # just past noon there
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert car.baseline().departure_key != key, "the departure's own appointment replanned"
        assert car.preview.snapshot().remaining_kwh == pytest.approx(10.0)


async def test_a_persons_stop_ends_with_the_unplug_and_the_replug_plans_again(
    hass: HomeAssistant, transport: Any
) -> None:
    """A person's Stop pauses Auto for the plug-in session it was given in: the unplug ends it, and the
    car plugged in again is Auto's, whose window open now starts."""
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen)
        await car.executor.async_manual_stop()
        await hass.async_block_till_done()
        starts = _turn_ons(calls)
        assert car.executor.pause_intent.choice == "manual"

        await car.unplug()
        assert not car.executor.pause_intent.admitted
        await car.replug()

        assert _turn_ons(calls) == starts + 1


async def test_a_pause_keeps_a_replug_from_starting_anything(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen)
        await car.preview.async_pause(PAUSE_UNTIL_RESUMED)
        await hass.async_block_till_done()
        starts = _turn_ons(calls)

        await car.unplug()
        await car.replug()

        assert _turn_ons(calls) == starts and car.controller.plan is None


async def test_load_balancing_caps_the_start_after_a_replug(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen)
        await car.unplug()
        car.controller.set_start_cap(lambda: 2.0)
        starts = _turn_ons(calls)

        await car.replug()

        assert _turn_ons(calls) == starts, "no headroom for the minimum current"
        assert car.controller.paused_by_balancing is True


async def test_the_energy_stop_ends_the_plan_when_the_need_is_delivered(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0)
        assert car.controller.plan is not None and _turn_ons(calls) == 1
        await _delivered(hass, frozen, 1002.0)
        assert car.controller.plan is not None, "two of three kWh: the plan goes on"

        await _delivered(hass, frozen, 1003.0, minutes=5)  # the register alone: no price, no recalculation

        assert car.controller.plan is None and _turn_offs(calls) == 1
        assert car.preview.snapshot().state == "nothing_to_charge"


async def test_a_need_met_by_charge_now_clears_the_plan_and_lets_the_charge_go_on(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, flat=True)  # equal prices plan the latest hours: the window is ahead
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0)
        assert car.controller.plan is not None and not car.executor.window_charging_now()
        await car.executor.async_manual_start()
        await hass.async_block_till_done()

        await _delivered(hass, frozen, 1003.2)
        await _delivered(hass, frozen, 1003.3, minutes=1)

        assert car.controller.plan is None, "the windows ahead would buy what nobody needs"
        assert _turn_offs(calls) == 0, "Charge now is the person's; it is not stopped"


async def test_a_need_met_by_solar_clears_the_plan_and_leaves_solar_its_charge(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, flat=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=3.0)
        await car.controller.async_start(10, cause="solar")  # what a solar start sends
        await hass.async_block_till_done()

        await _delivered(hass, frozen, 1003.0)
        await _delivered(hass, frozen, 1003.1, minutes=1)

        assert car.controller.plan is None and _turn_offs(calls) == 0


async def test_a_target_plan_replugged_in_its_window_starts_and_one_at_target_does_not(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        charger, _, soc_entity = await charger_and_car(hass, amps=16, phases=3)
        car = Car(hass, frozen, charger)
        await car.plug.set(True)
        assert car.executor.window_charging_now() and _turn_ons(calls) == 1
        await car.unplug()
        await car.replug()
        assert _turn_ons(calls) == 2, "40 % of an 80 % target: the window starts again"

        await car.unplug()
        hass.states.async_set(soc_entity, "81", {"device_class": "battery", "unit_of_measurement": "%"})
        await hass.async_block_till_done()
        await car.replug()

        assert _turn_ons(calls) == 2, "at the target, nothing starts"
        assert car.controller.plan is None


async def test_a_status_that_settles_through_a_few_values_is_planned_for_once(
    hass: HomeAssistant, transport: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen)
        calculations: list[Any] = []
        original = car.preview._calculate  # noqa: SLF001 - counting the one door every replan uses

        async def counted(settings: Any) -> Any:
            calculations.append(settings)
            return await original(settings)

        monkeypatch.setattr(car.preview, "_calculate", counted)
        await car.plug.set(False, control="off")
        frozen.tick(timedelta(seconds=1))
        await car.plug.set(True)
        frozen.tick(timedelta(seconds=1))
        await car.plug.set(False)
        frozen.tick(timedelta(seconds=1))
        await car.plug.set(True)
        assert calculations == [], "nothing until the status has settled"

        await car.settle()

        assert len(calculations) == 1
