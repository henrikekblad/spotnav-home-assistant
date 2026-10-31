"""Load balancing with a charger that can only be switched on and off (`ChargingController.is_switch_only`).

A site behind a 20 A main fuse (no safety margin), derived from its meter, with an OCPP charger whose current is set
(`A`, ChangeConfiguration) and a plain switch charger whose current cannot be (`B`, a smart plug or a Monta-style
charger). Driven through `SiteCapacityController` with the real regulator and explicit passes, as
`tests/test_lb_start_credit.py` drives its HALO.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CHARGER_PRIORITY_FIRST,
    CONF_CHARGER_PRIORITY,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_POWER_ENTITY,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    MEASUREMENT_MODE_DERIVED,
    REGULATOR_DWELL_S,
)
from custom_components.spotnav.diagnostics import entry_diagnostics
from custom_components.spotnav.site.regulator_damping import RegulatorDamper

from .charger_helpers import mock_ocpp_assigned_current
from .helpers import make_entry, make_site_entry
from .test_yield_stepping_wiring import _DamperClock, _SecondsClock
from .world import controller_of, separate_entities_source, set_charger_delivered_a, set_derived_site_entities

_V = 230.0
_PLUG = {"unit_of_measurement": "W", "device_class": "power"}


def _site_a(hass: HomeAssistant, site: str, l1: float, l2: float, l3: float) -> None:
    for phase, amps in (("l1", l1), ("l2", l2), ("l3", l3)):
        hass.states.async_set(f"sensor.{site}_power_{phase}", str(amps * _V), {"unit_of_measurement": "W"})


def _wiring(prefix: str | None) -> dict[str, Any]:
    wiring: dict[str, Any] = {"phases": 3, "phase": None, "min_current_a": 6.0}
    if prefix is not None:
        wiring[CONF_MEASURED_CURRENT_SOURCE] = separate_entities_source(
            f"sensor.{prefix}_l1", f"sensor.{prefix}_l2", f"sensor.{prefix}_l3"
        )
    return wiring


class _World:
    def __init__(self, hass: HomeAssistant, name: str, site: Any, chargers: dict[str, Any], configure, clock, dampers):
        self.hass = hass
        self.name = name
        self.site = site
        self.chargers = chargers
        self.configure = configure
        self.clock = clock
        self.dampers = dampers
        self.turn_on: list = []
        self.turn_off: list = []

    def cc(self, key: str):
        return controller_of(self.hass, self.chargers[key].entry_id)

    def entry_id(self, key: str) -> str:
        return self.chargers[key].entry_id

    def switch(self, key: str, state: str) -> None:
        self.hass.states.async_set(f"switch.{self.name}_{key}", state)

    def reads(self, key: str, amps: float) -> None:
        set_charger_delivered_a(self.hass, f"{self.name}_{key}", amps)

    def meter(self, amps: float) -> None:
        _site_a(self.hass, f"{self.name}_site", amps, amps, amps)

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)
        for damper_clock in self.dampers:
            damper_clock.advance(seconds)

    async def tick(self) -> None:
        await self.hass.async_block_till_done()
        self.site._recompute(schedule_apply=False)  # noqa: SLF001
        await self.site._async_apply_active_control()  # noqa: SLF001
        await self.hass.async_block_till_done()

    def stops(self, key: str) -> int:
        return sum(1 for call in self.turn_off if call.data.get("entity_id") == f"switch.{self.name}_{key}")

    def starts(self, key: str) -> int:
        return sum(1 for call in self.turn_on if call.data.get("entity_id") == f"switch.{self.name}_{key}")

    def decision(self, key: str):
        return self.site.regulator_decisions[self.entry_id(key)]


async def _world(
    hass: HomeAssistant,
    monkeypatch,
    name: str,
    *,
    commandable: bool = True,
    switches: tuple[str, ...] = ("b",),
    switch_measured: bool = True,
    switch_extra: dict[str, Any] | None = None,
    commandable_extra: dict[str, Any] | None = None,
) -> _World:
    from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter

    monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda self: True)
    configure = mock_ocpp_assigned_current(hass, "1.16,2.10")
    chargers: dict[str, Any] = {}
    wiring: dict[str, Any] = {}
    if commandable:
        prefix = f"{name}_a"
        hass.states.async_set(f"switch.{prefix}", "off")
        chargers["a"] = make_entry(
            hass,
            entry_id=prefix,
            charge_control=f"switch.{prefix}",
            current_limit=f"number.{prefix}_connector_1_session_current_limit",
            webhook_id=f"webhook-{prefix}",
            title="A",
            current_control=CURRENT_CONTROL_CHANGE_CONFIGURATION,
            ocpp_target=(prefix, 1),
            extra=commandable_extra,
        )
        wiring[prefix] = _wiring(prefix)
    for key in switches:
        prefix = f"{name}_{key}"
        hass.states.async_set(f"switch.{prefix}", "off")
        chargers[key] = make_entry(
            hass,
            entry_id=prefix,
            charge_control=f"switch.{prefix}",
            current_limit=None,
            webhook_id=f"webhook-{prefix}",
            title=key.upper(),
            extra=switch_extra,
        )
        wiring[prefix] = _wiring(prefix if switch_measured else None)
    for entry in chargers.values():
        if entry.state is not ConfigEntryState.LOADED:
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        set_charger_delivered_a(hass, entry.entry_id, 0.0)
    site_name = f"{name}_site"
    derived = set_derived_site_entities(hass, site_name, 0.0)
    _site_a(hass, site_name, 2.0, 2.0, 2.0)
    site_entry = make_site_entry(
        hass,
        entry_id=site_name,
        main_fuse_a=20.0,
        safety_margin_a=0.0,
        charger_entry_ids=[entry.entry_id for entry in chargers.values()],
        phase_wiring=wiring,
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived,
        active_control_enabled=True,
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, site_entry.entry_id)
    world_turn_on = async_mock_service(hass, "switch", "turn_on")
    world_turn_off = async_mock_service(hass, "switch", "turn_off")
    if site._state_listener_cancel is not None:  # noqa: SLF001
        site._state_listener_cancel()  # noqa: SLF001
        site._state_listener_cancel = None  # noqa: SLF001
    for cancel in site._controller_listener_cancels:  # noqa: SLF001
        cancel()
    site._controller_listener_cancels.clear()  # noqa: SLF001
    clock = _SecondsClock()
    site._yield_now = clock  # noqa: SLF001
    dampers = []
    for entry in chargers.values():
        cc = controller_of(hass, entry.entry_id)
        cc.set_start_cap(
            lambda entry_id=entry.entry_id: site.start_allowance_a(entry_id),
            reserve=lambda amps, entry_id=entry.entry_id: site.reserve_start(entry_id, amps),
            record=lambda amps, reason, entry_id=entry.entry_id: site.note_charger_write(entry_id, amps, reason),
        )
        damper_clock = _DamperClock()
        site._dampers[entry.entry_id] = RegulatorDamper(now=damper_clock)  # noqa: SLF001
        dampers.append(damper_clock)
    configure.clear()
    world = _World(hass, name, site, chargers, configure, clock, dampers)
    world.turn_on = world_turn_on
    world.turn_off = world_turn_off
    return world


async def _charging(world: _World, key: str, amps: float, *, start: int | None = 16) -> None:
    if start is not None:
        assert await world.cc(key).async_start(start, cause="plan_window")
    world.switch(key, "on")
    world.reads(key, amps)
    await world.hass.async_block_till_done()


async def test_the_switched_current_is_learned_from_the_chargers_own_current_sensors(
    hass: HomeAssistant, monkeypatch
) -> None:
    world = await _world(hass, monkeypatch, "learn", commandable=False)
    cc = world.cc("b")
    assert cc.is_switch_only
    # Nothing learned: the charging current asked for.
    assert await cc.async_start(10, cause="plan_window")
    assert cc.switched_current().source == "charging_current"
    assert cc.switched_current().amps == 10.0
    world.switch("b", "on")
    world.reads("b", 15.8)
    world.meter(2.0 + 15.8)
    await world.tick()
    assert cc.switched_current().source == "measured"
    assert cc.switched_current().amps == 15.8
    assert cc.switched_current_snapshot()["measured_a"] == 15.8
    diagnostics = entry_diagnostics(hass, world.chargers["b"])["controller"]["switched_current"]
    assert diagnostics["source"] == "measured" and diagnostics["amps"] == 15.8
    # The load balancer reckons it at its whole switched current, rounded up.
    request = next(r for r in world.site._build_requests() if r.charger_entry_id == world.entry_id("b"))  # noqa: SLF001
    assert request.switch_only and request.requested_current_a == 16.0 and request.min_current_a == 16.0


async def test_the_switched_current_is_learned_from_a_smart_plugs_power(hass: HomeAssistant, monkeypatch) -> None:
    hass.states.async_set("sensor.plug_b_power", "0", _PLUG)
    world = await _world(
        hass,
        monkeypatch,
        "plug",
        commandable=False,
        switch_measured=False,
        switch_extra={CONF_POWER_ENTITY: "sensor.plug_b_power"},
    )
    cc = world.cc("b")
    await _charging(world, "b", 0.0, start=10)
    hass.states.async_set("sensor.plug_b_power", str(3 * _V * 13.0), _PLUG)
    world.meter(15.0)
    await world.tick()
    value = cc.switched_current()
    assert value.source == "measured"
    assert round(value.amps, 1) == 13.0
    # The plug's power stands in for the charger's own current in the regulator too.
    decision = world.decision("b")
    assert decision.reason == "no_change_requested"
    assert round(decision.basis["L1"].measured_current_a, 1) == 13.0


async def test_a_switch_only_charger_never_blocks_its_neighbour_with_an_unknown_request(
    hass: HomeAssistant, monkeypatch
) -> None:
    world = await _world(hass, monkeypatch, "unknown")
    await _charging(world, "a", 8.0)
    # B was switched on by hand: nothing was asked of it, and it reads 10 A.
    await _charging(world, "b", 10.0, start=None)
    world.meter(2.0 + 8.0 + 10.0 - 4.0)
    await world.tick()
    allocations = {a.charger_entry_id: a for a in world.site.result.allocations}
    assert allocations[world.entry_id("b")].requested_current_a is not None
    assert allocations[world.entry_id("b")].state != "requested_current_unknown"
    assert world.decision("a").reason != "shared_phase_blocked_by_another_chargers_undetermined_decision"
    assert world.decision("a").proposed_current_a is not None


async def test_over_the_fuse_the_commandable_charger_is_lowered_first_then_the_switch_one_paused_and_resumed(
    hass: HomeAssistant, monkeypatch
) -> None:
    world = await _world(hass, monkeypatch, "over")
    await _charging(world, "a", 16.0)
    await _charging(world, "b", 10.0)
    world.meter(18.0)
    await world.tick()
    world.configure.clear()

    # 2 A over: A takes the cut, B runs on.
    world.meter(22.0)
    world.advance(5.0)
    await world.tick()
    assert world.decision("a").reason == "reducing_current_due_to_active_import_overload"
    assert world.configure and world.configure[-1].data["value"].startswith("1.14,")
    assert world.decision("b").reason == "no_change_requested"
    assert world.stops("b") == 0

    # The house takes far more: lowering A cannot cover it, so B is paused whole.
    world.reads("a", 14.0)
    world.meter(40.0)
    world.advance(5.0)
    await world.tick()
    assert world.decision("b").reason == "switched_load_paused_for_import_overload"
    assert world.stops("b") == 1
    world.switch("b", "off")
    world.reads("b", 0.0)
    await hass.async_block_till_done()
    assert world.cc("b").paused_by_balancing

    # Room for 10 A, but not for its whole current plus the margin: it stays paused.
    started = world.starts("b")
    world.switch("a", "off")
    world.reads("a", 0.0)
    world.meter(9.5)
    for _ in range(3):
        world.advance(REGULATOR_DWELL_S)
        await world.tick()
    assert world.starts("b") == started

    # Room for all of it and the margin, held for the dwell: resumed.
    world.meter(6.0)
    await world.tick()
    assert world.starts("b") == started, "the dwell starts now"
    world.advance(REGULATOR_DWELL_S)
    await world.tick()
    assert world.starts("b") == started + 1
    assert not world.cc("b").paused_by_balancing


async def test_a_site_of_only_switch_chargers_is_protected(hass: HomeAssistant, monkeypatch) -> None:
    world = await _world(hass, monkeypatch, "only", commandable=False, switches=("b", "c"))
    await _charging(world, "b", 10.0, start=10)
    await _charging(world, "c", 10.0, start=10)
    world.meter(18.0)
    await world.tick()
    assert world.stops("b") == world.stops("c") == 0

    # 4 A over: one of them is paused (the first in the site's order), which is enough.
    world.meter(24.0)
    world.advance(5.0)
    await world.tick()
    assert world.stops("b") == 1
    assert world.stops("c") == 0


async def test_a_switch_only_charger_with_no_reading_is_taken_to_draw_its_switched_current(
    hass: HomeAssistant, monkeypatch
) -> None:
    world = await _world(hass, monkeypatch, "blind", commandable=False, switch_measured=False)
    await _charging(world, "b", 0.0, start=16)
    world.meter(18.0)
    await world.tick()
    assert world.decision("b").basis["L1"].measured_current_a == 16.0
    world.meter(21.0)
    world.advance(5.0)
    await world.tick()
    assert world.stops("b") == 1


async def test_a_start_the_cap_held_back_is_resumed_once_its_whole_current_fits(
    hass: HomeAssistant, monkeypatch
) -> None:
    world = await _world(hass, monkeypatch, "cap", commandable=False)
    cc = world.cc("b")
    world.meter(10.0)
    await world.tick()
    # 10 A of room for a 16 A switched current: the start is refused and waits.
    assert not await cc.async_start(16, cause="plan_window")
    assert cc.paused_by_balancing and world.starts("b") == 0

    world.meter(2.0)
    await world.tick()
    world.advance(REGULATOR_DWELL_S)
    await world.tick()
    assert world.starts("b") == 1
    assert not cc.paused_by_balancing


async def test_a_site_step_is_learned_through_the_site_for_a_charger_with_no_reading(
    hass: HomeAssistant, monkeypatch
) -> None:
    world = await _world(hass, monkeypatch, "step", commandable=False, switch_measured=False)
    cc = world.cc("b")
    learner = cc.switched_current_learner

    async def edge(on: bool, before: float, after: float) -> None:
        world.meter(before)
        await world.tick()
        if on:
            assert await cc.async_start(10, cause="plan_window")
        else:
            await cc.async_stop()
        world.switch("b", "on" if on else "off")
        await world.tick()
        world.meter(after)
        for _ in range(5):
            world.advance(6.0)
            await world.tick()

    await edge(True, 2.0, 15.0)
    assert learner.snapshot(10.0)["unconfirmed_step_a"] == 13.0
    await edge(False, 15.0, 2.0)
    assert cc.switched_current().source == "site_step"
    assert cc.switched_current().amps == 13.0


async def test_a_lost_direction_on_a_direct_site_still_pauses_a_switch_only_charger_over_the_fuse(
    hass: HomeAssistant,
) -> None:
    from .test_direct_signed_active_control import _charger_reads, _confirm, _report, _site, _start, _tick

    prefix = "lost_switch"
    hass.states.async_set(f"switch.{prefix}", "off")
    charger = make_entry(
        hass,
        entry_id=prefix,
        charge_control=f"switch.{prefix}",
        current_limit=None,
        webhook_id=f"webhook-{prefix}",
        title="Switch",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    async_mock_service(hass, "switch", "turn_on")
    turn_off = async_mock_service(hass, "switch", "turn_off")
    _charger_reads(hass, prefix, 0.0)
    _report(hass, "lost_switch_site", (12.0, 13.0, 14.0), 39.0 * 230.0)
    site_entry = _site(hass, "lost_switch_site", [charger], [prefix])
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, site_entry.entry_id)
    await _confirm(hass, "lost_switch_site")

    await _start(hass, charger, prefix, 10)
    _charger_reads(hass, prefix, 10.0)
    await _confirm(hass, "lost_switch_site", currents=(12.0, 13.0, 14.0), times=4)
    assert controller_of(hass, charger.entry_id).is_switch_only

    # The total turns against the currents: the direction is withdrawn mid-charge; nothing over, nothing done.
    _report(hass, "lost_switch_site", (12.0, 13.0, 14.0), -39.0 * 230.0)
    await _tick(hass)
    assert site.regulator_decisions[charger.entry_id].proposed_current_a is None
    assert turn_off == []

    # L3 over the limit by |I|: it cannot be lowered, so it is paused.
    _report(hass, "lost_switch_site", (12.0, 13.0, 26.0), -51.0 * 230.0)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason) == (
        0.0,
        "lowering_overload_on_current_magnitude_direction_unknown",
    )
    assert [call.data.get("entity_id") for call in turn_off] == [f"switch.{prefix}"]


def _assigned(world: _World) -> list[int]:
    return [int(call.data["value"].split(",")[0].split(".")[1]) for call in world.configure]


async def test_an_ev_and_a_switch_charger_ten_amps_over_lower_the_ev_and_pause_the_switch_charger(
    hass: HomeAssistant, monkeypatch
) -> None:
    """An EV at 10 A and a 10 A switch charger (a mower, say), 10 A over: the EV goes to its minimum and the switch
    charger is paused, in one pass; the EV is not paused while the switch charger runs on."""
    world = await _world(hass, monkeypatch, "mower")
    await _charging(world, "a", 10.0, start=10)
    await _charging(world, "b", 10.0, start=10)
    world.meter(20.0)
    await world.tick()
    world.configure.clear()

    world.meter(30.0)
    world.advance(5.0)
    await world.tick()
    assert world.decision("a").proposed_current_a == 6.0
    assert world.decision("a").pause_if_switched_pause_fails
    assert world.decision("b").reason == "switched_load_paused_for_import_overload"
    assert _assigned(world) == [6]
    assert world.stops("b") == 1
    assert world.stops("a") == 0


async def test_the_priority_class_comes_first_a_normal_ev_is_paused_before_a_first_switch_charger(
    hass: HomeAssistant, monkeypatch
) -> None:
    world = await _world(
        hass, monkeypatch, "classes", switch_extra={CONF_CHARGER_PRIORITY: CHARGER_PRIORITY_FIRST}
    )
    await _charging(world, "a", 10.0, start=10)
    await _charging(world, "b", 10.0, start=10)
    world.meter(20.0)
    await world.tick()
    world.configure.clear()

    # 8 A over: the Normal EV is lowered to its minimum and, still over, paused; that is enough, and the First
    # switch charger runs on.
    world.meter(28.0)
    world.advance(5.0)
    await world.tick()
    assert world.decision("a").proposed_current_a == 0.0
    assert world.stops("a") == 1
    assert world.decision("b").reason == "no_change_requested"
    assert world.stops("b") == 0

    # Far more over: the First switch charger is paused as well, after the Normal EV.
    world.switch("a", "off")
    world.reads("a", 0.0)
    world.meter(35.0)
    world.advance(5.0)
    await world.tick()
    assert world.decision("b").reason == "switched_load_paused_for_import_overload"
    assert world.stops("b") == 1


async def test_a_refused_switch_pause_falls_through_to_pausing_the_ev_in_the_same_pass(
    hass: HomeAssistant, monkeypatch
) -> None:
    world = await _world(hass, monkeypatch, "refused")
    await _charging(world, "a", 10.0, start=10)
    await _charging(world, "b", 10.0, start=10)
    world.meter(20.0)
    await world.tick()
    world.configure.clear()

    stops: list[str] = []

    async def turn_off(call) -> None:
        entity_id = call.data.get("entity_id")
        stops.append(entity_id if isinstance(entity_id, str) else entity_id[0])
        if "refused_b" in str(entity_id):
            raise HomeAssistantError("the plug did not answer")

    hass.services.async_register("switch", "turn_off", turn_off)

    world.meter(30.0)
    world.advance(5.0)
    await world.tick()
    assert world.decision("a").pause_if_switched_pause_fails
    # The EV's lowering to its minimum went out first, then, the switch charger's pause refused, the EV's pause.
    assert _assigned(world) == [6]
    assert "switch.refused_b" in stops
    assert "switch.refused_a" in stops
    assert world.cc("a").paused_by_balancing or world.cc("a").stop_settling
