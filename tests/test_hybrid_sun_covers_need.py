"""Hybrid with forecast sun that covers the whole remaining need: nothing to buy from the grid.

The field case (2026-10-04): an EV6 at 96 % (77.4 kWh, own limit 100 %), Hybrid, 3.5 kWh asked for by hand
(capped at the battery's room, 3.44 kWh), departure 08:00 tomorrow before tomorrow's prices are published,
and a sunny forecast. Hybrid credited the sun with the whole need, `grid_kwh` was 0, and the planner refused
the zero amount (`invalid_energy`), which surfaced as `planning_unavailable` / `unexpected_failure`.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.execution import hybrid_execution
from custom_components.spotnav.planning.auto_controller import AutoPlannerController
from custom_components.spotnav.planning.auto_settings import DRIVER_MANUAL_KWH, DRIVER_TARGET_SOC, TargetSocIntent
from custom_components.spotnav.planning.hybrid_forecast import ForecastReadResult
from custom_components.spotnav.runtime import preview_for
from tests.relay import TODAY, serve as serve_prices

from .world import charger_and_car, go_auto

pytestmark = pytest.mark.usefixtures("offline_relay")

BEFORE_PUBLICATION = "2026-09-22 07:38:00"
SETTINGS: dict[str, Any] = {
    "strategy": "hybrid", "amps": 16, "phases": 3, "max_periods": 2,
    "departure_enabled": True, "departure": time(8, 0),
}


def _sunny(monkeypatch: pytest.MonkeyPatch, wh: float) -> None:
    async def read(_hass: Any, _entries: Any, **_kw: Any) -> ForecastReadResult:
        base = datetime(2026, 9, 22, tzinfo=timezone.utc)
        hours = {base + timedelta(hours=h): (wh if 8 <= h % 24 <= 15 else 0.0) for h in range(48)}
        return ForecastReadResult(wh_hours=hours, sources_read=("solcast",))

    monkeypatch.setattr(hybrid_execution, "async_read_forecast_wh", read)


async def _field_case(hass: HomeAssistant, driver: str) -> Any:
    charger, car_id, _ = await charger_and_car(
        hass, capacity=77.4, soc_percent="96", driver=DRIVER_MANUAL_KWH, requested_kwh=3.5,
        target=TargetSocIntent(), **SETTINGS,
    )
    if driver == DRIVER_TARGET_SOC:
        await go_auto(
            hass, charger.entry_id, driver=DRIVER_TARGET_SOC,
            target=TargetSocIntent(vehicle_id=car_id, target_percent=100), **SETTINGS,
        )
    preview = preview_for(hass, charger.entry_id)
    await preview.async_recalculate()
    return charger, preview.snapshot()


@pytest.mark.parametrize("driver", [DRIVER_MANUAL_KWH, DRIVER_TARGET_SOC])
async def test_sun_that_covers_the_whole_need_is_a_normal_state_not_a_failure(
    hass: HomeAssistant, transport: Any, monkeypatch: pytest.MonkeyPatch, driver: str
) -> None:
    serve_prices(transport, days=(TODAY,), rising=True)  # tomorrow is not published yet
    _sunny(monkeypatch, 5000.0)
    with freeze_time(BEFORE_PUBLICATION):
        charger, snapshot = await _field_case(hass, driver)
        assert (snapshot.state, snapshot.reason) == ("nothing_to_charge", "solar_covers_need")
        assert snapshot.last_error_code is None and snapshot.proposal is None
        status = dashboard_api.serialize_dashboard(
            dashboard_api.capture_dashboard(hass, charger), can_act=True
        )["status"]
    assert status["tone"] == "normal"
    # The hybrid headline (with no site in this test it has no recorded state to word), and no failure.
    assert status["lines"][0]["code"].startswith("hybrid_")
    assert all(line["code"] not in ("planning_unavailable", "planning_error") for line in status["lines"])


@pytest.mark.parametrize("driver", [DRIVER_MANUAL_KWH, DRIVER_TARGET_SOC])
async def test_without_the_sun_the_same_case_still_waits_for_the_publication(
    hass: HomeAssistant, transport: Any, driver: str
) -> None:
    serve_prices(transport, days=(TODAY,), rising=True)
    with freeze_time(BEFORE_PUBLICATION):
        _charger, snapshot = await _field_case(hass, driver)
    assert (snapshot.state, snapshot.reason) == ("waiting_for_publication", "publication_pending")


def test_an_unmapped_planner_code_is_named_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert AutoPlannerController._map_planner_code("invalid_energy") == "unexpected_failure"  # noqa: SLF001
    assert "invalid_energy" in caplog.text
    caplog.clear()
    assert AutoPlannerController._map_planner_code("deadline_too_short") == "deadline_too_short"  # noqa: SLF001
    assert caplog.text == ""
