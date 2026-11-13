"""What SpotNav detected: each kind's choice, why, and the near-misses (`site/detected.py`), read from a site with a
Sigenergy battery (`site/detected_hass.py`), in its diagnostics, the debug bundle and `spotnav/get_detected`."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.debug_bundle import async_build_debug_bundle
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.site.detected import (
    KINDS,
    BatteryOffer,
    ChargerIntegration,
    ChargerRow,
    MeterOffer,
    OutdoorOffer,
    VehicleRow,
    answer,
    battery,
    chargers,
    holiday_calendar,
    meter,
    outdoor_sensor,
    production,
    solar_forecast,
    vehicles,
    weather,
)

from tests.test_battery_runtime import _site
from tests.world import admin, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")


def _reasons(item: Any) -> list[tuple[str | None, str]]:
    return [(miss.entity_id or miss.name, miss.reason) for miss in item.near_misses]


# --- the builders -------------------------------------------------------------------------------------------------


def test_every_kind_is_answered_once_in_order() -> None:
    result = answer([weather([], None, "weather.forecast_home", None)])
    assert [item["kind"] for item in result["items"]] == list(KINDS)
    assert result["items"][0] == {
        "kind": "chargers", "status": "none", "chosen": [], "why": "not_looked_for", "detail": {}, "near_misses": [],
    }


def test_a_charger_integration_no_spotnav_charger_uses_is_a_near_miss() -> None:
    item = chargers(
        [ChargerRow("Garage", "easee", "switch.garage", "number.garage", "easee")],
        [
            ChargerIntegration("easee", "Easee", "charger", True),
            ChargerIntegration("zaptec", "Zaptec", "charger", False),
            ChargerIntegration("evcc_intg", "evcc", "external_controller", False),
            ChargerIntegration("smappee", "Smappee", "excluded", False),
        ],
    )
    assert item.status == "configured" and item.chosen == ("switch.garage",)
    assert _reasons(item) == [("Zaptec", "not_added_as_charger"), ("evcc", "external_controller")]
    assert chargers([], []).status == "none"


def test_the_meter_set_and_another_meter_offered() -> None:
    own = MeterOffer("Pulse", "tibber", ("sensor.l1", "sensor.l2", "sensor.l3"))
    other = MeterOffer("Han", "han", ("sensor.h1",), disabled_entity_ids=("sensor.h1",))
    item = meter("direct", ["sensor.l1", "sensor.l2", "sensor.l3"], "sensor.grid", {}, [own, other])
    assert item.why == "measurement:direct" and "sensor.grid" in item.chosen
    assert _reasons(item) == [("sensor.h1", "entities_disabled")]
    assert meter(None, [], None, {}, [own]).status == "none"


def test_the_battery_names_its_adapter_its_missing_facts_and_unusable_controls() -> None:
    facts = {
        "battery_entity_id": "sensor.sigen_plant_battery_power",
        "integration": "sigen",
        "brand": "sigenergy",
        "sources": {"power": "sensor.x", "soc": "sensor.soc"},
        "management": {"state": "externally_controlled", "reason": "integration:checkwatt"},
        "controls": {
            "hold": {"method": "number", "found": True, "enabled": False, "available": False, "entity_id": "number.h"},
            "grid_charge": {"method": "select", "found": False},
        },
    }
    item = battery(facts, {"own_optimiser": "AI mode"}, [BatteryOffer("Other", "huawei_solar", "sensor.huawei")])
    assert item.why == "adapter:sigenergy" and item.detail["management"] == "externally_controlled"
    assert ("sensor.huawei", "other_battery") in _reasons(item)
    assert ("number.h", "control_disabled:hold") in _reasons(item)
    assert (None, "control_not_found:grid_charge") in _reasons(item)
    assert (None, "fact_not_found:capacity") in _reasons(item)
    assert battery({}, None, []).status == "none"


def test_the_sun_and_the_production() -> None:
    item = solar_forecast(["Home"], {"Home": "forecast_solar", "Roof": "solcast_solar"}, "shadow")
    assert item.status == "configured" and _reasons(item) == [("Roof (solcast_solar)", "not_chosen")]
    assert solar_forecast([], {}, None).why == "not_configured"
    assert production([], "none").why == "energy_dashboard_has_no_solar"
    assert production(["sensor.pv"], "ok").chosen == ("sensor.pv",)


def test_the_weather_the_outdoor_sensor_and_the_holidays() -> None:
    item = weather(["weather.forecast_home", "weather.smhi"], "weather.forecast_home", "weather.forecast_home", "ok")
    assert item.why == "default_weather" and _reasons(item) == [("weather.smhi", "not_the_default")]
    assert weather(["weather.a", "weather.b"], None, "weather.forecast_home", None).status == "ambiguous"
    two = [OutdoorOffer("sensor.a", "nibe_heatpump"), OutdoorOffer("sensor.b", "myuplink"),
           OutdoorOffer("sensor.c", "melcloud", disabled=True)]
    item = outdoor_sensor(two, None, "weather")
    assert item.status == "ambiguous"
    assert _reasons(item) == [("sensor.a", "several_heat_pumps"), ("sensor.b", "several_heat_pumps"),
                              ("sensor.c", "disabled")]
    assert outdoor_sensor([], None, "weather").why == "weather_entity_used"
    item = holiday_calendar({"calendar.se": "SE", "calendar.no": "NO"}, "calendar.se", "SE")
    assert item.why == "home_country" and _reasons(item) == [("calendar.no", "other_country")]
    assert holiday_calendar({}, None, "SE").why == "weekday_weekend_only"


def test_the_cars_and_those_with_several_levels() -> None:
    item = vehicles([VehicleRow("Kia", "sensor.kia_soc")], [("Volvo", ["sensor.v1", "sensor.v2"])])
    assert item.chosen == ("sensor.kia_soc",)
    assert _reasons(item) == [("sensor.v1", "several_soc_readings"), ("sensor.v2", "several_soc_readings")]
    assert vehicles([], []).status == "none"


# --- on a site ----------------------------------------------------------------------------------------------------


async def test_a_sites_list_is_in_its_diagnostics_the_bundle_and_the_command(hass: HomeAssistant, hass_ws_client) -> None:
    MockConfigEntry(domain="checkwatt", entry_id="CW").add_to_hass(hass)
    hass.states.async_set("weather.forecast_home", "sunny", {"temperature": 5})
    hass.states.async_set("weather.smhi", "cloudy", {"temperature": 4})
    owner = MockConfigEntry(domain="nibe_heatpump", entry_id="NIBE")
    owner.add_to_hass(hass)
    er.async_get(hass).async_get_or_create("sensor", "nibe_heatpump", "abc-40004", config_entry=owner)
    site, controller = await _site(hass)
    controller.battery_shadow.tick(controller.demand.now())

    detected = (await async_get_config_entry_diagnostics(hass, site))["detected"]

    items = {item["kind"]: item for item in detected["items"]}
    assert [item["kind"] for item in detected["items"]] == list(KINDS)
    assert items["battery"]["chosen"] == ["sensor.sigen_plant_battery_power"]
    assert items["battery"]["why"] == "adapter:sigenergy"
    assert items["battery"]["detail"]["management"] == "externally_controlled"
    assert items["battery"]["detail"]["coexistence"]["watch_only"] is True
    assert items["weather"]["chosen"] == ["weather.forecast_home"]
    assert items["weather"]["near_misses"] == [{"entity_id": "weather.smhi", "name": None, "reason": "not_the_default"}]
    assert items["outdoor_sensor"]["status"] in ("found", "none")
    assert items["chargers"]["status"] == "configured"

    bundle = await async_build_debug_bundle(hass)
    assert bundle["sites"][0]["detected"]["items"] == detected["items"]

    client = await admin(hass, hass_ws_client)
    reply = await ws_call(client, {"type": "spotnav/get_detected", "api_version": 1, "site_id": site.entry_id})
    assert reply["success"] and reply["result"]["items"] == detected["items"]
    refused = await ws_call(client, {"type": "spotnav/get_detected", "api_version": 2, "site_id": site.entry_id})
    assert not refused["success"]
    unknown = await ws_call(client, {"type": "spotnav/get_detected", "api_version": 1, "site_id": "nope"})
    assert unknown["error"]["code"] == "spotnav_unknown_site"
