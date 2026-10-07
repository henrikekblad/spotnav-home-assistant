"""The sun never starts or modulates a charger with no car.

The field case (2026-10-07, "Driveway", an Easee on hybrid, `connection: disconnected` all morning): at 07:49:28 the
sun's rules sent resume and 7 A to the empty charger and logged `solar_start`; from 07:53 the decision log showed
`on_modulate` 13 <-> 14 A every 1 to 5 s with the car drawing 0 W for some ten minutes, holding the site's surplus
for a charger with no car.

Every tick is an explicit site recompute at a hand-set instant (`tests.test_solar_execution`).
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.planning.auto_settings import STRATEGY_HYBRID, STRATEGY_SOLAR
from custom_components.spotnav.planning.status_compose import compose_status, StatusFacts

from .test_mode_switch_handover import _garage, _sun
from .world import tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")


def _actions(coordinator: Any) -> list[tuple[str, str]]:
    return [(entry["action"], entry["reason"]) for entry in coordinator.decision_log if entry["action"] != "hold"]


@pytest.mark.parametrize("strategy", [STRATEGY_SOLAR, STRATEGY_HYBRID])
async def test_an_unplugged_charger_is_never_armed_or_started_on_any_surplus(
    hass: HomeAssistant, strategy: str
) -> None:
    charger, site, controller, coordinator, clock, easee = await _garage(hass, strategy)
    await easee.status("disconnected")
    _sun(hass, export_w=4000.0)

    for at in (0.0, 60.0, 125.0, 400.0, 900.0):
        clock.value = at
        await tick_site(hass, site)

    assert coordinator.state is not None and coordinator.state.state == "off"
    assert coordinator.state.reason == "no_car"
    assert "resume" not in easee.commands and not easee.limits
    assert _actions(coordinator) == []
    assert coordinator.share_member(site, order=0) is None, "an empty charger holds no share of the surplus"


async def test_a_charger_unplugged_under_a_running_solar_charge_goes_off_without_a_stop(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, easee = await _garage(hass, STRATEGY_SOLAR)
    _sun(hass, export_w=4000.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.state == "on"
    assert "resume" in easee.commands
    easee.commands.clear()

    # The car leaves before it ever drew: the charger says disconnected.
    await easee.status("disconnected")
    clock.value = 130.0
    await tick_site(hass, site)

    assert coordinator.state.state == "off" and coordinator.state.reason == "no_car"
    assert easee.pauses == 0, "nothing to stop on an empty charger"

    # The sun goes on shining: nothing arms, nothing is sent.
    for at in (200.0, 400.0, 900.0):
        clock.value = at
        await tick_site(hass, site)
    assert coordinator.state.state == "off"
    assert easee.commands == []


async def test_an_unknown_connection_keeps_todays_behaviour(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, easee = await _garage(hass, STRATEGY_SOLAR)
    hass.states.async_set("sensor.easee_status", "unavailable")
    await hass.async_block_till_done()
    _sun(hass, export_w=4000.0)
    await tick_site(hass, site)

    assert coordinator.state is not None and coordinator.state.state == "arming"



def _solar_line(hass: HomeAssistant, charger: Any) -> dict[str, Any]:
    """The solar headline the status composes from this charger's solar facts."""
    facts = dashboard_api.status_facts(dashboard_api.capture_dashboard(hass, charger))
    assert facts.solar is not None
    return compose_status(StatusFacts(now=facts.now, strategy=STRATEGY_SOLAR, solar=facts.solar))["lines"][0]


@pytest.mark.parametrize(
    ("export_w", "line"),
    [
        (4000.0, {"code": "solar_no_car_surplus", "params": {"surplus_kw": 4.0}}),
        (600.0, {"code": "solar_no_car", "params": {}}),
        (0.0, {"code": "solar_no_car", "params": {}}),
    ],
)
async def test_an_empty_charger_on_solar_says_no_car_and_whether_the_sun_would_start_one(
    hass: HomeAssistant, export_w: float, line: dict[str, Any]
) -> None:
    """Not "waiting for sun" while the sun shines on an empty charger: the line says no car is plugged in, and that
    plugging one in would start it when the surplus covers the start minimum (600 W on one phase is short of 6 A)."""
    charger, site, controller, coordinator, clock, easee = await _garage(hass, STRATEGY_SOLAR)
    await easee.status("disconnected")
    _sun(hass, export_w=export_w)
    for at in (0.0, 60.0, 125.0):
        clock.value = at
        await tick_site(hass, site)

    assert coordinator.state is not None and coordinator.state.reason == "no_car"
    assert _solar_line(hass, charger) == line


def test_the_webhook_words_the_no_car_lines_as_waiting_for_sun_to_an_app_that_does_not_ask_for_them() -> None:
    """An app released before `solar_no_car` and `solar_no_car_surplus` would word them "see Home Assistant": the
    webhook says `solar_waiting_for_sun` in their place unless the request reads `solar_no_car_status`."""
    from custom_components.spotnav.api.webhook import _for_app

    for code, params in (("solar_no_car", {}), ("solar_no_car_surplus", {"surplus_kw": 4.0})):
        body = {
            "ok": True,
            "status": {
                "tone": "normal",
                "lines": [
                    {"code": code, "params": params},
                    {"code": "solar_site_incomplete", "params": {"phases": ["L2"]}},
                ],
            },
        }
        older = _for_app(body, {"action": "dashboard", "reads": ["identification_status"]})
        assert older["status"]["lines"] == [
            {"code": "solar_waiting_for_sun", "params": {}},
            {"code": "solar_site_incomplete", "params": {"phases": ["L2"]}},
        ]
        assert older["status"]["tone"] == "normal"
        newer = _for_app(body, {"action": "dashboard", "reads": ["solar_no_car_status"]})
        assert newer["status"]["lines"] == body["status"]["lines"]
        assert body["status"]["lines"][0]["code"] == code, "a copy"
