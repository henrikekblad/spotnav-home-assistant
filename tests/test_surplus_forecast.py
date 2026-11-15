"""The expected solar surplus (`site/surplus_forecast.py`) and its sensor (`SiteExpectedSurplusEntity`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import CONF_SOLAR_FORECAST_ENTRIES, DOMAIN
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site.house_forecast import SURPLUS_NO_SOLAR_STATISTICS, SURPLUS_OK
from custom_components.spotnav.site.solar_correction_shadow import SURPLUS_NO_SUN
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
    # The import target's net figure is never the surplus's house: the sun is taken off it already.
    monkeypatch.setattr(
        controller.house_forecast, "plan_kw_by_hour", lambda _now: {hour + timedelta(hours=i): 0.0 for i in range(6)}
    )
    monkeypatch.setattr(
        controller.house_forecast,
        "surplus_house_by_hour",
        lambda _now: ({hour + timedelta(hours=i): 1.0 for i in range(6)}, SURPLUS_OK),
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

    # No whole load (no solar production in the Energy dashboard): unknown, with the reason in the diagnostics.
    monkeypatch.setattr(
        controller.house_forecast, "surplus_house_by_hour", lambda _now: ({}, SURPLUS_NO_SOLAR_STATISTICS)
    )
    for listener in list(controller.house_forecast._listeners):
        listener()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unknown"
    assert correction.snapshot()["expected_surplus_status"] == SURPLUS_NO_SOLAR_STATISTICS

    correction.last_refresh = now - timedelta(hours=4)
    for listener in list(controller.house_forecast._listeners):
        listener()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unknown"
    assert correction.surplus_status(now) == SURPLUS_NO_SUN


async def test_no_sensor_on_a_site_without_a_solar_forecast(hass: HomeAssistant, freezer) -> None:
    entry = await pilot_site(hass, freezer)

    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_expected_solar_surplus") is None


async def test_the_house_and_solar_forecast_sensors_carry_time_and_value_rows_out_of_the_recorder(
    hass: HomeAssistant, freezer
) -> None:
    entry = await pilot_site(hass, freezer, **{CONF_SOLAR_FORECAST_ENTRIES: ["solcast1"]})
    controller = site_controller_for(hass, entry.entry_id)
    registry = er.async_get(hass)
    house_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_house_load_forecast")
    solar_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_solar_forecast")
    assert house_id is not None and solar_id is not None
    assert hass.states.get(house_id).state == "unknown"
    assert hass.states.get(solar_id).state == "unknown"

    now = datetime.now(UTC)
    hour = now.replace(minute=0, second=0, microsecond=0)
    controller.house_forecast._rows = [(hour + timedelta(hours=i), 1.0 + i) for i in range(3)]
    controller.house_forecast.basis = "gross"
    correction = controller.solar_correction
    correction.sun_kwh = {hour + timedelta(hours=i): 2.5 for i in range(4)}
    correction.sun_basis = "corrected"
    correction.last_refresh = now
    for listener in list(controller.house_forecast._listeners):
        listener()
    await hass.async_block_till_done()

    house = hass.states.get(house_id)
    assert float(house.state) == 1.0
    assert house.attributes["unit_of_measurement"] == "kW"
    # A forecast keeps no long-term statistics.
    assert "state_class" not in house.attributes and "state_class" not in hass.states.get(solar_id).attributes
    assert house.attributes["basis"] == "gross"
    assert [row["value"] for row in house.attributes["forecast"]] == [1.0, 2.0, 3.0]
    assert set(house.attributes["forecast"][0]) == {"time", "value"}
    solar = hass.states.get(solar_id)
    assert float(solar.state) == 2.5
    assert solar.attributes["basis"] == "corrected"
    assert len(solar.attributes["forecast"]) == 4
    for entity in (house_id, solar_id):
        assert "forecast" in hass.data["sensor"].get_entity(entity)._unrecorded_attributes

    # The sun forecast read too long ago says nothing; the house keeps its own.
    correction.last_refresh = now - timedelta(hours=4)
    for listener in list(controller.house_forecast._listeners):
        listener()
    await hass.async_block_till_done()
    assert hass.states.get(solar_id).state == "unknown"
    assert float(hass.states.get(house_id).state) == 1.0


async def test_no_solar_forecast_sensor_without_a_solar_forecast(hass: HomeAssistant, freezer) -> None:
    entry = await pilot_site(hass, freezer)

    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_solar_forecast") is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_house_load_forecast") is not None
