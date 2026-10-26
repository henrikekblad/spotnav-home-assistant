"""A pause asked for again while a stop of ours settles settles too: no pass, pause, recompute, pass loop.

A derived site with active control on and 5 A of room; an OCPP charger asked to start at 16 A is refused (below the
6 A floor) and then charges anyway (it began by itself). Load balancing pauses it: the stop goes out, and the
charger, like any real one, reports off only once it has answered. Every pause asked for meanwhile sends nothing
(`STOP_SETTLE_S`), but it told the site again, the site recomputed, its next pass forgot the pause it had written
(the charger still reads charging) and paused again, and so on as fast as the event loop ran, until the charger
reported off. Now the remembered pause holds while the stop settles; once the settle time is over a charger still
charging is paused again, one stop per tick as before.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.const import (
    CONF_GRID_POWER_SOURCE,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_SITE_CURRENT_SIGNED,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    MEASUREMENT_MODE_DERIVED,
    SITE_RECOMPUTE_INTERVAL_S,
)
from custom_components.spotnav.execution.controller import STOP_SETTLE_S

from .helpers import make_entry, make_site_entry, set_current_sensor
from .world import controller_of, separate_entities_source, set_derived_site_entities

_P = "h_charger"
_SWITCH = f"switch.{_P}"
# A hang is a failure, not a suite that never ends. The clock ticks on from where it is frozen (`tick=True`): in a
# clock that stands still no `asyncio` timeout ever elapses.
_SETTLE_TIMEOUT_S = 10.0


async def _settle(hass: HomeAssistant) -> None:
    await asyncio.wait_for(hass.async_block_till_done(), _SETTLE_TIMEOUT_S)


async def _tick(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SITE_RECOMPUTE_INTERVAL_S + 1))
    await _settle(hass)


def _read_site(hass: HomeAssistant) -> None:
    """12/13/14 A on the site's phases, 39 A of import on the grid meter: 5 A of room under a 20 A fuse."""
    for phase, amps in zip(("l1", "l2", "l3"), (12, 13, 14), strict=True):
        set_current_sensor(hass, f"sensor.h_site_{phase}", amps)
    hass.states.async_set("sensor.h_grid", str(39 * 230), {"unit_of_measurement": "W"})


async def _site_with_refused_start(hass: HomeAssistant) -> tuple[Any, list[Any]]:
    hass.states.async_set(_SWITCH, "off")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.6,2.10"})
    async_mock_service(hass, "ocpp", "configure")
    charger = make_entry(
        hass,
        entry_id=_P,
        charge_control=_SWITCH,
        current_limit=f"number.{_P}_connector_1_session_current_limit",
        webhook_id="wh-h",
        title="H",
        current_control=CURRENT_CONTROL_CHANGE_CONFIGURATION,
        ocpp_target=(_P, 1),
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    # The charger's switch moves only when the charger reports: a stop is recorded, the switch stays as it is.
    stops: list[Any] = []

    async def stop(call: Any) -> None:
        stops.append(call)

    hass.services.async_register("switch", "turn_off", stop)
    for phase in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.{_P}_{phase}", 0)
    _read_site(hass)
    site = make_site_entry(
        hass,
        entry_id="h_site",
        main_fuse_a=20.0,
        safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                    f"sensor.{_P}_l1", f"sensor.{_P}_l2", f"sensor.{_P}_l3"
                ),
            }
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=set_derived_site_entities(hass, "h_der", 3220.0),
        active_control_enabled=True,
        extra_data={CONF_SITE_CURRENT_SIGNED: True, CONF_GRID_POWER_SOURCE: {"power": "sensor.h_grid"}},
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    for _ in range(3):
        _read_site(hass)
        await _tick(hass)
    controller = controller_of(hass, charger.entry_id)
    assert await controller.async_start(amps=16) is False, "5 A of room: the start is refused"
    stops.clear()
    return controller, stops


@pytest.mark.freeze_time(tick=True)
async def test_a_pause_while_the_stop_settles_settles(hass: HomeAssistant, freezer) -> None:
    controller, stops = await _site_with_refused_start(hass)
    # The charger charges anyway; one tick pauses it, and the pass ends there.
    hass.states.async_set(_SWITCH, "on")
    await _tick(hass)
    assert len(stops) == 1

    # The charger has not answered: the next tick within the settle time sends nothing.
    freezer.tick(timedelta(seconds=STOP_SETTLE_S / 2))
    _read_site(hass)
    await _tick(hass)
    assert len(stops) == 1

    # Unanswered past the settle time: paused once more, as before.
    freezer.tick(timedelta(seconds=STOP_SETTLE_S))
    _read_site(hass)
    await _tick(hass)
    assert len(stops) == 2

    # The charger answers: the charge is held back for room, for the resume to give back.
    hass.states.async_set(_SWITCH, "off")
    await _settle(hass)
    assert controller.paused_by_balancing
    assert len(stops) == 2
