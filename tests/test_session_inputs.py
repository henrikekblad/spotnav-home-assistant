"""What a session recorder reads from a charger: the solar share, the estimate and the price book."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.execution.solar_execution import SolarExecutionState
from custom_components.spotnav.runtime import charger_data
from custom_components.spotnav.sessions import inputs
from custom_components.spotnav.sessions.inputs import price_book_for, session_facts, solar_share_of

from .world import go_auto, setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")

NOW = "2026-09-22 06:00:00"


def solar_state(**changes) -> SolarExecutionState:
    base = SolarExecutionState(
        state="on", action="hold", reason="ok", requested_a=10, net_grid_w=0.0, export_w=0.0,
        car_w=4000.0, battery_w=None, available_w=3000.0, available_a=None, priority_effective=None,
        active_control_active=False,
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    ("state", "strategy", "expected"),
    [
        (solar_state(), "solar", 0.75),
        (solar_state(available_w=9000.0), "hybrid", 1.0),
        (solar_state(available_w=-500.0), "solar", 0.0),
        (solar_state(held_by_plan=True), "hybrid", None),
        (solar_state(state="off"), "solar", None),
        (solar_state(car_w=0.0), "solar", None),
        (solar_state(car_w=None), "solar", None),
        (solar_state(available_w=None), "solar", None),
        (solar_state(), "cheapest", None),
        (None, "solar", None),
    ],
)
def test_the_solar_share_is_the_surplus_over_the_cars_power_or_unknown(
    monkeypatch: pytest.MonkeyPatch, hass: HomeAssistant, state, strategy, expected
) -> None:
    monkeypatch.setattr(inputs, "solar_execution_state", lambda _hass, _id: state)

    assert solar_share_of(hass, "entry_a", strategy) == expected


async def test_the_estimate_follows_the_asked_for_current_phases_and_voltage(hass: HomeAssistant) -> None:
    with freeze_time(NOW):
        entry = await setup_charger(hass)
        await go_auto(hass, amps=10, phases=3)
        controller = charger_data(hass, entry.entry_id).controller

        facts = session_facts(hass, controller)

        # sqrt(3) x 400 V x 10 A
        assert facts.estimate_kw == pytest.approx(6.928, abs=0.001)
        assert facts.strategy == "cheapest" and facts.register_kwh is None
        assert facts.charging is False and facts.register_integrated is False


async def test_the_price_book_is_the_dashboards_own_effective_prices(hass: HomeAssistant) -> None:
    with freeze_time(NOW):
        entry = await setup_charger(hass)
        await go_auto(hass)
        captured = dashboard_api.capture_dashboard(hass, entry)

        book = price_book_for(hass, entry.entry_id, datetime.now(timezone.utc))

        assert book is not None and book.currency == captured.area_entry.currency
        assert book.minor_unit == captured.area_entry.minor_unit
        by_start = {row.utc_start: row.effective_minor_per_kwh for row in book.intervals}
        for row in captured.intervals:
            assert by_start[row.utc_start] == row.effective_minor_per_kwh


async def test_no_market_means_no_price_book(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)

    assert price_book_for(hass, entry.entry_id, datetime.now(timezone.utc)) is None
