"""The expected solar surplus (`site/surplus_forecast.py`) and its sensor (`SiteExpectedSurplusEntity`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import CONF_SOLAR_FORECAST_ENTRIES, DOMAIN
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site.surplus_forecast import (
    expected_surplus,
    remaining_today_kwh,
    window_kwh,
)

from .test_demand_site import pilot_site

pytestmark = pytest.mark.usefixtures("offline_relay")

NOON = datetime(2026, 6, 10, 10, 0, tzinfo=UTC)  # 12:00 local in summer


def _hours(*values: float) -> dict[datetime, float]:
    return {NOON + timedelta(hours=i): v for i, v in enumerate(values)}


def test_the_surplus_is_the_sun_above_the_house_and_never_negative() -> None:
    shown = expected_surplus(_hours(3.0, 1.0, 2.5), _hours(1.0, 1.5, 0.5), NOON)

    assert [h.surplus_kwh for h in shown] == [2.0, 0.0, 2.0]
    assert shown[1].sun_kwh == 1.0 and shown[1].house_kwh == 1.5


def test_hours_the_house_forecast_does_not_know_and_past_hours_are_left_out() -> None:
    sun = {NOON - timedelta(hours=1): 4.0, **_hours(3.0, 3.0, 3.0)}
    house = {NOON: 1.0, NOON + timedelta(hours=2): 1.0}

    shown = expected_surplus(sun, house, NOON + timedelta(minutes=20))

    assert [h.start for h in shown] == [NOON, NOON + timedelta(hours=2)]


def test_the_horizon_is_bounded() -> None:
    sun = {NOON + timedelta(hours=i): 1.0 for i in range(40)}
    shown = expected_surplus(sun, {start: 0.0 for start in sun}, NOON)

    assert len(shown) == 24


def test_windows_count_each_hour_by_its_share_inside() -> None:
    shown = expected_surplus(_hours(4.0, 4.0, 4.0, 4.0, 4.0), _hours(0.0, 0.0, 0.0, 0.0, 0.0), NOON)
    now = NOON + timedelta(minutes=30)

    assert window_kwh(shown, now, timedelta(hours=2)) == pytest.approx(8.0)  # 2 + 4 + 2
    assert window_kwh(shown, now, timedelta(minutes=30)) == pytest.approx(2.0)
    assert window_kwh([], now, timedelta(hours=2)) is None
    assert window_kwh(shown, NOON + timedelta(hours=10), timedelta(hours=2)) is None


def test_the_rest_of_today_ends_at_local_midnight() -> None:
    # 22:00 UTC is midnight in Stockholm in summer: only the hour before it is today.
    start = datetime(2026, 6, 10, 20, 0, tzinfo=UTC)
    sun = {start + timedelta(hours=i): 2.0 for i in range(4)}
    shown = expected_surplus(sun, {s: 0.0 for s in sun}, start)

    assert remaining_today_kwh(shown, start, "Europe/Stockholm") == pytest.approx(4.0)


async def test_the_sensor_shows_the_next_hours_and_is_unknown_without_either_forecast(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await pilot_site(hass, freezer, **{CONF_SOLAR_FORECAST_ENTRIES: ["solcast1"]})
    controller = site_controller_for(hass, entry.entry_id)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_expected_solar_surplus")
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "unknown"

    now = datetime.now(UTC)
    hour = now.replace(minute=0, second=0, microsecond=0)
    correction = controller.solar_correction
    correction.sun_kwh = {hour + timedelta(hours=i): 3.0 for i in range(6)}
    correction.sun_basis = "corrected"
    correction.last_refresh = now
    monkeypatch.setattr(
        controller.house_forecast, "plan_kw_by_hour", lambda _now: {hour + timedelta(hours=i): 1.0 for i in range(6)}
    )
    for listener in list(controller.house_forecast._listeners):
        listener()
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert float(state.state) == pytest.approx(8.0, abs=0.1)
    assert state.attributes["sun"] == "corrected"
    assert state.attributes["window_hours"] == 4
    assert len(state.attributes["hours"]) == 6
    assert state.attributes["hours"][0]["surplus_kwh"] == 2.0

    correction.last_refresh = now - timedelta(hours=4)
    for listener in list(controller.house_forecast._listeners):
        listener()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unknown"


async def test_no_sensor_on_a_site_without_a_solar_forecast(hass: HomeAssistant, freezer) -> None:
    entry = await pilot_site(hass, freezer)

    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_expected_solar_surplus") is None
