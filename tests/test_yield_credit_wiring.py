"""The battery credit, the decision log and the limit explanation through `SiteCapacityController`.

Same isolation as `test_yield_stepping_wiring.py`: explicit `_async_apply_active_control()` passes,
the stepper's and the damper's clocks moved by hand, handmade regulator decisions.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import SOLAR_PRIORITY_BATTERY_FIRST
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.site.regulator_damping import RegulatorDamper
from custom_components.spotnav.site.site_capacity_controller import (
    LIMIT_CAUSE_BATTERY_SHARES_FUSE,
    LIMIT_CAUSE_HOUSE_CONSUMPTION,
)

from .test_yield_stepping_wiring import (
    _DamperClock,
    _decision,
    _SecondsClock,
    _seed_assigned_a,
    _yield_setup,
)
from .world import controller_of, set_charger_delivered_a, set_site_current_a

_VOLTAGE_V = 230.0
_BATTERY = "sensor.credit_battery_power"


def _set_battery_a(hass: HomeAssistant, amps_per_phase: float) -> None:
    """The battery's charge, as the aggregate power the three phases would share equally."""
    hass.states.async_set(
        _BATTERY, str(round(amps_per_phase * 3 * _VOLTAGE_V)), {"unit_of_measurement": "W"}
    )


async def _confirmed_site(hass: HomeAssistant, entry_id: str, **setup):
    """A grid-charging battery that gave way to a 6 -> 8 A probe: yield confirmed and verified."""
    _set_battery_a(hass, 15.9)
    site_entry, charger, configure_calls = await _yield_setup(
        hass, entry_id=entry_id, battery_entity=_BATTERY, **setup
    )
    controller = controller_of(hass, site_entry.entry_id)
    yield_clock = _SecondsClock()
    controller._yield_now = yield_clock
    damper_clock = _DamperClock()
    controller._dampers[charger.entry_id] = RegulatorDamper(now=damper_clock)
    controller._yield_steppers.clear()
    prefix = f"{entry_id}_charger"
    site_prefix = f"{entry_id}_site"

    _seed_assigned_a(controller, charger.entry_id, 6.0)
    await controller._async_apply_active_control()
    configure_calls.clear()

    set_site_current_a(hass, site_prefix, 20.0)
    set_charger_delivered_a(hass, prefix, 5.34)
    pause = _decision(
        proposed_a=0.0, requested_a=16.0, reason="paused_safe_current_below_charger_minimum"
    )
    controller.regulator_decisions = {charger.entry_id: pause}
    await controller._async_apply_active_control()
    assert controller._yield_stepping_last[charger.entry_id]["reason"] == "probe_step"
    damper_clock.advance(61.0)
    await controller._async_apply_active_control()
    assert configure_calls[-1].data["value"] == "1.8,2.10"
    configure_calls.clear()

    # The car takes the 2 A; the battery gives up 1.3 A per phase and the grid does not move.
    set_charger_delivered_a(hass, prefix, 7.12)
    _set_battery_a(hass, 14.6)
    await controller._async_apply_active_control()
    assert controller._yield_stepping_last[charger.entry_id]["state"] == "settling"
    yield_clock.advance(31.0)
    await controller._async_apply_active_control()
    verdict = controller._yield_stepping_last[charger.entry_id]
    assert verdict["state"] == "confirmed"
    assert verdict["battery_verified"] is True
    assert configure_calls == []
    return controller, charger, configure_calls, yield_clock, pause


async def test_a_verified_yielding_battery_gives_a_larger_step_without_the_dwell(
    hass: HomeAssistant,
) -> None:
    controller, charger, configure_calls, yield_clock, pause = await _confirmed_site(
        hass, "credit"
    )

    yield_clock.advance(1.0)
    await controller._async_apply_active_control()

    verdict = controller._yield_stepping_last[charger.entry_id]
    assert (verdict["reason"], verdict["battery_credit_a"]) == ("confirmed_step", 14.6)
    # 8 + 3 A, written in the very pass: the damper's 60 s dwell does not hold a verified step.
    assert [call.data["value"] for call in configure_calls] == ["1.11,2.10"]


async def test_battery_first_keeps_todays_step_and_the_dwell(hass: HomeAssistant) -> None:
    controller, charger, configure_calls, yield_clock, pause = await _confirmed_site(
        hass, "creditbf", solar_priority=SOLAR_PRIORITY_BATTERY_FIRST
    )

    yield_clock.advance(1.0)
    await controller._async_apply_active_control()

    verdict = controller._yield_stepping_last[charger.entry_id]
    assert (verdict["reason"], verdict["battery_credit_a"]) == ("confirmed_step", None)
    # A 2 A step through the damper: pending, not written in this pass.
    assert configure_calls == []


async def test_the_decision_log_records_each_change_with_its_context(hass: HomeAssistant) -> None:
    controller, charger, configure_calls, yield_clock, pause = await _confirmed_site(
        hass, "creditlog"
    )
    yield_clock.advance(1.0)
    await controller._async_apply_active_control()

    log = controller.regulator_decision_log
    wrote = [entry for entry in log if entry["outcome"] == "wrote"]
    last = wrote[-1]
    assert (last["from_a"], last["to_a"]) == (8, 11)
    assert last["charger_id"] == charger.entry_id
    assert last["yield_reason"] == "confirmed_step"
    assert last["detail"] == "verified_step"
    assert last["reason"] == "paused_safe_current_below_charger_minimum"
    assert last["battery_power_w"] == round(14.6 * 3 * _VOLTAGE_V)
    assert last["site_current_a"]["L1"] > 19.5
    assert last["charger_current_a"] == {"L1": 7.12, "L2": 7.12, "L3": 7.12}
    assert "time" in last and "limiting_phase" in last
    # Held passes are recorded too, and the diagnostics carry the same list.
    assert any(entry["outcome"] == "held" for entry in log)
    site_entry = hass.config_entries.async_get_entry(controller.entry_id)
    diagnostics = await async_get_config_entry_diagnostics(hass, site_entry)
    assert diagnostics["regulator_decision_log"] == log


async def test_the_decision_log_is_bounded(hass: HomeAssistant) -> None:
    controller, charger, *_ = await _confirmed_site(hass, "creditbound")
    for index in range(500):
        controller._decision_log.append({"index": index})

    log = controller.regulator_decision_log
    assert len(log) == 200
    assert log[-1] == {"index": 499}


async def test_the_limit_is_explained_by_the_battery_sharing_the_fuse(hass: HomeAssistant) -> None:
    controller, charger, configure_calls, yield_clock, pause = await _confirmed_site(
        hass, "creditwhy"
    )
    yield_clock.advance(1.0)
    await controller._async_apply_active_control()

    assert controller.load_balancing_limit(charger.entry_id) == (11.0, LIMIT_CAUSE_BATTERY_SHARES_FUSE)


async def test_the_limit_is_house_consumption_without_a_charging_battery(
    hass: HomeAssistant,
) -> None:
    controller, charger, configure_calls, yield_clock, pause = await _confirmed_site(
        hass, "credithouse"
    )
    _set_battery_a(hass, 0.0)

    assert controller.load_balancing_limit(charger.entry_id) == (8.0, LIMIT_CAUSE_HOUSE_CONSUMPTION)


async def test_no_limit_is_reported_once_the_car_has_what_it_asked_for(hass: HomeAssistant) -> None:
    controller, charger, configure_calls, yield_clock, pause = await _confirmed_site(
        hass, "creditdone"
    )
    controller.regulator_decisions = {
        charger.entry_id: _decision(proposed_a=8.0, requested_a=8.0, reason="no_change_requested")
    }

    assert controller.load_balancing_limit(charger.entry_id) == (None, None)
