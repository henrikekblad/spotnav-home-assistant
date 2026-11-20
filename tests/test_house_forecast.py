"""The house load forecast's Home Assistant shell (`site/house_forecast.py`): its sources picked with no setting, the
hourly refresh on a real site, the `forecast` attribute, the diagnostics and the log kept across a restart."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav import time_zones
from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site import house_forecast as house_forecast_module
from custom_components.spotnav.site.house_history import MIN_GROSS_HOURS
from custom_components.spotnav.site.forecast_log import (
    KIND_HOUSE,
    KIND_HOUSE_DAY_AHEAD,
    KIND_HOUSE_MEDIAN,
    KIND_HOUSE_RAW,
    KIND_IMPORT,
)
from custom_components.spotnav.site.house_forecast import (
    BASIS_GROSS,
    BASIS_NET,
    SURPLUS_COLLECTING,
    SURPLUS_NO_SOLAR_STATISTICS,
    SURPLUS_OK,
    holiday_dates,
    hourly_temperatures,
    OUTDOOR_SENSOR,
    OUTDOOR_WEATHER,
    pick_holiday_calendar,
    pick_outdoor_sensor,
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


def test_a_heat_pumps_outdoor_sensor_is_taken_only_when_it_is_the_one() -> None:
    nibe = ("sensor.bt1_outdoor_temperature", "nibe_heatpump", "ABC123-40004")
    nibe_s = ("sensor.current_outdoor_temperature_bt1", "nibe_heatpump", "entry-30002")
    uplink = ("sensor.current_outd_temp_bt1", "myuplink", "robin-r-1234-aa-bb-40004")
    melcloud = ("sensor.ecodan_outside_temperature", "melcloud", "123-aa:bb-outside_temperature")
    thermia = ("sensor.outdoor_temperature", "thermiagenesis", "thermiagenesis_input_outdoor_temperature")
    assert pick_outdoor_sensor([thermia]) == thermia[0]
    assert pick_outdoor_sensor([("sensor.x", "thermiagenesis", "thermiagenesis_input_room_temperature_sensor")]) is None
    assert pick_outdoor_sensor([nibe]) == nibe[0]
    assert pick_outdoor_sensor([nibe_s]) == nibe_s[0]
    assert pick_outdoor_sensor([uplink]) == uplink[0]
    assert pick_outdoor_sensor([melcloud]) == melcloud[0]
    # Two heat pumps, or the same one through two integrations: which is meant cannot be told.
    assert pick_outdoor_sensor([nibe, uplink]) is None
    # Another coil, another integration, a name that says outdoor, or no sensor at all: none.
    assert pick_outdoor_sensor([("sensor.bt2", "nibe_heatpump", "ABC123-40008")]) is None
    assert pick_outdoor_sensor([("sensor.utomhus_temperatur", "template", "outdoor-40004")]) is None
    assert pick_outdoor_sensor([("number.x", "myuplink", "dev-40004")]) is None
    assert pick_outdoor_sensor([]) is None


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
    assert {KIND_HOUSE, KIND_HOUSE_RAW, KIND_HOUSE_MEDIAN} <= set(block["log"]["kinds"]) <= {
        KIND_HOUSE,
        KIND_HOUSE_RAW,
        KIND_HOUSE_DAY_AHEAD,
        KIND_HOUSE_MEDIAN,
    }
    # The median's error band waits for a week of its own misses.
    assert block["band"]["status"] == "learning" and block["band"]["hours"] == []
    # A site without a battery: the house is the import less the chargers, and the import target reads the same.
    assert block["basis"] == BASIS_NET and block["battery"] is False and block["import_forecast"] is None
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


async def test_a_weather_and_a_calendar_that_never_answer_end_the_refresh_as_not_read(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setattr(house_forecast_module, "READ_TIMEOUT_S", 0.05)
    hass.states.async_set("weather.forecast_home", "cloudy", {"temperature": 4.0, "temperature_unit": "°C"})
    er.async_get(hass).async_get_or_create("calendar", "holiday", "se_holidays", suggested_object_id="sverige")
    calls: list[str] = []

    async def hangs(call: ServiceCall) -> dict[str, Any]:
        # The test's clock is frozen: the hung call moves it on, so the read's timeout is due.
        calls.append(call.service)
        freezer.tick(timedelta(seconds=1))
        await asyncio.Event().wait()
        return {}

    hass.services.async_register("weather", "get_forecasts", hangs, supports_response=SupportsResponse.ONLY)
    hass.services.async_register("calendar", "get_events", hangs, supports_response=SupportsResponse.ONLY)
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    await run(hass, freezer, 65, 1500.0)
    await hass.async_block_till_done()
    forecast = controller.house_forecast
    assert {"get_forecasts", "get_events"} <= set(calls)
    assert not forecast._running  # noqa: SLF001 - the refresh ended
    block = forecast.diagnostics()
    assert block["weather"] == WEATHER_NO_FORECAST and block["retry_at"] is not None


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


async def test_the_temperatures_are_kept_in_their_own_store_and_survive_a_restart(
    hass: HomeAssistant, freezer, hass_storage: dict[str, Any]
) -> None:
    """The measured hours' temperatures, their source and the forecast as issued are kept beside the log, counted in
    the diagnostics, written at the unload and back after a restart."""
    weather(hass, 4.0)
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    seed(controller, days=3)
    await run(hass, freezer, 125, 1500.0)
    await hass.async_block_till_done()
    house = controller.house_forecast
    key = house_forecast_module.temperature_store_key(entry.entry_id)
    block = (await async_get_config_entry_diagnostics(hass, entry))["load_forecast"]["temperature_history"]
    assert block["measured_hours"] >= 3 * 24
    assert block["sources"] and block["sources"][0][1] == "weather:weather.forecast_home"
    assert block["forecast_sources"] == [[block["forecast_sources"][0][0], "weather:weather.forecast_home"]]
    assert block["last_hours"] >= 47
    assert block["offset"]["reason"] == "same_source" and block["offset"]["active"] is False
    kept = house.temperature_history.as_dict()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    # The unload wrote what waited.
    assert hass_storage[key]["data"]["measured"] == kept["measured"]
    again = site_controller_for(hass, entry.entry_id).house_forecast
    assert again.temperature_history.as_dict()["measured"][: len(kept["measured"])] == kept["measured"]
    assert again.temperature_history.sources == house.temperature_history.sources


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


async def test_a_heat_pumps_outdoor_sensor_is_preferred_over_the_weather_and_shown(hass: HomeAssistant, freezer) -> None:
    weather(hass, 4.0)
    er.async_get(hass).async_get_or_create(
        "sensor", "myuplink", "dev-40004", suggested_object_id="current_outd_temp_bt1"
    )
    hass.states.async_set("sensor.current_outd_temp_bt1", "-3.5", {"unit_of_measurement": "°C"})
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    await run(hass, freezer, 65, 1500.0)
    await hass.async_block_till_done()

    forecast = controller.house_forecast
    assert forecast.outdoor_c() == -3.5
    block = (await async_get_config_entry_diagnostics(hass, entry))["load_forecast"]
    assert block["outdoor"] == {"source": OUTDOOR_SENSOR, "sensor": "sensor.current_outd_temp_bt1"}
    assert [hour.outdoor_c for hour in controller.demand.tracker.hours] == [-3.5]
    # The forecast ahead stays the weather entity's.
    assert block["forecast_temperatures"] == 48

    # A sensor that does not read: the weather entity's temperature, and it says so.
    hass.states.async_set("sensor.current_outd_temp_bt1", "unavailable")
    assert forecast.outdoor_c() == 4.0
    assert forecast.diagnostics()["outdoor"]["source"] == OUTDOOR_WEATHER


async def test_the_forecast_temperatures_are_moved_onto_the_heat_pump_sensors_scale(hass: HomeAssistant, freezer) -> None:
    """History from the heat pump's sensor, forecast from the weather entity: with ten days of hours (of the two weeks
    seeded) where the sensor read 1.5 °C above the weather entity's last forecast, the forecast temperatures fed to the model are 1.5 °C
    warmer; the log of the forecast as issued keeps them as fetched."""
    weather(hass, 4.0)
    er.async_get(hass).async_get_or_create(
        "sensor", "myuplink", "dev-40004", suggested_object_id="current_outd_temp_bt1"
    )
    hass.states.async_set("sensor.current_outd_temp_bt1", "-3.5", {"unit_of_measurement": "°C"})
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    seed(controller)
    house = controller.house_forecast
    now = controller.demand.now().replace(minute=0, second=0, microsecond=0)
    kept = house.temperature_history
    first = now - timedelta(days=10)
    first_key = int(first.timestamp()) // 3600
    kept.sources = [(first_key, "sensor:sensor.current_outd_temp_bt1")]
    kept.forecast_sources = [(first_key, "weather:weather.forecast_home")]
    # The seeded hours read 5.0 °C on the sensor; the weather entity had forecast 3.5 °C for them.
    for index in range(10 * 24):
        kept.last[first_key + index] = 35
    await house.async_refresh()

    assert house.offset.active and house.offset.offset_c == pytest.approx(1.5) and house.offset.pairs == 240
    ahead = [hour for hour in house.forecast.hours if hour.start > now]
    # The weather entity's forecast is 2 °C; the model reads 3.5 °C.
    assert ahead and all(hour.outdoor_c == pytest.approx(3.5) for hour in ahead)
    assert kept.last[first_key + 10 * 24 + 1] == 20
    block = house.diagnostics()["temperature_history"]["offset"]
    assert block == {"offset_c": 1.5, "pairs": 240, "active": True, "reason": "learned", "clamped": False}

    # The sensor gone, the history is the weather entity's again: nothing is moved.
    hass.states.async_set("sensor.current_outd_temp_bt1", "unavailable")
    freezer.tick(timedelta(hours=1))
    await house.async_refresh()
    assert house.offset.reason == "same_source" and not house.offset.active
    assert all(hour.outdoor_c == pytest.approx(2.0) for hour in house.forecast.hours if hour.start > now + timedelta(hours=1))


# ---------------------------------------------------------------------------------------------- a battery site


async def test_on_a_battery_site_the_house_is_its_whole_load_and_the_import_target_keeps_the_net_figure(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's case: the battery covers the house, so the import less the chargers reads 0.2 kW; the house draws
    1.4 kW (0.9 kW of `grid - car - battery` and 0.5 kWh of sun per hour). The shown forecast, the log and the battery
    plan's median take 1.4; a plan under the import target keeps the net 0.2."""
    weather(hass, 4.0)
    hass.states.async_set("sensor.battery_power", "0", {"unit_of_measurement": "W"})
    entry = await pilot_site(hass, freezer, battery_aggregate_power_entity="sensor.battery_power")
    controller = site_controller_for(hass, entry.entry_id)
    house = controller.house_forecast
    seed(controller, kw=0.2)
    now = controller.demand.now()
    tracker = controller.demand.tracker
    recent = now - timedelta(hours=12)
    tracker.hours = [
        HourMean(**{**{slot: getattr(hour, slot) for slot in HourMean.__slots__}, "ex_battery_kw": 0.9})
        if hour.start >= recent
        else hour
        for hour in tracker.hours
    ]
    reads: list[datetime] = []

    async def ids(hass: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return ("sensor.pv",), ()

    async def statistics(hass: Any, wanted: Any, start: datetime, end: datetime, period: str) -> dict[str, Any]:
        reads.append(start)
        moment, rows = start, []
        while moment < end:
            rows.append({"start": moment.timestamp(), "change": 0.5})
            moment += timedelta(hours=1)
        return {"sensor.pv": rows}

    monkeypatch.setattr(house_forecast_module, "async_energy_ids", ids)
    monkeypatch.setattr(house_forecast_module, "async_statistics", statistics)
    # The whole load from its first hours on: the import less the chargers is the battery's, never the house. Twelve
    # hours fill no hour's cell yet, so the forecast is their flat mean.
    await house.async_refresh()
    assert house.basis == BASIS_GROSS
    assert len(house.history.gross) == 12 and reads
    assert {round(hour.kw, 3) for hour in house.forecast.hours} == {1.4}
    assert {round(hour.kw, 3) for hour in house.import_forecast.hours} == {0.2}
    before = house.log.hours(KIND_HOUSE)
    assert before and all(hour.forecast == pytest.approx(1.4) for hour in before.values())

    # The battery plan's own rows of the two weeks before: taken over once, with the sun's production.
    old = [(start, 0.9) for start in sorted(house.history.rows) if start < min(house.history.gross)]
    assert await house.async_take_rows(old, now) == len(old)
    assert await house.async_take_rows(old, now) == 0
    await house.async_refresh()

    assert house.basis == BASIS_GROSS
    assert {round(hour.kw, 3) for hour in house.forecast.hours} == {1.4}
    assert {round(hour.kw, 3) for hour in house.median.hours} == {1.4}
    assert {round(hour.kw, 3) for hour in house.import_forecast.hours} == {0.2}
    assert all(item["kw"] == 1.4 for item in house.attribute)
    by_hour, basis, with_sun = house.battery_house_by_hour()
    assert basis == BASIS_GROSS and with_sun and {round(kw, 3) for kw in by_hour.values()} == {1.4}
    # The expected surplus: the whole load's median too, never the net figure.
    surplus_house, status = house.surplus_house_by_hour(now)
    assert status == SURPLUS_OK and {round(kw, 3) for kw in surplus_house.values()} == {1.4}
    # What a plan under the import target counts on: the net figure.
    assert {round(kw, 3) for kw in house.plan_kw_by_hour(now).values()} == {0.2}
    assert house.plan_kw_at(now) == pytest.approx(0.2)
    # The log: the house kinds on the whole load from now on, the import target's beside them.
    assert all(hour.forecast == pytest.approx(1.4) for hour in house.log.hours(KIND_HOUSE).values())
    assert all(hour.forecast == pytest.approx(0.2) for hour in house.log.hours(KIND_IMPORT).values())
    block = (await async_get_config_entry_diagnostics(hass, entry))["load_forecast"]
    assert block["basis"] == BASIS_GROSS and block["battery"] is True and block["production"] == "ok"
    assert block["history"]["gross_hours"] == len(house.history.gross) and block["import_forecast"]["hours"]

    # Kept across a restart: the whole load, when it was taken over, and the figure the log is on.
    gross = dict(house.history.gross)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    again = site_controller_for(hass, entry.entry_id).house_forecast
    assert again.history.gross == gross and again.history.gross_migrated_at is not None
    assert again.basis == BASIS_GROSS

    # The battery taken out of the site, the solar statistics kept: still the whole load.
    monkeypatch.setattr(again, "_has_battery", lambda: False)
    await again.async_refresh()
    assert again.basis == BASIS_GROSS and again.import_forecast is not None

    # And the solar statistics too: back to the net figure, one forecast for both.
    monkeypatch.setattr(house_forecast_module, "async_energy_ids", no_ids)
    await again.async_refresh()
    assert again.basis == BASIS_NET and again.import_forecast is None
    assert {round(hour.kw, 3) for hour in again.forecast.hours} == {0.2}
    assert again.surplus_house_by_hour(controller.demand.now()) == ({}, SURPLUS_NO_SOLAR_STATISTICS)


async def test_a_battery_that_grid_charges_is_never_forecast_as_the_house(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's site, 14 Nov: the import less the chargers read ~0 while the battery covered the house and 13.7 kW
    in the hour it grid-charged at the fuse (14:00), so a forecast on it showed 4.8-17 kW for a house drawing 1.5 kW.
    With one day of the whole load (1.0 kW of `grid - car - battery` and 0.5 kWh of sun per hour) the house is that
    whole load at once; only the import target's forecast keeps the grid-charging hour."""
    weather(hass, 4.0)
    hass.states.async_set("sensor.battery_power", "0", {"unit_of_measurement": "W"})
    entry = await pilot_site(hass, freezer, battery_aggregate_power_entity="sensor.battery_power")
    controller = site_controller_for(hass, entry.entry_id)
    house = controller.house_forecast
    seed(controller, kw=0.0)
    zone = time_zones.require_zone(TZ)
    tracker = controller.demand.tracker
    tracker.hours = [
        HourMean(**{**{slot: getattr(hour, slot) for slot in HourMean.__slots__}, "house_kw": 13.7})
        if hour.start.astimezone(zone).hour == 14 and hour.house_kw is not None
        else hour
        for hour in tracker.hours
    ]
    with_ex_battery(controller, 1.0, 24)
    solar_statistics(monkeypatch, 0.5)
    await house.async_refresh()

    assert house.basis == BASIS_GROSS
    assert {round(hour.kw, 3) for hour in house.forecast.hours} == {1.5}
    assert {round(kw, 3) for kw in house.battery_house_by_hour()[0].values()} == {1.5}
    assert all(item["kw"] == 1.5 for item in house.attribute)
    # The import target counts on what the import less the chargers did: the grid-charging hour stays there.
    target = house.import_forecast.by_hour()
    assert {round(kw, 1) for start, kw in target.items() if start.astimezone(zone).hour == 14} == {13.7}


async def no_ids(hass: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (), ()


def with_ex_battery(controller: Any, kw: float, hours: int) -> None:
    """The tracker's last `hours` hours carry `grid - car - battery` at `kw`."""
    tracker = controller.demand.tracker
    recent = controller.demand.now() - timedelta(hours=hours)
    tracker.hours = [
        HourMean(**{**{slot: getattr(hour, slot) for slot in HourMean.__slots__}, "ex_battery_kw": kw})
        if hour.start >= recent
        else hour
        for hour in tracker.hours
    ]


def solar_statistics(monkeypatch: pytest.MonkeyPatch, kwh: float) -> None:
    """The Energy dashboard has `sensor.pv`, producing `kwh` in every hour."""

    async def ids(hass: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return ("sensor.pv",), ()

    async def statistics(hass: Any, wanted: Any, start: datetime, end: datetime, period: str) -> dict[str, Any]:
        moment, rows = start, []
        while moment < end:
            rows.append({"start": moment.timestamp(), "change": kwh})
            moment += timedelta(hours=1)
        return {"sensor.pv": rows}

    monkeypatch.setattr(house_forecast_module, "async_energy_ids", ids)
    monkeypatch.setattr(house_forecast_module, "async_statistics", statistics)


# ---------------------------------------------------------------------------------------------- a solar site


async def test_on_a_solar_site_without_a_battery_the_house_is_its_whole_load_after_three_days(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No battery, the sun in the Energy dashboard: the import less the chargers (0.3 kW) is the house less the sun;
    `grid - car` (the battery term 0) is 0.3 kW and the sun adds 1.2 kWh per hour, so the house draws 1.5 kW. The
    whole load counts from `MIN_GROSS_HOURS` hours on; the import target keeps the net figure."""
    weather(hass, 4.0)
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    house = controller.house_forecast
    assert not controller.has_battery_reading()
    seed(controller, kw=0.3)
    solar_statistics(monkeypatch, 1.2)
    now = controller.demand.now()

    with_ex_battery(controller, 0.3, 12)
    await house.async_refresh()
    assert house.basis == BASIS_NET and len(house.history.gross) == 12
    assert all(sun for _kw, sun in house.history.gross.values())
    # Collecting: no surplus rather than one with the sun taken off twice.
    assert house.surplus_house_by_hour(now) == ({}, SURPLUS_COLLECTING)

    # Three days of hours filed (as the hourly refreshes do, `PRODUCTION_WAIT_H` back each).
    for hour in sorted(house.history.rows)[-MIN_GROSS_HOURS:]:
        house.history.gross[hour] = (1.5, True)
    await house.async_refresh()
    assert house.basis == BASIS_GROSS
    assert {round(hour.kw, 3) for hour in house.median.hours if hour.kw is not None} == {1.5}
    assert {round(kw, 3) for kw in house.plan_kw_by_hour(now).values()} == {0.3}
    by_hour, status = house.surplus_house_by_hour(now)
    assert status == SURPLUS_OK and by_hour and {round(kw, 3) for kw in by_hour.values()} == {1.5}
    assert min(by_hour) >= now.replace(minute=0, second=0, microsecond=0)
    block = (await async_get_config_entry_diagnostics(hass, entry))["load_forecast"]
    assert block["basis"] == BASIS_GROSS and block["battery"] is False and block["production"] == "ok"


async def test_a_solar_site_without_a_battery_files_no_hour_without_its_production(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The statistics there but not yet compiled for the hours: they wait, never filed as the house less the sun."""
    weather(hass, 4.0)
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    house = controller.house_forecast
    seed(controller, kw=0.3)
    with_ex_battery(controller, 0.3, 12)

    async def ids(hass: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return ("sensor.pv",), ()

    async def nothing(hass: Any, wanted: Any, start: datetime, end: datetime, period: str) -> dict[str, Any]:
        return {"sensor.pv": []}

    monkeypatch.setattr(house_forecast_module, "async_energy_ids", ids)
    monkeypatch.setattr(house_forecast_module, "async_statistics", nothing)
    await house.async_refresh()
    assert house.history.gross == {} and house.basis == BASIS_NET


async def test_a_site_without_solar_statistics_keeps_the_net_figure_and_has_no_surplus(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    weather(hass, 4.0)
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    house = controller.house_forecast
    seed(controller, kw=0.3)
    with_ex_battery(controller, 0.3, 12)
    monkeypatch.setattr(house_forecast_module, "async_energy_ids", no_ids)
    await house.async_refresh()
    now = controller.demand.now()
    assert house.basis == BASIS_NET and house.history.gross == {} and house.import_forecast is None
    assert {round(kw, 3) for kw in house.plan_kw_by_hour(now).values()} == {0.3}
    assert house.surplus_house_by_hour(now) == ({}, SURPLUS_NO_SOLAR_STATISTICS)
    assert house.diagnostics()["production"] == "no_solar_statistics"


async def test_a_site_without_a_battery_records_grid_less_car(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hour tracker is given a battery term of 0 on a site without a battery, a live reading on one with."""
    seen: list[Any] = []
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    monkeypatch.setattr(controller.demand, "due", lambda: True)
    monkeypatch.setattr(controller.demand, "observe", lambda *args, **kwargs: seen.append(kwargs["battery_w"]))
    controller._observe_demand()
    monkeypatch.setattr(controller, "_battery_live_w", lambda: (True, None))
    controller._observe_demand()
    monkeypatch.setattr(controller, "_battery_live_w", lambda: (True, -800.0))
    controller._observe_demand()
    assert seen == [0.0, None, -800.0]
