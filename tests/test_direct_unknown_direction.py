"""A direct site that can never know a phase's direction (its currents carry no sign, or no total grid power to
confirm one): load balancing still lowers an overload by |I|, whoever started the charge, never raises on it, caps a
start by the room |I| leaves, and resumes a charge it paused (`SiteCapacityController`). The reported case: a Tibber
Pulse's currents picked by hand without *The grid current is signed* and without the total, 25 A fuse, 1 A margin,
a car started at 16 A, L3 read 32.8 A."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import CONF_GRID_POWER_SOURCE, CONF_SITE_CURRENT_SIGNED, REGULATOR_DWELL_S

from .helpers import make_entry, make_site_entry
from .test_direct_signed_active_control import _assigned, _charger_reads, _ocpp_charger, _report, _tick, _wiring
from .world import controller_of

MAGNITUDE = "lowering_overload_on_current_magnitude_direction_unknown"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _unsigned_site(hass: HomeAssistant, site: str, chargers: list, prefixes: list[str], extra: dict | None = None):
    return make_site_entry(
        hass,
        entry_id=site,
        main_fuse_a=25.0,
        safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id for charger in chargers],
        phase_wiring={charger.entry_id: _wiring(prefix) for charger, prefix in zip(chargers, prefixes)},
        active_control_enabled=True,
        extra_data=extra,
    )


async def _setup(hass: HomeAssistant, name: str, site_currents=(8.0, 8.0, 8.0)):
    charger, configure_calls = await _ocpp_charger(hass, f"{name}_charger")
    _charger_reads(hass, f"{name}_charger", 0.0)
    _report(hass, f"{name}_site", site_currents, 0.0)
    site_entry = _unsigned_site(hass, f"{name}_site", [charger], [f"{name}_charger"])
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    return charger, configure_calls, controller_of(hass, site_entry.entry_id)


async def test_the_reported_case_is_lowered_at_once_and_never_raised(hass: HomeAssistant) -> None:
    charger, configure_calls, site = await _setup(hass, "pulse")
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

    # The load goes and more is asked for: nothing is raised on an unknown direction.
    configure_calls.clear()
    _charger_reads(hass, "pulse_charger", 7.0)
    await cc.async_set_requested_current(20)
    _report(hass, "pulse_site", (17.0, 17.0, 15.0), 0.0)
    await _tick(hass)
    await _tick(hass)
    assert site.regulator_decisions[charger.entry_id].proposed_current_a is None
    assert configure_calls == []


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
    hass: HomeAssistant, monkeypatch
) -> None:
    """The person raises the charging current during a charge (`async_apply_setting_current`): the write goes out at
    no more than the room |I| leaves, as a start does, and the regulator raises nothing from there."""
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

    configure_calls.clear()
    await _tick(hass)
    assert site.regulator_decisions[charger.entry_id].proposed_current_a is None
    assert configure_calls == []
