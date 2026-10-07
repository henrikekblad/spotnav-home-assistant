"""The sun's current reaches the charger on a site where load balancing does not write it.

The field case (2026-10-07, "Site Watty": Tibber Pulse phase currents, so a direct site, active control off, two
single-phase Easee chargers of unknown phase, a SolaX battery, `car_first`): the sun asked "Garage" for 10 A and then
9 A and `requested_current_a` became 9, but no current write followed and the car went on drawing 12.6 A. A
modulation was only ever recorded for site capacity's damped write, which runs only in active control on a derived
site. Solar now writes its own current wherever that pass does not, and leaves it alone wherever it does.

Every tick is an explicit site recompute at a hand-set instant (`tests.test_solar_execution`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_GRID_POWER_SOURCE,
    CONF_MEASURED_CURRENT_SOURCE,
)
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.execution.solar_execution import SOLAR_WRITE_INTERVAL_S
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.planning.auto_settings import STRATEGY_SOLAR
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.site.measurement_source import PhaseMeasurementSource, source_to_dict

from .charger_helpers import detected_config
from .charger_shapes import register_shape, SHAPES
from .helpers import make_entry, make_site_entry
from .test_mode_switch_handover import _garage, _sun, Easee
from .world import controller_of, SecondsClock, set_charger_delivered_a, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")

PHASES = ("L1", "L2", "L3")
TOTAL = "sensor.grid_total_w"
STATUS = "sensor.easee_status"
PREFIX = "garage"
VOLTAGE_V = 230.0


async def _watty(hass: HomeAssistant, *, amps: int | None = 16) -> tuple[Any, ...]:
    """The field site: an Easee on one phase of unknown phase, on a direct site (phase currents and the meter's
    total power) with active control off, the charger on `solar` with `amps` set. Returns
    `(charger, site, controller, coordinator, clock, easee)`."""
    ids = register_shape(hass, SHAPES["easee"])
    config = detected_config(detect_charger(hass, ids["device_id"]))
    hass.states.async_set(STATUS, "awaiting_start", {"config_authorizationRequired": False})
    charger = make_entry(
        hass,
        entry_id=PREFIX,
        charge_control=config["charge_control"] if "charge_control" in config else STATUS,
        current_limit=None,
        webhook_id="webhook-garage",
        title="Garage",
        extra=config,
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    easee = Easee(hass)
    store = domain_data(hass).auto_store
    await store.async_update(charger.entry_id, mutate=lambda s: replace(s, strategy=STRATEGY_SOLAR, amps=amps))
    clock = SecondsClock(0.0)
    coordinator = hass.config_entries.async_get_entry(charger.entry_id).runtime_data.solar
    coordinator._now = clock.now

    for phase in PHASES:
        hass.states.async_set(f"sensor.watty_{phase.lower()}", "3.0", {"unit_of_measurement": "A"})
    set_charger_delivered_a(hass, PREFIX, 0.0)
    _grid(hass, 0.0)
    site_entry = make_site_entry(
        hass,
        entry_id="watty",
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 1,
                "phase": None,
                "min_current_a": 6.0,
                CONF_MEASURED_CURRENT_SOURCE: source_to_dict(
                    PhaseMeasurementSource(
                        kind="separate_entities",
                        entity_ids={p: f"sensor.{PREFIX}_{p.lower()}" for p in PHASES},
                    )
                ),
            }
        },
        extra_data={CONF_GRID_POWER_SOURCE: {"power": TOTAL}},
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, site_entry.entry_id)
    for attr in ("_timer_cancel", "_state_listener_cancel"):
        cancel = getattr(site, attr)
        if cancel is not None:
            cancel()
            setattr(site, attr, None)
    controller = controller_of(hass, charger.entry_id)
    return charger, site, controller, coordinator, clock, easee


def _grid(hass: HomeAssistant, watts: float) -> None:
    """The meter's total grid power, import positive."""
    hass.states.async_set(TOTAL, str(watts), {"unit_of_measurement": "W"})


def _car(hass: HomeAssistant, amps: float) -> None:
    """The car's draw on its one phase (the charger reads the same on every phase of its sensor set)."""
    set_charger_delivered_a(hass, PREFIX, amps)


def _sun_for(hass: HomeAssistant, *, car_a: float, available_a: float) -> None:
    """The grid as it reads with the car at `car_a` and `available_a` of sun in all."""
    _car(hass, car_a)
    _grid(hass, -(available_a - car_a) * VOLTAGE_V)


async def _started(hass: HomeAssistant, site: Any, clock: SecondsClock, easee: Easee) -> None:
    """Armed and started by the sun at 7 A (the Easee's start minimum), then charging at 7 A."""
    _sun_for(hass, car_a=0.0, available_a=9.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert "resume" in easee.commands
    assert easee.limits == [7]
    await easee.status("charging")
    _sun_for(hass, car_a=7.0, available_a=9.0)
    clock.value = 130.0
    await tick_site(hass, site)


async def test_the_suns_current_is_written_on_a_direct_site_with_active_control_off(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, easee = await _watty(hass)
    await _started(hass, site, clock, easee)
    assert not site.config.get(CONF_ACTIVE_CONTROL_ENABLED)

    # Past the start's verification, 10.5 A of sun: the sun asks for 10 A, and 10 A reaches the charger.
    clock.value = 250.0
    _sun_for(hass, car_a=7.0, available_a=10.5)
    await tick_site(hass, site)

    assert controller.requested_current_a == 10
    assert easee.limits == [7, 10]
    assert coordinator.state is not None and coordinator.state.solar_current_writer == "solar"
    assert site.solar_surplus_snapshot[charger.entry_id]["solar_current_writer"] == "solar"
    dump = await async_get_config_entry_diagnostics(hass, hass.config_entries.async_get_entry("watty"))
    assert dump["solar_current_writer"] == {charger.entry_id: "solar"}


async def test_a_later_step_waits_out_the_write_interval_then_goes_out(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, easee = await _watty(hass)
    await _started(hass, site, clock, easee)
    clock.value = 250.0
    _sun_for(hass, car_a=7.0, available_a=10.5)
    await tick_site(hass, site)
    assert easee.limits[-1] == 10

    # The sun falls by one amp and stays there: the sun asks for 9 A once it lasted, at 266 s.
    _sun_for(hass, car_a=10.0, available_a=9.5)
    for at in (255.0, 266.0):
        clock.value = at
        await tick_site(hass, site)
    assert controller.requested_current_a == 9
    assert easee.limits[-1] == 10, "within the write interval of the last write"

    clock.value = 250.0 + SOLAR_WRITE_INTERVAL_S
    await tick_site(hass, site)
    assert easee.limits == [7, 10, 9]

    # Nothing changes: nothing more is written.
    for at in (300.0, 330.0, 360.0):
        clock.value = at
        await tick_site(hass, site)
    assert easee.limits == [7, 10, 9]


async def test_the_written_current_never_exceeds_the_amps_set(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, easee = await _watty(hass, amps=8)
    await _started(hass, site, clock, easee)

    clock.value = 250.0
    _sun_for(hass, car_a=7.0, available_a=14.5)
    await tick_site(hass, site)

    assert controller.requested_current_a == 14
    assert easee.limits == [7, 8]


async def test_no_write_while_the_charger_is_paused(hass: HomeAssistant) -> None:
    """A charger that is paused takes no current (a limit above 0 would lift an Easee's pause)."""
    charger, site, controller, coordinator, clock, easee = await _watty(hass)
    await _started(hass, site, clock, easee)
    await easee.status("paused")
    clock.value = 250.0
    _sun_for(hass, car_a=7.0, available_a=12.5)
    await tick_site(hass, site)

    assert easee.limits == [7]


async def test_active_control_on_a_derived_site_stays_the_only_writer(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    async def spy(self: ChargingController, amps: int, *, cap_a: int) -> str:
        calls.append(amps)
        return "assigned"

    monkeypatch.setattr(ChargingController, "async_write_solar_current", spy)
    charger, site, controller, coordinator, clock, easee = await _garage(hass, STRATEGY_SOLAR)
    store = domain_data(hass).auto_store
    await store.async_update(charger.entry_id, mutate=lambda s: replace(s, amps=16))
    assert site.active_control_writes(charger.entry_id) is False, "active control is off"
    site.config[CONF_ACTIVE_CONTROL_ENABLED] = True
    assert site.active_control_writes(charger.entry_id) is True

    _sun(hass, export_w=2000.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    await easee.status("charging")
    set_charger_delivered_a(hass, charger.entry_id, 7.0)
    _sun(hass, export_w=3.0 * VOLTAGE_V)
    clock.value = 300.0
    await tick_site(hass, site)

    assert controller.requested_current_a == 10
    assert coordinator.state is not None and coordinator.state.solar_current_writer == "active_control"
    assert calls == []


async def test_a_derived_site_with_active_control_off_has_the_sun_write_its_current(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, easee = await _garage(hass, STRATEGY_SOLAR)
    store = domain_data(hass).auto_store
    await store.async_update(charger.entry_id, mutate=lambda s: replace(s, amps=16))
    _sun(hass, export_w=2000.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    await easee.status("charging")
    set_charger_delivered_a(hass, charger.entry_id, 7.0)
    _sun(hass, export_w=3.0 * VOLTAGE_V)
    clock.value = 300.0
    await tick_site(hass, site)

    assert controller.requested_current_a == 10
    assert coordinator.state is not None and coordinator.state.solar_current_writer == "solar"
    assert easee.limits[-1] == 10


async def test_the_controllers_own_write_holds_the_floor_the_cap_and_a_stop_on_its_way(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, easee = await _watty(hass)
    await _started(hass, site, clock, easee)

    assert await controller.async_write_solar_current(12, cap_a=10) == "assigned"
    assert easee.limits == [7, 10], "never above the amps set"
    assert await controller.async_write_solar_current(12, cap_a=5) == "below_minimum"
    controller._stop_in_flight = True  # noqa: SLF001 - a stop on its way is the fact under test
    assert await controller.async_write_solar_current(9, cap_a=16) == "stop_in_flight"
    controller._stop_in_flight = False  # noqa: SLF001
    assert easee.limits == [7, 10]
