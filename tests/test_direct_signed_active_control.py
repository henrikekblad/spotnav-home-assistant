"""Active control on a direct site with signed currents: what a charge keeps when its confirmation is lost, and
how the sun's and active control's writes share the room (`SiteCapacityController`)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.const import (
    CONF_GRID_POWER_SOURCE,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_SITE_CURRENT_SIGNED,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    SITE_RECOMPUTE_INTERVAL_S,
)
from custom_components.spotnav.execution.chargers.base import WRITE_SOLAR

from .helpers import make_entry, make_site_entry, set_current_sensor
from .world import controller_of, separate_entities_source

PHASES = ("L1", "L2", "L3")


async def _tick(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SITE_RECOMPUTE_INTERVAL_S + 1))
    await hass.async_block_till_done()


def _report(hass: HomeAssistant, site: str, currents, grid_w: float) -> None:
    for suffix, amps in zip(("l1", "l2", "l3"), currents):
        set_current_sensor(hass, f"sensor.{site}_{suffix}", amps)
    hass.states.async_set(f"sensor.{site}_grid_power", str(grid_w), {"unit_of_measurement": "W"})


async def _confirm(hass: HomeAssistant, site: str, currents=(12.0, 13.0, 14.0), times: int = 8) -> None:
    for _ in range(times):
        _report(hass, site, currents, sum(currents) * 230.0)
        await _tick(hass)


def _charger_reads(hass: HomeAssistant, prefix: str, amps: float) -> None:
    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.{prefix}_{suffix}", amps)


def _wiring(prefix: str) -> dict:
    return {
        "phases": 3,
        "phase": None,
        CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
            f"sensor.{prefix}_l1", f"sensor.{prefix}_l2", f"sensor.{prefix}_l3"
        ),
    }


async def _ocpp_charger(hass: HomeAssistant, prefix: str):
    hass.states.async_set(f"switch.{prefix}", "off")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.6,2.10"})
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    charger = make_entry(
        hass,
        entry_id=prefix,
        charge_control=f"switch.{prefix}",
        current_limit=f"number.{prefix}_connector_1_session_current_limit",
        webhook_id=f"webhook-{prefix}",
        title="Direct charger",
        current_control=CURRENT_CONTROL_CHANGE_CONFIGURATION,
        ocpp_target=(prefix, 1),
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    return charger, configure_calls


async def _start(hass: HomeAssistant, charger, prefix: str, amps: int) -> None:
    await controller_of(hass, charger.entry_id).async_start(amps=amps)
    hass.states.async_set(f"switch.{prefix}", "on")
    await _tick(hass)


async def _stop(hass: HomeAssistant, prefix: str) -> None:
    hass.states.async_set(f"switch.{prefix}", "off")
    await _tick(hass)


def _site(hass: HomeAssistant, site: str, chargers: list, prefixes: list[str]):
    return make_site_entry(
        hass,
        entry_id=site,
        main_fuse_a=25.0,
        safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id for charger in chargers],
        phase_wiring={charger.entry_id: _wiring(prefix) for charger, prefix in zip(chargers, prefixes)},
        active_control_enabled=True,
        extra_data={CONF_SITE_CURRENT_SIGNED: True, CONF_GRID_POWER_SOURCE: {"power": f"sensor.{site}_grid_power"}},
    )


def _assigned(calls) -> list[int]:
    return [int(call.data["value"].split(",")[0].split(".")[1]) for call in calls]


async def test_a_lost_confirmation_still_lowers_an_overload_by_magnitude_and_never_raises(hass: HomeAssistant) -> None:
    charger, configure_calls = await _ocpp_charger(hass, "lost_charger")
    _charger_reads(hass, "lost_charger", 0.0)
    _report(hass, "lost_site", (12.0, 13.0, 14.0), 39.0 * 230.0)
    site_entry = _site(hass, "lost_site", [charger], ["lost_charger"])
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site_controller = controller_of(hass, site_entry.entry_id)
    await _confirm(hass, "lost_site")

    # The charge starts confirmed: active control is its writer for the whole charge.
    await _start(hass, charger, "lost_charger", 10)
    _charger_reads(hass, "lost_charger", 10.0)
    await _confirm(hass, "lost_site", currents=(12.0, 13.0, 14.0), times=4)
    assert site_controller._writer_direction_known(charger.entry_id)
    assert site_controller.active_control_writes(charger.entry_id)

    # The total turns against the currents: the direction is withdrawn mid-charge.
    _report(hass, "lost_site", (12.0, 13.0, 14.0), -39.0 * 230.0)
    await _tick(hass)
    assert not site_controller._direction_known_for(charger.entry_id)
    assert site_controller.active_control_writes(charger.entry_id)  # the sun does not take over
    decision = site_controller.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason) == (None, "phase_direction_unknown")

    # No overload and a raise asked for: nothing is written.
    configure_calls.clear()
    await controller_of(hass, charger.entry_id).async_set_requested_current(20)
    _charger_reads(hass, "lost_charger", 10.0)
    _report(hass, "lost_site", (12.0, 13.0, 14.0), -39.0 * 230.0)
    await _tick(hass)
    assert site_controller.regulator_decisions[charger.entry_id].proposed_current_a is None
    assert configure_calls == []

    # L3 over the 24 A limit by |I|: lowered at once to the charger's 10 A less the 2 A over.
    _report(hass, "lost_site", (12.0, 13.0, 26.0), -51.0 * 230.0)
    await _tick(hass)
    decision = site_controller.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason, decision.limiting_phase) == (
        8.0,
        "lowering_overload_on_current_magnitude_direction_unknown",
        "L3",
    )
    assert _assigned(configure_calls)[-1:] == [8]


async def test_the_sun_and_active_control_do_not_give_the_same_room_away_twice(hass: HomeAssistant) -> None:
    sun, _ = await _ocpp_charger(hass, "share_sun")
    ours, _ = await _ocpp_charger(hass, "share_ours")
    _charger_reads(hass, "share_sun", 0.0)
    _charger_reads(hass, "share_ours", 0.0)
    _report(hass, "share_site", (12.0, 13.0, 14.0), 39.0 * 230.0)
    site_entry = _site(hass, "share_site", [sun, ours], ["share_sun", "share_ours"])
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site_controller = controller_of(hass, site_entry.entry_id)

    # The sun's charger starts before the sign is confirmed (the sun writes it), ours after (active control does).
    await _start(hass, sun, "share_sun", 6)
    _charger_reads(hass, "share_sun", 6.0)
    await _confirm(hass, "share_site")
    await _start(hass, ours, "share_ours", 6)
    _charger_reads(hass, "share_ours", 6.0)
    await controller_of(hass, ours.entry_id).async_set_requested_current(16)
    await _confirm(hass, "share_site", times=3)
    assert not site_controller.active_control_writes(sun.entry_id)
    assert site_controller.active_control_writes(ours.entry_id)

    # 24 A limit, 14 A on L3: 10 A of room. With nothing given away, ours may have the 16 A it asks for.
    site_controller._regulator_write_reservations.clear()
    site_controller._recompute(schedule_apply=False)
    free = site_controller.regulator_decisions[ours.entry_id]
    assert free.reason == "increase_within_safe_uncredited_margin"
    assert free.proposed_current_a == 16.0

    # The sun writes its charger 10 A while it draws 6 A: 4 A of the room is the sun's until drawn.
    site_controller.note_charger_write(sun.entry_id, 10.0, WRITE_SOLAR)
    site_controller._recompute(schedule_apply=False)
    shared = site_controller.regulator_decisions[ours.entry_id]
    assert shared.proposed_current_a == 12.0
    # Once the sun's charger draws it, the site reading carries it; it is not counted again.
    _charger_reads(hass, "share_sun", 10.0)
    _report(hass, "share_site", (16.0, 17.0, 18.0), 51.0 * 230.0)
    await _tick(hass)
    drawn = site_controller.regulator_decisions[ours.entry_id]
    assert drawn.proposed_current_a == 12.0  # 6 A of room on L3 over ours at 6 A, not 4 A less again

    # The other way: active control's raise of ours is not given to the sun's write meanwhile.
    _charger_reads(hass, "share_sun", 6.0)
    _report(hass, "share_site", (12.0, 13.0, 14.0), 39.0 * 230.0)
    await _tick(hass)
    site_controller._solar_write_reservations.clear()
    site_controller._regulator_write_reservations.clear()
    before = site_controller.start_allowance_a(sun.entry_id)
    site_controller._reserve_write(site_controller._regulator_write_reservations, ours.entry_id, 9.0)
    after = site_controller.start_allowance_a(sun.entry_id)
    assert before == 16.0  # 10 A of room plus the 6 A it draws
    assert after == 13.0  # less the 3 A ours was given and has not drawn
