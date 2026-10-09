"""Load balancing right after a start, and the charge it paused: the field trace of 2026-10-06.

A Charge Amps HALO on OCPP behind a 20 A main fuse with no safety margin, the site derived from a Sigenergy meter.
03:30:00 the plan's window started the car; 03:30:14 the site meter already showed it (L1 17.25 A) while the
charger's own current still read 0 (OCPP meter values lag), so the regulator took the car for house load and paused
it. The pause was then never resumed inside the window: the regulator's damper still believed the old current and
every pass "paused" the stopped charger again, which forgot the charge it held back. 04:00:04 the same stopped the
next window's start that was still on its way.

Driven through `SiteCapacityController` with the real regulator, explicit passes, and the stepper's, the damper's
and the dwell's clocks moved by hand.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_MEASURED_CURRENT_SOURCE,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    MEASUREMENT_MODE_DERIVED,
)
from custom_components.spotnav.core.events import BalancingPause, CommandResult
from custom_components.spotnav.core.ownership import decide
from custom_components.spotnav.core.session import ChargeSession
from custom_components.spotnav.site.regulator_damping import RegulatorDamper
from custom_components.spotnav.site.site_capacity_controller import START_CREDIT_S

from .helpers import make_entry, make_site_entry
from .test_battery_fuse import _end_the_wish
from .test_yield_stepping_wiring import _DamperClock, _SecondsClock
from .world import controller_of, separate_entities_source, set_charger_delivered_a, set_derived_site_entities

_PAUSE = "paused_safe_current_below_charger_minimum"
_V = 230.0


def _site_a(hass: HomeAssistant, site: str, l1: float, l2: float, l3: float) -> None:
    """The site meter's current per phase (all import, unity power factor)."""
    for phase, amps in (("l1", l1), ("l2", l2), ("l3", l3)):
        hass.states.async_set(f"sensor.{site}_power_{phase}", str(amps * _V), {"unit_of_measurement": "W"})


class _Halo:
    def __init__(self, hass, controller, charger, cc, site, prefix, configure, turn_on, turn_off, clock, damper_clock):
        self.hass = hass
        self.controller = controller
        self.charger = charger
        self.cc = cc
        self.site = site
        self.prefix = prefix
        self.configure = configure
        self.turn_on = turn_on
        self.turn_off = turn_off
        self.clock = clock
        self.damper_clock = damper_clock

    async def tick(self) -> None:
        """One recompute on the latest readings and the apply pass it schedules."""
        self.controller._recompute(schedule_apply=False)  # noqa: SLF001
        await self.controller._async_apply_active_control()  # noqa: SLF001
        await self.hass.async_block_till_done()

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.damper_clock.advance(seconds)

    def switch(self, state: str) -> None:
        self.hass.states.async_set(f"switch.{self.prefix}", state)

    def reads(self, amps: float) -> None:
        set_charger_delivered_a(self.hass, self.prefix, amps)

    def log(self) -> list[dict]:
        return self.controller.regulator_decision_log

    def written(self) -> list[str]:
        return [call.data["value"] for call in self.configure]

    async def start(self, amps: int = 16) -> None:
        """The plan's window starts the car; its charge control is not seen on yet (OCPP answers later)."""
        assert await self.cc.async_start(amps, cause="plan_window")
        await self.hass.async_block_till_done()


async def _halo(
    hass: HomeAssistant,
    monkeypatch,
    entry_id: str = "halo",
    *,
    l1=1.63,
    l2=0.92,
    l3=6.36,
    battery_entity: str | None = None,
) -> _Halo:
    """The owner's site at 03:30, the car plugged in, not charging, the charger reading nothing. `battery_entity`: the
    home battery's power (W, positive while it charges), as the owner's Sigenergy reports it."""
    from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter

    prefix = f"{entry_id}_charger"
    hass.states.async_set(f"switch.{prefix}", "off")
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.16,2.10"})
    configure = async_mock_service(hass, "ocpp", "configure")
    monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda self: True)
    charger = make_entry(
        hass,
        entry_id=prefix,
        charge_control=f"switch.{prefix}",
        current_limit=f"number.{prefix}_connector_1_session_current_limit",
        webhook_id=f"webhook-{entry_id}",
        title="HALO",
        current_control=CURRENT_CONTROL_CHANGE_CONFIGURATION,
        ocpp_target=(prefix, 1),
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    set_charger_delivered_a(hass, prefix, 0.0)
    site = f"{entry_id}_site"
    derived = set_derived_site_entities(hass, site, 0.0)
    _site_a(hass, site, l1, l2, l3)
    site_entry = make_site_entry(
        hass,
        entry_id=site,
        main_fuse_a=20.0,
        safety_margin_a=0.0,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                "min_current_a": 6.0,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                    f"sensor.{prefix}_l1", f"sensor.{prefix}_l2", f"sensor.{prefix}_l3"
                ),
            }
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived,
        active_control_enabled=True,
        yield_stepping_enabled=True,
        battery_aggregate_power_entity=battery_entity,
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    controller = controller_of(hass, site_entry.entry_id)
    # Registered after the entries: setting them up loads the switch platform, whose own services would win.
    turn_on = async_mock_service(hass, "switch", "turn_on")
    turn_off = async_mock_service(hass, "switch", "turn_off")
    # Explicit passes only, on whole readings.
    if controller._state_listener_cancel is not None:  # noqa: SLF001
        controller._state_listener_cancel()  # noqa: SLF001
        controller._state_listener_cancel = None  # noqa: SLF001
    for cancel in controller._controller_listener_cancels:  # noqa: SLF001
        cancel()
    controller._controller_listener_cancels.clear()  # noqa: SLF001
    cc = controller_of(hass, charger.entry_id)
    # The start cap and its reservation, as the site wires them.
    cc.set_start_cap(
        lambda: controller.start_allowance_a(charger.entry_id),
        reserve=lambda amps: controller.reserve_start(charger.entry_id, amps),
    )
    clock = _SecondsClock()
    controller._yield_now = clock  # noqa: SLF001
    damper_clock = _DamperClock()
    controller._dampers[charger.entry_id] = RegulatorDamper(now=damper_clock)  # noqa: SLF001
    controller._yield_steppers.clear()  # noqa: SLF001
    configure.clear()
    return _Halo(hass, controller, charger, cc, site, prefix, configure, turn_on, turn_off, clock, damper_clock)


def _reasons(halo: _Halo, since: int = 0) -> list[str]:
    return [entry["reason"] for entry in halo.log()[since:]]


# -- 1. the charger's own draw is credited right after a start


async def test_0330_the_site_seeing_the_car_before_its_own_reading_does_not_pause_it(
    hass: HomeAssistant, monkeypatch
) -> None:
    halo = await _halo(hass, monkeypatch)
    await halo.start()
    assert halo.written() == ["1.13,2.10"], "started at what L3 allows: 20 - 6.36"
    halo.switch("on")
    await hass.async_block_till_done()
    since = len(halo.log())

    # 03:30:14: the meter shows the car on L1 first, the charger's own current still reads 0.
    _site_a(hass, halo.site, 17.14, 0.92, 6.26)
    await halo.tick()
    # And a moment later on every phase.
    _site_a(hass, halo.site, 17.25, 13.92, 19.26)
    await halo.tick()
    halo.advance(30.0)
    await halo.tick()

    assert halo.turn_off == [], "the car the site meter shows is the charger's own, not house load"
    assert _PAUSE not in _reasons(halo, since)

    # The charger's reading arrives: from now on it is what counts.
    halo.reads(13.0)
    await halo.tick()
    assert halo.turn_off == []
    assert halo.controller.start_credit_snapshot() == {}


async def test_0200_a_pause_pending_its_dwell_is_never_proposed_while_the_car_is_credited(
    hass: HomeAssistant, monkeypatch
) -> None:
    halo = await _halo(hass, monkeypatch, l1=1.66, l2=0.83, l3=0.74)
    await halo.start()
    halo.switch("on")
    await hass.async_block_till_done()
    since = len(halo.log())
    # 02:00:14: the car on L1 only; 02:00:35 everywhere, and the charger's reading with it.
    _site_a(hass, halo.site, 17.11, 0.91, 0.93)
    await halo.tick()
    halo.advance(20.0)
    _site_a(hass, halo.site, 17.21, 16.41, 16.08)
    halo.reads(15.6)
    await halo.tick()
    assert _PAUSE not in _reasons(halo, since)
    assert halo.turn_off == []


async def test_a_real_overload_with_the_credit_applied_still_lowers_at_once(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _halo(hass, monkeypatch)
    await halo.start()
    halo.switch("on")
    await hass.async_block_till_done()
    halo.configure.clear()
    # L3 carries 6.36 A of house load and the car's 13 A, plus 2 A more: over the fuse even with the car credited.
    _site_a(hass, halo.site, 14.63, 13.92, 21.36)
    await halo.tick()
    assert halo.written() == ["1.11,2.10"], "lowered by the overload at once, no dwell: 13 - 1.36"
    assert halo.turn_off == []


async def test_an_overload_the_minimum_cannot_fit_still_pauses_at_once(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _halo(hass, monkeypatch)
    await halo.start()
    halo.switch("on")
    await hass.async_block_till_done()
    _site_a(hass, halo.site, 30.0, 13.92, 19.36)
    await halo.tick()
    assert len(halo.turn_off) == 1, "13 credited, 10 over the fuse: no room for the minimum, stopped at once"


async def test_the_credit_is_never_more_than_the_charger_was_given(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _halo(hass, monkeypatch)
    await halo.start()
    halo.switch("on")
    await hass.async_block_till_done()
    halo.configure.clear()
    # L1 rose by 25 A: the car's 13 and 12 more of something else, which is not the car's.
    _site_a(hass, halo.site, 26.63, 13.92, 19.36)
    await halo.tick()
    assert halo.written() == ["1.6,2.10"], "13 credited and 6.63 over: lowered to 6, not judged on a 25 A credit"


async def test_the_credit_ends_after_its_bound_and_the_measurement_counts_again(
    hass: HomeAssistant, monkeypatch
) -> None:
    halo = await _halo(hass, monkeypatch)
    await halo.start()
    halo.switch("on")
    await hass.async_block_till_done()
    _site_a(hass, halo.site, 17.14, 13.92, 19.36)
    await halo.tick()
    assert halo.controller.start_credit_snapshot() != {}
    halo.advance(START_CREDIT_S + 1.0)
    await halo.tick()
    assert halo.controller.start_credit_snapshot() == {}
    assert _PAUSE in _reasons(halo), "still nothing from the charger itself after the bound: today's rule again"


# -- 2. a balancing pause is resumed inside the window


async def _paused_in_the_window(halo: _Halo) -> None:
    """The car charges at 13 A by its own reading; a load on L1 leaves no room for the minimum: paused at once."""
    await halo.start()
    halo.switch("on")
    halo.reads(13.0)
    _site_a(halo.hass, halo.site, 14.63, 13.92, 19.36)
    await halo.hass.async_block_till_done()
    await halo.tick()
    _site_a(halo.hass, halo.site, 30.0, 13.92, 19.36)
    await halo.tick()
    assert len(halo.turn_off) == 1
    assert halo.cc.paused_by_balancing is False, "still seen charging until the charger answers"
    # The charger answers: off, drawing nothing; the load is gone again.
    halo.switch("off")
    halo.reads(0.0)
    _site_a(halo.hass, halo.site, 1.6, 0.85, 4.44)
    await halo.hass.async_block_till_done()
    assert halo.cc.paused_by_balancing


async def test_a_balancing_pause_is_resumed_in_the_window_after_the_dwell(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _halo(hass, monkeypatch)
    await _paused_in_the_window(halo)
    starts = len(halo.turn_on)

    await halo.tick()
    halo.advance(30.0)
    await halo.tick()
    assert len(halo.turn_on) == starts, "not before the dwell"
    assert len(halo.turn_off) == 1, "a stopped charger is not paused again"
    assert halo.cc.paused_by_balancing, "and the charge it holds back is not forgotten"

    halo.advance(31.0)
    await halo.tick()
    assert len(halo.turn_on) == starts + 1
    assert halo.log()[-1]["outcome"] == "resumed"
    assert halo.cc.charge_origin == "plan_window" and halo.cc._plan_charge, "the plan's charge, not a new one"  # noqa: SLF001


@pytest.mark.parametrize("ending", ["final_window_end", "person_stop"])
async def test_a_balancing_pause_is_not_resumed_once_the_charge_is_no_longer_wanted(
    hass: HomeAssistant, monkeypatch, ending: str
) -> None:
    halo = await _halo(hass, monkeypatch, f"halo_{ending}")
    await _paused_in_the_window(halo)
    starts = len(halo.turn_on)
    await _end_the_wish(hass, halo.cc, halo.charger, ending)
    for _ in range(3):
        halo.advance(61.0)
        await halo.tick()
    assert len(halo.turn_on) == starts


async def test_0400_a_start_on_its_way_is_not_paused_for_asking_nothing(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _halo(hass, monkeypatch, l1=0.6, l2=0.37, l3=0.46)
    await _paused_in_the_window(halo)
    _site_a(hass, halo.site, 0.6, 0.37, 0.46)
    # 04:00: the next window starts the car; the charger has not said it is on yet.
    await halo.start()
    assert halo.cc.start_pending
    for _ in range(3):
        halo.advance(25.0)
        await halo.tick()
    assert len(halo.turn_off) == 1, "the start on its way is not stopped as a charger asking for nothing"


async def test_a_repeat_pause_of_a_stopped_charge_keeps_the_charge_it_holds_back(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _halo(hass, monkeypatch)
    await _paused_in_the_window(halo)
    # The regulator's damper forgotten (a restart, active control turned off and on): it pauses once more.
    halo.controller._dampers.clear()  # noqa: SLF001
    await halo.tick()
    assert halo.cc.paused_by_balancing


def test_the_core_keeps_the_held_charge_through_a_repeat_pause() -> None:
    now = dt_util.utcnow()
    session = ChargeSession(plugged=True, balancing_paused=True, paused_origin="plan")
    session, commands = decide(session, BalancingPause(code="pause", was_on=False), now)
    assert [command.kind for command in commands] == ["stop"]
    session, _ = decide(session, CommandResult(command="stop", reason="balancing", executed=True), now)
    assert session.balancing_paused and session.paused_origin == "plan"


def test_a_pause_of_a_charge_nobody_held_still_holds_nothing() -> None:
    now = dt_util.utcnow()
    session, _ = decide(ChargeSession(plugged=True), BalancingPause(code="pause", was_on=False), now)
    session, _ = decide(session, CommandResult(command="stop", reason="balancing", executed=True), now)
    assert not session.balancing_paused


async def test_a_resume_is_retried_after_the_dwell_when_nothing_moved(hass: HomeAssistant, monkeypatch) -> None:
    """The time after a pause in the trace (03:30-04:00): a quiet site, the charge resumed once, not once a pass."""
    halo = await _halo(hass, monkeypatch)
    await _paused_in_the_window(halo)
    starts = len(halo.turn_on)
    for _ in range(4):
        halo.advance(61.0)
        await halo.tick()
    assert len(halo.turn_on) == starts + 1
