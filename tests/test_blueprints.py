"""The automation blueprints SpotNav ships (`custom_components/spotnav/blueprints/automation/`): valid for Home Assistant's blueprint schema, and
doing what they say with sample states, run as real automations."""

from __future__ import annotations

import asyncio
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint.models import Blueprint
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, State
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service, mock_restore_cache

BLUEPRINTS = Path(__file__).resolve().parent.parent / "custom_components" / "spotnav" / "blueprints" / "automation"
NAMES = ("price_setpoint.yaml", "solar_surplus_switch.yaml", "relay_by_price.yaml")

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



# ------------------------------------------------------------------------------------------------ surplus and cheap


async def _cheap_automation(hass: HomeAssistant, tmp_path: Path, **extra: Any):
    return await _surplus_automation(hass, tmp_path, POOL, cheap_entity=CHEAP, **extra)


async def test_surplus_without_the_cheap_input_ignores_cheap(hass: HomeAssistant, tmp_path: Path, freezer) -> None:
    _surplus(hass, 0)
    hass.states.async_set(CHEAP, "on")
    hass.states.async_set(POOL, "off")
    freezer.tick(timedelta(minutes=30))
    on, _, _, _ = await _surplus_automation(hass, tmp_path, POOL)
    hass.states.async_set(CHEAP, "off")
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert on == []
    _surplus(hass, 3500)
    await hass.async_block_till_done()
    assert len(on) == 1


async def test_cheap_on_without_surplus_switches_on(hass: HomeAssistant, tmp_path: Path, freezer) -> None:
    # The clock first: states set before it would be newer than the frozen time when it lies before now, so the
    # minimum off time would never have passed.
    freezer.move_to(dt_util.now().replace(hour=12, minute=1))
    _surplus(hass, 0)
    hass.states.async_set(CHEAP, "off")
    hass.states.async_set(POOL, "off")
    freezer.tick(timedelta(minutes=30))
    on, _, _, _ = await _cheap_automation(hass, tmp_path)
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert [call.data["entity_id"] for call in on] == [[POOL]]


async def test_cheap_off_without_surplus_switches_off_after_minimum_on_time(
    hass: HomeAssistant, tmp_path: Path, freezer
) -> None:
    _surplus(hass, 0)
    hass.states.async_set(CHEAP, "on")
    hass.states.async_set(POOL, "on")
    freezer.tick(timedelta(minutes=1))
    _, off, _, _ = await _cheap_automation(hass, tmp_path, min_on_minutes=10)
    hass.states.async_set(CHEAP, "off")
    await hass.async_block_till_done()
    assert off == []
    freezer.tick(timedelta(minutes=10))
    _surplus(hass, 100)
    await hass.async_block_till_done()
    _surplus(hass, 0)
    await hass.async_block_till_done()
    assert off and all(call.data["entity_id"] == [POOL] for call in off)


async def test_cheap_keeps_it_on_when_the_surplus_is_gone(hass: HomeAssistant, tmp_path: Path, freezer) -> None:
    _surplus(hass, 0)
    hass.states.async_set(CHEAP, "on")
    hass.states.async_set(POOL, "on")
    freezer.tick(timedelta(minutes=30))
    _, off, _, _ = await _cheap_automation(hass, tmp_path)
    _surplus(hass, 0)
    await hass.async_block_till_done()
    _surplus(hass, 50)
    await hass.async_block_till_done()
    assert off == []


async def test_surplus_while_not_cheap_switches_on(hass: HomeAssistant, tmp_path: Path, freezer) -> None:
    _surplus(hass, 0)
    hass.states.async_set(CHEAP, "off")
    hass.states.async_set(POOL, "off")
    freezer.tick(timedelta(minutes=30))
    on, _, _, _ = await _cheap_automation(hass, tmp_path)
    _surplus(hass, 3500)
    await hass.async_block_till_done()
    assert len(on) == 1


async def test_cheap_outside_the_window_stays_off_and_window_end_switches_off(
    hass: HomeAssistant, tmp_path: Path, freezer
) -> None:
    local = dt_util.now().replace(hour=8, minute=0, second=0, microsecond=0)
    freezer.move_to(local)
    _surplus(hass, 0)
    hass.states.async_set(CHEAP, "off")
    hass.states.async_set(POOL, "off")
    on, off, _, _ = await _cheap_automation(
        hass, tmp_path, window_start="09:00:00", window_end="16:00:00", min_off_minutes=0, min_on_minutes=0
    )
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert on == []

    freezer.move_to(local.replace(hour=9, minute=1))
    hass.states.async_set(CHEAP, "off")
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert len(on) == 1

    hass.states.async_set(POOL, "on")
    end = local.replace(hour=16)
    freezer.move_to(end)
    async_fire_time_changed(hass, end)
    await hass.async_block_till_done()
    assert len(off) == 1


async def test_minimum_times_hold_across_a_switch_between_reasons(
    hass: HomeAssistant, tmp_path: Path, freezer
) -> None:
    _surplus(hass, 0)
    hass.states.async_set(CHEAP, "on")
    hass.states.async_set(POOL, "on")
    freezer.tick(timedelta(minutes=1))
    _, off, _, _ = await _cheap_automation(hass, tmp_path, min_on_minutes=10)
    # Cheap ends and the sun takes over at once: still on, nothing asked.
    _surplus(hass, 4000)
    hass.states.async_set(CHEAP, "off")
    await hass.async_block_till_done()
    assert off == []
    # The sun goes within the minimum on time: held.
    freezer.tick(timedelta(minutes=2))
    _surplus(hass, 0)
    await hass.async_block_till_done()
    assert off == []
    freezer.tick(timedelta(minutes=10))
    _surplus(hass, 10)
    await hass.async_block_till_done()
    _surplus(hass, 0)
    await hass.async_block_till_done()
    assert off

    # Off after the sun: cheap starts within the minimum off time, held until it has passed.
    on = async_mock_service(hass, "homeassistant", "turn_on")
    hass.states.async_set(POOL, "off")
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert on == []
    freezer.tick(timedelta(minutes=6))
    hass.states.async_set(CHEAP, "off")
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert len(on) == 1


# ------------------------------------------------------------------------------------------------ relay by price

RELAY = "switch.heat_pump_curve"
RELAY_B = "switch.heat_pump_sg_b"
OUTDOOR = "sensor.outdoor_temperature"


def relays_of(hass: HomeAssistant) -> "_Relays":
    return hass.data["test_relays"]


class _Relays:
    """switch.turn_on and switch.turn_off that set the switch's state, as a real relay's would, and keep each call
    in order as (service, entity)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.calls: list[tuple[str, str]] = []
        hass.data["test_relays"] = self
        for service in ("turn_on", "turn_off"):
            hass.services.async_register("switch", service, self._handler(hass, service))

    def _handler(self, hass: HomeAssistant, service: str):
        async def handle(call) -> None:
            for entity in call.data["entity_id"]:
                self.calls.append((service, entity))
                hass.states.async_set(entity, "on" if service == "turn_on" else "off")

        return handle

    def take(self) -> list[tuple[str, str]]:
        calls, self.calls = self.calls, []
        return calls


def _relay_prices(hass: HomeAssistant, *, cheap: str = "off", expensive: str = "off") -> None:
    hass.states.async_set(CHEAP, cheap)
    hass.states.async_set(EXPENSIVE, expensive)


async def _relay_automation(hass: HomeAssistant, tmp_path: Path, mode: str, **extra: Any) -> _Relays:
    relays = _Relays(hass)
    await _automation(
        hass,
        tmp_path,
        "relay_by_price.yaml",
        {"mode": mode, "relay": RELAY, "cheap_now": CHEAP, "expensive_now": EXPENSIVE, **extra},
    )
    return relays


async def _at(hass: HomeAssistant, freezer, when) -> None:
    """The clock moved to `when`, with every timer due by then fired."""
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


async def _cheap_on(hass: HomeAssistant, freezer, when) -> list[tuple[str, str]]:
    """Cheap now off and on at `when`, the clock then moved past a pulse; the relay calls made."""
    hass.states.async_set(CHEAP, "off")
    await _at(hass, freezer, when)
    hass.states.async_set(CHEAP, "on")
    # A pulse's delay keeps its run going, so wait for nothing but the pulse to start.
    for _ in range(20):
        await asyncio.sleep(0)
    await _at(hass, freezer, when + timedelta(seconds=61))
    return relays_of(hass).take()


@pytest.fixture
def morning(freezer):
    """10:02 local time on a fixed day, set before anything else so states are not newer than the clock."""
    start = dt_util.now().replace(year=2026, month=11, day=16, hour=10, minute=2, second=0, microsecond=0)
    freezer.move_to(start)
    return start


async def test_relay_closed_while_expensive(hass: HomeAssistant, tmp_path: Path, morning) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "off")
    relays = await _relay_automation(hass, tmp_path, "expensive_closed")
    assert relays.take() == []

    hass.states.async_set(EXPENSIVE, "on")
    await hass.async_block_till_done()
    assert relays.take() == [("turn_on", RELAY)]

    hass.states.async_set(EXPENSIVE, "off")
    await hass.async_block_till_done()
    assert relays.take() == [("turn_off", RELAY)]
    # Cheap does nothing in this mode.
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert relays.take() == []


async def test_relay_opens_after_the_longest_closed_time_and_rests(
    hass: HomeAssistant, tmp_path: Path, freezer, morning
) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "off")
    relays = await _relay_automation(hass, tmp_path, "expensive_closed", max_on_hours=3, min_rest_hours=1)
    hass.states.async_set(EXPENSIVE, "on")
    await hass.async_block_till_done()
    assert relays.take() == [("turn_on", RELAY)]

    # Still expensive after 2:59: closed. At 3 hours: opened.
    await _at(hass, freezer, morning + timedelta(hours=2, minutes=59))
    assert relays.take() == []
    await _at(hass, freezer, morning + timedelta(hours=3, seconds=1))
    assert relays.take() == [("turn_off", RELAY)]

    # Resting: neither the 5-minute run nor a price change closes it within the hour.
    await _at(hass, freezer, morning + timedelta(hours=3, minutes=30))
    hass.states.async_set(EXPENSIVE, "on", {"threshold": 2})
    await hass.async_block_till_done()
    await _at(hass, freezer, morning + timedelta(hours=3, minutes=59))
    assert relays.take() == []
    # The rest is over: closed again while it is still expensive.
    await _at(hass, freezer, morning + timedelta(hours=4, minutes=1))
    assert relays.take() == [("turn_on", RELAY)]


async def test_relay_rests_between_two_expensive_spells(hass: HomeAssistant, tmp_path: Path, freezer, morning) -> None:
    _relay_prices(hass, expensive="on")
    hass.states.async_set(RELAY, "off")
    relays = await _relay_automation(hass, tmp_path, "expensive_closed", min_rest_hours=1)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert relays.take() == [("turn_on", RELAY)]
    await _at(hass, freezer, morning + timedelta(minutes=30))
    hass.states.async_set(EXPENSIVE, "off")
    await hass.async_block_till_done()
    assert relays.take() == [("turn_off", RELAY)]
    await _at(hass, freezer, morning + timedelta(minutes=45))
    hass.states.async_set(EXPENSIVE, "on")
    await hass.async_block_till_done()
    assert relays.take() == []
    await _at(hass, freezer, morning + timedelta(minutes=91))
    assert relays.take() == [("turn_on", RELAY)]


async def test_relay_not_closed_below_the_lowest_outdoor_temperature(
    hass: HomeAssistant, tmp_path: Path, freezer, morning
) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "off")
    hass.states.async_set(OUTDOOR, "-18.5", {"unit_of_measurement": "°C", "device_class": "temperature"})
    relays = await _relay_automation(hass, tmp_path, "expensive_closed", outdoor_sensor=OUTDOOR, min_outdoor=-15)
    hass.states.async_set(EXPENSIVE, "on")
    await hass.async_block_till_done()
    assert relays.take() == []

    hass.states.async_set(OUTDOOR, "-9", {"unit_of_measurement": "°C", "device_class": "temperature"})
    await _at(hass, freezer, morning + timedelta(minutes=5))
    assert relays.take() == [("turn_on", RELAY)]

    # Colder while closed: opened at the next run.
    hass.states.async_set(OUTDOOR, "-16", {"unit_of_measurement": "°C", "device_class": "temperature"})
    await _at(hass, freezer, morning + timedelta(minutes=10))
    assert relays.take() == [("turn_off", RELAY)]

    # No reading: not closed either.
    hass.states.async_set(OUTDOOR, "unavailable")
    await _at(hass, freezer, morning + timedelta(hours=2))
    assert relays.take() == []


async def _expensive_until(hass: HomeAssistant, start, minutes: int | None) -> None:
    """Expensive now on, with a next_change `minutes` after `start` (none: no attribute)."""
    attributes = {} if minutes is None else {"next_change": (start + timedelta(minutes=minutes)).isoformat()}
    hass.states.async_set(EXPENSIVE, "on", attributes)
    await hass.async_block_till_done()


async def test_relay_a_single_expensive_quarter_does_not_close(
    hass: HomeAssistant, tmp_path: Path, freezer, morning
) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "off")
    relays = await _relay_automation(hass, tmp_path, "expensive_closed")
    await _expensive_until(hass, morning, 15)
    assert relays.take() == []
    await _at(hass, freezer, morning + timedelta(minutes=5))
    assert relays.take() == []
    hass.states.async_set(EXPENSIVE, "off")
    await hass.async_block_till_done()
    assert relays.take() == []


async def test_relay_a_long_expensive_stretch_closes_and_stays_closed_until_it_ends(
    hass: HomeAssistant, tmp_path: Path, freezer, morning
) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "off")
    relays = await _relay_automation(hass, tmp_path, "expensive_closed")
    await _expensive_until(hass, morning, 120)
    assert relays.take() == [("turn_on", RELAY)]
    # Less than the shortest closing is left: it stays closed.
    await _at(hass, freezer, morning + timedelta(minutes=100))
    assert relays.take() == []
    assert hass.states.get(RELAY).state == "on"
    hass.states.async_set(EXPENSIVE, "off")
    await hass.async_block_till_done()
    assert relays.take() == [("turn_off", RELAY)]


async def test_relay_without_next_change_closes_as_before(hass: HomeAssistant, tmp_path: Path, morning) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "off")
    relays = await _relay_automation(hass, tmp_path, "expensive_closed")
    await _expensive_until(hass, morning, None)
    assert relays.take() == [("turn_on", RELAY)]


async def test_relay_shortest_closing_zero_closes_for_a_single_quarter(
    hass: HomeAssistant, tmp_path: Path, morning
) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "off")
    relays = await _relay_automation(hass, tmp_path, "expensive_closed", min_closed_minutes=0)
    await _expensive_until(hass, morning, 15)
    assert relays.take() == [("turn_on", RELAY)]


async def test_relay_sg_ready_blocked_needs_the_shortest_closing(
    hass: HomeAssistant, tmp_path: Path, freezer, morning
) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "off")
    hass.states.async_set(RELAY_B, "off")
    relays = await _relay_automation(hass, tmp_path, "sg_ready", relay_b=RELAY_B)
    await _expensive_until(hass, morning, 15)
    assert relays.take() == []
    # A longer stretch known at the next run (an attribute change alone does not trigger it).
    await _expensive_until(hass, morning, 90)
    await _at(hass, freezer, morning + timedelta(minutes=5))
    assert relays.take() == [("turn_on", RELAY)]


async def test_relay_does_nothing_while_unavailable(hass: HomeAssistant, tmp_path: Path, morning) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "unavailable")
    relays = await _relay_automation(hass, tmp_path, "expensive_closed")
    hass.states.async_set(EXPENSIVE, "on")
    await hass.async_block_till_done()
    assert relays.take() == []


async def test_relay_start_opens_what_the_price_does_not_call_for(
    hass: HomeAssistant, tmp_path: Path, morning
) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "on")
    relays = await _relay_automation(hass, tmp_path, "expensive_closed")
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert relays.take() == [("turn_off", RELAY)]


async def test_relay_reload_closes_what_the_price_calls_for(hass: HomeAssistant, tmp_path: Path, morning) -> None:
    _relay_prices(hass, expensive="on")
    hass.states.async_set(RELAY, "off")
    relays = await _relay_automation(hass, tmp_path, "expensive_closed")
    hass.bus.async_fire("automation_reloaded")
    await hass.async_block_till_done()
    assert relays.take() == [("turn_on", RELAY)]


async def test_relay_counts_the_closed_time_over_a_restart(hass: HomeAssistant, tmp_path: Path, morning) -> None:
    # Closed by the automation 3 hours ago (kept over the restart); the relay's own state is new since the start.
    mock_restore_cache(
        hass,
        [State("automation.under_test", "on", {"last_triggered": (morning - timedelta(hours=3, minutes=1)).isoformat()})],
    )
    _relay_prices(hass, expensive="on")
    hass.states.async_set(RELAY, "on")
    relays = await _relay_automation(hass, tmp_path, "expensive_closed", max_on_hours=3)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert relays.take() == [("turn_off", RELAY)]


async def test_relay_pulses_once_a_day_when_cheap(hass: HomeAssistant, tmp_path: Path, freezer, morning) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "off")
    await _relay_automation(hass, tmp_path, "cheap_pulse", pulse_seconds=2)

    pulse = [("turn_on", RELAY), ("turn_off", RELAY)]
    assert await _cheap_on(hass, freezer, morning) == pulse
    # Cheap again later the same day, and the 5-minute runs while it lasts: no second pulse.
    assert await _cheap_on(hass, freezer, morning + timedelta(hours=5)) == []
    await _at(hass, freezer, morning + timedelta(hours=5, minutes=10))
    assert relays_of(hass).take() == []
    # The next day: one again.
    assert await _cheap_on(hass, freezer, morning + timedelta(hours=16)) == pulse


async def test_relay_pulses_per_day_and_the_shortest_time_between(
    hass: HomeAssistant, tmp_path: Path, freezer, morning
) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "off")
    await _relay_automation(hass, tmp_path, "cheap_pulse", pulse_seconds=1, pulses_per_day=2, min_interval_hours=3)

    pulse = [("turn_on", RELAY), ("turn_off", RELAY)]
    # 10:02: the first of the day's two halves.
    assert await _cheap_on(hass, freezer, morning) == pulse
    # 11:32: the same half.
    assert await _cheap_on(hass, freezer, morning + timedelta(hours=1.5)) == []
    # 12:32: the second half, but only 2.5 hours since the last pulse.
    assert await _cheap_on(hass, freezer, morning + timedelta(hours=2.5)) == []
    # 13:32: the second half, 3.5 hours on.
    assert await _cheap_on(hass, freezer, morning + timedelta(hours=3.5)) == pulse
    # 20:02: the second half has had its pulse.
    assert await _cheap_on(hass, freezer, morning + timedelta(hours=10)) == []


async def test_relay_pulse_mode_opens_a_relay_left_closed_at_the_start(
    hass: HomeAssistant, tmp_path: Path, morning
) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "on")
    relays = await _relay_automation(hass, tmp_path, "cheap_pulse")
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert relays.take() == [("turn_off", RELAY)]


async def test_relay_sg_ready_states(hass: HomeAssistant, tmp_path: Path, freezer, morning) -> None:
    # Both open since well before.
    freezer.move_to(morning - timedelta(hours=5))
    _relay_prices(hass)
    hass.states.async_set(RELAY, "off")
    hass.states.async_set(RELAY_B, "off")
    freezer.move_to(morning)
    relays = await _relay_automation(hass, tmp_path, "sg_ready", relay_b=RELAY_B, max_on_hours=2, min_rest_hours=1)

    # Cheap: recommended raise (A open, B closed).
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert relays.take() == [("turn_on", RELAY_B)]

    # Straight from cheap to expensive: B is opened before A is closed, never both closed.
    hass.states.async_set(CHEAP, "off")
    hass.states.async_set(EXPENSIVE, "on")
    await hass.async_block_till_done()
    assert relays.take() == [("turn_off", RELAY_B), ("turn_on", RELAY)]

    # Blocked for at most 2 hours, then normal for the rest, then blocked again.
    await _at(hass, freezer, morning + timedelta(hours=2, seconds=1))
    assert relays.take() == [("turn_off", RELAY)]
    await _at(hass, freezer, morning + timedelta(hours=2, minutes=55))
    assert relays.take() == []
    await _at(hass, freezer, morning + timedelta(hours=3, minutes=5))
    assert relays.take() == [("turn_on", RELAY)]

    # Expensive to cheap: A opened before B is closed.
    hass.states.async_set(EXPENSIVE, "off")
    hass.states.async_set(CHEAP, "on")
    await hass.async_block_till_done()
    assert relays.take() == [("turn_off", RELAY), ("turn_on", RELAY_B)]

    # Neither: normal, both open.
    hass.states.async_set(CHEAP, "off")
    await hass.async_block_till_done()
    assert relays.take() == [("turn_off", RELAY_B)]


async def test_relay_sg_ready_outdoor_floor_and_start(hass: HomeAssistant, tmp_path: Path, morning) -> None:
    _relay_prices(hass, expensive="on")
    hass.states.async_set(RELAY, "on")
    hass.states.async_set(RELAY_B, "on")
    hass.states.async_set(OUTDOOR, "-20", {"unit_of_measurement": "°C", "device_class": "temperature"})
    relays = await _relay_automation(hass, tmp_path, "sg_ready", relay_b=RELAY_B, outdoor_sensor=OUTDOOR)
    # Both closed (forced raise) at the start, expensive and too cold: both opened, normal.
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert sorted(relays.take()) == [("turn_off", RELAY), ("turn_off", RELAY_B)]


async def test_relay_sg_ready_without_relay_b_does_nothing(hass: HomeAssistant, tmp_path: Path, morning) -> None:
    _relay_prices(hass)
    hass.states.async_set(RELAY, "off")
    relays = await _relay_automation(hass, tmp_path, "sg_ready")
    hass.states.async_set(EXPENSIVE, "on")
    await hass.async_block_till_done()
    assert relays.take() == []
