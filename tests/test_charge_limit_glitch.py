"""A car's charge limit that glitches mid-charge never ends a plan at the car's limit.

The night of 2026-11-22: a Kia EV6 at 92 % charging to a 93 % target, its AC limit 100 and its DC limit 80. At 05:50
the Kia cloud sent the AC limit as 0 (back at 100 later). The 0 was discarded, so the DC 80 became the ceiling, the
target was capped at it, the plan became one the car ends, and the top-off left the charge on until the car stopped
itself at 100 %. Now the AC limit stays the car's limit whatever it reads, an unreadable value keeps the last good
ceiling, and a ceiling that falls below the car's level during a charge is held for that charge.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.vehicles.soc_estimate import SocReader

from .test_top_off import CONFIG, Charger, _later, _plan
from .world import vehicle_device

LIMIT_ATTRIBUTES = {"unit_of_measurement": "%", "min": 50, "max": 100, "step": 10, "mode": "slider"}


def _ev6(hass: HomeAssistant, *, soc: str = "92", ac: str = "100", dc: str = "80") -> dict[str, Any]:
    vehicle = vehicle_device(
        hass,
        unique_id="ev6",
        name="EV6",
        limits=(("ev6_ac_charging_limit", ac), ("ev6_dc_charging_limit", dc)),
    )
    hass.states.async_set(vehicle["soc"], soc, {"device_class": "battery", "unit_of_measurement": "%"})
    return vehicle


def _reader(hass: HomeAssistant, charging: dict[str, bool] | None = None, controller: list[Any] | None = None) -> SocReader:
    def is_charging() -> bool:
        if controller:
            return bool(controller[0].charging)
        return bool(charging and charging["on"])

    return SocReader(
        hass,
        "entry_a",
        raw_reader=lambda _vehicle_id: None,
        register_entity_id=lambda: None,
        charge_control=lambda: None,
        remembered_capacity=lambda _vehicle_id: 77.4,
        charging=is_charging,

    )


def _set_limit(hass: HomeAssistant, reader: SocReader, vehicle: dict[str, Any], which: int, value: str) -> None:
    """The car's limit entity changes; the facts watch reads the car again (`_on_fact_changed`)."""
    hass.states.async_set(vehicle["limits"][which], value, LIMIT_ATTRIBUTES)
    reader._forget_facts(vehicle["device_id"])  # noqa: SLF001


async def test_an_ac_limit_glitching_to_zero_keeps_the_ceiling(hass: HomeAssistant) -> None:
    vehicle = _ev6(hass)
    reader = _reader(hass, {"on": True})
    assert reader.vehicle_max_percent(vehicle["device_id"]) == 100.0

    _set_limit(hass, reader, vehicle, 0, "0")
    assert reader.vehicle_max_percent(vehicle["device_id"]) == 100.0, "never the DC 80"

    _set_limit(hass, reader, vehicle, 0, "100")
    assert reader.vehicle_max_percent(vehicle["device_id"]) == 100.0


async def test_a_limit_falling_below_the_level_during_a_charge_is_held_for_that_charge(hass: HomeAssistant) -> None:
    vehicle = _ev6(hass)
    charging = {"on": True}
    reader = _reader(hass, charging)
    assert reader.vehicle_max_percent(vehicle["device_id"]) == 100.0

    _set_limit(hass, reader, vehicle, 0, "50")
    assert reader.vehicle_max_percent(vehicle["device_id"]) == 100.0, "a valid 50 under the car's 92 % mid-charge"
    _set_limit(hass, reader, vehicle, 0, "95")
    assert reader.vehicle_max_percent(vehicle["device_id"]) == 100.0, "held for the rest of the charge"

    charging["on"] = False
    _set_limit(hass, reader, vehicle, 0, "50")
    assert reader.vehicle_max_percent(vehicle["device_id"]) == 50.0, "the charge is over: the car's limit"


async def test_a_genuine_lowering_is_honoured(hass: HomeAssistant) -> None:
    vehicle = _ev6(hass, soc="60")
    charging = {"on": False}
    reader = _reader(hass, charging)
    assert reader.vehicle_max_percent(vehicle["device_id"]) == 100.0

    _set_limit(hass, reader, vehicle, 0, "80")
    assert reader.vehicle_max_percent(vehicle["device_id"]) == 80.0, "lowered while not charging"

    charging["on"] = True
    _set_limit(hass, reader, vehicle, 0, "70")
    assert reader.vehicle_max_percent(vehicle["device_id"]) == 70.0, "lowered mid-charge, still above the level"


async def test_a_car_with_only_a_dc_limit_reads_it(hass: HomeAssistant) -> None:
    vehicle = vehicle_device(hass, unique_id="dc_only", name="DC only", limits=(("dc_charging_limit", "80"),))
    reader = _reader(hass, {"on": False})
    assert reader.vehicle_max_percent(vehicle["device_id"]) == 80.0


async def test_the_ev6_night_stops_at_its_target_at_the_window_end(hass: HomeAssistant, freezer: Any) -> None:
    """AC 0 / DC 80 at 92 % with a 93 % target: SpotNav ends the charge at the window end, no top-off."""
    vehicle = _ev6(hass)
    charger = Charger(hass)
    holder: list[Any] = []
    reader = _reader(hass, controller=holder)
    controller = ChargingController(hass, "entry_a", dict(CONFIG), vehicle_limit_reader=reader.vehicle_max_percent)
    holder.append(controller)
    await controller.async_initialize()
    end = dt_util.utcnow() + timedelta(minutes=10)
    await controller.async_install(replace(_plan(end, target=93.0), vehicle_id=vehicle["device_id"]))
    await hass.async_block_till_done()
    assert charger.calls == ["turn_on"]
    charger.amps(10.0)
    await hass.async_block_till_done()
    assert not controller.charges_to_vehicle_limit()

    _set_limit(hass, reader, vehicle, 0, "0")
    assert reader.vehicle_max_percent(vehicle["device_id"]) == 100.0
    assert not controller.charges_to_vehicle_limit(), "still a plan SpotNav ends at 93 %"

    await _later(hass, freezer, 600)
    assert controller.top_off_until is None and controller.plan is None
    assert charger.stops == 1, "the window end stops the charge"
    await controller.async_shutdown()
