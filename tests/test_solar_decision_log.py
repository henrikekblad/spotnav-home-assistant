"""Solar's decision log, and the wait after a charge the car ended kept across a restart and a strategy switch.

The world is `tests.test_solar_car_ended`'s: an OCPP charger the sun started at 6 A whose car then ended
the charge by itself.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.execution.solar_execution import SOLAR_DECISION_LOG_LENGTH
from custom_components.spotnav.planning.auto_settings import STRATEGY_CHEAPEST, STRATEGY_SOLAR
from custom_components.spotnav.site.solar_surplus import SolarConfig, SolarController

from .test_solar_car_ended import _car_ends_the_charge, _started
from .world import controller_of, go_auto, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")

ENTRY_KEYS = {
    "time",
    "strategy",
    "from",
    "to",
    "action",
    "reason",
    "held_by_plan",
    "requested_a",
    "available_w",
    "export_w",
    "battery_w",
    "net_grid_w",
    "car_w",
    "priority",
}


async def test_the_log_holds_changes_and_actions_only(hass: HomeAssistant) -> None:
    world = await _started(hass)
    _charger, site_entry, _controller, coordinator, clock, *_rest, site = world

    log = coordinator.decision_log
    assert all(set(entry) == ENTRY_KEYS for entry in log)
    start = next(entry for entry in log if entry["action"] == "start")
    assert (start["from"], start["to"], start["strategy"]) == ("arming", "on", STRATEGY_SOLAR)
    assert start["requested_a"] == 6.0 and start["available_w"] is not None

    # Unchanged ticks add nothing.
    before = len(coordinator.decision_log)
    for t in (140.0, 150.0, 160.0, 170.0):
        clock.value = t
        await tick_site(hass, site)
    assert len(coordinator.decision_log) == before

    await _car_ends_the_charge(hass, world)
    ended = [entry for entry in coordinator.decision_log if entry["reason"] == "car_stopped"]
    assert ended[0]["action"] == "stop" and ended[0]["from"] == "on" and ended[0]["to"] == "off"
    count = len(coordinator.decision_log)
    for t in (1900.0, 2500.0):
        clock.value = t
        await tick_site(hass, site)
    assert len(coordinator.decision_log) == count, "an unchanged hold is not logged again"

    # The site's diagnostics (and with them the debug bundle) carry it per member charger.
    dump = await async_get_config_entry_diagnostics(hass, site_entry)
    assert dump["solar_decision_log"][world[0].entry_id] == coordinator.decision_log
    bundle_site = dump["debug_bundle"]["sites"][0]
    assert bundle_site["diagnostics"]["solar_decision_log"][world[0].entry_id][-1]["reason"] == "car_stopped"
    json.dumps(dump)


async def test_the_log_is_bounded(hass: HomeAssistant) -> None:
    world = await _started(hass)
    coordinator = world[3]
    for index in range(SOLAR_DECISION_LOG_LENGTH + 20):
        coordinator._record_decision(  # noqa: SLF001 - fill the ring directly
            state="on", action="set_current", reason="on_modulate", requested_a=float(index)
        )
    log = coordinator.decision_log
    assert len(log) == SOLAR_DECISION_LOG_LENGTH
    assert log[-1]["requested_a"] == float(SOLAR_DECISION_LOG_LENGTH + 19)


async def _restart(hass: HomeAssistant, charger: Any, clock: Any) -> Any:
    """Reload the charger entry, as a restart does, and give the new coordinator the fake clock."""
    assert await hass.config_entries.async_reload(charger.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.config_entries.async_get_entry(charger.entry_id).runtime_data.solar
    coordinator._now = clock.now  # noqa: SLF001 - the fake clock, as `solar_setup` installs it
    coordinator.async_start()
    return coordinator


async def test_a_stopped_cars_wait_survives_a_restart(hass: HomeAssistant) -> None:
    world = await _started(hass)
    charger, _site_entry, controller, coordinator, clock, turn_on_calls, _off, site = world
    await _car_ends_the_charge(hass, world)
    assert coordinator.state.reason == "car_stopped"
    record = controller.solar_car_ended
    assert record is not None and record["cause"] == "car_stopped" and record["retry_at"] is not None
    assert record["next_retry_s"] == 3600.0
    retry_at = coordinator.state.retry_at

    clock.value = 0.0
    coordinator = await _restart(hass, charger, clock)
    assert controller_of(hass, charger.entry_id).solar_car_ended == record
    turn_on_calls.clear()
    await tick_site(hass, site)
    # The same retry time on the wall clock (the test's monotonic clock restarted at zero).
    assert coordinator.state is not None and coordinator.state.reason == "car_stopped"
    assert coordinator.state.retry_at is not None and retry_at is not None
    assert abs((coordinator.state.retry_at - retry_at).total_seconds()) < 5.0
    # Well inside the half hour left of it: the sun starts nothing.
    for t in range(60, 1200, 60):
        clock.value = float(t)
        await tick_site(hass, site)
    assert not turn_on_calls
    assert coordinator.state.reason == "car_stopped"


async def test_a_full_car_survives_a_strategy_switch(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    world = await _started(hass)
    charger, _site_entry, controller, coordinator, clock, turn_on_calls, _off, site = world
    plugged = controller.plugged_in_at
    monkeypatch.setattr(coordinator, "_vehicle_context", lambda: (plugged, 100.0, 100.0, None))
    await _car_ends_the_charge(hass, world)
    assert coordinator.state.reason == "vehicle_full"
    record = controller.solar_car_ended
    assert record is not None and record["cause"] == "vehicle_full" and record["retry_at"] is None
    assert record["context"]["soc_percent"] == 100.0

    await go_auto(hass, charger.entry_id, strategy=STRATEGY_CHEAPEST, phases=3)
    clock.value = 2000.0
    await tick_site(hass, site)
    assert coordinator.state is None
    await go_auto(hass, charger.entry_id, strategy=STRATEGY_SOLAR, phases=3)
    for t in (2100.0, 5000.0, 9000.0):
        clock.value = t
        await tick_site(hass, site)
        assert coordinator.state.reason == "vehicle_full"
    assert len(turn_on_calls) == 1
    reasons = [entry["reason"] for entry in coordinator.decision_log]
    assert "strategy_left" in reasons


def test_the_controller_seeds_a_kept_wait() -> None:
    config = SolarConfig(priority="car_first")
    solar = SolarController(config)
    solar.seed_ended_backoff(100.0, "car_stopped", 600.0, 7200.0)
    assert solar.ended == "car_stopped"
    assert solar.retry_in(100.0) == 600.0
    assert solar.ended_backoff(100.0) == ("car_stopped", 600.0, 7200.0)
    # A retry already due: the car may be tried now, the longer next wait kept.
    solar.seed_ended_backoff(100.0, "car_stopped", -5.0, 7200.0)
    assert solar.retry_in(100.0) is None and solar.ended == "car_stopped"
    # Garbage from storage is no wait.
    solar.seed_ended_backoff(100.0, "bogus", 600.0, None)  # type: ignore[arg-type]
    assert solar.ended is None and solar.ended_backoff(100.0)[2] == 7200.0
    solar.seed_ended_backoff(100.0, "vehicle_full", None, 1e9)
    assert solar.ended == "vehicle_full" and solar.retry_in(100.0) is None
    assert solar.ended_backoff(100.0)[2] == config.ended_retry_max_s
