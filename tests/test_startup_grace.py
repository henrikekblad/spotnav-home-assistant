"""The start-up grace: what is not yet known is said so for a few minutes, never asserted as a fallback."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.dashboard import serialize_dashboard
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.startup import STARTUP_GRACE_S, startup_state

from .helpers import setup_two_chargers

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def state(seconds: float, *, forecast: bool = False, charger: bool = False):
    return startup_state(
        now=T0 + timedelta(seconds=seconds), started_at=T0, forecast_pending=forecast, charger_pending=charger
    )


def test_it_is_on_only_while_something_is_awaited_inside_the_cap() -> None:
    assert not state(5).active, "nothing awaited"
    assert state(5, forecast=True).active
    waiting = state(5, forecast=True, charger=True)
    assert waiting.waiting_for == ("forecast", "charger")
    assert waiting.until == T0 + timedelta(seconds=STARTUP_GRACE_S)
    assert state(STARTUP_GRACE_S - 1, charger=True).active
    assert not state(STARTUP_GRACE_S, charger=True).active, "capped"
    assert state(STARTUP_GRACE_S + 60, charger=True).until is None


def test_a_forecast_is_awaited_only_by_hybrid_with_sources_chosen_and_none_read() -> None:
    hybrid = SimpleNamespace(strategy="hybrid")
    site = SimpleNamespace(solar_forecast_selected=("entry",), hybrid_state={"forecast_sources": []})
    pending = dashboard_api._forecast_pending
    assert pending(None, site) is False
    assert pending(hybrid, None) is False
    assert pending(hybrid, SimpleNamespace(solar_forecast_selected=(), hybrid_state=None)) is False
    assert pending(hybrid, SimpleNamespace(solar_forecast_selected=("entry",), hybrid_state=None)) is True
    assert pending(hybrid, site) is True
    site.hybrid_state = {"forecast_sources": ["entry"]}
    assert pending(hybrid, site) is False, "ends as soon as a source reports"
    assert pending(SimpleNamespace(strategy="cheapest"), SimpleNamespace(
        solar_forecast_selected=("entry",), hybrid_state=None)) is False


async def test_a_silent_charger_is_unknown_not_off_and_the_status_says_starting_up(
    hass: HomeAssistant,
) -> None:
    entry_a, *_ = await setup_two_chargers(hass)
    hass.states.async_set("switch.charger_a", "unavailable")

    response = serialize_dashboard(dashboard_api.capture_dashboard(hass, entry_a), can_act=True)
    block = response["starting_up"]
    assert block["active"] is True and block["waiting_for"] == ["charger"]
    assert block["until"] is not None
    assert response["live"]["charging"] is None, "unknown, not off"
    assert [line["code"] for line in response["status"]["lines"]] == ["starting_up"]
    assert response["status"]["tone"] == "normal"

    # Once it reports, the grace ends at once and the facts are the charger's own.
    hass.states.async_set("switch.charger_a", "off")
    response = serialize_dashboard(dashboard_api.capture_dashboard(hass, entry_a), can_act=True)
    assert response["starting_up"] == {"active": False, "until": None, "waiting_for": []}
    assert response["live"]["charging"] is False
    assert "starting_up" not in [line["code"] for line in response["status"]["lines"]]


async def test_the_grace_is_capped_when_a_charger_stays_silent(hass: HomeAssistant) -> None:
    entry_a, *_ = await setup_two_chargers(hass)
    hass.states.async_set("switch.charger_a", "unavailable")
    data = domain_data(hass)
    data.started_at = datetime.now(timezone.utc) - timedelta(seconds=STARTUP_GRACE_S + 5)

    response = serialize_dashboard(dashboard_api.capture_dashboard(hass, entry_a), can_act=True)
    assert response["starting_up"]["active"] is False
    assert response["live"]["charging"] is False
    assert "starting_up" not in [line["code"] for line in response["status"]["lines"]]


async def test_a_forecast_that_has_not_loaded_is_not_reported_as_no_forecast_source(
    hass: HomeAssistant,
) -> None:
    entry_a, *_ = await setup_two_chargers(hass)
    capture = dashboard_api.capture_dashboard(hass, entry_a)
    from custom_components.spotnav.startup import StartupState

    waiting = StartupState(active=True, until=T0, waiting_for=("forecast",))
    hybrid_settings = replace(capture.settings, strategy="hybrid")
    site = SimpleNamespace(hybrid_state={"state": "no_forecast", "reason": "no_forecast_cheapest"})
    held = replace(capture, settings=hybrid_settings, site=site, starting_up=waiting)

    state_block = dashboard_api.serialize_strategy_state(held)
    assert state_block["state"] == "unknown" and state_block["reason"] == "starting_up"

    # The same capture once the grace is over keeps saying what the planner says.
    over = replace(held, starting_up=dashboard_api.NOT_STARTING)
    assert dashboard_api.serialize_strategy_state(over)["reason"] == "no_forecast_cheapest"
