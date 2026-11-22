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

HEATER_W = 3000.0


def _heater(world: Any) -> list[bool]:
    """A water heater of 3 kW in the world's house, switched by the shed order: the meter reads it while it is not
    paused for the ceiling. Returns the record of its states, one per step."""
    controller = world.halo.controller
    base_a = world.house_a
    seen: list[bool] = []

    def offer() -> Offer:
        on = controller.shed_order.until(LOAD_WATER_HEATER, dt_util.utcnow()) is None
        return Offer(live=True, ready=True, give_w=HEATER_W if on else 0.0, drawing=on)

    controller.water_heater_control.shed_offer = offer

    original = world.show

    def show() -> None:
        on = controller.shed_order.until(LOAD_WATER_HEATER, dt_util.utcnow()) is None
        world.house_a = base_a + (HEATER_W / (3 * _V) if on else 0.0)
        if not seen or seen[-1] != on:
            seen.append(on)
        original()

    world.show = show
    return seen


async def test_the_water_heater_pauses_before_the_car_is_cut(hass: HomeAssistant, monkeypatch) -> None:
    """A 6 kW target, the house at 1 kW plus a 3 kW water heater, a 16 A car: without the order the car would be paused
    for the hour (5.88 - 4 kW leaves less than its minimum); with it the heater gives way first and the car takes what
    remains, every hour under the target."""
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "shed")
        heater = _heater(world)
        await world.step()
        await _charging(world)
        await world.run(130)
        controller = world.halo.controller
        assert world.turn_offs == 0, "the car was paused"
        assert world.car_a == 7.0
        assert heater == [True, False]
        snapshot = controller.shed_order.snapshot()
        assert snapshot["steps"][LOAD_WATER_HEATER]["state"] == "paused"
        assert LOAD_HEAT not in snapshot["steps"]
        # In the site's change log too.
        log = controller.import_target_snapshot()["log"]
        assert [entry["load"] for entry in log if entry["action"] == "shed_order_shed"] == [LOAD_WATER_HEATER]
        for hour in world.complete_hours():
            assert world.hour_kw(hour) <= 6.0, f"{hour}: {world.hour_kw(hour):.2f} kW"


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
        await world.run(10)
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
    await zones.async_look(now + 2 * MIN)
    status = zones.status(FLOOR)
    assert status is not None and status["reason"] == "ceiling" and status["held_c"] == 21.0
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
    order.steps[LOAD_WATER_HEATER] = Step(LOAD_WATER_HEATER, True, start, start + H, 3000.0, start)
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
