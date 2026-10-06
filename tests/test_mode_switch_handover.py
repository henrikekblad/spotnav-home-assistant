"""A strategy change hands a running charge to its new owner instead of stopping it.

The field case (2026-10-06, two Easee chargers through the `easee` integration, Tibber Watty phase currents, a
SolaX home battery on `car_first`, Solcast): "Garage", hybrid, a target of 100 % for a 15.6 kWh car, one phase at
16 A. The plan window started the charge at 06:30; at 08:13:08 the person switched hybrid to solar with 2.2 to
4.0 kW of sun available. The plan's own stop (pause) took the charge from the sun, which wanted to keep it, and the
sun's watch of its charge then sent a second pause 3.9 s later to the charger already awaiting a start. At 08:22:55
the switch back to hybrid stopped the charge once more, with the next planned period half an hour away.

Every tick is an explicit site recompute at a hand-set instant (`tests.test_solar_execution`).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution.controller import ChargingController, ChargingPlan
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.planning.auto_settings import (
    STRATEGY_CHEAPEST,
    STRATEGY_HYBRID,
    STRATEGY_SOLAR,
)
from custom_components.spotnav.planning.status_compose import (
    compose_status,
    HybridFacts,
    ProposalFacts,
    StatusFacts,
)
from custom_components.spotnav.runtime import domain_data, preview_for

from .charger_helpers import detected_config
from .charger_shapes import register_shape, SHAPES
from .world import controller_of, set_charger_delivered_a, set_site_power_w, solar_setup, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")

BATTERY = "sensor.home_battery_power"
STATUS = "sensor.easee_status"
T0 = dt_util.utcnow()


class Easee:
    """The Easee's service calls as the integration receives them, and its status as the cloud reports it."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.commands: list[str] = []
        self.limits: list[float] = []

        async def command(call: ServiceCall) -> None:
            self.commands.append(call.data["action_command"])

        async def limit(call: ServiceCall) -> None:
            self.limits.append(call.data["current"])

        hass.services.async_register("easee", "action_command", command)
        hass.services.async_register("easee", "set_charger_dynamic_limit", limit)

    @property
    def pauses(self) -> int:
        return self.commands.count("pause")

    async def status(self, value: str) -> None:
        self.hass.states.async_set(STATUS, value, {"config_authorizationRequired": False})
        await self.hass.async_block_till_done()


async def _garage(hass: HomeAssistant, strategy: str = STRATEGY_HYBRID) -> tuple[Any, ...]:
    """The field charger: an Easee on L1, its status `awaiting_start`, on a derived site with a home battery."""
    hass.states.async_set(BATTERY, "0", {"unit_of_measurement": "W"})
    ids = register_shape(hass, SHAPES["easee"])
    config = detected_config(detect_charger(hass, ids["device_id"]))
    hass.states.async_set(STATUS, "awaiting_start", {"config_authorizationRequired": False})
    charger, site_entry, controller, coordinator, clock, _on, _off = await solar_setup(
        hass, strategy=strategy, battery_entity=BATTERY, charger_config=config, phase="L1"
    )
    easee = Easee(hass)
    return charger, controller_of(hass, site_entry.entry_id), controller, coordinator, clock, easee


async def _plan_window_charge(hass: HomeAssistant, charger: Any, controller: ChargingController, easee: Easee) -> None:
    """06:30: the plan window starts the charge at 16 A; the car draws 14.5 A."""
    now = dt_util.utcnow()
    await controller.async_install(
        ChargingPlan(
            start=(now - timedelta(minutes=10)).isoformat(),
            end=(now + timedelta(minutes=45)).isoformat(),
            amps=16,
            phases=1,
            auto_identity="garage-hybrid",
        )
    )
    assert "resume" in easee.commands, "the window started the charge"
    set_charger_delivered_a(hass, charger.entry_id, 14.5)
    await easee.status("charging")
    assert controller.charge_origin == "plan_window" and controller.charging
    easee.commands.clear()


async def _switch(hass: HomeAssistant, charger: Any, strategy: str) -> None:
    """The person picks another strategy in the card: the settings write and its reconcile."""
    preview = preview_for(hass, charger.entry_id)
    assert preview is not None
    await preview.async_apply_settings(mutate=lambda settings: replace(settings, strategy=strategy))
    await hass.async_block_till_done()


def _sun(hass: HomeAssistant, *, export_w: float) -> None:
    """The grid on every phase: `export_w` in all exported beside the car's own draw (negative: imported)."""
    set_site_power_w(hass, "solar_site", -export_w / 3.0)


# ------------------------------------------------------------------------------------- plan → sun


async def test_0813_a_switch_to_solar_with_sun_hands_the_plans_charge_to_the_sun_without_a_stop(
    hass: HomeAssistant,
) -> None:
    charger, site, controller, coordinator, clock, easee = await _garage(hass)
    await _plan_window_charge(hass, charger, controller, easee)
    # The sun's rules have run beside the plan all morning: on, held by the plan.
    _sun(hass, export_w=600.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.state == "on" and coordinator.state.held_by_plan

    await _switch(hass, charger, STRATEGY_SOLAR)

    assert easee.pauses == 0, "the sun keeps the charge: no stop on the switch"
    assert controller.plan is None, "the plan is cleared"
    assert controller.charge_origin == "solar", "the charge is the sun's now"
    assert controller.charging

    # The sun modulates it from here: its first tick asks for what the surplus carries, not the plan's 16 A.
    clock.value += 5.0
    _sun(hass, export_w=-500.0)
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.state == "on" and not coordinator.state.held_by_plan
    assert controller.requested_current_a is not None and controller.requested_current_a < 16
    assert easee.pauses == 0


async def test_a_switch_to_solar_without_sun_stops_the_plans_charge_once(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, easee = await _garage(hass)
    await _plan_window_charge(hass, charger, controller, easee)
    # Importing: the sun's rules, observed beside the plan, have long gone off.
    _sun(hass, export_w=-2000.0)
    for at in (0.0, 300.0, 700.0, 1000.0):
        clock.value = at
        await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.state == "off"

    await _switch(hass, charger, STRATEGY_SOLAR)

    assert easee.pauses == 1
    assert controller.plan is None
    await easee.status("awaiting_start")
    for _ in range(5):
        clock.value += 2.0
        await tick_site(hass, site)
    assert easee.pauses == 1, "one stop for the switch"


@pytest.mark.parametrize(("export_w", "pauses"), [(600.0, 0), (-2000.0, 1)], ids=["sun", "no_sun"])
async def test_a_switch_from_cheapest_to_solar_asks_the_sun_on_the_reading_it_has(
    hass: HomeAssistant, export_w: float, pauses: int
) -> None:
    """Off `cheapest` the sun has no state of its own yet: it decides at the switch, by its rule for a charge that
    runs (the surplus at or above the charger's stop level)."""
    charger, site, controller, coordinator, clock, easee = await _garage(hass, STRATEGY_CHEAPEST)
    await _plan_window_charge(hass, charger, controller, easee)
    _sun(hass, export_w=export_w)
    await tick_site(hass, site)
    assert coordinator.state is None

    await _switch(hass, charger, STRATEGY_SOLAR)

    assert easee.pauses == pauses
    assert controller.plan is None
    assert (controller.charge_origin == "solar") is (pauses == 0)


async def test_a_sun_that_adopted_the_plans_charge_with_no_surplus_does_not_keep_it(hass: HomeAssistant) -> None:
    """Cheapest's window charges at 16 A with no sun (3.5 kW imported). The person switches to hybrid, where the sun
    adopts the running charge as `on` and then goes `disarming`, and a minute later to solar: the sun never had a
    surplus, so the switch stops the charge once, as the same reading does from cheapest. Handed over, the plan's
    16 A would run from the grid for the sun's minimum on time."""
    charger, site, controller, coordinator, clock, easee = await _garage(hass, STRATEGY_CHEAPEST)
    await _plan_window_charge(hass, charger, controller, easee)
    _sun(hass, export_w=-3500.0)
    await tick_site(hass, site)
    await _switch(hass, charger, STRATEGY_HYBRID)
    clock.value += 5.0
    await tick_site(hass, site)
    clock.value += 60.0
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.held_by_plan
    assert coordinator.state.state in ("on", "disarming")

    await _switch(hass, charger, STRATEGY_SOLAR)

    assert easee.pauses == 1
    assert controller.plan is None and controller.charge_origin is None


async def test_a_hand_over_writes_the_suns_current_at_once(hass: HomeAssistant) -> None:
    """Handed over with 9 A of sun beside the car, the sun asks for that current on the evaluation the hand-over
    itself triggers: the plan's 16 A does not go on."""
    charger, site, controller, coordinator, clock, easee = await _garage(hass)
    await _plan_window_charge(hass, charger, controller, easee)
    # The car's 14.5 A minus 1.25 kW imported: about 9 A of sun.
    _sun(hass, export_w=-1250.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.state == "on" and coordinator.state.held_by_plan

    await _switch(hass, charger, STRATEGY_SOLAR)

    assert easee.pauses == 0 and controller.charge_origin == "solar"
    assert controller.requested_current_a == 9


# ------------------------------------------------------------------------------------- sun → plan


async def test_leaving_solar_for_a_plan_window_open_now_hands_the_suns_charge_to_the_plan(
    hass: HomeAssistant,
) -> None:
    """The plan installed for the new strategy opens its window on the sun's running charge and makes it its own;
    the sun, leaving, does not stop it (1.11.0 stopped it in the middle of the window)."""
    charger, site, controller, coordinator, clock, easee = await _garage(hass, STRATEGY_SOLAR)
    _sun(hass, export_w=2000.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert controller.charge_origin == "solar"
    set_charger_delivered_a(hass, charger.entry_id, 6.0)
    await easee.status("charging")
    # Auto planned the new strategy: a window open now.
    store = domain_data(hass).auto_store
    await store.async_update(charger.entry_id, mutate=lambda s: replace(s, strategy=STRATEGY_CHEAPEST))
    now = dt_util.utcnow()
    await controller.async_install(
        ChargingPlan(
            start=(now - timedelta(minutes=1)).isoformat(),
            end=(now + timedelta(minutes=30)).isoformat(),
            amps=16,
            phases=1,
            auto_identity="garage-cheapest",
        )
    )
    easee.commands.clear()

    clock.value = 130.0
    await tick_site(hass, site)

    assert easee.pauses == 0, "the plan keeps the charge: no stop and no start"
    assert controller.charge_origin == "plan_window"
    assert coordinator.state is None


async def test_leaving_solar_with_no_plan_window_open_stops_the_suns_charge_once(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, easee = await _garage(hass, STRATEGY_SOLAR)
    _sun(hass, export_w=2000.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    set_charger_delivered_a(hass, charger.entry_id, 6.0)
    await easee.status("charging")
    store = domain_data(hass).auto_store
    await store.async_update(charger.entry_id, mutate=lambda s: replace(s, strategy=STRATEGY_CHEAPEST))
    easee.commands.clear()

    clock.value = 130.0
    await tick_site(hass, site)

    assert easee.pauses == 1
    assert controller.charge_origin is None


# ------------------------------------------------------------------------------ one stop per decision


async def test_the_suns_watch_sends_no_second_pause_to_a_charge_our_own_stop_ended_seconds_ago(
    hass: HomeAssistant,
) -> None:
    """08:13:11.95 in 1.11.0: our stop at 08:13:08 paused the charge, and the sun's watch of the charge it runs
    saw the charger awaiting a start (`charger_stopped`) and paused it again. A stop of ours still settling
    (`STOP_SETTLE_S`) is that decision's: nothing more goes out."""
    charger, site, controller, coordinator, clock, easee = await _garage(hass, STRATEGY_SOLAR)
    _sun(hass, export_w=2000.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    set_charger_delivered_a(hass, charger.entry_id, 8.0)
    await easee.status("charging")
    clock.value = 130.0
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.state == "on"
    easee.commands.clear()

    await controller.async_stop(urgent=False)
    assert easee.pauses == 1
    set_charger_delivered_a(hass, charger.entry_id, 0.0)
    await easee.status("awaiting_start")
    clock.value = 134.0
    await tick_site(hass, site)

    assert ("stop", "charger_stopped") in [(entry["action"], entry["reason"]) for entry in coordinator.decision_log]
    assert easee.pauses == 1, "no second pause for the same decision"


async def test_0818_a_self_start_taken_over_on_the_batterys_credit_is_stopped_once(hass: HomeAssistant) -> None:
    """08:18:50 the Easee began a charge by itself (the person changed its current); the sun took it over on a
    surplus a charging battery made, and at 08:19:17 the battery turned to feed the car: stopped once, and
    nothing of the sun's pauses it again while it awaits a start."""
    charger, site, controller, coordinator, clock, easee = await _garage(hass, STRATEGY_SOLAR)
    clock.value = 3600.0
    # Without the battery's 1.6 kW the sun would not carry the start minimum.
    hass.states.async_set(BATTERY, "1600", {"unit_of_measurement": "W"})
    _sun(hass, export_w=-500.0)
    set_charger_delivered_a(hass, charger.entry_id, 7.0)
    await easee.status("charging")
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.reason == "start_verifying"
    assert easee.pauses == 0

    clock.value = 3627.0
    hass.states.async_set(BATTERY, "-2378", {"unit_of_measurement": "W"})
    await tick_site(hass, site)
    assert coordinator.state.reason == "battery_credit_false"
    assert easee.pauses == 1

    set_charger_delivered_a(hass, charger.entry_id, 0.0)
    await easee.status("awaiting_start")
    for _ in range(20):
        clock.value += 3.0
        await tick_site(hass, site)
    assert easee.pauses == 1


# ------------------------------------------------------------------------------------ plan → plan


@pytest.mark.parametrize("strategy", [STRATEGY_CHEAPEST, STRATEGY_HYBRID])
async def test_a_switch_between_plans_inside_a_window_keeps_the_charge(hass: HomeAssistant, strategy: str) -> None:
    """Hybrid to cheapest, or cheapest to hybrid, while a window charges: the plan keeps the charge (a new plan
    waits for the window's boundary), with no stop and no start."""
    start_with = STRATEGY_HYBRID if strategy == STRATEGY_CHEAPEST else STRATEGY_CHEAPEST
    charger, site, controller, coordinator, clock, easee = await _garage(hass, start_with)
    await _plan_window_charge(hass, charger, controller, easee)
    _sun(hass, export_w=-2000.0)
    await tick_site(hass, site)

    await _switch(hass, charger, strategy)
    for _ in range(3):
        clock.value += 5.0
        await tick_site(hass, site)

    assert easee.commands == [], "no stop, no start"
    assert controller.charging and controller.charge_origin == "plan_window"


# ------------------------------------------------------------------------------- the SoC glitch


async def test_one_reading_that_says_the_need_is_met_ends_nothing_inside_the_window(hass: HomeAssistant) -> None:
    """08:11:06: one reading of 100 % (the next said 92 %) made hybrid `satisfied` for a minute. Inside the plan's
    window the sun holds nothing of the plan's charge, so the reading stopped nothing."""
    charger, site, controller, coordinator, clock, easee = await _garage(hass)
    await _plan_window_charge(hass, charger, controller, easee)
    _sun(hass, export_w=600.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)

    coordinator._hybrid_satisfied = lambda: True  # type: ignore[method-assign]
    clock.value = 130.0
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.satisfied
    del coordinator._hybrid_satisfied
    clock.value = 190.0
    await tick_site(hass, site)

    assert easee.commands == []
    assert controller.charging and controller.plan is not None


# --------------------------------------------------------------------------------- back to hybrid


async def test_0822_outside_hybrids_window_a_charge_with_no_sun_to_carry_it_is_stopped_once(
    hass: HomeAssistant,
) -> None:
    """Back on hybrid with no sun, the charge running is stopped once: by the sun's first reading of a charge it
    does not carry, or by the re-arm of the plan installed for the switch (one period, 08:30 to 09:00, ahead), which
    pauses an Easee outside the plan's windows. Whichever comes first, the other finds that stop on its way."""
    charger, site, controller, coordinator, clock, easee = await _garage(hass)
    _sun(hass, export_w=-2000.0)
    set_charger_delivered_a(hass, charger.entry_id, 7.0)
    await easee.status("charging")
    clock.value += 1.0
    await tick_site(hass, site)
    start = dt_util.utcnow() + timedelta(minutes=7)
    await controller.async_install(
        ChargingPlan(
            start=start.isoformat(),
            end=(start + timedelta(minutes=30)).isoformat(),
            amps=16,
            phases=1,
            auto_identity="garage-hybrid-0830",
        )
    )
    await easee.status("awaiting_start")
    for _ in range(5):
        clock.value += 3.0
        await tick_site(hass, site)

    assert easee.pauses == 1
    assert controller.plan is not None


def test_0822_hybrid_outside_its_window_names_the_next_period() -> None:
    """"Switched back to hybrid and nothing happens": the hybrid line names the period it waits for."""
    start = T0.replace(hour=8, minute=30, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    facts = StatusFacts(
        now=start - timedelta(minutes=7),
        strategy=STRATEGY_HYBRID,
        hybrid=HybridFacts("holding_back_does_not_pay", 1.04, 0.0, False),
        proposal=ProposalFacts(periods=((start, end),)),
    )
    line = compose_status(facts)["lines"][0]
    assert line["code"] == "hybrid_grid"
    assert line["params"]["window_start"] == start.isoformat() and line["params"]["window_end"] == end.isoformat()
