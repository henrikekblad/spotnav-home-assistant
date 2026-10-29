"""Phase switching on a charger whose phases are set only before a start (`PhaseSwitch.start_only`, Easee), and a
charger that reports its own switching (`PhaseSwitch.switching`: Lektrico, go-e, Wattpilot), carried out by the
solar execution and the charging controller.

The world is `tests.test_solar_phase_execution`'s: a solar site and a charger with a force-single-phase switch. The
two vendor traits are given to that switch here, so what is tested is the controller's handling of them, the same
for every adapter that has them (the Easee adapter's own refusal while charging is in
`tests/chargers/test_easee_phase_mode.py`).
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.chargers.phase_switches import ForceSinglePhaseSwitch
from custom_components.spotnav.execution.phase_switching import CHARGER_SWITCHING_RECHECK_S, SETTLE_S
from custom_components.spotnav.site.solar_surplus import UP_DELAY_S

from .test_solar_phase_execution import _charging_three_phases, _switched_down, _world, FORCE, SWITCH

pytestmark = pytest.mark.usefixtures("offline_relay")


@pytest.fixture
def start_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ForceSinglePhaseSwitch, "start_only", True)


@pytest.mark.usefixtures("start_only")
async def test_a_running_three_phase_charge_is_never_switched_down_it_stops_as_without_switching(
    hass: HomeAssistant, freezer: Any
) -> None:
    w = await _charging_three_phases(hass, freezer)
    assert w.controller.phase_switch_allowed() is True, "the setting is on and the capability is there"
    w.draw(6.0, sun_w=3000.0)
    start = w.t
    await w.run(start + 400.0)

    assert w.force_calls() == []
    assert "phase_down" not in w.reasons()
    assert "stop_after_delay" in w.reasons()
    assert w.charge_stops() == 1
    assert w.controller.phase_switches_this_plug_in == 0


@pytest.mark.usefixtures("start_only")
async def test_a_paused_solar_start_on_two_kw_is_made_on_one_phase_set_right_before_it(
    hass: HomeAssistant, freezer: Any
) -> None:
    w = await _world(hass, freezer)
    w.draw(0.0, sun_w=2000.0)
    await w.at(0.0)
    assert w.force_calls() == [], "never set while idle"
    await w.at(125.0)

    assert w.coordinator.state.action == "start"
    assert w.service_calls[-2:] == [("turn_on", FORCE), ("turn_on", SWITCH)]
    assert w.controller.phase_switches_this_plug_in == 0


@pytest.mark.usefixtures("start_only")
async def test_a_charge_started_on_one_phase_is_never_switched_up_while_it_runs(
    hass: HomeAssistant, freezer: Any
) -> None:
    w = await _world(hass, freezer)
    w.draw(0.0, sun_w=2000.0)
    await w.at(0.0)
    await w.at(125.0)
    await w.charger_takes(1)
    hass.states.async_set(SWITCH, "on")
    w.draw(6.0)
    await w.run(400.0)
    assert w.controller.switched_phases == 1
    # Plenty of sun for three phases, for far longer than the up delay.
    w.draw(16.0, sun_w=11000.0)
    await w.run(w.t + UP_DELAY_S + 900.0, step=25.0)

    assert w.force_calls() == ["1"]
    assert "phase_up" not in w.reasons()
    assert w.coordinator.state.state == "on"
    assert w.controller.switched_phases == 1


@pytest.mark.usefixtures("start_only")
async def test_three_phases_are_set_back_at_the_next_start_not_while_it_charges(
    hass: HomeAssistant, freezer: Any
) -> None:
    w = await _world(hass, freezer)
    hass.states.async_set(FORCE, "on")
    w.controller.reconcile_phases()
    assert w.controller.switched_phases == 1
    # Charging (on one phase): a plan's take-over leaves the phases as they are.
    hass.states.async_set(SWITCH, "on")
    await hass.async_block_till_done()
    assert w.controller.charging
    await w.controller._restore_three_phases_locked()  # noqa: SLF001 - the step before every non-solar start
    assert w.force_calls() == []
    assert w.controller.switched_phases == 1
    # Paused: the next start that is not the sun's sets three first.
    hass.states.async_set(SWITCH, "off")
    await hass.async_block_till_done()
    await w.controller.async_start(10, manual=True)
    assert w.force_calls() == ["3"]


async def test_a_charger_that_says_it_is_switching_settles_past_the_settle_time(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reported = {"switching": False}
    monkeypatch.setattr(ForceSinglePhaseSwitch, "switching", lambda self: reported["switching"])
    w = await _switched_down(hass, freezer)
    switched_at = w.t
    hass.states.async_set(SWITCH, "off")
    w.draw(0.0)
    reported["switching"] = True
    await w.run(switched_at + SETTLE_S + 30.0)

    assert w.controller.phase_switch_settling, "the charger still switches"
    assert w.controller.phase_switch_settle_left_s() >= CHARGER_SWITCHING_RECHECK_S
    assert "charger_stopped" not in w.reasons() and "car_stopped" not in w.reasons()
    reported["switching"] = False
    assert not w.controller.phase_switch_settling
    assert w.controller.phase_switch_settle_left_s() == 0.0


async def test_a_charger_switching_by_itself_counts_as_settling_too(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ForceSinglePhaseSwitch, "switching", lambda self: True)
    w = await _world(hass, freezer)

    assert w.controller.phase_switch_settling
    assert w.controller.phase_switching_facts()["settling"] is False, "no switch of ours settles"
