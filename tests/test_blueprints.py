"""The automation blueprints SpotNav ships (`custom_components/spotnav/blueprints/automation/`): valid for Home Assistant's blueprint schema, and
doing what they say with sample states, run as real automations."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint.models import Blueprint
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

BLUEPRINTS = Path(__file__).resolve().parent.parent / "custom_components" / "spotnav" / "blueprints" / "automation"
NAMES = ("price_setpoint.yaml", "solar_surplus_switch.yaml")

CHEAP = "binary_sensor.electricity_price_se4_cheap_now"
EXPENSIVE = "binary_sensor.electricity_price_se4_expensive_now"
PRICE = "sensor.electricity_price_se4_price_now"
ALARM = "alarm_control_panel.house"
SURPLUS = "sensor.home_solar_surplus"
POOL = "switch.pool_heater"
ALLOW = "input_boolean.pool_allowed"


@pytest.fixture(autouse=True)
async def automation_stopped_after(hass: HomeAssistant):
    """The automation's time triggers are detached at the end, so no timer outlives the test."""
    yield
    if hass.services.has_service("automation", "turn_off"):
        await hass.services.async_call("automation", "turn_off", {"entity_id": "all"}, blocking=True)
        await hass.async_block_till_done()


def test_the_folder_holds_exactly_the_listed_blueprints() -> None:
    assert sorted(path.name for path in BLUEPRINTS.iterdir()) == sorted(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_each_blueprint_passes_home_assistants_schema(name: str) -> None:
    blueprint = Blueprint(
        load_yaml_dict(str(BLUEPRINTS / name)),
        path=name,
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    assert blueprint.domain == "automation"
    assert blueprint.name.startswith("SpotNav: ")
    assert blueprint.metadata["source_url"] == (
        f"https://raw.githubusercontent.com/henrikekblad/spotnav-home-assistant/main/custom_components/spotnav/blueprints/automation/{name}"
    )
    # The first lines say SpotNav updates the file while it is unchanged, and that an edited copy is kept.
    first, second = (BLUEPRINTS / name).read_text().splitlines()[:2]
    assert first.startswith("# Installed by SpotNav") and "unchanged" in first
    assert second.startswith("# A copy you edit is kept")


async def _automation(hass: HomeAssistant, tmp_path: Path, name: str, inputs: dict[str, Any]) -> None:
    """Set up one automation from a blueprint, read from a config folder of its own."""
    hass.config.config_dir = str(tmp_path)
    folder = tmp_path / "blueprints" / "automation" / "spotnav"
    folder.mkdir(parents=True)
    shutil.copy(BLUEPRINTS / name, folder / name)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "under_test",
                "alias": "under test",
                "use_blueprint": {"path": f"spotnav/{name}", "input": inputs},
            }
        },
    )
    await hass.async_block_till_done()
    # A blueprint or input that fails validation leaves no automation, or an unavailable one.
    assert hass.states.get("automation.under_test") is not None
    assert hass.states.get("automation.under_test").state == "on"


# ------------------------------------------------------------------------------------------------ setpoint


def _price_states(hass: HomeAssistant, *, cheap: str = "off", expensive: str = "off", price: str = "0.8") -> None:
    hass.states.async_set(CHEAP, cheap)
    hass.states.async_set(EXPENSIVE, expensive)
    hass.states.async_set(PRICE, price, {"unit_of_measurement": "SEK/kWh"})


@pytest.fixture
async def setpoint(hass: HomeAssistant, tmp_path: Path, freezer):
    # A fixed clock from before the automation is set up, so its interval trigger is due at a known time.
    freezer.move_to("2026-11-01 10:05:00+00:00")
    _price_states(hass)
    hass.states.async_set("climate.hall", "heat", {"temperature": 19})
    hass.states.async_set("climate.bath", "heat", {"temperature": 19})
    hass.states.async_set(ALARM, "disarmed")
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _automation(
        hass,
        tmp_path,
        "price_setpoint.yaml",
        {
            "thermostats": ["climate.hall", "climate.bath"],
            "cheap_now": CHEAP,
            "expensive_now": EXPENSIVE,
            "price_now": PRICE,
            "max_price": 1.5,
            "away_entity": ALARM,
        },
    )
    return calls


def _set_to(calls) -> dict[str, float]:
    """Each thermostat's last written setpoint."""
    return {call.data["entity_id"][0]: call.data["temperature"] for call in calls}


async def test_setpoint_follows_cheap_and_expensive(hass: HomeAssistant, setpoint) -> None:
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert _set_to(setpoint) == {"climate.hall": 21.0, "climate.bath": 21.0}

    setpoint.clear()
    hass.states.async_set(CHEAP, "off")
    hass.states.async_set(EXPENSIVE, "on")
    await hass.async_block_till_done()
    assert _set_to(setpoint) == {"climate.hall": 17.0, "climate.bath": 17.0}


async def test_setpoint_writes_only_where_it_differs(hass: HomeAssistant, setpoint) -> None:
    hass.states.async_set("climate.hall", "heat", {"temperature": 21})
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert _set_to(setpoint) == {"climate.bath": 21.0}

    # Back to normal: 19, which the bath (still at 19 in its state) already has.
    setpoint.clear()
    hass.states.async_set(CHEAP, "off")
    await hass.async_block_till_done()
    assert _set_to(setpoint) == {"climate.hall": 19.0}


async def test_setpoint_skips_an_unavailable_thermostat(hass: HomeAssistant, setpoint) -> None:
    hass.states.async_set("climate.bath", "unavailable")
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert _set_to(setpoint) == {"climate.hall": 21.0}


async def test_setpoint_max_price_wins_over_cheap(hass: HomeAssistant, setpoint) -> None:
    hass.states.async_set(PRICE, "1.62", {"unit_of_measurement": "SEK/kWh"})
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert set(_set_to(setpoint).values()) == {17.0}


async def test_setpoint_away_wins_and_leaving_away_restores(hass: HomeAssistant, setpoint) -> None:
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    hass.states.async_set("climate.hall", "heat", {"temperature": 21})
    hass.states.async_set("climate.bath", "heat", {"temperature": 21})
    setpoint.clear()

    hass.states.async_set(ALARM, "armed_away")
    await hass.async_block_till_done()
    assert _set_to(setpoint) == {"climate.hall": 17.0, "climate.bath": 17.0}

    hass.states.async_set("climate.hall", "heat", {"temperature": 17})
    hass.states.async_set("climate.bath", "heat", {"temperature": 17})
    setpoint.clear()
    hass.states.async_set(ALARM, "disarmed")
    await hass.async_block_till_done()
    assert _set_to(setpoint) == {"climate.hall": 21.0, "climate.bath": 21.0}


async def test_setpoint_runs_on_its_interval(hass: HomeAssistant, setpoint, freezer) -> None:
    hass.states.async_set("climate.hall", "heat", {"temperature": 23})
    await hass.async_block_till_done()
    assert setpoint == []
    now = dt_util.now()
    next_quarter = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0) + timedelta(minutes=15)
    freezer.move_to(next_quarter)
    async_fire_time_changed(hass, next_quarter)
    await hass.async_block_till_done()
    assert _set_to(setpoint) == {"climate.hall": 19.0}


async def test_setpoint_without_the_optional_inputs(hass: HomeAssistant, tmp_path: Path) -> None:
    _price_states(hass, expensive="on", price="9")
    hass.states.async_set("climate.hall", "heat", {"temperature": 19})
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _automation(
        hass,
        tmp_path,
        "price_setpoint.yaml",
        {"thermostats": ["climate.hall"], "cheap_now": CHEAP, "expensive_now": EXPENSIVE, "price_now": PRICE},
    )
    hass.states.async_set(EXPENSIVE, "off")
    await hass.async_block_till_done()
    # Max price 0 is off, so a price of 9 does not matter: normal.
    assert calls == []
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert _set_to(calls) == {"climate.hall": 21.0}


# ------------------------------------------------------------------------------------------------ surplus


def _surplus(hass: HomeAssistant, watts: float | str) -> None:
    hass.states.async_set(SURPLUS, str(watts), {"unit_of_measurement": "W", "device_class": "power"})


async def _surplus_automation(hass: HomeAssistant, tmp_path: Path, target: str, **extra: Any):
    on = async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    climate_on = async_mock_service(hass, "climate", "turn_on")
    climate_off = async_mock_service(hass, "climate", "turn_off")
    await _automation(
        hass,
        tmp_path,
        "solar_surplus_switch.yaml",
        {"surplus_sensor": SURPLUS, "target": target, "on_threshold": 3, "off_threshold": 0, **extra},
    )
    return on, off, climate_on, climate_off


async def test_surplus_switches_on_and_off_with_hysteresis(hass: HomeAssistant, tmp_path: Path, freezer) -> None:
    freezer.move_to(dt_util.now().replace(hour=12, minute=1))
    _surplus(hass, 0)
    hass.states.async_set(POOL, "off")
    freezer.tick(timedelta(minutes=30))
    on, off, _, _ = await _surplus_automation(hass, tmp_path, POOL)

    _surplus(hass, 2500)
    await hass.async_block_till_done()
    assert on == []
    _surplus(hass, 3200)
    await hass.async_block_till_done()
    assert [call.data["entity_id"] for call in on] == [[POOL]]

    # On now; a surplus between the thresholds keeps it on, and none switches it off after the minimum on time.
    hass.states.async_set(POOL, "on")
    freezer.tick(timedelta(minutes=2))
    _surplus(hass, 0)
    await hass.async_block_till_done()
    assert off == []
    freezer.tick(timedelta(minutes=10))
    _surplus(hass, 400)
    await hass.async_block_till_done()
    assert off == []
    _surplus(hass, 0)
    await hass.async_block_till_done()
    # The mocked service leaves the switch on, so a 5-minute run that falls due here may ask again.
    assert off and all(call.data["entity_id"] == [POOL] for call in off)


async def test_surplus_waits_the_minimum_off_time(hass: HomeAssistant, tmp_path: Path, freezer) -> None:
    freezer.move_to(dt_util.now().replace(hour=12, minute=1))
    _surplus(hass, 0)
    hass.states.async_set(POOL, "off")
    on, _, _, _ = await _surplus_automation(hass, tmp_path, POOL, min_off_minutes=5)
    freezer.tick(timedelta(minutes=1))
    _surplus(hass, 4000)
    await hass.async_block_till_done()
    assert on == []
    freezer.tick(timedelta(minutes=5))
    _surplus(hass, 4100)
    await hass.async_block_till_done()
    assert len(on) == 1


async def test_surplus_unknown_does_nothing(hass: HomeAssistant, tmp_path: Path, freezer) -> None:
    _surplus(hass, 0)
    hass.states.async_set(POOL, "on")
    freezer.tick(timedelta(minutes=30))
    _, off, _, _ = await _surplus_automation(hass, tmp_path, POOL)
    _surplus(hass, "unknown")
    await hass.async_block_till_done()
    assert off == []


async def test_surplus_respects_the_allowed_entity(hass: HomeAssistant, tmp_path: Path, freezer) -> None:
    _surplus(hass, 0)
    hass.states.async_set(POOL, "off")
    hass.states.async_set(ALLOW, "off")
    freezer.tick(timedelta(minutes=30))
    on, _, _, _ = await _surplus_automation(hass, tmp_path, POOL, allowed_entity=ALLOW)
    _surplus(hass, 5000)
    await hass.async_block_till_done()
    assert on == []
    hass.states.async_set(ALLOW, "on")
    _surplus(hass, 5100)
    await hass.async_block_till_done()
    assert len(on) == 1


async def test_surplus_window(hass: HomeAssistant, tmp_path: Path, freezer) -> None:
    local = dt_util.now().replace(hour=8, minute=0, second=0, microsecond=0)
    freezer.move_to(local)
    _surplus(hass, 0)
    hass.states.async_set(POOL, "off")
    on, off, _, _ = await _surplus_automation(
        hass, tmp_path, POOL, window_start="09:00:00", window_end="16:00:00", min_off_minutes=0, min_on_minutes=0
    )
    _surplus(hass, 5000)
    await hass.async_block_till_done()
    assert on == []

    freezer.move_to(local.replace(hour=9, minute=1))
    _surplus(hass, 5100)
    await hass.async_block_till_done()
    assert len(on) == 1

    # Still a surplus at the window's end: switched off then, and not on again after it.
    hass.states.async_set(POOL, "on")
    end = local.replace(hour=16)
    freezer.move_to(end)
    async_fire_time_changed(hass, end)
    await hass.async_block_till_done()
    assert len(off) == 1
    hass.states.async_set(POOL, "off")
    _surplus(hass, 5200)
    await hass.async_block_till_done()
    assert len(on) == 1


async def test_surplus_turns_a_climate_entity_on(hass: HomeAssistant, tmp_path: Path, freezer) -> None:
    _surplus(hass, 0)
    hass.states.async_set("climate.pool", "off", {"temperature": 28})
    freezer.tick(timedelta(minutes=30))
    on, _, climate_on, _ = await _surplus_automation(hass, tmp_path, "climate.pool")
    _surplus(hass, 3500)
    await hass.async_block_till_done()
    assert on == []
    assert len(climate_on) == 1

