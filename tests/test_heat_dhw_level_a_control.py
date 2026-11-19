"""Level A's fixed rules in the heat control (`heat/control.py`, plan `plans/v3_dhw_plan.md` 5.2 and 5.3) on a relay
that blocks the compressor, so hot water too (the owner's NIBE): a held block never holds a run in progress beyond the
look that sees it, a run is delayed by at most one block, and the tank rule where a tank sensor reads."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from .test_heat_control import MIN, _plan, _running

pytestmark = pytest.mark.usefixtures("offline_relay")


async def test_a_held_block_ends_at_the_look_that_sees_a_hot_water_run(hass: HomeAssistant, freezer, monkeypatch) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    hot_water = control.shadow.hot_water
    making = {"now": False}
    monkeypatch.setattr(hot_water, "making_now", lambda: making["now"])
    await control.async_look(now)
    assert relay.calls == ["on"] and control.held
    # The closing caught a run as it began (the meter shows it a moment late): the block ends at once, before its
    # least 45 minutes.
    making["now"] = True
    await control.async_look(now + 2 * MIN)
    assert relay.calls == ["on", "off"] and not control.held and control.status()["reason"] == "hot_water"
    # No block again during the run, nor in the rest after the block once it is over.
    _plan(control, now + 20 * MIN, 0, 1)
    await control.async_look(now + 20 * MIN)
    making["now"] = False
    await control.async_look(now + 40 * MIN)
    assert relay.calls == ["on", "off"] and control.status()["reason"] == "rest"


async def test_a_run_is_delayed_by_at_most_one_block(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """Six lowered hours in a row in the plan: the first block ends at three hours, the pump's held run comes in the
    rest after it, and the next block starts only after the run is over."""
    _site_, control, relay, now = await _running(hass, freezer, 0, 1, 2, 3, 4, 5)
    hot_water = control.shadow.hot_water
    making = {"now": False}
    monkeypatch.setattr(hot_water, "making_now", lambda: making["now"])
    await control.async_look(now)
    assert relay.calls == ["on"]
    _plan(control, now + 180 * MIN, 0, 1, 2)
    await control.async_look(now + 180 * MIN)
    assert relay.calls == ["on", "off"] and control.status()["reason"] == "max_run"
    # The pump reheats the tank in the hour of rest and runs on past its end.
    making["now"] = True
    for minute in (190, 220, 240):
        await control.async_look(now + minute * MIN)
    assert relay.calls == ["on", "off"] and control.status()["reason"] == "hot_water"
    making["now"] = False
    await control.async_look(now + 250 * MIN)
    assert relay.calls == ["on", "off", "on"]


def _pump(tank_c: float | None) -> Any:
    return SimpleNamespace(
        capabilities=(), cloud=False, price_control=SimpleNamespace(state="off"), priority=None, hot_water_temp_c=tank_c
    )


async def test_the_tank_rule_ends_a_block_below_forty_and_waits_for_forty_five(hass: HomeAssistant, freezer, monkeypatch) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1, 2)
    hot_water = control.shadow.hot_water
    monkeypatch.setattr(hot_water, "making_now", lambda: False)
    tank = {"c": 50.0}
    monkeypatch.setattr(type(hot_water), "_pump", lambda _self: _pump(tank["c"]))
    await control.async_look(now)
    assert relay.calls == ["on"]
    tank["c"] = 39.0
    await control.async_look(now + 5 * MIN)
    assert relay.calls == ["on", "off"] and control.status()["reason"] == "hot_water_low"
    # Back over 40 but under 45: still no block, also after the rest.
    tank["c"] = 43.0
    await control.async_look(now + 70 * MIN)
    assert relay.calls == ["on", "off"] and control.status()["reason"] == "hot_water_low"
    tank["c"] = 45.5
    await control.async_look(now + 75 * MIN)
    assert relay.calls == ["on", "off", "on"]


async def test_the_tank_rule_without_a_tank_sensor_holds_nothing(hass: HomeAssistant, freezer, monkeypatch) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0)
    hot_water = control.shadow.hot_water
    monkeypatch.setattr(hot_water, "making_now", lambda: False)
    monkeypatch.setattr(type(hot_water), "_pump", lambda _self: _pump(None))
    await control.async_look(now)
    assert relay.calls == ["on"]


async def test_a_run_seen_under_a_block_long_held_keeps_the_block(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """Review S4: a run the meter shows under a block held for a while proves the block does not stop hot water there
    (a relay that only lowers the curve, a pump whose menu lets hot water run): the lowering stays, its writes and
    savings with it. Only a run caught at the closing ends it."""
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    hot_water = control.shadow.hot_water
    making = {"now": False}
    monkeypatch.setattr(hot_water, "making_now", lambda: making["now"])
    await control.async_look(now)
    assert relay.calls == ["on"]
    making["now"] = True
    await control.async_look(now + 20 * MIN)
    assert relay.calls == ["on"] and control.held and control.status()["reason"] == "planned"


async def test_the_tank_rule_sees_the_tank_reheated_during_a_run(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """Review NIT: the tank rule is stepped at every look, also while a run holds the block off, so a tank reheated
    to 52 °C during the run and at 43 °C after it is no longer low."""
    _site_, control, relay, now = await _running(hass, freezer, 0, 1, 2)
    hot_water = control.shadow.hot_water
    making = {"now": False}
    tank = {"c": 38.0}
    monkeypatch.setattr(hot_water, "making_now", lambda: making["now"])
    monkeypatch.setattr(type(hot_water), "_pump", lambda _self: _pump(tank["c"]))
    await control.async_look(now)
    assert relay.calls == [] and control.status()["reason"] == "hot_water_low"
    making["now"], tank["c"] = True, 52.0
    await control.async_look(now + 10 * MIN)
    making["now"], tank["c"] = False, 43.0
    await control.async_look(now + 40 * MIN)
    assert relay.calls == ["on"]
