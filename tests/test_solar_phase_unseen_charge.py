"""A start-only phase switch is never sent to a charger that may be charging where SpotNav cannot see it (SEM #1423,
#1425): its charge control reads off while a measurement shows the car drawing (a switch that does not open the
contactor)."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.chargers.phase_switches import ForceSinglePhaseSwitch

from .test_solar_phase_execution import _world, FORCE

pytestmark = pytest.mark.usefixtures("offline_relay", "chargers_not_switch_only")


@pytest.fixture
def start_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ForceSinglePhaseSwitch, "start_only", True)


@pytest.mark.usefixtures("start_only")
async def test_a_charger_drawing_with_its_control_off_gets_no_phase_switch(hass: HomeAssistant, freezer: Any) -> None:
    w = await _world(hass, freezer)
    w.draw(8.0, sun_w=7500.0)
    await w.at(0.0)
    await w.run(400.0)
    assert w.force_calls() == []


@pytest.mark.usefixtures("start_only")
async def test_a_charger_whose_status_cannot_be_read_gets_no_phase_switch(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its status sensor dark: nothing says it is not charging, so no switch is made before a start (and 2 kW of sun
    does not carry three phases: nothing starts)."""
    w = await _world(hass, freezer)
    monkeypatch.setattr(w.controller.adapter, "charging_observation", lambda: None)
    w.draw(0.0, sun_w=2000.0)
    await w.at(0.0)
    await w.run(400.0)
    assert w.force_calls() == []


@pytest.mark.usefixtures("start_only")
async def test_a_charger_seen_at_rest_still_starts_on_one_phase(hass: HomeAssistant, freezer: Any) -> None:
    w = await _world(hass, freezer)
    w.draw(0.0, sun_w=2000.0)
    await w.at(0.0)
    await w.at(125.0)
    assert w.force_calls() == ["1"]


@pytest.mark.usefixtures("start_only")
async def test_three_phases_are_not_set_back_while_the_status_cannot_be_read(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(hass, freezer)
    hass.states.async_set(FORCE, "on")
    w.controller.reconcile_phases()
    assert w.controller.switched_phases == 1
    monkeypatch.setattr(w.controller.adapter, "charging_observation", lambda: None)
    await w.controller._restore_three_phases_locked()  # noqa: SLF001 - the step before every non-solar start
    assert w.force_calls() == []
    monkeypatch.undo()
    monkeypatch.setattr(ForceSinglePhaseSwitch, "start_only", True)
    await w.controller._restore_three_phases_locked()  # noqa: SLF001
    assert w.force_calls() == ["3"], "seen at rest: set back"
