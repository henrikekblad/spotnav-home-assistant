"""Solar's ceiling (`execution/solar_execution.py`, `_solar_ceiling_a`): the charger's own maximum
(`ChargingController.current_range`), no more than the amps set for it, and bounded by the fuse as before. Not a fixed
16 A.

Every tick is an explicit site recompute at a hand-set instant (`tests.test_solar_execution`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CURRENT_LIMIT, CONF_MAIN_FUSE_A
from custom_components.spotnav.runtime import domain_data

from .test_solar_phase_execution import _switched_down
from .test_solar_current_writer import _started, _sun_for, _watty
from .world import PHASES, controller_of, solar_setup, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay", "chargers_not_switch_only")

CHARGER = "solar_charger"
SWITCH = f"switch.{CHARGER}"
LIMIT = f"number.{CHARGER}_current"
VOLTAGE_V = 230.0


class _Site:
    """One solar charger whose current is set through a number entity stating `max_a`, on `phases` (L1 alone on
    one), on a derived site with `main_fuse_a`."""

    def __init__(self, hass: HomeAssistant, world: tuple[Any, ...], phases: int) -> None:
        self.hass = hass
        charger, site_entry, controller, coordinator, clock, turn_on, _turn_off = world
        self.charger = charger
        self.controller = controller
        self.coordinator = coordinator
        self.clock = clock
        self.turn_on = turn_on
        self.site = controller_of(hass, site_entry.entry_id)
        self.phases = phases
        self.sun_w = 0.0
        self.amps = 0.0
        self.t = 0.0
        self.requested: list[float | None] = []

    def draw(self, amps: float, sun_w: float | None = None) -> None:
        """The car draws `amps` on its phases; the grid carries the rest of the sun, spread over the three."""
        self.amps = amps
        if sun_w is not None:
            self.sun_w = sun_w
        for phase in PHASES:
            on_phase = amps if (self.phases == 3 or phase == "L1") else 0.0
            self.hass.states.async_set(
                f"sensor.{CHARGER}_{phase.lower()}", str(on_phase), {"unit_of_measurement": "A"}, force_update=True
            )
            self.hass.states.async_set(
                f"sensor.solar_site_power_{phase.lower()}",
                str(on_phase * VOLTAGE_V - self.sun_w / 3),
                {"unit_of_measurement": "W"},
                force_update=True,
            )

    async def at(self, t: float) -> None:
        self.t = t
        self.clock.value = t
        self.draw(self.amps)
        await tick_site(self.hass, self.site)
        state = self.coordinator.state
        self.requested.append(None if state is None else state.requested_a)

    async def run(self, until: float, step: float = 10.0) -> None:
        t = self.t + step
        while t <= until + 1e-9:
            await self.at(t)
            t += step

    async def charge_on_sun(self, sun_w: float) -> None:
        """Started by the sun at its minimum, then the car follows every current the sun asks for."""
        self.draw(0.0, sun_w=sun_w)
        await self.at(0.0)
        await self.at(125.0)
        assert self.coordinator.state.action == "start"
        self.hass.states.async_set(SWITCH, "on")
        self.draw(6.0)
        t = 125.0
        while t < 900.0:
            t += 10.0
            await self.at(t)
            requested = self.coordinator.state.requested_a
            if requested is not None:
                self.draw(requested)

    def ceiling(self) -> float:
        return self.coordinator._solar.config.max_current_a  # noqa: SLF001


def _limit_entity(hass: HomeAssistant, max_a: float | None) -> None:
    attributes: dict[str, Any] = {"unit_of_measurement": "A", "min": 6}
    if max_a is not None:
        attributes["max"] = max_a
    hass.states.async_set(LIMIT, "6", attributes)


async def _site(
    hass: HomeAssistant,
    *,
    phases: int,
    max_a: float | None,
    amps: int | None = None,
    main_fuse_a: float = 63.0,
) -> _Site:
    _limit_entity(hass, max_a)
    world = await solar_setup(
        hass,
        main_fuse_a=main_fuse_a,
        charger_config={CONF_CHARGE_CONTROL: SWITCH, CONF_CURRENT_LIMIT: LIMIT},
        phase="L1" if phases == 1 else None,
    )
    async_mock_service(hass, "number", "set_value")
    charger = world[0]
    store = domain_data(hass).auto_store
    await store.async_update(charger.entry_id, mutate=lambda s: replace(s, amps=amps))
    return _Site(hass, world, phases)


async def test_a_32_a_single_phase_charger_takes_6_kw_of_sun_whole(hass: HomeAssistant) -> None:
    s = await _site(hass, phases=1, max_a=32)
    await s.charge_on_sun(6000.0)
    assert s.ceiling() == 32.0
    # 6 000 W / 230 V on one phase: 26 A, where a fixed 16 A ceiling left 2.3 kW of it to the grid.
    assert s.controller.requested_current_a == 26
    assert s.coordinator.state.state == "on"


async def test_a_16_a_charger_is_still_asked_for_no_more_than_16_a(hass: HomeAssistant) -> None:
    s = await _site(hass, phases=1, max_a=16)
    await s.charge_on_sun(6000.0)
    assert s.ceiling() == 16.0
    assert s.controller.requested_current_a == 16


async def test_a_32_a_three_phase_charger_takes_up_to_32_a(hass: HomeAssistant) -> None:
    s = await _site(hass, phases=3, max_a=32)
    # 15 kW over three phases: 21 A, past the old 16 A.
    await s.charge_on_sun(15_000.0)
    assert s.controller.requested_current_a == 21
    # 25 kW would carry 36 A: the charger's own 32 A is the most asked for.
    s.draw(s.amps, sun_w=25_000.0)
    await s.run(s.t + 100.0)
    for _ in range(20):
        await s.at(s.t + 10.0)
        requested = s.coordinator.state.requested_a
        if requested is not None:
            s.draw(requested)
    assert s.controller.requested_current_a == 32


async def test_the_amps_set_bound_the_sun_as_they_bound_a_plan_charge(hass: HomeAssistant) -> None:
    s = await _site(hass, phases=1, max_a=32, amps=20)
    await s.charge_on_sun(6000.0)
    assert s.ceiling() == 20.0
    assert s.controller.requested_current_a == 20


async def test_the_fuse_still_bounds_the_sun(hass: HomeAssistant) -> None:
    """A charger stating no maximum (32 A), 32 A set, on a direct site whose phase carries 3 A besides the car: 26 A of
    sun, but a 16 A fuse less its 1 A margin leaves the car 7 A drawn now plus 12 A of headroom, 19 A in all."""
    charger, site, controller, coordinator, clock, easee = await _watty(hass, amps=32)
    site.config[CONF_MAIN_FUSE_A] = 16.0
    await _started(hass, site, clock, easee)
    for at in (250.0, 261.0, 300.0):
        clock.value = at
        _sun_for(hass, car_a=7.0, available_a=26.5)
        await tick_site(hass, site)
    assert coordinator._solar.config.max_current_a == 32.0  # noqa: SLF001
    assert controller.requested_current_a == 19
    assert easee.limits == [7, 19]


async def test_the_ceiling_follows_the_chargers_stated_maximum_and_falls_back_with_it(hass: HomeAssistant) -> None:
    s = await _site(hass, phases=1, max_a=16)
    await s.charge_on_sun(6000.0)
    assert s.controller.requested_current_a == 16
    # The charger's entity now states 32 A (a profile changed, say): the running charge follows it.
    _limit_entity(hass, 32)
    await s.run(s.t + 30.0)
    assert s.ceiling() == 32.0
    assert s.controller.requested_current_a == 26
    # The entity goes unavailable and states nothing: the everyday default `current_range` falls back to.
    hass.states.async_set(LIMIT, "unavailable", {})
    await s.at(s.t + 10.0)
    assert s.ceiling() == float(s.controller.current_range()["max_a"]) == 32.0
    _limit_entity(hass, 20)
    for _ in range(3):
        await s.at(s.t + 10.0)
        requested = s.coordinator.state.requested_a
        if requested is not None:
            s.draw(requested)
    assert s.ceiling() == 20.0
    assert s.controller.requested_current_a == 20


async def test_after_a_switch_to_one_phase_the_chargers_own_maximum_applies(hass: HomeAssistant, freezer: Any) -> None:
    w = await _switched_down(hass, freezer)
    switched_at = w.t
    await w.charger_takes(1)
    w.draw(13.0)
    await w.run(switched_at + 60.0)
    assert w.controller.switched_phases == 1
    # 4.4 kW: 19 A on one phase, short of what carries three phases again. A charger stating no maximum: 32 A.
    w.draw(13.0, sun_w=4400.0)
    for _ in range(20):
        await w.at(w.t + 10.0)
        requested = w.coordinator.state.requested_a
        if requested is not None:
            w.draw(requested)
    assert w.controller.switched_phases == 1
    assert w.coordinator._solar.config.max_current_a == 32.0  # noqa: SLF001
    assert w.controller.requested_current_a == 19
