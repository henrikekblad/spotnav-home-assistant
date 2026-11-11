"""One site reading, one cut, also when the second cut would be a pause: the night of 2026-11-11 (times UTC).

A direct site with signed per-phase readings (a HomeWizard P1), 25 A main fuse, 5 A safety margin (a 20 A limit), a
three-phase charger read every few seconds (an Alfen over modbus), minimum 6 A, the plan asking 18 A. A house load of
about 10 A on L1 and L2 comes and goes.

    02:58:40  wrote 8 -> 18 (increase_within_safe_uncredited_margin), site 8.89/5.98/7.54
    03:00:06  wrote 18 -> 9 (reducing_current_due_to_active_import_overload), site 26.35/22.91/14.52,
              charger 15.54/15.09/13.96: the cut
    03:00:06  held 9 (overload_already_lowered), the same readings
    03:00:10  held 0 (paused_safe_current_below_charger_minimum): site 26.35/22.91/14.52, unchanged, the charger
              8.77/8.45/7.86, fresh: the cut took, and the stop went out
    03:00:16  site 11.76/19.11/8.37, the first site reading after the cut; the car was started again at 03:01:21

The second decision paired the site reading taken while the car drew 15.5 A with the charger's new 8.8 A: a house of
26.35 - 8.77 = 17.6 A on L1, 2.4 A left for the car, a pause. The same site reading was cut on twice, the second time
to a stop: a needless stop and restart, a contactor cycle. Now a cut again on the reading the last cut was made on is
held while the charger's own reading shows that cut took (`StepGuard.cut_took_on_reading`); the next site reading
decides as before, cut or pause included, and a charger that still reads above what was written is cut again at once.

Driven through `SiteCapacityController` with the real regulator and the real charger controller.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_GRID_POWER_SOURCE,
    CONF_SITE_CURRENT_SIGNED,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
)
from custom_components.spotnav.site.regulator import MEASUREMENT_LOST_GRACE_S
from custom_components.spotnav.site.step_guard import CUT_SHOWN_AFTER_S, SAME_READING_S, StepGuard, UP_MARGIN_A

from .charger_helpers import mock_ocpp_assigned_current
from .helpers import make_entry, make_site_entry, set_current_sensor
from .test_direct_signed_active_control import _assigned, _tick, _wiring
from .world import controller_of

_V = 230.0
_MAX_AGE_S = 120.0
#: The house while the direction is confirmed (above the 500 W zero band on every phase), and the house as the night's
#: log had it at the raise, the site with the car at 8 A less that 8 A.
_HOUSE_BEFORE = (3.0, 2.6, 2.6)
_HOUSE = (0.89, 0.5, 0.5)
_RAISED = (26.35, 22.91, 14.52)
_CAR_AT_CUT = (15.54, 15.09, 13.96)
_CAR_AFTER_CUT = (8.77, 8.45, 7.86)
_AFTER_CUT = (11.76, 19.11, 8.37)


def _site_reads(hass: HomeAssistant, site: str, currents: tuple[float, float, float]) -> None:
    for suffix, amps in zip(("l1", "l2", "l3"), currents):
        set_current_sensor(hass, f"sensor.{site}_{suffix}", amps)
    hass.states.async_set(f"sensor.{site}_grid_power", str(round(sum(currents) * _V, 1)), {"unit_of_measurement": "W"})


def _charger_reads(hass: HomeAssistant, prefix: str, currents: tuple[float, float, float]) -> None:
    for suffix, amps in zip(("l1", "l2", "l3"), currents):
        set_current_sensor(hass, f"sensor.{prefix}_{suffix}", amps)


def _with_car(car: float, wobble: float = 0.0, house: tuple[float, float, float] = _HOUSE) -> tuple[float, float, float]:
    return tuple(round(h + car + wobble, 2) for h in house)  # type: ignore[return-value]


async def _charger(hass: HomeAssistant, prefix: str):
    """An OCPP charger that takes every current written (a must-lower write is read back)."""
    hass.states.async_set(f"switch.{prefix}", "off")
    async_mock_service(hass, "switch", "turn_on")
    configure_calls = mock_ocpp_assigned_current(hass, "1.6,2.10")
    charger = make_entry(
        hass,
        entry_id=prefix,
        charge_control=f"switch.{prefix}",
        current_limit=f"number.{prefix}_connector_1_session_current_limit",
        webhook_id=f"webhook-{prefix}",
        title="Alfen",
        current_control=CURRENT_CONTROL_CHANGE_CONFIGURATION,
        ocpp_target=(prefix, 1),
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    return charger, configure_calls


async def _night(hass: HomeAssistant, freezer, name: str):
    """The site at 03:00:06: the car cut 18 -> 9 on the reading `_RAISED`, the stop not sent."""
    charger, configure_calls = await _charger(hass, f"{name}_charger")
    _charger_reads(hass, f"{name}_charger", (0.0, 0.0, 0.0))
    _site_reads(hass, f"{name}_site", _with_car(0.0, house=_HOUSE_BEFORE))
    site_entry = make_site_entry(
        hass,
        entry_id=f"{name}_site",
        main_fuse_a=25.0,
        safety_margin_a=5.0,
        max_age_s=_MAX_AGE_S,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={charger.entry_id: _wiring(f"{name}_charger")},
        active_control_enabled=True,
        extra_data={
            CONF_SITE_CURRENT_SIGNED: True,
            CONF_GRID_POWER_SOURCE: {"power": f"sensor.{name}_site_grid_power"},
        },
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, site_entry.entry_id)
    cc = controller_of(hass, charger.entry_id)
    turn_off = async_mock_service(hass, "switch", "turn_off")
    # The direction confirmed on the readings before the car.
    for i in range(8):
        freezer.tick(timedelta(seconds=1))
        _site_reads(hass, f"{name}_site", _with_car(0.0, 0.01 * (i % 2), house=_HOUSE_BEFORE))
        await _tick(hass)

    # The plan starts the car at 8 A; the site shows it, and the house's own load goes.
    assert await cc.async_start(amps=8, cause="plan_window")
    hass.states.async_set(f"switch.{name}_charger", "on")
    freezer.tick(timedelta(seconds=2))
    _charger_reads(hass, f"{name}_charger", (8.0, 8.0, 8.0))
    for i in range(4):
        freezer.tick(timedelta(seconds=1))
        _site_reads(hass, f"{name}_site", _with_car(8.0, 0.01 * (i % 2), house=_HOUSE_BEFORE if i < 2 else _HOUSE))
        await _tick(hass)
    assert site.active_control_writes(charger.entry_id)

    # 02:58:40, the plan asks 18 A, written once the room has lasted the dwell (the meter reports every 10 s).
    await cc.async_set_requested_current(18)
    for i in range(7):
        freezer.tick(timedelta(seconds=10))
        _site_reads(hass, f"{name}_site", _with_car(8.0, 0.02 + 0.01 * (i % 2)))
        await _tick(hass)
    assert _assigned(configure_calls)[-1:] == [18]
    assert turn_off == []

    # The car takes the raise; 03:00:06, the house load on L1 and L2 with the car at 15.5 A: 6.35 A over on L1, cut
    # to 15.54 - 6.35 = 9 A.
    for i in range(8):
        freezer.tick(timedelta(seconds=10))
        _charger_reads(hass, f"{name}_charger", (15.54 + 0.01 * (i % 2), 15.09, 13.96))
        _site_reads(hass, f"{name}_site", _with_car(15.54, 0.01 * (i % 2)))
        await _tick(hass)
    assert _assigned(configure_calls)[-1:] == [18]
    freezer.tick(timedelta(seconds=6))
    _charger_reads(hass, f"{name}_charger", _CAR_AT_CUT)
    _site_reads(hass, f"{name}_site", _RAISED)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason, decision.limiting_phase) == (
        9.0,
        "reducing_current_due_to_active_import_overload",
        "L1",
    )
    assert _assigned(configure_calls)[-2:] == [18, 9]
    assert turn_off == []

    # The pass right after, the same readings: nothing to lower.
    freezer.tick(timedelta(milliseconds=300))
    await _tick(hass)
    assert _assigned(configure_calls)[-2:] == [18, 9]
    assert turn_off == []
    return charger, configure_calls, turn_off, site


async def test_the_cut_that_took_is_not_followed_by_a_pause_on_the_same_site_reading(hass: HomeAssistant, freezer) -> None:
    charger, configure_calls, turn_off, site = await _night(hass, freezer, "held")

    # 03:00:10: the charger shows the cut took, the site's meter has not reported since. Reckoned afresh that is a
    # house of 17.6 A on L1 and 2.4 A for the car, a pause; it is the reading the cut was made on, so it is held.
    freezer.tick(timedelta(seconds=4))
    _charger_reads(hass, "held_charger", _CAR_AFTER_CUT)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason) == (0.0, "paused_safe_current_below_charger_minimum")
    assert _assigned(configure_calls)[-2:] == [18, 9]
    assert turn_off == []
    assert site.regulator_decision_log[-1]["detail"] == "held_same_reading"

    # Held on, as long as the reading is the same one.
    freezer.tick(timedelta(seconds=2))
    await _tick(hass)
    assert _assigned(configure_calls)[-2:] == [18, 9]
    assert turn_off == []

    # 03:00:16, the first site reading after the cut: 0.9 A of room on L2, nothing over, no pause; the regulator
    # goes on as usual (a raise waits for its dwell).
    freezer.tick(timedelta(seconds=4))
    _site_reads(hass, "held_site", _AFTER_CUT)
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert decision.proposed_current_a is not None
    assert decision.proposed_current_a >= 9.0
    assert decision.reason != "paused_safe_current_below_charger_minimum"
    assert _assigned(configure_calls)[-2:] == [18, 9]
    assert turn_off == []


async def test_a_charger_that_still_reads_above_what_was_written_is_cut_again_at_once(hass: HomeAssistant, freezer) -> None:
    """The cut did not take: the charger reads 15 A against the 9 A written. Cut again at once, as before (15 - 6.35
    gives 8 A)."""
    charger, configure_calls, turn_off, site = await _night(hass, freezer, "again")
    freezer.tick(timedelta(seconds=4))
    _charger_reads(hass, "again_charger", (15.0, 15.0, 15.0))
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason) == (8.0, "reducing_current_due_to_active_import_overload")
    assert _assigned(configure_calls)[-3:] == [18, 9, 8]
    assert turn_off == []


async def test_the_next_site_reading_that_still_shows_the_overload_pauses_as_before(hass: HomeAssistant, freezer) -> None:
    """The cut took and the hold is on; the next reading of L1 still shows 24 A with the car at 8.8 A: 4.8 A left,
    below the minimum, the pause as before."""
    charger, configure_calls, turn_off, site = await _night(hass, freezer, "next")
    freezer.tick(timedelta(seconds=4))
    _charger_reads(hass, "next_charger", _CAR_AFTER_CUT)
    await _tick(hass)
    assert _assigned(configure_calls)[-2:] == [18, 9]
    assert turn_off == []

    freezer.tick(timedelta(seconds=6))
    _site_reads(hass, "next_site", (24.0, 20.5, 8.3))
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason) == (0.0, "paused_safe_current_below_charger_minimum")
    assert _assigned(configure_calls)[-2:] == [18, 9]
    assert [call.data.get("entity_id") for call in turn_off] == ["switch.next_charger"]


async def test_a_reading_older_than_the_maximum_age_is_left_to_the_stale_handling(hass: HomeAssistant, freezer) -> None:
    """The hold never outlives the site reading: once it is older than `max_age_s` the stale handling decides as
    before, nothing on an unusable reading, then the charger stepped to its minimum when the measurement has been
    lost for `MEASUREMENT_LOST_GRACE_S`. Never the pause."""
    charger, configure_calls, turn_off, site = await _night(hass, freezer, "stale")
    freezer.tick(timedelta(seconds=4))
    _charger_reads(hass, "stale_charger", _CAR_AFTER_CUT)
    await _tick(hass)
    assert _assigned(configure_calls)[-2:] == [18, 9]
    assert turn_off == []

    # The site's meter goes quiet: the reading grows older than the maximum age, and nothing is decided on it.
    freezer.tick(timedelta(seconds=_MAX_AGE_S + 1))
    _charger_reads(hass, "stale_charger", (8.76, 8.45, 7.85))
    await _tick(hass)
    decision = site.regulator_decisions[charger.entry_id]
    assert (decision.proposed_current_a, decision.reason) == (None, "phase_measurement_unusable")
    assert _assigned(configure_calls)[-2:] == [18, 9]
    assert turn_off == []

    # The measurement has been lost for the grace: stepped to the minimum, as any charger on a lost measurement.
    freezer.tick(timedelta(seconds=MEASUREMENT_LOST_GRACE_S + 1))
    _charger_reads(hass, "stale_charger", (8.77, 8.46, 7.86))
    await _tick(hass)
    assert _assigned(configure_calls)[-3:] == [18, 9, 6]
    assert turn_off == []


def test_the_guard_holds_a_cut_again_only_where_the_charger_shows_the_cut_took() -> None:
    guard = StepGuard()
    guard.note_write(now=100.0, previous_a=18.0, written_a=9.0, site_read_at=99.0)
    took = dict(site_read_at=99.0, written_a=9.0, written_at=100.0)
    # Every phase read since the cut at or below what was written plus the step: held.
    assert guard.cut_took_on_reading(readings=[(8.77, 104.0), (8.45, 104.0), (7.86, 104.0)], **took)
    assert guard.cut_took_on_reading(readings=[(9.0 + UP_MARGIN_A, 104.0)], **took)
    # A phase read since the cut above it: the cut did not take, cut again.
    assert not guard.cut_took_on_reading(readings=[(9.0 + UP_MARGIN_A + 0.1, 104.0)], **took)
    assert not guard.cut_took_on_reading(readings=[(8.77, 104.0), (15.0, 104.0)], **took)
    # A phase read before the cut went out, or within the second after, shows nothing of it: the phases read since
    # decide.
    assert guard.cut_took_on_reading(readings=[(8.77, 104.0), (15.09, 99.5), (13.96, 100.0)], **took)
    assert guard.cut_took_on_reading(readings=[(8.77, 104.0), (15.09, 100.0 + CUT_SHOWN_AFTER_S - 0.1)], **took)
    assert not guard.cut_took_on_reading(readings=[(8.77, 104.0), (15.09, 100.0 + CUT_SHOWN_AFTER_S)], **took)
    assert not guard.cut_took_on_reading(readings=[(8.0, 99.5), (8.0, 100.5)], **took)
    # A newer site reading decides on its own; one within `SAME_READING_S` is the same reading, reckoned back from
    # its age a moment later.
    assert not guard.cut_took_on_reading(**{**took, "site_read_at": 105.0}, readings=[(8.0, 104.0)])
    assert not guard.cut_took_on_reading(**{**took, "site_read_at": 99.0 + SAME_READING_S + 0.1}, readings=[(8.0, 104.0)])
    assert guard.cut_took_on_reading(**{**took, "site_read_at": 99.0 + SAME_READING_S - 0.1}, readings=[(8.0, 104.0)])
    # No cut on record, or nothing to compare with: never held.
    assert not StepGuard().cut_took_on_reading(readings=[(8.0, 104.0)], **took)
    assert not guard.cut_took_on_reading(**{**took, "written_a": None}, readings=[(8.0, 104.0)])
    assert not guard.cut_took_on_reading(readings=[(8.0, None)], **took)
    assert not guard.cut_took_on_reading(readings=[(None, 104.0)], **took)
    assert not guard.cut_took_on_reading(readings=[], **took)
