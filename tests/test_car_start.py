"""Starting the charge in a car that refuses the current the charger offers (`execution/car_start.py`,
`vehicles/vehicle_charge_start.py`).

The field case: an Easee offered current to a Tesla (Tessie) whose own charge was stopped from the car's side; the
connector said `SuspendedEV` for two nights below the target and the car's own limit, and only a start from the car's
side cleared it. Now, after two minutes of that in a charge SpotNav runs, SpotNav turns the car's charge switch on,
once per plug-in; a car that still refuses, or has no switch, gets one notice per plug-in and a status line. The switch
is never turned off.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, MockConfigEntry

from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CHARGER_CURRENT_ENTITIES
from custom_components.spotnav.execution import car_start
from custom_components.spotnav.execution.car_start import (
    CAR_START_AFTER_S,
    CarRefusalWatch,
    level_allows_start,
)
from custom_components.spotnav.execution.controller import ChargingController, ChargingPlan
from custom_components.spotnav.execution.plan_car_ended import PLAN_CAR_IDLE_S, PlanChargeWatch
from custom_components.spotnav.execution.target_stop import SocReading
from custom_components.spotnav.notifications.messages import compose
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.texts import languages
from custom_components.spotnav.vehicles import vehicle_charge_start
from custom_components.spotnav.vehicles.discovery_decisions import DiscoveryDecisionStore

SWITCH = "switch.halo"
CURRENT = "sensor.halo_current"
STATUS = "sensor.halo_status"
CONFIG = {CONF_CHARGE_CONTROL: SWITCH, CONF_CHARGER_CURRENT_ENTITIES: [CURRENT]}
T0 = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)

SUFFIXES = {
    "tessie": "charge_state_charge_enable_request",
    "teslemetry": "charge_state_user_charge_enable_request",
    "tesla_fleet": "charge_state_user_charge_enable_request",
}


# ---------------------------------------------------------------------------------------------- the pure rules


@pytest.mark.parametrize(
    ("level", "target", "limit", "allowed"),
    [
        (65.0, 80.0, 80.0, True),
        (78.0, 80.0, 90.0, True),  # exactly two points below
        (78.5, 80.0, 90.0, False),
        (79.0, 82.0, 80.0, False),  # the car's own limit caps the target
        (80.0, 80.0, 80.0, False),  # a full car refuses rightly
        (60.0, None, 80.0, True),  # no target: the car's own limit
        (98.5, None, None, False),  # nothing stated: 100 %
        (None, 80.0, 80.0, False),  # a level not known never pokes the car
    ],
)
def test_the_level_must_be_two_points_below_the_target_and_the_cars_own_limit(
    level: float | None, target: float | None, limit: float | None, allowed: bool
) -> None:
    assert level_allows_start(level, target, limit) is allowed


def _refused(watch: CarRefusalWatch, at: datetime, **facts: object) -> float | None:
    values: dict[str, object] = {
        "now": at,
        "owned": True,
        "control_on": True,
        "drawing": False,
        "connector_status": "SuspendedEV",
        "held": False,
        "balancing_paused": False,
        "start_pending": False,
        "phase_switch_settling": False,
    }
    values.update(facts)
    return watch.observe(**values)  # type: ignore[arg-type]


def test_the_car_refuses_while_the_charger_offers_and_the_connector_says_suspended_ev() -> None:
    watch = CarRefusalWatch()
    assert _refused(watch, T0) == 0.0
    assert _refused(watch, T0 + timedelta(seconds=CAR_START_AFTER_S)) == CAR_START_AFTER_S
    # Any spelling of the car's refusal: a status other than OCPP's is lower-cased.
    assert _refused(watch, T0 + timedelta(seconds=CAR_START_AFTER_S + 30), connector_status="suspendedev") == 150.0


@pytest.mark.parametrize(
    "facts",
    [
        {"owned": False},  # not a charge SpotNav runs
        {"control_on": False},
        {"held": True},  # the charger's own scheduler or load balancer
        {"balancing_paused": True},
        {"start_pending": True},  # the Start not answered yet
        {"phase_switch_settling": True},
        {"connector_status": "SuspendedEVSE"},  # the charger holds the car back
        {"connector_status": "Charging", "drawing": None},
        {"connector_status": None},
        {"drawing": True},
    ],
)
def test_a_charger_that_does_not_offer_current_is_not_the_car_refusing(facts: dict[str, object]) -> None:
    watch = CarRefusalWatch()
    assert _refused(watch, T0) == 0.0
    assert _refused(watch, T0 + timedelta(seconds=60), **facts) is None
    assert _refused(watch, T0 + timedelta(seconds=CAR_START_AFTER_S)) == 0.0, "the clock begins again"


def test_the_plan_watch_counts_the_cars_idling_from_the_start_in_the_car() -> None:
    watch = PlanChargeWatch()
    facts: dict[str, Any] = {
        "plan_charge": True, "control_on": True, "drawing": False, "connector_status": "SuspendedEV",
        "held": False, "balancing_paused": False, "start_pending": False,
    }
    assert not watch.observe(now=T0, **facts)
    started = T0 + timedelta(seconds=CAR_START_AFTER_S)
    at = T0 + timedelta(seconds=PLAN_CAR_IDLE_S)
    assert not watch.observe(now=at, restart_at=started, **facts), "five minutes from the start in the car"
    assert watch.observe(now=started + timedelta(seconds=PLAN_CAR_IDLE_S), restart_at=started, **facts)


def test_the_switch_is_never_turned_off() -> None:
    """Only `switch.turn_on`: turning the car's charge off would leave it in the very state that is refused."""
    for module in (vehicle_charge_start, car_start):
        assert "turn_off" not in inspect.getsource(module)


# ---------------------------------------------------------------------------------------------- the car's switch


def _car(hass: HomeAssistant, platform: str, *, key: str | None = None, state: str = "off", name: str = "Car") -> tuple[str, str]:
    """A car of `platform` with a battery level and its charge switch (`key`: its unique id's suffix)."""
    MockConfigEntry(domain=platform, entry_id=f"{platform}_{name}").add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=f"{platform}_{name}", identifiers={(platform, name)}, name=name
    )
    registry = er.async_get(hass)
    battery = registry.async_get_or_create(
        "sensor", platform, f"VIN{name}-charge_state_battery_level", device_id=device.id
    )
    hass.states.async_set(battery.entity_id, "65", {"device_class": "battery", "unit_of_measurement": "%"})
    switch = registry.async_get_or_create(
        "switch", platform, f"VIN{name}-{key or SUFFIXES.get(platform, 'charge')}", device_id=device.id,
        suggested_object_id=f"{name.lower()}_{platform}_charge",
    )
    hass.states.async_set(switch.entity_id, state)
    return device.id, switch.entity_id


@pytest.mark.parametrize("platform", sorted(SUFFIXES))
async def test_the_charge_switch_of_each_tesla_integration_is_found(hass: HomeAssistant, platform: str) -> None:
    car, switch = _car(hass, platform)
    found = vehicle_charge_start.charge_switch(hass, car)
    assert found is not None and found.entity_id == switch and found.integration == platform


async def test_another_integration_or_an_unavailable_switch_has_none(hass: HomeAssistant) -> None:
    kia, _ = _car(hass, "kia_uvo", name="Kia")
    assert vehicle_charge_start.charge_switch(hass, kia) is None
    # Tessie's key on a Teslemetry car is not Teslemetry's switch.
    other, _ = _car(hass, "teslemetry", key=SUFFIXES["tessie"], name="Other")
    assert vehicle_charge_start.charge_switch(hass, other) is None
    tesla, switch = _car(hass, "tessie", name="Tesla", state="unavailable")
    assert vehicle_charge_start.charge_switch(hass, tesla) is None
    assert vehicle_charge_start.charge_start_skip_reason(hass, tesla, "p") == vehicle_charge_start.SKIP_NO_SWITCH
    er.async_get(hass).async_update_entity(switch, disabled_by=er.RegistryEntryDisabler.USER)
    hass.states.async_set(switch, "off")
    assert vehicle_charge_start.charge_switch(hass, tesla) is None
    assert vehicle_charge_start.charge_switch(hass, None) is None


async def _store(hass: HomeAssistant) -> DiscoveryDecisionStore:
    store = DiscoveryDecisionStore(hass)
    await store.async_load()
    domain_data(hass).decision_store = store
    return store


async def test_once_per_plug_in_and_not_within_half_an_hour_across_a_restart(hass: HomeAssistant, freezer: Any) -> None:
    freezer.move_to(T0)
    await _store(hass)
    car, switch = _car(hass, "tessie")
    calls: list[ServiceCall] = []

    async def record(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("switch", "turn_on", record)
    hass.services.async_register("switch", "turn_off", record)
    plug_in = T0.isoformat()
    assert await vehicle_charge_start.async_start_charge_in_car(hass, car, plug_in) == vehicle_charge_start.RESULT_OK
    assert [(c.service, c.data["entity_id"]) for c in calls] == [("turn_on", switch)]

    # Home Assistant restarts: the record is read again from storage.
    await _store(hass)
    freezer.tick(timedelta(hours=2))
    assert vehicle_charge_start.charge_start_skip_reason(hass, car, plug_in) == vehicle_charge_start.SKIP_SAME_PLUG_IN
    # A replug ten minutes after a start: too soon.
    freezer.move_to(T0 + timedelta(minutes=10))
    assert vehicle_charge_start.charge_start_skip_reason(hass, car, "later") == vehicle_charge_start.SKIP_TOO_SOON
    freezer.move_to(T0 + timedelta(seconds=vehicle_charge_start.CHARGE_START_MIN_INTERVAL_S))
    assert vehicle_charge_start.charge_start_skip_reason(hass, car, "later") is None
    assert all(c.service == "turn_on" for c in calls)

    shown = vehicle_charge_start.charge_start_diagnostics(hass)[car]
    assert shown["switch"] == switch
    assert shown["last"]["result"] == "ok" and shown["last"]["integration"] == "tessie"
    assert shown["last"]["plug_in"] == plug_in and shown["last"]["at"] == T0.isoformat()


async def test_a_failing_call_is_swallowed_and_still_counts(hass: HomeAssistant, freezer: Any) -> None:
    freezer.move_to(T0)
    await _store(hass)
    car, _ = _car(hass, "teslemetry")

    async def fail(_call: ServiceCall) -> None:
        raise RuntimeError("rate limited")

    hass.services.async_register("switch", "turn_on", fail)
    assert await vehicle_charge_start.async_start_charge_in_car(hass, car, "p") == vehicle_charge_start.RESULT_FAILED
    assert vehicle_charge_start.last_charge_start(hass, car)["result"] == "failed"
    assert vehicle_charge_start.charge_start_skip_reason(hass, car, "q") == vehicle_charge_start.SKIP_TOO_SOON


# ---------------------------------------------------------------------------------------------- the controller


class Halo:
    """A switch charger with a measured current and a status sensor; the car's own switch is recorded apart."""

    def __init__(self, hass: HomeAssistant, car_switch: str | None) -> None:
        self.hass = hass
        self.car_switch = car_switch
        self.calls: list[str] = []
        self.car_calls: list[str] = []
        hass.states.async_set(SWITCH, "off")
        self.status("suspended_ev")
        self.amps(0.0)

        async def handle(call: ServiceCall) -> None:
            entity_ids = call.data.get("entity_id")
            entity_ids = [entity_ids] if isinstance(entity_ids, str) else list(entity_ids or ())
            if car_switch is not None and car_switch in entity_ids:
                self.car_calls.append(call.service)
                return
            self.calls.append(call.service)
            hass.states.async_set(SWITCH, "on" if call.service == "turn_on" else "off")

        hass.services.async_register("switch", "turn_on", handle)
        hass.services.async_register("switch", "turn_off", handle)

    def amps(self, value: float) -> None:
        self.hass.states.async_set(CURRENT, str(value), {"unit_of_measurement": "A"})

    def status(self, value: str) -> None:
        self.hass.states.async_set(STATUS, value)

    def draws(self) -> None:
        self.status("charging")
        self.amps(15.0)


class Car:
    def __init__(self, percent: float | None) -> None:
        self.percent = percent

    def __call__(self, vehicle_id: str | None) -> SocReading:
        return SocReading(self.percent, "vehicle", "sensor.car_battery", vehicle_id, 600.0)


def _plan(vehicle_id: str, target: float = 80.0) -> ChargingPlan:
    now = dt_util.utcnow()
    start, end = now - timedelta(minutes=5), now + timedelta(hours=3)
    return ChargingPlan(
        start=start.isoformat(),
        end=end.isoformat(),
        amps=16,
        periods=[{"start": start.isoformat(), "end": end.isoformat()}],
        target_soc_percent=target,
        vehicle_id=vehicle_id,
        auto_identity="0123456789abcdef0123456789abcdef",
        auto_settings_revision=1,
        auto_price_identity="test-price-identity",
    )


async def _later(hass: HomeAssistant, freezer: Any, seconds: float, step: float = 30.0) -> None:
    left = seconds
    while left > 0:
        moved = min(step, left)
        freezer.tick(timedelta(seconds=moved))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        left -= moved


_CONTROLLERS: list[ChargingController] = []


@pytest.fixture(autouse=True)
async def _shut_down() -> Any:
    """Every controller a test built is shut down after it (its timers would linger)."""
    yield
    while _CONTROLLERS:
        await _CONTROLLERS.pop().async_shutdown()


async def _refusing(
    hass: HomeAssistant, freezer: Any, platform: str | None = "tessie", *, level: float = 65.0,
    target: float = 80.0, limit: float = 80.0,
) -> tuple[ChargingController, Halo, list[tuple[str | None, bool]], str]:
    """A plan's charge in an open window that the car refuses from the start (the connector at `SuspendedEV`)."""
    freezer.move_to(T0)
    await _store(hass)
    car, switch = _car(hass, platform) if platform is not None else ("car", None)
    halo = Halo(hass, switch)
    controller = ChargingController(
        hass, "halo", dict(CONFIG), soc_reader=Car(level), vehicle_limit_reader=lambda _vehicle_id: limit
    )
    await controller.async_initialize()
    _CONTROLLERS.append(controller)
    # The car was plugged in a while ago (what names the plug-in for the stored start and notice).
    controller._plugged_in_at = T0 - timedelta(hours=1)  # noqa: SLF001
    # The charger's status sensor: `charging`, and `suspended_ev` for a car connected that asks for no current.
    adapter = controller.adapter
    adapter.status_entity_id = STATUS
    adapter._charging_values = ("charging",)  # noqa: SLF001 - the profile a status sensor brings
    adapter._idle_values = ("suspended_ev",)  # noqa: SLF001
    told: list[tuple[str | None, bool]] = []
    controller.set_car_ended_observer(lambda vehicle_id, full: told.append((vehicle_id, full)))
    await controller.async_install(_plan(car, target))
    await hass.async_block_till_done()
    assert halo.calls == ["turn_on"], "the open window starts the charge"
    return controller, halo, told, car


async def test_a_refusing_car_has_its_charge_started_once_and_taking_it_tells_nothing(
    hass: HomeAssistant, freezer: Any
) -> None:
    controller, halo, told, _car_id = await _refusing(hass, freezer)

    await _later(hass, freezer, CAR_START_AFTER_S - 30)
    assert halo.car_calls == [], "not before two minutes"
    await _later(hass, freezer, 60)
    assert halo.car_calls == ["turn_on"]
    assert controller.car_start_status == {"state": "started", "soc_percent": 65.0, "target": 80.0}
    assert controller.car_refuses_notice is None

    # The car takes the charge.
    halo.draws()
    await _later(hass, freezer, 60)
    assert controller.car_start_status is None and controller.car_refuses_notice is None
    assert told == [], "the car did not end the charge"
    # It stops again later in the plug-in: no second start, the person is told instead.
    halo.status("suspended_ev")
    halo.amps(0.0)
    await _later(hass, freezer, CAR_START_AFTER_S + 30)
    assert halo.car_calls == ["turn_on"]
    assert controller.car_start_status is not None and controller.car_start_status["state"] == "refuses"
    assert "turn_off" not in halo.car_calls


async def test_a_car_that_still_refuses_is_told_once_and_its_end_comes_as_before(
    hass: HomeAssistant, freezer: Any
) -> None:
    controller, halo, told, car_id = await _refusing(hass, freezer, "teslemetry")

    await _later(hass, freezer, CAR_START_AFTER_S + 30)
    assert halo.car_calls == ["turn_on"]
    await _later(hass, freezer, CAR_START_AFTER_S - 30)
    assert controller.car_start_status is not None and controller.car_start_status["state"] == "started"
    await _later(hass, freezer, 60)
    status = controller.car_start_status
    assert status == {"state": "refuses", "soc_percent": 65.0, "target": 80.0}
    notice = controller.car_refuses_notice
    assert notice is not None and notice["soc_percent"] == 65.0 and notice["target"] == 80.0
    assert vehicle_charge_start.notice_told(hass, car_id, controller._car_start.plug_in)  # noqa: SLF001
    assert told == [], "the car's end counts from the start in the car"

    # The car's end of the plan's charge runs as before, five minutes after the start in the car.
    await _later(hass, freezer, PLAN_CAR_IDLE_S)
    assert told == [(car_id, False)]
    await _later(hass, freezer, 30 * 60)
    assert halo.car_calls == ["turn_on"], "once per plug-in"
    assert controller.car_refuses_notice == notice, "told once"
    assert "turn_off" not in halo.car_calls


@pytest.mark.parametrize(("level", "target", "limit"), [(79.0, 80.0, 90.0), (80.0, 90.0, 80.0), (100.0, 100.0, 100.0)])
async def test_a_car_at_its_target_or_its_own_limit_is_never_poked(
    hass: HomeAssistant, freezer: Any, level: float, target: float, limit: float
) -> None:
    controller, halo, _told, _car_id = await _refusing(hass, freezer, level=level, target=target, limit=limit)
    await _later(hass, freezer, 3 * CAR_START_AFTER_S)
    assert halo.car_calls == []
    assert controller.car_start_status is None and controller.car_refuses_notice is None


@pytest.mark.parametrize("status", ["suspended_evse", "awaiting_start"])
async def test_a_charger_that_holds_the_car_back_is_not_the_car_refusing(
    hass: HomeAssistant, freezer: Any, status: str
) -> None:
    controller, halo, _told, _car_id = await _refusing(hass, freezer)
    halo.status(status)
    await _later(hass, freezer, 3 * CAR_START_AFTER_S)
    assert halo.car_calls == [] and controller.car_start_status is None


async def test_load_balancing_holding_the_charge_back_is_not_the_car_refusing(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, halo, _told, _car_id = await _refusing(hass, freezer)
    monkeypatch.setattr(ChargingController, "balancing_stop_on_its_way", property(lambda _self: True))
    await _later(hass, freezer, 3 * CAR_START_AFTER_S)
    assert halo.car_calls == [] and controller.car_start_status is None


@pytest.mark.parametrize("platform", ["kia_uvo", None])
async def test_a_car_with_no_switch_is_told_at_once(hass: HomeAssistant, freezer: Any, platform: str | None) -> None:
    controller, halo, _told, _car_id = await _refusing(hass, freezer, platform)
    await _later(hass, freezer, CAR_START_AFTER_S + 30)
    assert halo.car_calls == []
    assert controller.car_start_status == {"state": "refuses", "soc_percent": 65.0, "target": 80.0}
    assert controller.car_refuses_notice is not None


async def test_an_unavailable_switch_is_told_at_once(hass: HomeAssistant, freezer: Any) -> None:
    controller, halo, _told, car_id = await _refusing(hass, freezer)
    switch = vehicle_charge_start.charge_switch(hass, car_id)
    assert switch is not None
    hass.states.async_set(switch.entity_id, "unavailable")
    await _later(hass, freezer, CAR_START_AFTER_S + 30)
    assert halo.car_calls == [] and controller.car_refuses_notice is not None


async def test_a_replug_within_half_an_hour_is_told_without_a_second_start(hass: HomeAssistant, freezer: Any) -> None:
    controller, halo, _told, _car_id = await _refusing(hass, freezer)
    await _later(hass, freezer, CAR_START_AFTER_S + 30)
    assert halo.car_calls == ["turn_on"]
    # Unplugged and plugged in again ten minutes later (a new plug-in for the controller).
    freezer.tick(timedelta(minutes=10))
    controller._session_generation += 1  # noqa: SLF001 - the unplug and the plug-in as the controller counts them
    controller._plugged_in_at = dt_util.utcnow()  # noqa: SLF001
    assert controller.car_start_status is None, "a new plug-in begins afresh"
    await _later(hass, freezer, CAR_START_AFTER_S + 30)
    assert halo.car_calls == ["turn_on"], "not within half an hour of the last start in the car"
    assert controller.car_start_status is not None and controller.car_start_status["state"] == "refuses"


async def test_a_restart_does_not_tell_the_notice_again(hass: HomeAssistant, freezer: Any) -> None:
    controller, halo, _told, car_id = await _refusing(hass, freezer, "kia_uvo")
    await _later(hass, freezer, CAR_START_AFTER_S + 30)
    assert controller.car_refuses_notice is not None
    # Home Assistant restarts within the plug-in: the controller's memory is gone, the stored notice is not.
    await _store(hass)
    controller._car_start = car_start.CarStartMemory()  # noqa: SLF001
    controller._car_refusal.reset()  # noqa: SLF001
    await _later(hass, freezer, CAR_START_AFTER_S + 30)
    assert controller.car_start_status is not None and controller.car_start_status["state"] == "refuses"
    assert controller.car_refuses_notice is None, "told before the restart"


# ---------------------------------------------------------------------------------------------- the notice


def test_the_notice_says_the_level_the_target_and_what_to_do() -> None:
    facts = {"reason": "car_refuses", "level_percent": 65.0, "target_percent": 80.0}
    assert compose("plan_stopped", "Hemma", facts, "sv")[1] == (
        "Bilen tar inte emot ström vid 65\u00a0% fast målet är 80\u00a0%. Starta laddningen i bilens app."
    )
    assert compose("plan_stopped", "Home", facts, "en")[1] == (
        "The car is not taking current at 65% though the target is 80%. Start the charge in the car's app."
    )
    for language in languages():
        with_level = compose("plan_stopped", "G", facts, language)[1]
        without = compose("plan_stopped", "G", {"reason": "car_refuses"}, language)[1]
        assert "65" in with_level and "80" in with_level, language
        assert without != compose("plan_stopped", "G", {"reason": "not_started"}, language)[1], language


@pytest.mark.usefixtures("offline_relay")
async def test_the_phones_hear_the_notice_once_per_plug_in(hass: HomeAssistant, freezer: Any) -> None:
    from custom_components.spotnav.runtime import charger_data

    from .test_notifications import _charger, _later as _tick

    freezer.move_to(T0)
    entry, calls = await _charger(hass)
    controller = charger_data(hass, entry.entry_id).controller
    memory = controller._car_start_memory()  # noqa: SLF001 - the refusal as the look decides it
    memory.level_percent, memory.target_percent = 65.0, 80.0
    controller._car_refuses(memory, dt_util.utcnow())  # noqa: SLF001
    await hass.async_block_till_done()
    assert [call.data["message"] for call in calls] == [
        "The car is not taking current at 65% though the target is 80%. Start the charge in the car's app."
    ]
    assert calls[0].data["data"]["tag"] == f"spotnav_{entry.entry_id}_plan_stopped"
    await _tick(hass, freezer, 60 * 60)
    controller._notify()  # noqa: SLF001
    await hass.async_block_till_done()
    assert len(calls) == 1, "once per plug-in"


def test_an_app_that_does_not_read_the_lines_gets_none_of_them() -> None:
    from custom_components.spotnav.api.webhook import _for_app

    status = {
        "tone": "notice",
        "lines": [
            {"code": "charging_now", "params": {"until": None}},
            {"code": "car_refuses", "params": {"soc_percent": 65.0, "target": 80.0}},
        ],
    }
    older = _for_app({"status": status}, {"reads": []})["status"]
    assert older == {"tone": "normal", "lines": [{"code": "charging_now", "params": {"until": None}}]}
    assert _for_app({"status": status}, {"reads": ["car_start"]})["status"] == status
