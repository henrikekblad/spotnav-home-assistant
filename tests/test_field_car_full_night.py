"""Field night of 2026-10-09/10, replayed through the real charger entry: the EV6 full at its limit, SpotNav done.

A HALO (16 A, three phases) and a Kia EV6 (77.4 kWh) at 72 % by its cloud, which said nothing new all night; a second
car at the same charger; a target at the EV6's own 100 % limit. From 23:00 the plan's windows charged 24.26 kWh; at
02:24 the car dropped to 0.57 A and from 02:35 the connector said `SuspendedEV`. SpotNav planned a new window every
half hour until the departure at 06:00 and then showed an execution error.

Now: the estimate from the delivered energy survives the card listing both cars and reads the car's 100 %; five
minutes after the car stopped drawing the plan ends as reached, the car is asked once for a fresh reading, and the
planner plans nothing more.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.api.dashboard import capture_soc, capture_vehicles
from custom_components.spotnav.execution.controller import ChargingPlan
from custom_components.spotnav.execution.plan_car_ended import PLAN_CAR_IDLE_S
from custom_components.spotnav.planning.auto_settings import TargetSocIntent
from custom_components.spotnav.runtime import charger_data, domain_data
from custom_components.spotnav.vehicles.vehicle_refresh import limiter_for

from .world import add_car, charger_and_car, controller_of, preview_for, REGISTER, settings_of

pytestmark = pytest.mark.usefixtures("offline_relay")

ATTRIBUTES = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}
CURRENT = "sensor.halo_current_import"


class Night:
    def __init__(self, hass: HomeAssistant, freezer: Any) -> None:
        self.hass, self.freezer = hass, freezer
        self.switch_calls: list[str] = []
        self.refreshed: list[list[str]] = []

    async def build(self) -> None:
        hass = self.hass
        charger, ev6, soc = await charger_and_car(hass, capacity=77.4, soc_percent="72")
        self.charger, self.ev6, self.soc_entity = charger, ev6, soc
        add_car(hass, "testbil", percent="55")
        self.register(6821.568)
        await domain_data(hass).auto_store.async_update(
            charger.entry_id, mutate=lambda s: replace(s, target=TargetSocIntent(vehicle_id=ev6, target_percent=100))
        )
        self.controller = controller_of(hass, charger.entry_id)
        # The HALO's current import, read as the charger's measured current.
        self.controller.adapter.current_entity_ids = (CURRENT,)
        self.amps(0.0)

        async def switch(call: ServiceCall) -> None:
            self.switch_calls.append(call.service)
            hass.states.async_set("switch.wallbox", "on" if call.service == "turn_on" else "off")

        for domain in ("switch", "homeassistant"):
            hass.services.async_register(domain, "turn_on", switch)
            hass.services.async_register(domain, "turn_off", switch)

        async def refresh(call: ServiceCall) -> None:
            # The Kia's cloud answers with what it had: the same 72 %.
            self.refreshed.append(list(call.data["entity_id"]))

        hass.services.async_register("homeassistant", "update_entity", refresh)
        limiter_for(hass).clock = lambda: dt_util.utcnow().timestamp()
        # The plug-in reading anchors at the register as it was.
        reading = charger_data(hass, charger.entry_id).soc_reader.read(ev6)
        assert reading is not None and reading.soc_percent == 72.0
        await hass.async_block_till_done()

    def register(self, kwh: float) -> None:
        self.hass.states.async_set(REGISTER, f"{kwh:.3f}", ATTRIBUTES)

    def amps(self, value: float) -> None:
        self.hass.states.async_set(CURRENT, str(value), {"unit_of_measurement": "A"})

    async def later(self, seconds: float, step: float = 30.0) -> None:
        left = seconds
        while left > 0:
            moved = min(step, left)
            self.freezer.tick(timedelta(seconds=moved))
            async_fire_time_changed(self.hass, dt_util.utcnow())
            await self.hass.async_block_till_done()
            left -= moved

    def show_the_card(self) -> None:
        settings = settings_of(self.hass, self.charger.entry_id)
        capture_vehicles(self.hass, self.charger.entry_id, settings)
        capture_soc(self.hass, self.charger.entry_id, settings)


async def test_the_car_full_at_its_limit_ends_the_plan_and_nothing_more_is_planned(
    hass: HomeAssistant, freezer: Any
) -> None:
    night = Night(hass, freezer)
    await night.build()
    controller = night.controller
    now = dt_util.utcnow()
    window = (now - timedelta(minutes=1), now + timedelta(hours=3))
    await controller.async_install(
        ChargingPlan(
            start=window[0].isoformat(), end=window[1].isoformat(), amps=16, phases=3,
            periods=[{"start": window[0].isoformat(), "end": window[1].isoformat()}],
            target_soc_percent=100.0, vehicle_id=night.ev6,
        )
    )
    await hass.async_block_till_done()
    assert controller.plan is not None and night.switch_calls[-1] == "turn_on"

    # The night's charge: 24.26 kWh at about 11 kW, the card looked at now and then.
    night.amps(14.8)
    for step in range(1, 12):
        night.register(6821.568 + 24.257 * step / 11)
        await night.later(600)
        night.show_the_card()
    reading = charger_data(hass, night.charger.entry_id).soc_reader.read(night.ev6)
    assert reading is not None and reading.estimated and reading.soc_percent == 100.0
    assert controller.plan is not None, "a target at the car's limit is the car's to end"

    # 02:24: the car is full; 0.57 A, then `SuspendedEV` at 0 A.
    night.amps(0.57)
    await night.later(PLAN_CAR_IDLE_S + 60)

    assert controller.plan is None, "the plan ends: nothing is chased"
    record = controller.target_stop_record
    assert record is not None and record["basis"] == "estimate" and record["car_ended"] is True
    assert controller.completion_record is not None and controller.completion_record["reason"] == "vehicle_full"
    assert night.switch_calls[-1] == "turn_off"
    assert night.refreshed and night.soc_entity in night.refreshed[0], "the car is asked once for a fresh reading"
    asked = len(night.refreshed)

    preview = preview_for(hass, night.charger.entry_id)
    snapshot = preview.snapshot()
    assert (snapshot.state, snapshot.reason) == ("nothing_to_charge", "already_at_target")

    # The rest of the night: no window is planned or started, and nobody asks the car again.
    for _ in range(8):
        await night.later(30 * 60, step=300)
        night.show_the_card()
        await preview.async_recalculate()
    assert controller.plan is None and night.switch_calls[-1] == "turn_off"
    assert preview.snapshot().state == "nothing_to_charge"
    assert len(night.refreshed) == asked
    shadow = controller._shadow.diagnostics()["coverage"]["kinds"]  # noqa: SLF001 - what the field report shows
    assert shadow["car_ended"]["compared"] == 1 and shadow["target_reached"]["compared"] == 1
