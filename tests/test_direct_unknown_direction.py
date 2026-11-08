"""A direct site that can never know a phase's direction (its currents carry no sign, or no total grid power to
confirm one): load balancing still lowers an overload by |I|, whoever started the charge, raises the charge again
within the room |I| leaves (never above its request), caps a start by that room, and resumes a charge it paused
(`SiteCapacityController`). The reported cases: a Tibber Pulse's currents picked by hand without *The grid current is
signed* and without the total, 25 A fuse, 1 A margin, a car started at 16 A, L3 read 32.8 A; and a SolarEdge meter's
currents (magnitudes), 25 A fuse, no margin, a plan's 16 A lowered to 6 A by the oven and never raised again."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import CONF_GRID_POWER_SOURCE, CONF_SITE_CURRENT_SIGNED, REGULATOR_DWELL_S
from custom_components.spotnav.site.regulator import MAGNITUDE_RAISE_REASON
from custom_components.spotnav.site.step_guard import CATCH_UP_S

from .helpers import make_entry, make_site_entry
from .test_direct_signed_active_control import _assigned, _charger_reads, _ocpp_charger, _report, _tick, _wiring
from .world import controller_of

MAGNITUDE = "lowering_overload_on_current_magnitude_direction_unknown"
RAISE = MAGNITUDE_RAISE_REASON


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _DwellClock:
    """The damper's clock, run ahead of the wall clock by `shift` seconds: the raise's dwell is elapsed without
    freezing the wall clock, under which a write and the reading it answers would share one instant."""

    def __init__(self) -> None:
        self.shift = 0.0

    def __call__(self):
        from homeassistant.util import dt as dt_util

        return dt_util.utcnow() + timedelta(seconds=self.shift)


def _dwell_clock(site, charger_entry_id: str) -> _DwellClock:
    clock = _DwellClock()
    site._damper_for(charger_entry_id)._now = clock  # noqa: SLF001
    return clock


def _unsigned_site(
    hass: HomeAssistant, site: str, chargers: list, prefixes: list[str], extra: dict | None = None, margin: float = 1.0
):
    return make_site_entry(
        hass,
        entry_id=site,
        main_fuse_a=25.0,
        safety_margin_a=margin,
        charger_entry_ids=[charger.entry_id for charger in chargers],
        phase_wiring={charger.entry_id: _wiring(prefix) for charger, prefix in zip(chargers, prefixes)},
        active_control_enabled=True,
        extra_data=extra,
    )


async def _setup(hass: HomeAssistant, name: str, site_currents=(8.0, 8.0, 8.0), margin: float = 1.0):
    charger, configure_calls = await _ocpp_charger(hass, f"{name}_charger")
    _charger_reads(hass, f"{name}_charger", 0.0)
    _report(hass, f"{name}_site", site_currents, 0.0)
    site_entry = _unsigned_site(hass, f"{name}_site", [charger], [f"{name}_charger"], margin=margin)
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    return charger, configure_calls, controller_of(hass, site_entry.entry_id)


async def test_the_reported_case_is_lowered_at_once_and_raised_again_within_the_room(hass: HomeAssistant) -> None:
    charger, configure_calls, site = await _setup(hass, "pulse")
    dwell = _dwell_clock(site, charger.entry_id)
    cc = controller_of(hass, charger.entry_id)

    # The plan's window opens with room for the car's 16 A on every phase (24 A limit, 8 A on L3).
    assert await cc.async_start(amps=16, cause="plan_window")
    hass.states.async_set("switch.pulse_charger", "on")
    _charger_reads(hass, "pulse_charger", 16.0)
    _report(hass, "pulse_site", (24.0, 24.0, 24.0), 0.0)
    await _tick(hass)
    assert _assigned(configure_calls)[-1:] == [16]
    assert not site._direction_known_for(charger.entry_id)
    assert not site.active_control_writes(charger.entry_id)
    decision = site.regulator_decisions[charger.entry_id]
    assert decision.proposed_current_a is None
    assert decision.reason == "phase_direction_unknown_at_charge_start"

    # The house takes more on L3: 32.8 A against a 24 A limit. Lowered at once to 16 - 8.8 = 7.2, so 7 A.
    _report(hass, "pulse_site", (24.0, 24.0, 32.8), 0.0)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason, decision.limiting_phase) == (7.0, MAGNITUDE, "L3")
    assert _assigned(configure_calls)[-1:] == [7]

    # The load goes and more is asked for: raised within the room |I| leaves (7 A on L1 with the car at 7 A, so 14 A),
    # a step short of the edge, once the room has lasted for the dwell. Never the 20 A asked for.
    configure_calls.clear()
    _report(hass, "pulse_site", (17.0, 17.0, 15.0), 0.0)
    _charger_reads(hass, "pulse_charger", 7.0)
    await cc.async_set_requested_current(20)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason, decision.limiting_phase) == (14.0, RAISE, "L1")
    assert configure_calls == []  # the dwell
    dwell.shift = REGULATOR_DWELL_S + 1
    await _tick(hass)
    assert _assigned(configure_calls) == [13]


async def test_the_oven_case_is_lowered_at_once_and_comes_back_up_in_steps(hass: HomeAssistant) -> None:
    """The SolarEdge case: 25 A fuse, no margin, a plan's 16 A on three phases. The oven puts L3 at 34.4 A: lowered at
    once to 6 A. The oven goes off: the car comes back up in steps within the room, to its 16 A, each step after the
    dwell. The oven comes on again: lowered again at once. Never a write storm: one lowering, one raise per step."""
    charger, configure_calls, site = await _setup(hass, "oven", site_currents=(2.0, 2.0, 3.5), margin=0.0)
    dwell = _dwell_clock(site, charger.entry_id)
    cc = controller_of(hass, charger.entry_id)
    assert await cc.async_start(amps=16, cause="plan_window")
    hass.states.async_set("switch.oven_charger", "on")
    _charger_reads(hass, "oven_charger", 16.0)
    _report(hass, "oven_site", (18.0, 18.0, 19.5), 0.0)
    await _tick(hass)
    assert _assigned(configure_calls) == [16]

    # 08:17, the oven: L3 at 34.4 A against the 25 A fuse, the car at 16 A: 16 - 9.4 = 6.6, so 6 A, at once.
    _report(hass, "oven_site", (18.0, 18.0, 34.4), 0.0)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason, decision.limiting_phase) == (6.0, MAGNITUDE, "L3")
    assert _assigned(configure_calls) == [16, 6]
    # The car follows (the site's meter shows it first, then the charger's own reading); the oven is still on: 0.6 A
    # of room is no whole amp, nothing is raised.
    _report(hass, "oven_site", (8.0, 8.0, 24.4), 0.0)
    _charger_reads(hass, "oven_charger", 6.0)
    await _tick(hass)
    assert site.regulator_decisions[charger.entry_id].proposed_current_a is None
    assert _assigned(configure_calls) == [16, 6]

    # The oven cycles off but another load keeps L3 at 18 A: 7 A of room, so 13 A proposed, 12 A a step short of the
    # edge, and only once the room has lasted for the dwell. Before, this was held `not_a_lowering` for hours.
    _report(hass, "oven_site", (8.0, 8.0, 18.0), 0.0)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason, decision.limiting_phase) == (13.0, RAISE, "L3")
    assert _assigned(configure_calls) == [16, 6]
    dwell.shift = REGULATOR_DWELL_S / 2
    await _tick(hass)
    assert _assigned(configure_calls) == [16, 6]
    dwell.shift = REGULATOR_DWELL_S + 1
    await _tick(hass)
    assert _assigned(configure_calls) == [16, 6, 12]

    # The car follows to 12 A and the other load goes too: L3 at 15.5 A, 9.5 A of room, the request within it: 16 A,
    # a new candidate with its own dwell.
    _report(hass, "oven_site", (14.0, 14.0, 15.5), 0.0)
    _charger_reads(hass, "oven_charger", 12.0)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason) == (16.0, RAISE)
    assert _assigned(configure_calls) == [16, 6, 12]
    dwell.shift += REGULATOR_DWELL_S + 1
    await _tick(hass)
    assert _assigned(configure_calls) == [16, 6, 12, 16]
    assert cc.requested_current_a == 16

    # The oven again, the car at 16 A: lowered again at once.
    _charger_reads(hass, "oven_charger", 16.0)
    _report(hass, "oven_site", (18.0, 18.0, 34.4), 0.0)
    await _tick(hass)
    assert _assigned(configure_calls) == [16, 6, 12, 16, 6]


async def test_a_person_start_and_a_plan_start_are_capped_by_the_room_the_current_leaves(hass: HomeAssistant) -> None:
    charger, configure_calls, site = await _setup(hass, "capped", site_currents=(10.0, 10.0, 12.0))
    cc = controller_of(hass, charger.entry_id)
    # 24 A limit, 12 A on L3: a start gets 12 A of the 16 asked.
    assert site.start_allowance_a(charger.entry_id) == 12.0
    assert await cc.async_start(amps=16, manual=True)
    assert _assigned(configure_calls)[-1:] == [12]

    hass.states.async_set("switch.capped_charger", "off")
    await _tick(hass)
    site.reserve_start(charger.entry_id, None)
    _report(hass, "capped_site", (10.0, 10.0, 15.0), 0.0)
    await _tick(hass)
    configure_calls.clear()
    assert await cc.async_start(amps=16, cause="plan_window")
    assert _assigned(configure_calls)[-1:] == [9]


async def test_a_charge_paused_on_an_unknown_direction_is_resumed_within_the_room(
    hass: HomeAssistant, monkeypatch
) -> None:
    from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter

    monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda self: True)
    charger, configure_calls, site = await _setup(hass, "resume")
    clock = _Clock()
    site._yield_now = clock
    cc = controller_of(hass, charger.entry_id)
    assert await cc.async_start(amps=10, manual=True)
    hass.states.async_set("switch.resume_charger", "on")
    _charger_reads(hass, "resume_charger", 10.0)
    _report(hass, "resume_site", (20.0, 20.0, 18.0), 0.0)
    await _tick(hass)

    # 10 A over on L3 with the car at 10 A: no room for its 6 A minimum, so it is paused.
    turn_off = async_mock_service(hass, "switch", "turn_off")
    _report(hass, "resume_site", (20.0, 20.0, 34.0), 0.0)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason) == (0.0, MAGNITUDE)
    assert [call.data.get("entity_id") for call in turn_off] == ["switch.resume_charger"]
    hass.states.async_set("switch.resume_charger", "off")
    _charger_reads(hass, "resume_charger", 0.0)
    _report(hass, "resume_site", (10.0, 10.0, 15.0), 0.0)
    await _tick(hass)
    assert cc.paused_by_balancing

    # 9 A of room on L3 for the dwell: started again at 9 A, the room |I| leaves.
    configure_calls.clear()
    clock.now += REGULATOR_DWELL_S + 1
    await _tick(hass)
    assert _assigned(configure_calls)[-1:] == [9]


async def test_a_switch_only_charger_on_an_unknown_direction_is_paused_over_the_fuse(hass: HomeAssistant) -> None:
    prefix = "unsigned_switch"
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
    _report(hass, "unsigned_switch_site", (10.0, 10.0, 8.0), 0.0)
    site_entry = _unsigned_site(hass, "unsigned_switch_site", [charger], [prefix])
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, site_entry.entry_id)
    cc = controller_of(hass, charger.entry_id)
    assert cc.is_switch_only
    assert await cc.async_start(amps=10, manual=True)
    hass.states.async_set(f"switch.{prefix}", "on")
    _charger_reads(hass, prefix, 10.0)
    _report(hass, "unsigned_switch_site", (20.0, 20.0, 18.0), 0.0)
    await _tick(hass)
    assert turn_off == []

    _report(hass, "unsigned_switch_site", (20.0, 20.0, 26.0), 0.0)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason) == (0.0, MAGNITUDE)
    assert [call.data.get("entity_id") for call in turn_off] == [f"switch.{prefix}"]


async def test_a_signed_site_with_its_total_keeps_its_rules(hass: HomeAssistant) -> None:
    """Where the direction can be known, nothing changes: the unsigned site's lowering is not taken for one that
    knows it imports (`decide_charging_current` decides there)."""
    charger, _ = await _ocpp_charger(hass, "known_charger")
    _charger_reads(hass, "known_charger", 0.0)
    _report(hass, "known_site", (12.0, 13.0, 14.0), 39.0 * 230.0)
    site_entry = _unsigned_site(
        hass,
        "known_site",
        [charger],
        ["known_charger"],
        extra={CONF_SITE_CURRENT_SIGNED: True, CONF_GRID_POWER_SOURCE: {"power": "sensor.known_site_grid_power"}},
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, site_entry.entry_id)
    for _ in range(8):
        _report(hass, "known_site", (12.0, 13.0, 14.0), 39.0 * 230.0)
        await _tick(hass)
    cc = controller_of(hass, charger.entry_id)
    assert await cc.async_start(amps=10, manual=True)
    hass.states.async_set("switch.known_charger", "on")
    _charger_reads(hass, "known_charger", 10.0)
    for _ in range(4):
        _report(hass, "known_site", (22.0, 23.0, 27.0), 72.0 * 230.0)
        await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert site.active_control_writes(charger.entry_id)
    assert decision.reason == "reducing_current_due_to_active_import_overload"


async def test_the_site_says_load_balancing_only_lowers_until_the_sign_and_the_total_are_set(
    hass: HomeAssistant,
) -> None:
    from custom_components.spotnav.api.dashboard import _active_control_reason

    charger, _, site = await _setup(hass, "hint")
    load_balancing = site.capability_snapshot.load_balancing
    # Still available (it lowers an overload), but never without a reason.
    assert load_balancing.active_available
    assert load_balancing.blocking_reasons == ("site_direction_unknown",)
    assert _active_control_reason(site) == "site_direction_unknown"

    # The sign alone is not enough: the total confirms it.
    site.config[CONF_SITE_CURRENT_SIGNED] = True
    assert site.capability_snapshot.load_balancing.blocking_reasons == ("site_direction_unknown",)
    # Both set: the reason goes by itself.
    site.config[CONF_GRID_POWER_SOURCE] = {"power": "sensor.hint_site_grid_power"}
    assert site.capability_snapshot.load_balancing.blocking_reasons == ()
    assert _active_control_reason(site) is None


async def test_a_raised_charging_current_setting_is_capped_by_the_room_the_current_leaves(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The person raises the charging current during a charge (`async_apply_setting_current`): the write goes out at
    no more than the room |I| leaves, as a start does; the regulator gives the rest once there is room for it."""
    charger, configure_calls, site = await _setup(hass, "setting")
    cc = controller_of(hass, charger.entry_id)
    assert await cc.async_start(amps=10, manual=True)
    hass.states.async_set("switch.setting_charger", "on")
    _charger_reads(hass, "setting_charger", 10.0)
    _report(hass, "setting_site", (18.0, 18.0, 22.0), 0.0)
    await _tick(hass)
    assert _assigned(configure_calls)[-1:] == [10]

    # 24 A limit, 22 A on L3 with the car at 10 A: 12 A is all a write may give it, whatever is set.
    configure_calls.clear()
    monkeypatch.setattr(cc, "_setting_amps", lambda: 16)
    await cc.async_apply_setting_current()
    await hass.async_block_till_done()
    assert _assigned(configure_calls) == [12]
    assert cc.requested_current_a == 16

    # The 12 A written is all the room there is: the regulator's 12 A is no raise over it.
    configure_calls.clear()
    await _tick(hass)
    assert site.regulator_decisions[charger.entry_id].proposed_current_a == 12.0
    assert configure_calls == []

    # The car follows and the house gives way (L3 at 14 A with the car at 12 A): the rest of the 16 A, after the dwell.
    freezer.tick(timedelta(seconds=15))
    _report(hass, "setting_site", (18.0, 18.0, 14.0), 0.0)
    _charger_reads(hass, "setting_charger", 12.0)
    await _tick(hass)
    assert (site.regulator_decisions[charger.entry_id].proposed_current_a, site.regulator_decisions[charger.entry_id].reason) == (16.0, RAISE)
    assert configure_calls == []
    freezer.tick(timedelta(seconds=REGULATOR_DWELL_S + 1))
    _report(hass, "setting_site", (18.0, 18.0, 14.0), 0.0)
    await _tick(hass)
    assert _assigned(configure_calls) == [16]


async def test_a_ceiling_between_nothing_and_the_minimum_pauses_the_charge(hass: HomeAssistant) -> None:
    """L3 at 31 A against a 24 A limit with the car at 10 A: 3 A is left, below its 6 A minimum. The charge is
    paused at once, through the lowering's own path (it needs no signed power), not held for want of one."""
    charger, configure_calls, site = await _setup(hass, "between")
    cc = controller_of(hass, charger.entry_id)
    assert await cc.async_start(amps=10, manual=True)
    hass.states.async_set("switch.between_charger", "on")
    _charger_reads(hass, "between_charger", 10.0)
    _report(hass, "between_site", (20.0, 20.0, 18.0), 0.0)
    await _tick(hass)
    turn_off = async_mock_service(hass, "switch", "turn_off")

    _report(hass, "between_site", (20.0, 20.0, 31.0), 0.0)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason) == (0.0, MAGNITUDE)
    assert [call.data.get("entity_id") for call in turn_off] == ["switch.between_charger"]


async def test_a_slow_meter_does_not_hold_the_lowering(hass: HomeAssistant, monkeypatch) -> None:
    """Load balancing is held while a meter it reads reports too seldom, but an overload's lowering is not: the
    lowering by |I| goes out as an import overload's does, and a raise is still held."""
    from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController

    charger, configure_calls, site = await _setup(hass, "slow")
    cc = controller_of(hass, charger.entry_id)
    assert await cc.async_start(amps=16, manual=True)
    hass.states.async_set("switch.slow_charger", "on")
    _charger_reads(hass, "slow_charger", 16.0)
    _report(hass, "slow_site", (24.0, 24.0, 24.0), 0.0)
    await _tick(hass)
    monkeypatch.setattr(
        SiteCapacityController, "load_balancing_slow_meters", property(lambda self: {"sensor.slow_site_l3": 120.0})
    )
    configure_calls.clear()
    _report(hass, "slow_site", (24.0, 24.0, 32.8), 0.0)
    await _tick(hass)
    assert site.regulator_decisions[charger.entry_id].reason == MAGNITUDE
    assert _assigned(configure_calls)[-1:] == [7]


async def test_an_unavailable_site_gives_its_own_reason_not_the_direction(hass: HomeAssistant) -> None:
    from custom_components.spotnav.api.dashboard import _active_control_reason

    _report(hass, "nocharger_site", (8.0, 8.0, 8.0), 0.0)
    site_entry = _unsigned_site(hass, "nocharger_site", [], [])
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, site_entry.entry_id)
    load_balancing = site.capability_snapshot.load_balancing
    assert not load_balancing.active_available
    assert load_balancing.blocking_reasons == ("site_direction_unknown",)
    assert _active_control_reason(site) == "no_commandable_charger"


async def test_a_lowered_charger_still_reading_its_old_current_is_not_given_it_back(
    hass: HomeAssistant, monkeypatch
) -> None:
    """After a lowering to 7 A, a charger read far less often still reports 16 A for a while. What a write may give
    it is then reckoned from the 7 A written, not from the 16 A read: a changed setting does not raise it again."""
    charger, configure_calls, site = await _setup(hass, "lag")
    cc = controller_of(hass, charger.entry_id)
    assert await cc.async_start(amps=16, manual=True)
    hass.states.async_set("switch.lag_charger", "on")
    _charger_reads(hass, "lag_charger", 16.0)
    _report(hass, "lag_site", (24.0, 24.0, 24.0), 0.0)
    await _tick(hass)
    _report(hass, "lag_site", (24.0, 24.0, 32.8), 0.0)
    await _tick(hass)
    assert _assigned(configure_calls)[-1:] == [7]

    # The site already shows the car at 7 A (L3 at 23.8 A); the charger's own reading still says 16 A.
    _report(hass, "lag_site", (15.0, 15.0, 23.8), 0.0)
    await _tick(hass)
    assert site.start_allowance_a(charger.entry_id) == pytest.approx(7.2)
    configure_calls.clear()
    monkeypatch.setattr(cc, "_setting_amps", lambda: 16)
    await cc.async_apply_setting_current()
    await hass.async_block_till_done()
    assert _assigned(configure_calls) in ([], [7])


async def _at_eight(hass: HomeAssistant, freezer, name: str):
    """A car started at 16 A, lowered to 8 A by L3 at 32 A against the 24 A limit; it follows to 8 A."""
    charger, configure_calls, site = await _setup(hass, name)
    cc = controller_of(hass, charger.entry_id)
    assert await cc.async_start(amps=16, manual=True)
    hass.states.async_set(f"switch.{name}_charger", "on")
    _charger_reads(hass, f"{name}_charger", 16.0)
    _report(hass, f"{name}_site", (24.0, 24.0, 24.0), 0.0)
    await _tick(hass)
    _report(hass, f"{name}_site", (24.0, 24.0, 32.0), 0.0)
    await _tick(hass)
    assert _assigned(configure_calls)[-1:] == [8]
    assert site._damper_for(charger.entry_id).last_written_a == 8.0  # noqa: SLF001
    # The car follows: the site's meter shows it first, then the charger's own reading.
    freezer.tick(timedelta(seconds=15))
    _report(hass, f"{name}_site", (16.0, 16.0, 20.0), 0.0)
    _charger_reads(hass, f"{name}_charger", 8.0)
    await _tick(hass)
    configure_calls.clear()
    return charger, configure_calls, site


async def test_a_charger_above_what_was_written_is_lowered_from_what_it_draws(
    hass: HomeAssistant, freezer, monkeypatch
) -> None:
    """8 A remembered, the car at 16 A (a current set in the vendor's app), L3 3 A over: the proposal is 13 A, above
    the 8 A written, and was held `not_a_lowering` on every pass with the phase over the fuse. Now the lowering
    starts from the 16 A the charger draws and goes out at no more than the 8 A written, at once."""
    from custom_components.spotnav.execution.chargers.ocpp import OcppAssignedCurrent

    async def _confirmed(self, connector: int, amps: int) -> bool:
        return True

    # The must-lower write is read back; this charger confirms it.
    monkeypatch.setattr(OcppAssignedCurrent, "_async_confirm", _confirmed)
    charger, configure_calls, site = await _at_eight(hass, freezer, "above")
    # The charger first, the site's meter a moment later (each reading is a pass of its own).
    freezer.tick(timedelta(seconds=15))
    _charger_reads(hass, "above_charger", 16.0)
    await hass.async_block_till_done()
    assert configure_calls == []
    _report(hass, "above_site", (24.0, 24.0, 27.0), 0.0)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason) == (13.0, MAGNITUDE)
    assert _assigned(configure_calls) == [8]
    assert site._damper_for(charger.entry_id).last_written_a == 8.0  # noqa: SLF001


async def test_a_reading_before_the_charger_had_time_to_follow_the_write_does_not_count(
    hass: HomeAssistant, freezer
) -> None:
    """A reading taken within `CATCH_UP_S` of the write is the charger still on its way down, not a charger above
    what was written: held as before, also once that reading has aged."""
    charger, configure_calls, site = await _setup(hass, "early")
    cc = controller_of(hass, charger.entry_id)
    assert await cc.async_start(amps=16, manual=True)
    hass.states.async_set("switch.early_charger", "on")
    _charger_reads(hass, "early_charger", 16.0)
    _report(hass, "early_site", (24.0, 24.0, 24.0), 0.0)
    await _tick(hass)
    _report(hass, "early_site", (24.0, 24.0, 32.0), 0.0)
    await _tick(hass)
    assert _assigned(configure_calls)[-1:] == [8]
    configure_calls.clear()
    freezer.tick(timedelta(seconds=CATCH_UP_S / 2))
    _report(hass, "early_site", (24.0, 24.0, 26.0), 0.0)
    _charger_reads(hass, "early_charger", 15.0)
    await _tick(hass)
    assert site.regulator_decisions[charger.entry_id].reason == MAGNITUDE
    freezer.tick(timedelta(seconds=CATCH_UP_S))
    _report(hass, "early_site", (24.0, 24.0, 26.5), 0.0)
    await _tick(hass)
    assert configure_calls == []
    assert site._damper_for(charger.entry_id).last_written_a == 8.0  # noqa: SLF001


async def test_a_pause_still_settling_is_not_sent_again_on_the_old_current(hass: HomeAssistant, freezer) -> None:
    """A pause by |I| goes out; the charger still reports its 10 A until it answers the stop. That is the stop on its
    way, not a charger above the nothing written: the pause is not sent again."""
    charger, configure_calls, site = await _setup(hass, "settle")
    cc = controller_of(hass, charger.entry_id)
    assert await cc.async_start(amps=10, manual=True)
    hass.states.async_set("switch.settle_charger", "on")
    _charger_reads(hass, "settle_charger", 10.0)
    _report(hass, "settle_site", (20.0, 20.0, 18.0), 0.0)
    await _tick(hass)
    turn_off = async_mock_service(hass, "switch", "turn_off")
    _report(hass, "settle_site", (20.0, 20.0, 31.0), 0.0)
    await _tick(hass)
    assert len(turn_off) == 1
    assert cc.stop_settling
    freezer.tick(timedelta(seconds=CATCH_UP_S + 1))
    _charger_reads(hass, "settle_charger", 10.5)
    _report(hass, "settle_site", (20.0, 20.0, 31.5), 0.0)
    await _tick(hass)
    assert cc.stop_settling
    assert len(turn_off) == 1
    assert site._damper_for(charger.entry_id).last_written_a == 0.0  # noqa: SLF001


async def test_a_charge_a_person_restarts_after_a_pause_is_lowered_not_paused_again(
    hass: HomeAssistant, freezer
) -> None:
    """Paused by |I|, the charger stops; minutes later a person starts it on the charger at 10 A (no start of ours,
    the site is not told) and L3 is 3 A over: the remembered pause is forgotten and the charge is lowered to the
    proposal, as for an import overload, never stopped a second time."""
    charger, configure_calls, site = await _setup(hass, "restart")
    cc = controller_of(hass, charger.entry_id)
    assert await cc.async_start(amps=10, manual=True)
    hass.states.async_set("switch.restart_charger", "on")
    _charger_reads(hass, "restart_charger", 10.0)
    _report(hass, "restart_site", (20.0, 20.0, 18.0), 0.0)
    await _tick(hass)
    turn_off = async_mock_service(hass, "switch", "turn_off")
    _report(hass, "restart_site", (20.0, 20.0, 31.0), 0.0)
    await _tick(hass)
    assert len(turn_off) == 1
    assert site._damper_for(charger.entry_id).last_written_a == 0.0  # noqa: SLF001
    freezer.tick(timedelta(seconds=5))
    hass.states.async_set("switch.restart_charger", "off")
    _charger_reads(hass, "restart_charger", 0.0)
    _report(hass, "restart_site", (20.0, 20.0, 20.0), 0.0)
    await _tick(hass)

    freezer.tick(timedelta(seconds=CATCH_UP_S * 30))
    hass.states.async_set("switch.restart_charger", "on")
    _charger_reads(hass, "restart_charger", 10.0)
    _report(hass, "restart_site", (20.0, 20.0, 27.0), 0.0)
    configure_calls.clear()
    await _tick(hass)
    await _tick(hass)

    assert site.regulator_decisions[charger.entry_id].reason == MAGNITUDE
    assert _assigned(configure_calls)[-1:] == [7]
    assert len(turn_off) == 1, "lowered, not stopped again"
