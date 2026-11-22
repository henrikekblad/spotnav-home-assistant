"""The shed order through the real site (`site/shed_order.py` run by `ImportTargetHold.hold`), and the heat control's
and the zones' step for the ceiling (`heat/control.py`, `heat/zones_control.py`). Nothing reaches a device: the
chargers, the relay and the thermostats are fakes."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.heat.relay_control import MAX_DAY, MAX_RUN
from custom_components.spotnav.site.shed_order import LOAD_HEAT, LOAD_WATER_HEATER, Offer, Step

from .test_heat_control import _plan, _running, H, MIN, ROOM
from .test_import_target_hold import _charging, _HOUR, _V, _world

HEATER_W = 1500.0


def _heater(world: Any, heater_w: float = HEATER_W) -> list[bool]:
    """A water heater (1.5 kW unless said) in the world's house, switched by the shed order: the meter reads it while it is not
    paused for the ceiling. Returns the record of its states, one per step."""
    controller = world.halo.controller
    base_a = world.house_a
    seen: list[bool] = []

    def offer() -> Offer:
        on = controller.shed_order.until(LOAD_WATER_HEATER, dt_util.utcnow()) is None
        return Offer(live=True, ready=True, give_w=heater_w if on else 0.0, drawing=on, held=not on)

    controller.water_heater_control.shed_offer = offer

    original = world.show

    def show() -> None:
        on = controller.shed_order.until(LOAD_WATER_HEATER, dt_util.utcnow()) is None
        world.house_a = base_a + (heater_w / (3 * _V) if on else 0.0)
        if not seen or seen[-1] != on:
            seen.append(on)
        original()

    world.show = show
    return seen


async def test_the_water_heater_pauses_before_the_car_is_cut_further(hass: HomeAssistant, monkeypatch) -> None:
    """An 8 kW target (7.84 held), the house at 1 kW plus a 1.5 kW water heater, a 16 A car: the hold holds the car at
    7 A; once the order may act (`REST_S` after the start) the heater gives way and the car takes what remains (9 A or more),
    never paused, every hour under the target."""
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "shed", target_kw=8.0)
        heater = _heater(world)
        await world.step()
        await _charging(world)
        await world.run(10)
        assert world.car_a == 7.0 and heater == [True]
        await world.run(120)
        controller = world.halo.controller
        assert world.turn_offs == 0, "the car was paused"
        assert world.car_a >= 9.0
        assert heater == [True, False]
        snapshot = controller.shed_order.snapshot()
        assert snapshot["steps"][LOAD_WATER_HEATER]["state"] == "paused" and snapshot["steps"][LOAD_WATER_HEATER]["taken"]
        assert LOAD_HEAT not in snapshot["steps"]
        # In the site's change log too.
        log = controller.import_target_snapshot()["log"]
        assert [entry["load"] for entry in log if entry["action"] == "shed_order_shed"] == [LOAD_WATER_HEATER]
        for hour in world.complete_hours():
            assert world.hour_kw(hour) <= 8.0, f"{hour}: {world.hour_kw(hour):.2f} kW"


async def test_a_car_the_hold_would_pause_keeps_charging_and_the_water_heater_pauses(hass: HomeAssistant, monkeypatch) -> None:
    """A 6 kW target (5.88 held), the house at 1 kW plus a 3 kW metered water heater, a 16 A car: the hold alone would
    pause the car for the hour (5.88 - 4 kW is below its minimum); the heater goes at once instead, without the enter
    delay, and the car charges on what remains, every hour under the target."""
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "shed6")
        heater = _heater(world, 3000.0)
        # The order's rest after a start (`REST_S`) has passed before the car starts.
        await world.run(16)
        await _charging(world)
        await world.run(120)
        controller = world.halo.controller
        assert world.turn_offs == 0, "the car was paused"
        assert world.car_a >= 6.0
        assert heater == [True, False]
        assert controller.shed_order.steps[LOAD_WATER_HEATER].taken
        for hour in world.complete_hours():
            assert world.hour_kw(hour) <= 6.0, f"{hour}: {world.hour_kw(hour):.2f} kW"


async def test_a_passing_loss_of_the_budget_keeps_the_heater_paused_and_load_balancing_off_ends_it(
    hass: HomeAssistant, monkeypatch
) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "blip", target_kw=8.0)
        heater = _heater(world)
        await world.step()
        await _charging(world)
        await world.run(30)
        assert heater == [True, False]
        # The house unknown for a minute: no budget for that while (a passing reason); the heater stays paused.
        from custom_components.spotnav.site import import_target_hold

        real = import_target_hold.hour_budget
        monkeypatch.setattr(import_target_hold, "hour_budget", lambda **_kwargs: (None, "house_unknown"))
        await world.run(1)
        assert world.halo.controller._target_not_applied == "house_unknown"  # noqa: SLF001
        assert heater == [True, False]
        monkeypatch.setattr(import_target_hold, "hour_budget", real)
        await world.run(2)
        assert LOAD_WATER_HEATER in world.halo.controller.shed_order.steps
        # Load balancing off: put back at once.
        monkeypatch.setattr(world.halo.controller, "_active_control_allowed", lambda: False)
        await world.step()
        assert world.halo.controller.shed_order.steps == {}


async def test_without_a_target_nothing_is_shed(hass: HomeAssistant, monkeypatch) -> None:
    """The fuse-only case: no import target, no power level: nothing paused, whatever the house draws."""
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "fuse_only", target_kw=None, house_w=6000.0)
        heater = _heater(world)
        await world.step()
        await _charging(world)
        await world.run(10)
        assert heater == [True]
        assert world.halo.controller.shed_order.steps == {}
        assert world.halo.controller.shed_order.snapshot()["projection"] is None


async def test_a_watched_water_heater_only_would_and_the_car_is_held_as_before(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        # The house alone over the target: over with or without the car.
        world = await _world(hass, monkeypatch, frozen, "watched", house_w=7000.0)
        controller = world.halo.controller
        controller.water_heater_control.shed_offer = lambda: Offer(live=False, ready=True, give_w=HEATER_W)
        await world.step()
        await _charging(world)
        await world.run(20)
        step = controller.shed_order.steps[LOAD_WATER_HEATER]
        assert not step.live
        assert controller.shed_order.until(LOAD_WATER_HEATER, dt_util.utcnow()) is None
        # Nothing counted for the car: paused for the hour as without the order.
        assert world.turn_offs == 1


# -- the heat step through the heat control's own rules


def _shed_heat(control: Any, now: Any, minutes: int = 60) -> None:
    order = control.site.shed_order
    order.steps[LOAD_HEAT] = Step(LOAD_HEAT, True, now, now + minutes * MIN, 1500.0, now.replace(minute=0))


async def test_heat_is_lowered_for_the_ceiling_by_its_own_rules(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer)
    await control.async_look(now)
    offer = control.offer
    assert offer is not None and offer.live and offer.ready
    _shed_heat(control, now)
    await control.async_look(now + MIN)
    assert relay.calls == ["on"] and control.held
    status = control.status()
    assert (status["reason"], status["until"]) == ("ceiling", (now + 60 * MIN).isoformat())
    # Its offer says it holds the step (the order counts the step as taken).
    await control.async_look(now + 2 * MIN)
    assert control.offer is not None and control.offer.held
    # Put back after 20 minutes: held to its least lowering (45 minutes), then opened.
    del control.site.shed_order.steps[LOAD_HEAT]
    await control.async_look(now + 20 * MIN)
    assert relay.calls == ["on"] and status["lowered"]
    await control.async_look(now + 47 * MIN)
    assert relay.calls == ["on", "off"]


async def test_a_cold_room_ends_the_heat_step_at_once(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer)
    await control.async_look(now)
    _shed_heat(control, now)
    await control.async_look(now + MIN)
    assert control.held
    hass.states.async_set(ROOM, "20.3", {"unit_of_measurement": "°C"})
    await control.async_look(now + 5 * MIN)
    assert relay.calls == ["on", "off"]
    assert control.offer is not None and not control.offer.ready and control.offer.why == "room_cold"


async def test_the_heat_step_keeps_three_hours_in_a_row(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer)
    await control.async_look(now)
    _shed_heat(control, now, minutes=10 * 60)
    await control.async_look(now + MIN)
    assert control.held
    _plan(control, now + MIN + MAX_RUN)
    await control.async_look(now + MIN + MAX_RUN)
    assert relay.calls == ["on", "off"] and control.status()["reason"] == "max_run"


async def test_no_heat_step_past_the_day_s_six_hours(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer)
    control.lowerings = [[now - 2 * H - MAX_DAY, now - 2 * H]]
    control.last_end = now - 2 * H
    await control.async_look(now)
    assert control.offer is not None and not control.offer.ready
    _shed_heat(control, now)
    await control.async_look(now + MIN)
    assert relay.calls == []


async def test_a_watched_heat_pump_is_offered_as_would_and_never_written(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, control_enabled=False)
    await control.async_look(now)
    assert control.offer is not None and not control.offer.live
    _shed_heat(control, now)
    await control.async_look(now + MIN)
    assert relay.calls == []


async def test_a_zone_is_lowered_one_degree_for_the_ceiling(hass: HomeAssistant, freezer) -> None:
    from .test_heat_zones_control import _running as _zones_running, FLOOR  # noqa: PLC0415

    _entry, zones, thermostat, now = await _zones_running(hass, freezer, {})
    await zones.async_look(now)
    assert thermostat.calls == []
    offer = zones.offers.get(FLOOR)
    assert offer is not None and offer.live and offer.ready
    zones.site.shed_order.steps[LOAD_HEAT] = Step(LOAD_HEAT, True, now, now + 60 * MIN, 500.0, now.replace(minute=0))
    await zones.async_look(now + MIN)
    assert thermostat.calls == [21.0]
    # Written, not read back yet: being taken, not held.
    assert zones.offers[FLOOR].pending and not zones.offers[FLOOR].held
    await zones.async_look(now + 2 * MIN)
    status = zones.status(FLOOR)
    assert status is not None and status["reason"] == "ceiling" and status["held_c"] == 21.0
    assert zones.offers[FLOOR].held
    # A cold zone is never lowered for the ceiling.
    del zones.site.shed_order.steps[LOAD_HEAT]
    thermostat.attributes = {**thermostat.attributes, "current_temperature": 21.0}
    thermostat.show()
    await zones.async_look(now + 3 * H)
    await zones.async_look(now + 3 * H + MIN)
    offer = zones.offers.get(FLOOR)
    assert offer is not None and not offer.ready and offer.why == "zone_cold"


def test_the_flexible_loads_say_paused_for_the_ceiling_blocked_or_would() -> None:
    from types import SimpleNamespace

    from custom_components.spotnav.planning.flexible_load import FlexibleLoad, Goal
    from custom_components.spotnav.site.flexible_loads import with_states
    from custom_components.spotnav.site.shed_order import ShedOrder

    order = ShedOrder()
    start = _HOUR
    order.steps[LOAD_WATER_HEATER] = Step(LOAD_WATER_HEATER, True, start, start + H, 3000.0, start, taken=True)
    order.blocked[LOAD_HEAT] = "room_cold"
    site = SimpleNamespace(
        shed_order=order,
        water_heater_control=SimpleNamespace(held_off=True),
        heat_control=SimpleNamespace(held=False),
        zones_control=SimpleNamespace(zones={}),
        _target_paused_hour={"car1": start},
        _target_budget=SimpleNamespace(hour_start=start),
    )
    loads = [
        FlexibleLoad(id="switch.vvb", kind="water_heater", site="s", goal=Goal("runtime_per_day"), controlled=True),
        FlexibleLoad(id="switch.vp", kind="heat", site="s", goal=Goal("comfort_band"), controlled=True),
        FlexibleLoad(id="car1", kind="car", site="s", goal=Goal("energy_by"), controlled=True),
        FlexibleLoad(id="climate.hall", kind="heat_zone", site="s", goal=Goal("comfort_band"), controlled=False),
    ]
    found = {load.id: (load.state, load.state_reason) for load in with_states(site, loads)}
    assert found == {
        "switch.vvb": ("paused", "ceiling"),
        "switch.vp": ("blocked", "room_cold"),
        "car1": ("paused", "ceiling"),
        "climate.hall": ("watched", None),
    }
    # A watched water heater the order would pause: watched, for the ceiling.
    order.steps[LOAD_WATER_HEATER] = Step(LOAD_WATER_HEATER, False, start, start + H, 3000.0, start)
    watched = replace(loads[0], controlled=False)
    assert [(load.state, load.state_reason) for load in with_states(site, [watched])] == [("watched", "ceiling")]


def test_a_car_counts_at_what_its_plan_needs_not_at_what_the_fuse_would_give() -> None:
    """A car the fuse would let draw 11 kW whose plan needs 3.7 kW now is over by nothing in a 6 kW hour with a 1 kW
    house: no reason to pause the water heater."""
    from custom_components.spotnav.site.import_target import HourBudget, TargetMember
    from custom_components.spotnav.site.import_target_hold import _projection

    budget = HourBudget(
        target_kw=6.0, hour_start=_HOUR, hour_end=_HOUR + H, so_far_kwh=0.0, hour_kw=0.0, remaining_s=3000.0,
        house_w=1000.0, rate_w=5880.0, allowed_w=4880.0, house_over=False, hold_kw=5.88,
    )
    car = TargetMember(charger_entry_id="garage", phases=3, min_a=6.0, wants_a=16.0)
    assert _projection(budget, [car]).excess_w > 0
    assert _projection(budget, [car], {"garage": 3700.0}).excess_w < 0
    # A plan asking more than the fuse gives counts at the fuse's.
    assert _projection(budget, [car], {"garage": 20000.0}).cars_held_w == 16.0 * 3 * _V


def test_the_plan_view_shows_only_what_the_controls_hold() -> None:
    from types import SimpleNamespace

    from custom_components.spotnav.api.plan import _shed
    from custom_components.spotnav.site.shed_order import ShedOrder

    order = ShedOrder()
    start = _HOUR
    order.steps[LOAD_WATER_HEATER] = Step(LOAD_WATER_HEATER, True, start, start + H, 1500.0, start)
    order.steps[LOAD_HEAT] = Step(LOAD_HEAT, True, start, start + H, 1500.0, start, taken=True)
    controller = SimpleNamespace(
        shed_order=order, water_heater_control=SimpleNamespace(held_off=False), heat_control=SimpleNamespace(held=False)
    )
    # Steps not held by their controls: nothing shown.
    assert _shed(controller, LOAD_WATER_HEATER) is None
    assert _shed(controller, LOAD_HEAT) is None
    order.steps[LOAD_WATER_HEATER].taken = True
    controller.water_heater_control.held_off = True
    controller.heat_control.held = True
    assert _shed(controller, LOAD_WATER_HEATER) == (start, start + H)
    assert _shed(controller, LOAD_HEAT) == (start, start + H)
    # A watched load's step is shown as would.
    order.steps[LOAD_WATER_HEATER] = Step(LOAD_WATER_HEATER, False, start, start + H, 1500.0, start, taken=True)
    controller.water_heater_control.held_off = False
    assert _shed(controller, LOAD_WATER_HEATER) == (start, start + H)


async def test_a_relay_slow_to_read_back_says_the_step_is_being_taken(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer)
    await control.async_look(now)
    relay.ignore = True
    _shed_heat(control, now)
    await control.async_look(now + MIN)
    assert relay.calls == ["on"] and control.pending is not None
    assert control.offer is not None and control.offer.pending and not control.offer.held
    relay.ignore = False
    relay.hass.states.async_set(relay.entity_id, "on")
    await control.async_look(now + 2 * MIN)
    await control.async_look(now + 3 * MIN)
    assert control.held and control.offer is not None and control.offer.held
