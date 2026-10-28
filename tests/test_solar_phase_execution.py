"""Phase switching carried out (`execution/phase_switching.py`, `execution/solar_execution.py`): a Peblar whose core
integration offers the force-single-phase switch, on a three-phase charger in a solar site, with the charger's
`phase_switching` option on.

Every tick is an explicit site recompute at a hand-set instant (`tests.test_solar_execution`); the wall clock (which
the settle and the confirmation run on) is moved with it. The charger's read-back is the switch's state, which the
test sets as the charger would once it took a switch.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import callback, Event, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, MockConfigEntry

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_PLATFORM,
    CONF_PHASE_SWITCHING,
    DOMAIN,
)
from custom_components.spotnav.execution.chargers.base import ASSIGN_ASSIGNED
from custom_components.spotnav.execution.phase_switching import CONFIRM_S, SETTLE_S
from custom_components.spotnav.site.solar_surplus import MIN_BETWEEN_S, UP_DELAY_S

from .world import PHASES, controller_of, solar_setup, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")

CHARGER = "solar_charger"
SWITCH = f"switch.{CHARGER}"
FORCE = f"switch.{CHARGER}_force_single_phase"
VOLTAGE_V = 230.0


class _World:
    """One solar charger on a Peblar device with the force-single-phase switch, and both clocks."""

    def __init__(self, hass: HomeAssistant, freezer: Any, world: tuple[Any, ...]) -> None:
        self.hass = hass
        self.freezer = freezer
        charger, site_entry, controller, coordinator, clock, turn_on, turn_off = world
        self.charger = charger
        self.controller = controller
        self.coordinator = coordinator
        self.clock = clock
        self.turn_on = turn_on
        self.turn_off = turn_off
        self.site = controller_of(hass, site_entry.entry_id)
        self.base = dt_util.utcnow()
        self.t = 0.0
        self.sun_w = 0.0
        self.amps = 0.0
        self.verdicts: list[tuple[float, str, str, str]] = []
        # Every switch service call, in order.
        self.service_calls: list[tuple[str, str]] = []

        @callback
        def _called(event: Event) -> None:
            if event.data.get("domain") == "switch":
                entity_id = (event.data.get("service_data") or {}).get("entity_id")
                self.service_calls.append((event.data["service"], entity_id))

        hass.bus.async_listen(EVENT_CALL_SERVICE, _called)

    def phases_now(self) -> int:
        return 1 if self.hass.states.get(FORCE).state == "on" else 3

    def draw(self, amps: float, sun_w: float | None = None) -> None:
        """The car draws `amps` on the phases the charger is set to (L1 alone on one phase); the grid carries the
        rest of the sun, per phase."""
        self.amps = amps
        if sun_w is not None:
            self.sun_w = sun_w
        one = self.phases_now() == 1
        for phase in PHASES:
            on_phase = amps if (not one or phase == "L1") else 0.0
            # Written afresh on every tick (`force_update`): the meters report, and their readings are never stale.
            self.hass.states.async_set(
                f"sensor.{CHARGER}_{phase.lower()}", str(on_phase), {"unit_of_measurement": "A"}, force_update=True
            )
            self.hass.states.async_set(
                f"sensor.solar_site_power_{phase.lower()}",
                str(on_phase * VOLTAGE_V - self.sun_w / 3),
                {"unit_of_measurement": "W"},
                force_update=True,
            )
            for kind, value, unit in (("reactive", "0", "var"), ("voltage", str(VOLTAGE_V), "V")):
                self.hass.states.async_set(
                    f"sensor.solar_site_{kind}_{phase.lower()}", value, {"unit_of_measurement": unit}, force_update=True
                )

    async def at(self, t: float) -> None:
        """One tick at `t` seconds on both clocks."""
        self.t = t
        self.clock.value = t
        self.freezer.move_to(self.base + timedelta(seconds=t))
        async_fire_time_changed(self.hass)
        self.draw(self.amps)
        await tick_site(self.hass, self.site)
        state = self.coordinator.state
        if state is not None:
            self.verdicts.append((t, state.state, state.action, state.reason))

    async def run(self, until: float, step: float = 10.0) -> None:
        t = self.t + step
        while t <= until + 1e-9:
            await self.at(t)
            t += step

    def force_calls(self) -> list[str]:
        """The switch's calls in order: `1` for on (one phase), `3` for off."""
        return [
            "1" if service == "turn_on" else "3" for service, entity_id in self.service_calls if entity_id == FORCE
        ]

    def charge_stops(self) -> int:
        return sum(1 for c in self.turn_off if c.data.get("entity_id") == SWITCH)

    def reasons(self) -> list[str]:
        return [reason for _t, _state, _action, reason in self.verdicts]

    async def charger_takes(self, phases: int) -> None:
        """The charger takes the switch: its read-back shows `phases`, and the car draws again on them."""
        self.hass.states.async_set(FORCE, "on" if phases == 1 else "off")
        await self.hass.async_block_till_done()


async def _world(hass: HomeAssistant, freezer: Any, *, option: bool = True) -> _World:
    world = await solar_setup(
        hass,
        charger_config={CONF_CHARGE_CONTROL: SWITCH, CONF_CHARGER_PLATFORM: "peblar", CONF_PHASE_SWITCHING: option},
    )
    # The core Peblar integration's shape: the charge switch and the force-single-phase switch on one device.
    peblar = MockConfigEntry(domain="peblar", entry_id="peblar_entry")
    peblar.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=peblar.entry_id, identifiers={("peblar", "PB1")})
    registry = er.async_get(hass)
    # The charge switch's state stands in for the entity; registered under its own id.
    hass.states.async_remove(SWITCH)
    charge = registry.async_get_or_create(
        "switch", "peblar", "PB1_charge", config_entry=peblar, device_id=device.id, suggested_object_id=CHARGER
    )
    assert charge.entity_id == SWITCH
    force = registry.async_get_or_create(
        "switch",
        "peblar",
        "PB1_force_single_phase",
        config_entry=peblar,
        device_id=device.id,
        suggested_object_id=f"{CHARGER}_force_single_phase",
        translation_key="force_single_phase",
    )
    assert force.entity_id == FORCE
    hass.states.async_set(SWITCH, "off")
    hass.states.async_set(FORCE, "off")
    await hass.async_block_till_done()
    w = _World(hass, freezer, world)
    assert w.controller.adapter.phase_switch is not None
    return w


async def _charging_three_phases(hass: HomeAssistant, freezer: Any, *, option: bool = True) -> _World:
    """Solar started the car at 6 A on three phases at t=125, past its verification and minimum on time."""
    w = await _world(hass, freezer, option=option)
    w.draw(0.0, sun_w=4300.0)
    await w.at(0.0)
    await w.at(125.0)
    assert w.coordinator.state.action == "start"
    assert w.force_calls() == [], "the surplus carries three phases: started on the charger's own"
    hass.states.async_set(SWITCH, "on")
    w.draw(6.0)
    await w.run(800.0, step=25.0)
    assert w.coordinator.state.state == "on"
    return w


async def _switched_down(hass: HomeAssistant, freezer: Any) -> _World:
    """3 kW of sun on the three-phase charge: one switch to one phase after the stop delay."""
    w = await _charging_three_phases(hass, freezer)
    w.draw(6.0, sun_w=3000.0)
    start = w.t
    await w.run(start + 290.0)
    assert w.force_calls() == []
    while not w.force_calls() and w.t < start + 330.0:
        await w.at(w.t + 10.0)
    # Switched on the tick the stop delay ran out, instead of a stop.
    assert w.force_calls() == ["1"]
    assert w.reasons()[-1] == "phase_down"
    return w


async def test_three_kw_of_sun_switches_a_three_phase_charge_to_one_phase_instead_of_stopping(
    hass: HomeAssistant, freezer: Any
) -> None:
    w = await _switched_down(hass, freezer)
    switched_at = w.t
    assert w.controller.phase_switch_settling
    # The charger interrupts the charge for the switch, then takes it.
    w.draw(0.0)
    await w.run(switched_at + 20.0)
    await w.charger_takes(1)
    await w.run(switched_at + 60.0)
    assert w.controller.switched_phases == 1
    w.draw(6.0)
    await w.run(switched_at + 200.0)
    # Charging on one phase at what 3 kW carries there, never stopped.
    assert w.controller.requested_current_a == 13
    assert w.coordinator.state.state == "on"
    assert w.charge_stops() == 0
    assert "stop_after_delay" not in w.reasons()
    assert w.force_calls() == ["1"]
    # The site reckons it as a single-phase "Automatic" charger.
    assert w.site.reckoned_phases(CHARGER) == (1, None)
    facts = w.controller.phase_switching_facts()
    assert facts["allowed"] is True, facts
    assert facts["switched_phases"] == 1
    assert facts["switches_this_plug_in"] == 1
    assert facts["given_up"] is False


async def test_a_start_on_two_kw_of_sun_is_made_on_one_phase_set_right_before_it(
    hass: HomeAssistant, freezer: Any
) -> None:
    w = await _world(hass, freezer)
    w.draw(0.0, sun_w=2000.0)
    await w.at(0.0)
    assert w.force_calls() == [], "never set while idle"
    await w.at(125.0)
    assert w.coordinator.state.action == "start"
    # The phase first, then the start; a start's phase choice is no switch of the budget.
    assert w.service_calls[-2:] == [("turn_on", FORCE), ("turn_on", SWITCH)]
    assert w.controller.phase_switches_this_plug_in == 0
    await w.charger_takes(1)
    hass.states.async_set(SWITCH, "on")
    w.draw(6.0)
    await w.run(400.0, step=10.0)
    assert w.controller.switched_phases == 1
    assert w.coordinator.state.state == "on"
    assert w.controller.requested_current_a == 8  # 2 kW on one phase


async def test_rising_sun_switches_back_to_three_phases_after_the_up_delay_minimum_first(
    hass: HomeAssistant, freezer: Any
) -> None:
    w = await _switched_down(hass, freezer)
    switched_at = w.t
    await w.charger_takes(1)
    w.draw(13.0)
    await w.run(switched_at + 60.0)
    assert w.controller.switched_phases == 1
    order: list[str] = []
    adapter = w.controller.adapter
    set_phases = adapter.async_set_phases

    async def set_current(amps: int, **_kw: Any) -> str:
        order.append(f"current {amps}")
        return ASSIGN_ASSIGNED

    async def phases(n: int) -> Any:
        order.append(f"phases {n}")
        return await set_phases(n)

    adapter.async_set_current = set_current  # type: ignore[method-assign]
    adapter.async_set_phases = phases  # type: ignore[method-assign]
    # Sun for three phases with margin; one phase at its maximum.
    w.draw(16.0, sun_w=6000.0)
    rose_at = w.t
    await w.run(rose_at + UP_DELAY_S - 20.0)
    assert w.force_calls() == ["1"], "not before the up delay"
    while len(w.force_calls()) < 2 and w.t < max(rose_at + UP_DELAY_S, switched_at + MIN_BETWEEN_S) + 20.0:
        await w.at(w.t + 10.0)
    assert w.force_calls() == ["1", "3"]
    assert w.reasons()[-1] == "phase_up"
    # The minimum current went out before the switch, and the charge is asked no more than that.
    assert order.index("current 6") < order.index("phases 3")
    assert w.controller.requested_current_a == 6
    # Until the read-back shows it, the charge is reckoned on three phases: never more than three phases carry.
    await w.at(w.t + 10.0)
    assert w.controller.switched_phases is None
    assert w.controller.requested_current_a <= 8
    await w.charger_takes(3)
    w.draw(6.0)
    await w.run(w.t + 60.0)
    assert w.controller.phase_switching_facts()["given_up"] is False
    assert w.controller.requested_current_a == 8  # 6 kW over three phases
    assert w.charge_stops() == 0


async def test_a_dip_to_one_kw_stops_without_a_switch(hass: HomeAssistant, freezer: Any) -> None:
    w = await _charging_three_phases(hass, freezer)
    w.draw(6.0, sun_w=1000.0)
    start = w.t
    await w.run(start + 400.0)
    assert w.force_calls() == []
    assert "stop_after_delay" in w.reasons()
    assert w.charge_stops() == 1


async def test_option_off_never_switches(hass: HomeAssistant, freezer: Any) -> None:
    w = await _charging_three_phases(hass, freezer, option=False)
    assert w.controller.phase_switch_allowed() is False
    w.draw(6.0, sun_w=3000.0)
    start = w.t
    await w.run(start + 400.0)
    assert w.force_calls() == []
    assert "stop_after_delay" in w.reasons()
    assert w.controller.phase_switching_facts()["allowed"] is False


async def test_a_plan_window_start_sets_three_phases_before_the_start(hass: HomeAssistant, freezer: Any) -> None:
    w = await _switched_down(hass, freezer)
    await w.charger_takes(1)
    await w.run(w.t + 20.0)
    assert w.controller.switched_phases == 1
    switches = w.controller.phase_switches_this_plug_in
    order: list[str] = []
    adapter = w.controller.adapter
    set_phases, start = adapter.async_set_phases, adapter.async_start

    async def phases(n: int) -> Any:
        order.append(f"phases {n}")
        return await set_phases(n)

    async def started(amps: int | None) -> bool:
        order.append("start")
        return await start(amps)

    adapter.async_set_phases = phases  # type: ignore[method-assign]
    adapter.async_start = started  # type: ignore[method-assign]
    # The charge is off when the plan's window opens.
    hass.states.async_set(SWITCH, "off")
    await hass.async_block_till_done()
    assert await w.controller.async_start(16, cause="plan_window")
    assert order == ["phases 3", "start"]
    assert w.force_calls()[-1] == "3"
    assert w.controller.phase_switches_this_plug_in == switches, "setting three back is not a switch of the budget"
    assert w.controller.phase_switch_settling


async def test_a_persons_start_sets_three_phases_too_and_a_sun_start_does_not(
    hass: HomeAssistant, freezer: Any
) -> None:
    w = await _world(hass, freezer)
    hass.states.async_set(FORCE, "on")
    w.controller.reconcile_phases()
    assert w.controller.switched_phases == 1
    await w.controller.async_start(10, cause="solar")
    assert w.force_calls() == [], "the sun's own start keeps the phase it chose"
    hass.states.async_set(SWITCH, "off")
    await w.controller.async_start(10, manual=True)
    assert w.force_calls() == ["3"]


async def test_the_settle_after_a_switch_is_not_the_car_ending_the_charge(hass: HomeAssistant, freezer: Any) -> None:
    w = await _switched_down(hass, freezer)
    switched_at = w.t
    # The charger reports not charging for 60 s after the switch.
    hass.states.async_set(SWITCH, "off")
    w.draw(0.0)
    await w.run(switched_at + 60.0)
    assert w.controller.phase_switch_settling
    # Still solar's charge: neither ended by the car nor by the charger (on three phases until the read-back, short
    # of the minimum, the stop delay's timer runs, as it would).
    assert w.coordinator.state.state in ("on", "disarming")
    assert "charger_stopped" not in w.reasons() and "car_stopped" not in w.reasons()
    await w.charger_takes(1)
    hass.states.async_set(SWITCH, "on")
    w.draw(13.0)
    await w.run(switched_at + 150.0)
    assert not w.controller.phase_switch_settling
    assert w.coordinator.state.state == "on", w.verdicts[-20:]
    assert "charger_stopped" not in w.reasons() and "car_stopped" not in w.reasons(), w.verdicts
    assert w.charge_stops() == 0


async def test_a_switch_the_read_back_never_confirms_is_given_up_for_the_plug_in(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    w = await _switched_down(hass, freezer)
    switched_at = w.t
    # The switch stays off: the charger never took it.
    await w.run(switched_at + CONFIRM_S + 10.0)
    facts = w.controller.phase_switching_facts()
    assert facts["given_up"] is True
    assert facts["given_up_count"] == 1
    assert facts["allowed"] is False
    assert w.controller.switched_phases is None, "unconfirmed: reckoned on three phases"
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "phase switch" in r.getMessage()]
    assert len(warnings) == 1
    assert not [issue for (domain, _id), issue in ir.async_get(hass).issues.items() if domain == DOMAIN]
    # The charge goes on by the rules without switching: on three phases, short of the minimum, it stops.
    await w.run(w.t + 400.0)
    assert w.force_calls() == ["1"]
    assert "stop_after_delay" in w.reasons()


async def test_a_restart_while_forced_reads_the_switch_back(hass: HomeAssistant, freezer: Any) -> None:
    w = await _switched_down(hass, freezer)
    await w.charger_takes(1)
    await w.run(w.t + 20.0)
    assert w.controller.switched_phases == 1
    await hass.config_entries.async_reload(w.charger.entry_id)
    await hass.async_block_till_done()
    controller = controller_of(hass, w.charger.entry_id)
    assert controller is not w.controller
    controller.reconcile_phases()
    assert controller.switched_phases == 1
    assert controller.phase_switches_this_plug_in == 1, "the budget is the plug-in's, kept across the restart"
    # Read back as three: the read-back wins over what was stored.
    hass.states.async_set(FORCE, "off")
    controller.reconcile_phases()
    assert controller.switched_phases is None


async def test_a_restart_where_the_read_back_says_three_is_not_forced(hass: HomeAssistant, freezer: Any) -> None:
    w = await _switched_down(hass, freezer)
    await w.charger_takes(1)
    await w.run(w.t + 20.0)
    hass.states.async_set(FORCE, "off")
    await hass.config_entries.async_reload(w.charger.entry_id)
    await hass.async_block_till_done()
    controller = controller_of(hass, w.charger.entry_id)
    controller.reconcile_phases()
    assert controller.switched_phases is None
    site = controller_of(hass, "solar_site")
    assert site.reckoned_phases(CHARGER) == (3, None)


async def test_the_budget_resets_on_a_new_plug_in_only(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _switched_down(hass, freezer)
    await w.charger_takes(1)
    await w.run(w.t + 20.0)
    controller = w.controller
    solar = w.coordinator._solar  # noqa: SLF001
    assert controller.phase_switches_this_plug_in == 1
    assert solar._phase_switches == 1  # noqa: SLF001
    # A passing reading of no car, as solar sees it: not a new plug-in.
    monkeypatch.setattr(w.coordinator, "_car_unplugged", lambda: True)
    await w.at(w.t + 10.0)
    assert w.coordinator.state.reason == "no_car"
    monkeypatch.undo()
    await w.at(w.t + 10.0)
    assert controller.phase_switches_this_plug_in == 1
    assert w.coordinator._solar._phase_switches == 1  # noqa: SLF001
    # An unplug and a plug-in: the whole budget again. Nothing is sent at the unplug.
    controller._connection_changed(True, False)  # noqa: SLF001
    controller._connection_changed(False, True)  # noqa: SLF001
    await hass.async_block_till_done()
    await w.at(w.t + 10.0)
    assert controller.phase_switches_this_plug_in == 0
    assert w.coordinator._solar._phase_switches == 0  # noqa: SLF001
    assert controller.switched_phases == 1, "nothing is sent at an unplug: the charger is still forced"
    assert w.force_calls() == ["1"]


async def test_a_window_start_retry_waits_for_the_settle(hass: HomeAssistant, freezer: Any) -> None:
    """A charger that shows no charge while its switch settles has not failed a start: the retry is decided once
    the settle is over."""
    w = await _world(hass, freezer)
    controller = w.controller
    controller._start_retry_since = dt_util.utcnow()  # noqa: SLF001
    controller._phases.sent(3, dt_util.utcnow(), counted=False)  # noqa: SLF001
    controller._async_start_retry_callback(dt_util.utcnow())  # noqa: SLF001
    assert controller._start_retry_cancel is not None, "deferred to the settle's end"  # noqa: SLF001
    assert controller.window_start_retry["retries"] == 0
    assert controller.window_start_retry["last"] is None
    freezer.tick(timedelta(seconds=SETTLE_S + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    # Decided now (no window is open in this world: the retry ends there).
    assert controller.window_start_retry["last"] is not None


async def test_a_forced_one_phase_charge_teaches_nothing_about_the_car(hass: HomeAssistant, freezer: Any) -> None:
    from custom_components.spotnav.planning.phases import _counts, KEY_ONE_PHASE

    w = await _switched_down(hass, freezer)
    await w.charger_takes(1)
    w.draw(13.0)
    await w.run(w.t + 100.0)
    assert w.controller.switched_phases == 1 and not w.controller.phase_switch_settling
    controller = w.controller
    # The three-phase stretch before the switch ends; then a whole charge on the forced phase.
    hass.states.async_set(SWITCH, "off")
    controller._async_sample_phases()  # noqa: SLF001
    hass.states.async_set(SWITCH, "on")
    for _ in range(6):
        controller._async_sample_phases()  # noqa: SLF001
    hass.states.async_set(SWITCH, "off")
    w.draw(0.0)
    controller._async_sample_phases()  # noqa: SLF001
    await hass.async_block_till_done()
    assert _counts(hass, f"charger:{CHARGER}")[KEY_ONE_PHASE] == 0


async def test_load_balancing_must_lower_during_the_settle_still_lowers(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A site with active control and a charger whose phase switch settles: an import overload's lowering is written
    as ever, while a raise on that charger waits for the settle to end."""
    from custom_components.spotnav.execution.controller import REGULATED_WROTE, RegulatedWrite
    from custom_components.spotnav.site import site_capacity_controller as scc
    from custom_components.spotnav.site.regulator_damping import RegulatorDamper

    from .test_yield_stepping_wiring import _decision, _yield_setup

    site_entry, charger, _calls = await _yield_setup(hass, entry_id="settle_lb", yield_stepping_enabled=False)
    site = controller_of(hass, site_entry.entry_id)
    charger_controller = controller_of(hass, charger.entry_id)
    cid = charger.entry_id
    for cancel in site._controller_listener_cancels:  # noqa: SLF001
        cancel()
    site._controller_listener_cancels.clear()  # noqa: SLF001
    monkeypatch.setattr(scc, "applyability_failure", lambda **_kwargs: None)
    writes: list[tuple[int, bool]] = []

    async def write(amps: int, *, must_lower: bool) -> RegulatedWrite:
        writes.append((amps, must_lower))
        return RegulatedWrite(REGULATED_WROTE, "ok", True)

    monkeypatch.setattr(charger_controller, "async_apply_regulated_current", write)
    # A switch was just sent: it settles.
    charger_controller._phases.sent(1, dt_util.utcnow(), counted=True)  # noqa: SLF001
    assert charger_controller.phase_switch_settling

    async def apply(proposed: float, reason: str, previous: float = 16.0) -> None:
        writes.clear()
        damper = RegulatorDamper(deadband_a=0.0, dwell_s=0.0)
        damper.record_write(previous)
        site._dampers[cid] = damper  # noqa: SLF001
        site._written_at.clear()  # noqa: SLF001
        site.regulator_decisions = {cid: _decision(proposed_a=proposed, requested_a=16.0, reason=reason)}
        await site._async_apply_active_control()  # noqa: SLF001

    await apply(10.0, "reducing_current_due_to_active_import_overload")
    assert len(writes) == 1 and writes[0][1] is True and writes[0][0] < 16, "a must-lower never waits for the settle"
    await apply(12.0, "no_change_requested", previous=8.0)
    assert writes == [], "nothing else is decided on the settling charger"
    # Once settled, the same raise is written.
    charger_controller._phases.sent_at = dt_util.utcnow() - timedelta(seconds=SETTLE_S + 1)  # noqa: SLF001
    await apply(12.0, "no_change_requested", previous=8.0)
    assert writes and writes[0][0] > 8


async def test_given_up_on_one_phase_a_plan_window_still_sets_three_phases_before_the_start(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An up-switch the charger never confirmed leaves it on one phase, given up for the plug-in: a plan window's start
    still tries three phases once (the minimum current first, not counted) before it starts, and starts anyway."""
    w = await _switched_down(hass, freezer)
    switched_at = w.t
    await w.charger_takes(1)
    w.draw(16.0, sun_w=6000.0)
    await w.run(switched_at + 60.0)
    while len(w.force_calls()) < 2 and w.t < switched_at + MIN_BETWEEN_S + UP_DELAY_S + 60.0:
        await w.at(w.t + 10.0)
    assert w.force_calls() == ["1", "3"]
    # The read-back stays at one phase: given up, on one phase.
    await w.run(w.t + CONFIRM_S + 10.0)
    facts = w.controller.phase_switching_facts()
    assert facts["given_up"] is True and facts["given_up_count"] == 1
    assert w.controller.switched_phases == 1
    switches = w.controller.phase_switches_this_plug_in

    order: list[str] = []
    adapter = w.controller.adapter
    set_phases, start = adapter.async_set_phases, adapter.async_start

    async def phases(n: int) -> Any:
        order.append(f"phases {n}")
        return await set_phases(n)

    async def started(amps: int | None) -> bool:
        order.append("start")
        return await start(amps)

    adapter.async_set_phases = phases  # type: ignore[method-assign]
    adapter.async_start = started  # type: ignore[method-assign]
    hass.states.async_set(SWITCH, "off")
    await hass.async_block_till_done()
    assert await w.controller.async_start(16, cause="plan_window")
    assert order == ["phases 3", "start"]
    assert w.controller.phase_switches_this_plug_in == switches
    # Not confirmed either: no new give-up, and the next start of the plug-in does not try again.
    freezer.tick(timedelta(seconds=CONFIRM_S + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert w.controller.phase_switching_facts()["given_up_count"] == 1
    order.clear()
    hass.states.async_set(SWITCH, "off")
    assert await w.controller.async_start(16, cause="plan_window")
    assert order == ["start"]


async def test_a_charger_never_switched_is_never_touched_before_a_start(hass: HomeAssistant, freezer: Any) -> None:
    w = await _world(hass, freezer, option=False)
    hass.states.async_set(FORCE, "on")  # forced by hand, not by us
    w.controller.reconcile_phases()
    await w.controller.async_start(16, cause="plan_window")
    assert w.force_calls() == []
