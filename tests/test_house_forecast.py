"""The house load forecast's Home Assistant shell (`site/house_forecast.py`): its sources picked with no setting, the
hourly refresh on a real site, the `forecast` attribute, the diagnostics and the log kept across a restart."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site.forecast_log import KIND_HOUSE, KIND_HOUSE_DAY_AHEAD, KIND_HOUSE_RAW
from custom_components.spotnav.site.house_forecast import (
    holiday_dates,
    hourly_temperatures,
    pick_holiday_calendar,
    pick_weather_entity,
    to_celsius,
    WEATHER_NO_FORECAST,
    WEATHER_NONE,
    WEATHER_OK,
)

from .test_demand_site import pilot_site, run

pytestmark = pytest.mark.usefixtures("offline_relay")

TZ = "Europe/Stockholm"
UTC = timezone.utc


# ---------------------------------------------------------------------------------------------- the sources, pure


def test_the_weather_entity_is_forecast_home_else_the_only_one() -> None:
    assert pick_weather_entity(["weather.other", "weather.forecast_home", "sensor.x"]) == "weather.forecast_home"
    assert pick_weather_entity(["weather.smhi", "sensor.x"]) == "weather.smhi"
    assert pick_weather_entity(["weather.a", "weather.b"]) is None
    assert pick_weather_entity([]) is None


def test_temperatures_are_read_in_celsius_and_bad_ones_skipped() -> None:
    assert to_celsius("5.5", "°C") == 5.5
    assert to_celsius(41, "°F") == pytest.approx(5.0)
    assert to_celsius(5, "K") is None
    assert to_celsius("cold", "°C") is None
    assert to_celsius(500, "°C") is None
    response = {
        "weather.forecast_home": {
            "forecast": [
                {"datetime": "2026-11-16T10:00:00+00:00", "temperature": 3.0},
                {"datetime": "2026-11-16T10:30:00+00:00", "temperature": 9.0},  # same hour: the first wins
                {"datetime": "2026-11-16T11:00:00+00:00", "temperature": "x"},
                {"datetime": "nonsense", "temperature": 1.0},
                "junk",
            ]
        }
    }
    assert hourly_temperatures(response, "weather.forecast_home", "°C", TZ) == {
        datetime(2026, 11, 16, 10, tzinfo=UTC): 3.0
    }
    assert hourly_temperatures({"weather.other": {}}, "weather.forecast_home", "°C", TZ) == {}
    assert hourly_temperatures(None, "weather.forecast_home", "°C", TZ) == {}


def test_holiday_dates_and_the_calendar_for_the_home_country() -> None:
    response = {
        "calendar.sverige": {
            "events": [
                {"start": "2026-12-24", "end": "2026-12-24", "summary": "Julafton"},
                {"start": "2026-12-25", "end": "2026-12-27", "summary": "Two days"},
                {"start": "2026-12-31T10:00:00+01:00", "end": "2026-12-31T11:00:00+01:00"},
                {"start": "bad"},
            ]
        }
    }
    assert holiday_dates(response, "calendar.sverige") == {
        date(2026, 12, 24),
        date(2026, 12, 25),
        date(2026, 12, 26),
        date(2026, 12, 31),
    }
    assert holiday_dates("junk", "calendar.sverige") == set()
    assert pick_holiday_calendar({"calendar.a": "DK", "calendar.b": "SE"}, "SE") == "calendar.b"
    assert pick_holiday_calendar({"calendar.a": "DK"}, "SE") == "calendar.a"
    assert pick_holiday_calendar({"calendar.a": "DK", "calendar.b": "NO"}, "SE") is None
    assert pick_holiday_calendar({}, "SE") is None


# ---------------------------------------------------------------------------------------------- on a site


def weather(hass: HomeAssistant, temperature: float, *, hourly: bool = True) -> list[ServiceCall]:
    """`weather.forecast_home` at `temperature`, and a `weather.get_forecasts` that answers 48 hours at 2 °C below
    it (or fails, without `hourly`)."""
    hass.states.async_set("weather.forecast_home", "cloudy", {"temperature": temperature, "temperature_unit": "°C"})
    calls: list[ServiceCall] = []

    async def get_forecasts(call: ServiceCall) -> dict[str, Any]:
        calls.append(call)
        if not hourly:
            raise ValueError("no hourly forecast")
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        return {
            "weather.forecast_home": {
                "forecast": [
                    {"datetime": (start + timedelta(hours=index)).isoformat(), "temperature": temperature - 2}
                    for index in range(48)
                ]
            }
        }

    hass.services.async_register("weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)
    return calls


def seed(controller: Any, days: int = 14, kw: float = 1.2) -> None:
    """`days` of measured hours before now with the house at `kw` (the test world's charger cannot be read, so the
    hours it measures itself have no house share)."""
    tracker = controller.demand.tracker
    now = controller.demand.now().replace(minute=0, second=0, microsecond=0)
    rows = [
        HourMean(start=now - timedelta(hours=index), kw=kw, house_kw=kw, outdoor_c=5.0)
        for index in range(days * 24, 0, -1)
    ]
    tracker.month = (now.year, now.month)
    tracker.hours = [*rows, *tracker.hours]


def holiday_calendar(hass: HomeAssistant, day: date) -> None:
    er.async_get(hass).async_get_or_create("calendar", "holiday", "se_holidays", suggested_object_id="sverige")

    async def get_events(call: ServiceCall) -> dict[str, Any]:
        return {"calendar.sverige": {"events": [{"start": day.isoformat(), "end": day.isoformat()}]}}

    hass.services.async_register("calendar", "get_events", get_events, supports_response=SupportsResponse.ONLY)


async def test_a_site_records_the_temperature_refreshes_hourly_and_shows_the_forecast(
    hass: HomeAssistant, freezer
) -> None:
    calls = weather(hass, 4.0)
    holiday_calendar(hass, date(2026, 12, 24))
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    seed(controller)
    await run(hass, freezer, 60, 1500.0)
    await run(hass, freezer, 60, 2500.0)
    await run(hass, freezer, 5, 2000.0)
    await hass.async_block_till_done()

    hours = [hour for hour in controller.demand.tracker.hours if hour.house_kw is None]
    assert hours and all(hour.outdoor_c == 4.0 for hour in hours)
    # One refresh at the start and one per completed hour, each with one hourly forecast fetched.
    forecast = controller.house_forecast
    assert forecast.refreshes == len(calls) >= 3
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    block = diagnostics["load_forecast"]
    assert block["weather_entity"] == "weather.forecast_home"
    assert block["weather"] == WEATHER_OK
    assert block["forecast_temperatures"] == 48
    assert block["holidays"] == "calendar.sverige"
    assert block["holiday_dates"] == ["2026-12-24"]
    assert block["forecast"]["day_types"] == "weekday_weekend_holiday"
    assert len(block["forecast"]["hours"]) == 48
    assert {KIND_HOUSE, KIND_HOUSE_RAW} <= set(block["log"]["kinds"]) <= {KIND_HOUSE, KIND_HOUSE_RAW, KIND_HOUSE_DAY_AHEAD}
    assert diagnostics["demand"]["outdoor_c"]
    # Two weeks at 1.2 kW: the profile, shown for the next 24 hours.
    assert {row[2] for row in block["forecast"]["hours"]} == {"profile"}
    assert block["log"]["kinds"][KIND_HOUSE]["hours"]
    state = hass.states.get("sensor.site_capacity_state")
    shown = state.attributes["forecast"]
    assert len(shown) == 24 and all(item["kw"] == 1.2 for item in shown)


async def test_without_a_weather_entity_or_an_hourly_forecast_it_runs_without_the_temperature(
    hass: HomeAssistant, freezer
) -> None:
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    await run(hass, freezer, 65, 1500.0)
    await hass.async_block_till_done()
    block = controller.house_forecast.diagnostics()
    assert (block["weather"], block["holidays"], block["retry_at"]) == (WEATHER_NONE, "weekday_weekend_only", None)
    assert all(hour.outdoor_c is None for hour in controller.demand.tracker.hours)

    calls = weather(hass, 4.0, hourly=False)
    await run(hass, freezer, 60, 1500.0)
    await hass.async_block_till_done()
    block = controller.house_forecast.diagnostics()
    assert block["weather"] == WEATHER_NO_FORECAST
    assert block["retry_at"] is not None
    # A source that would not answer is asked again after ten minutes, not on every reading.
    assert 1 <= len(calls) <= 7


async def test_the_log_survives_a_restart(hass: HomeAssistant, freezer) -> None:
    weather(hass, 4.0)
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    seed(controller)
    await run(hass, freezer, 125, 1500.0)
    await hass.async_block_till_done()
    logged = controller.house_forecast.log.as_dict()
    assert logged["kinds"][KIND_HOUSE]["hours"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    again = site_controller_for(hass, entry.entry_id)
    assert again is not controller
    assert again.house_forecast.log.as_dict()["kinds"] == logged["kinds"]


async def test_the_history_takes_the_tracker_over_and_outlives_its_months(hass: HomeAssistant, freezer) -> None:
    """The first refresh takes the tracker's hours over; when the tracker drops a month the forecast still reads them,
    and the history comes back after a restart."""
    weather(hass, 4.0)
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    seed(controller, days=30)
    await run(hass, freezer, 65, 1500.0)
    await hass.async_block_till_done()
    house = controller.house_forecast
    assert house.history.diagnostics()["hours"] >= 30 * 24
    assert house.history.migrated_at is not None
    samples = house.forecast.samples
    # A month turns: the tracker forgets the month before; the forecast does not.
    controller.demand.tracker.previous = []
    controller.demand.tracker.hours = []
    await run(hass, freezer, 60, 1500.0)
    await hass.async_block_till_done()
    assert house.forecast.samples >= samples
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["load_forecast"]["history"]["hours"] >= 30 * 24
    kept = house.history.as_dict()["hours"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    again = site_controller_for(hass, entry.entry_id).house_forecast
    assert again.history.as_dict()["hours"][: len(kept)] == kept


async def test_the_next_day_is_logged_as_forecast_at_the_day_ahead_publication(hass: HomeAssistant, freezer) -> None:
    from zoneinfo import ZoneInfo

    weather(hass, 4.0)
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    seed(controller)
    house = controller.house_forecast
    await run(hass, freezer, 5, 1500.0)
    await hass.async_block_till_done()
    # 08:00 local: the next day's prices are not out, nothing is logged for it.
    assert KIND_HOUSE_DAY_AHEAD not in house.log.kinds
    zone = ZoneInfo(TZ)
    now = controller.demand.now()
    publication = now.astimezone(zone).replace(hour=13, minute=0, second=10)
    freezer.move_to(publication)
    await house.async_refresh()
    logged = house.log.kinds[KIND_HOUSE_DAY_AHEAD].hours
    tomorrow = publication.date() + timedelta(days=1)
    assert logged and {start.astimezone(zone).date() for start in logged} == {tomorrow}
    assert all(hour.made_at == publication.astimezone(UTC) for hour in logged.values())
    # The hour after, the same hours keep the figures made at the publication.
    freezer.move_to(publication + timedelta(hours=1))
    await house.async_refresh()
    assert all(hour.made_at == publication.astimezone(UTC) for hour in house.log.kinds[KIND_HOUSE_DAY_AHEAD].hours.values())
