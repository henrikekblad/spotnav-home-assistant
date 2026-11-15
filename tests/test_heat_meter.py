"""The heat pump's meter (`site/heat_meter.py`): found without a setting where it can be told, its hours from the
recorder's statistics, the house forecast split into heat and the rest (shown and logged only), and the house card's
answer with heat in it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site import heat_meter as heat_module
from custom_components.spotnav.site.forecast_log import (
    KIND_HEAT,
    KIND_HOUSE,
    KIND_HOUSE_SPLIT,
    DayReview,
    Score,
)
from custom_components.spotnav.site.heat_meter import (
    KIND_ENERGY,
    KIND_POWER,
    NO_METER,
    SOURCE_ENERGY_DASHBOARD,
    SOURCE_INTEGRATION,
    SOURCE_SETTING,
    STATUS_AMBIGUOUS,
    STATUS_FOUND,
    STATUS_NONE,
    STATUS_UNUSABLE,
    DashboardDevice,
    HeatMeter,
    dashboard_candidates,
    heat_split,
    hourly_kw,
    integration_candidates,
    meter_kind,
    needs_setting,
    pick_heat_meter,
    split_rows,
)
from custom_components.spotnav.site.house_forecast import heat_comparison

from .test_demand_site import pilot_site, run
from .test_house_api import ask

pytestmark = pytest.mark.usefixtures("offline_relay")

TZ = "Europe/Stockholm"
UTC = timezone.utc


# ---------------------------------------------------------------------------------------------- which sensor


def test_a_sensors_kind_is_its_device_class_else_its_unit() -> None:
    assert meter_kind("power", "W") == KIND_POWER
    assert meter_kind("energy", "kWh") == KIND_ENERGY
    assert meter_kind(None, "kW") == KIND_POWER
    assert meter_kind(None, "Wh") == KIND_ENERGY
    assert meter_kind("temperature", "°C") is None
    assert meter_kind(None, "A") is None
    # A device class wins over a unit that says otherwise.
    assert meter_kind("current", "W") is None


def test_the_energy_dashboards_heating_device_is_found_by_its_integration_or_its_name() -> None:
    nibe = DashboardDevice("sensor.nibe_energy", None, ("Energy",), ("nibe_heatpump",))
    named = DashboardDevice("sensor.hem_energy", "sensor.hem_power", ("Värmepump", "Home Energy Meter"), ("zwave_js",))
    owner = DashboardDevice("sensor.varme_energi", None, ("Elmätare värme (Nibe + panna)",), ("zwave_js",))
    washer = DashboardDevice("sensor.washer_energy", "sensor.washer_power", ("Tvättmaskin",), ("shelly",))
    floor = DashboardDevice("sensor.floor", None, ("Golvvärme badrum",), ("shelly",))
    # A heat pump integration's statistic, the energy statistic when no power sensor is given.
    assert dashboard_candidates([nibe, washer]) == [("sensor.nibe_energy", KIND_ENERGY)]
    # A name that says heating: the power sensor where the device has one.
    assert dashboard_candidates([named, washer]) == [("sensor.hem_power", KIND_POWER)]
    assert dashboard_candidates([owner]) == [("sensor.varme_energi", KIND_ENERGY)]
    # A floor heater is not named as "värme" on its own word: not counted.
    assert dashboard_candidates([floor, washer]) == []
    for name in ("Heat pump", "Wärmepumpe", "Lämpöpumppu", "Varmepumpe", "Pompe à chaleur", "Bomba de calor"):
        assert dashboard_candidates([DashboardDevice("sensor.x", None, (name,))]) == [("sensor.x", KIND_ENERGY)]


def test_an_integrations_own_whole_pump_meter() -> None:
    lux = ("sensor.luxtronik2_current_power_consumption", "luxtronik2", "sensor.luxtronik2_current_power_consumption", "power", "W")
    nibe_part = ("sensor.int_el_add_power", "nibe_heatpump", "abc-43084", "power", "kW")
    lux_other = ("sensor.luxtronik2_flow_in", "luxtronik2", "sensor.luxtronik2_flow_in", "temperature", "°C")
    assert integration_candidates([lux, nibe_part, lux_other]) == [(lux[0], KIND_POWER)]
    # NIBE's own sensors are parts of the pump (the immersion heater, the compressor): never the meter.
    assert integration_candidates([nibe_part]) == []


def test_the_meter_is_the_setting_else_the_one_found() -> None:
    dashboard = [("sensor.hem_power", KIND_POWER)]
    integration = [("sensor.lux_power", KIND_POWER)]
    assert pick_heat_meter("sensor.chosen", KIND_ENERGY, dashboard, integration) == HeatMeter(
        STATUS_FOUND, "sensor.chosen", KIND_ENERGY, SOURCE_SETTING
    )
    # A chosen sensor that is neither power nor energy is not read.
    assert pick_heat_meter("sensor.temperature", None, dashboard, []).status == STATUS_UNUSABLE
    assert pick_heat_meter(None, None, dashboard, integration) == HeatMeter(
        STATUS_FOUND, "sensor.hem_power", KIND_POWER, SOURCE_ENERGY_DASHBOARD
    )
    assert pick_heat_meter(None, None, [], integration).source == SOURCE_INTEGRATION
    # Two heating devices on the dashboard: which is meant cannot be told, and the integration's is not guessed.
    two = pick_heat_meter(None, None, [*dashboard, ("sensor.other", KIND_ENERGY)], integration)
    assert two.status == STATUS_AMBIGUOUS and two.candidates == ("sensor.hem_power", "sensor.other")
    assert not two.found
    assert pick_heat_meter(None, None, [], []) == NO_METER
    # The setting is offered while none, or more than one, is found without it.
    assert needs_setting(NO_METER) and needs_setting(two)
    assert not needs_setting(pick_heat_meter(None, None, dashboard, []))


# ---------------------------------------------------------------------------------------------- its hours


def test_hourly_figures_from_a_power_sensors_mean_and_an_energy_sensors_change() -> None:
    hour = datetime(2026, 11, 15, 12, tzinfo=UTC)
    power = [
        {"start": hour.timestamp(), "mean": 0.66},
        {"start": (hour + timedelta(hours=1)).timestamp() * 1000, "mean": 1.24},  # the WebSocket's milliseconds
        {"start": hour + timedelta(hours=2), "mean": -0.1},
        {"start": hour + timedelta(hours=3), "mean": None},
        {"start": hour + timedelta(hours=4), "mean": 5000.0},  # a W reading in a kW series: a glitch
    ]
    assert hourly_kw(power, KIND_POWER) == {hour: 0.66, hour + timedelta(hours=1): 1.24, hour + timedelta(hours=2): 0.0}
    energy = [{"start": hour.timestamp(), "change": 0.8}, {"start": (hour + timedelta(hours=1)).timestamp(), "change": -2}]
    assert hourly_kw(energy, KIND_ENERGY) == {hour: 0.8, hour + timedelta(hours=1): 0.0}


# ---------------------------------------------------------------------------------------------- the split


def _house(now: datetime, days: int) -> tuple[list[HourMean], dict[datetime, float]]:
    """A house whose heat pump draws 0.15 kW per °C below 15 °C and whose rest is 0.6 kW (1.4 kW at 18-20); the
    outdoor temperature swings from -5 to +9 °C over a four-day cycle."""
    current = now.replace(minute=0, second=0, microsecond=0)
    rows, heat = [], {}
    for index in range(days * 24, 0, -1):
        start = current - timedelta(hours=index)
        outdoor = 2.0 + 7.0 * ((index // 24) % 4 - 1.5) / 1.5
        kw_heat = round(0.15 * (15.0 - outdoor), 3)
        rest = 1.4 if 17 <= start.hour <= 19 else 0.6
        rows.append(HourMean(start=start, kw=kw_heat + rest, house_kw=kw_heat + rest, outdoor_c=outdoor))
        heat[start] = kw_heat
    return rows, heat


def test_the_house_forecast_split_into_heat_and_the_rest() -> None:
    now = datetime(2026, 11, 15, 22, 10, tzinfo=UTC)
    rows, heat = _house(now, 28)
    heat_rows, rest_rows = split_rows(rows, heat)
    assert len(heat_rows) == len(rest_rows) == len(rows)
    assert all(row.outdoor_c is not None for row in heat_rows)
    assert {round(row.house_kw, 3) for row in rest_rows} == {0.6, 1.4}
    current = now.replace(minute=0, second=0, microsecond=0)
    cold = {current + timedelta(hours=h): -10.0 for h in range(48)}
    split = heat_split(rows, heat, now, TZ, temperatures=cold, holidays=None)
    assert split is not None
    # Heat is learned against the outdoor temperature: a cold night ahead lifts heat, not the rest.
    assert split.heat.temperature.active
    mild = heat_split(rows, heat, now, TZ, temperatures={start: 9.0 for start in cold}, holidays=None)
    assert mild is not None
    first = current + timedelta(hours=2)
    assert split.heat.by_hour()[first] > mild.heat.by_hour()[first] + 1.0
    assert split.rest.by_hour()[first] == pytest.approx(mild.rest.by_hour()[first], abs=0.05)
    assert split.by_hour()[first] == pytest.approx(split.heat.by_hour()[first] + split.rest.by_hour()[first])
    # Hours without the meter's figure are left out; none at all, no split.
    assert heat_split(rows, {}, now, TZ, temperatures=cold, holidays=None) is None
    assert split_rows(rows, {rows[0].start: 9.9})[1][0].house_kw == 0.0


def test_the_days_comparison_lines() -> None:
    day = datetime(2026, 11, 14).date()
    review = lambda mae: DayReview(day, datetime(2026, 11, 15, tzinfo=UTC), Score(24, mae, 0.5, 0.0), None, None)  # noqa: E731
    lines = heat_comparison([(KIND_HOUSE, review(0.41)), (KIND_HOUSE_SPLIT, review(0.33)), (KIND_HEAT, review(0.2))])
    assert lines == [
        "2026-11-14: house forecast as heat plus the rest MAE 0.330 kW over 24 h, whole 0.410 kW over 24 h;"
        " heat alone MAE 0.200 kW over 24 h"
    ]
    assert heat_comparison([(KIND_HOUSE, review(0.41))]) == []


# ---------------------------------------------------------------------------------------------- on a site


def _fake_hours(values: dict[datetime, float]) -> Any:
    calls: list[tuple[str | None, datetime, datetime]] = []

    async def read(hass: Any, meter: HeatMeter, start: datetime, end: datetime) -> dict[datetime, float]:
        calls.append((meter.entity_id, start, end))
        return {moment: kw for moment, kw in values.items() if start <= moment < end}

    read.calls = calls  # type: ignore[attr-defined]
    return read


async def test_a_chosen_meter_is_read_split_logged_and_shown(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass.states.async_set("sensor.varme_effekt", "1093", {"unit_of_measurement": "W", "device_class": "power"})
    site = await pilot_site(hass, freezer, heat_meter_entity="sensor.varme_effekt")
    controller = site_controller_for(hass, site.entry_id)
    forecast = controller.house_forecast
    now = controller.demand.now()
    rows, heat = _house(now, 10)
    tracker = controller.demand.tracker
    tracker.month = (now.year, now.month)
    tracker.hours = [*rows, *tracker.hours]
    read = _fake_hours(heat)
    monkeypatch.setattr(heat_module, "async_heat_hours", read)

    await forecast.async_refresh()
    meter = forecast.heat.meter
    assert (meter.status, meter.entity_id, meter.kind, meter.source) == (STATUS_FOUND, "sensor.varme_effekt", KIND_POWER, SOURCE_SETTING)
    assert len(forecast.heat.by_hour) == len(heat)
    split = forecast.heat_split
    assert split is not None and split.heat.by_hour()
    assert forecast.log.hours(KIND_HEAT) and forecast.log.hours(KIND_HOUSE_SPLIT)
    # The plans keep the whole load's forecast: the split is shown and logged only.
    assert forecast.plan_kw_by_hour(now) == {
        start: kw for start, kw in forecast.forecast.known_by_hour().items() if start >= min(forecast.forecast.by_hour())
    }
    # The next refresh reads only the last hours again.
    await forecast.async_refresh()
    first, again = read.calls
    assert again[1] >= first[2] - timedelta(hours=4)
    diagnostics = forecast.diagnostics()["heat"]
    assert diagnostics["meter"]["entity_id"] == "sensor.varme_effekt" and diagnostics["meter"]["detected"]["status"] == STATUS_NONE
    assert diagnostics["forecast"]["hours"] and diagnostics["rest"]["hours"]

    answer = (await ask(hass, 1, api_version=1, site_id=site.entry_id))["result"]
    block = answer["heat"]
    assert block["status"] == STATUS_FOUND and block["entity_id"] == "sensor.varme_effekt"
    assert block["setting_offered"] is True and block["day"]["hours"] > 0
    past = [row for row in answer["hours"] if row["heat_kw"] is not None]
    assert past and all(row["heat_kw"] <= row["house_kw"] for row in past if row["house_kw"] is not None)
    assert any(row["heat_forecast_kw"] is not None for row in answer["hours"][-24:])

    # Another sensor chosen: the old hours and the heat kinds' days not yet reviewed are dropped.
    hass.states.async_set("sensor.other_heat", "2.0", {"unit_of_measurement": "kWh", "device_class": "energy"})
    entry = hass.config_entries.async_get_entry(site.entry_id)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "heat_meter_entity": "sensor.other_heat"})
    monkeypatch.setattr(heat_module, "async_heat_hours", _fake_hours({}))
    await forecast.async_refresh()
    assert forecast.heat.meter.kind == KIND_ENERGY and forecast.heat.by_hour == {}
    assert forecast.heat_split is None and not forecast.log.hours(KIND_HEAT)


async def test_the_energy_dashboards_heat_pump_is_found_without_a_setting(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = await pilot_site(hass, freezer)
    config_entry = hass.config_entries.async_get_entry(site.entry_id)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("test", "hem")}, name="Home Energy Meter"
    )
    dr.async_get(hass).async_update_device(device.id, name_by_user="Elmätare värmepump")
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "zwave_js", "hem-power", suggested_object_id="hem_power", device_id=device.id)
    registry.async_get_or_create("sensor", "zwave_js", "hem-energy", suggested_object_id="hem_energy", device_id=device.id)
    prefs: dict[str, Any] = {
        "device_consumption": [
            {"stat_consumption": "sensor.washer_energy", "name": "Tvättmaskin"},
            {"stat_consumption": "sensor.hem_energy", "stat_rate": "sensor.hem_power"},
        ]
    }

    async def fake_prefs(_hass: Any) -> Any:
        return prefs

    monkeypatch.setattr(heat_module, "async_energy_prefs", fake_prefs)
    monkeypatch.setattr(heat_module, "async_heat_hours", _fake_hours({}))
    controller = site_controller_for(hass, site.entry_id)
    meter = await controller.house_forecast.heat.async_detect()
    assert (meter.status, meter.entity_id, meter.kind, meter.source) == (
        STATUS_FOUND,
        "sensor.hem_power",
        KIND_POWER,
        SOURCE_ENERGY_DASHBOARD,
    )
    assert not needs_setting(controller.house_forecast.heat.detected)
    # A second heating device: ambiguous, and the house card's answer names both.
    prefs["device_consumption"].append({"stat_consumption": "sensor.pool_energy", "name": "Poolvärmepump"})
    await controller.house_forecast.async_refresh()
    answer = (await ask(hass, 1, api_version=1, site_id=site.entry_id))["result"]
    assert answer["heat"]["status"] == STATUS_AMBIGUOUS
    assert answer["heat"]["candidates"] == ["sensor.hem_power", "sensor.pool_energy"]
    assert answer["heat"]["setting_offered"] is True and answer["heat"]["day"] is None


async def test_without_a_meter_nothing_changes(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer)
    await run(hass, freezer, 65, 1500.0)
    controller = site_controller_for(hass, site.entry_id)
    assert controller.house_forecast.heat.meter == NO_METER and controller.house_forecast.heat_split is None
    answer = (await ask(hass, 1, api_version=1, site_id=site.entry_id))["result"]
    assert answer["heat"] is None
