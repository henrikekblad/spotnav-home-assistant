"""The fake spa and sauna in `tools/fake_devices/` load as integrations and SpotNav's detection finds them as a
controllable Balboa spa and a Saunum sauna with its door (`spa/detect.py`, `sauna/detect.py`)."""

from __future__ import annotations

import shutil
from datetime import timedelta
import sys
from pathlib import Path

import pytest
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

import custom_components

FAKES = Path(__file__).resolve().parent.parent / "tools" / "fake_devices"
DOMAINS = ("balboa", "saunum")


@pytest.fixture
def fake_devices(tmp_path, monkeypatch):
    """The fakes copied as an installation has them (manifest.json), on `custom_components`' path."""
    for domain in DOMAINS:
        target = tmp_path / domain
        shutil.copytree(FAKES / domain, target)
        (target / "manifest.demo.json").rename(target / "manifest.json")
    monkeypatch.setattr(custom_components, "__path__", [*custom_components.__path__, str(tmp_path)])
    yield
    for name in [name for name in sys.modules if name.split(".")[:2] in (["custom_components", d] for d in DOMAINS)]:
        del sys.modules[name]


async def _add(hass: HomeAssistant, domain: str) -> None:
    result = await hass.config_entries.flow.async_init(domain, context={"source": "user"})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()


async def test_the_fakes_are_found_as_a_balboa_spa_and_a_saunum_sauna(hass: HomeAssistant, fake_devices) -> None:
    from custom_components.spotnav.api.heat import pool_spa_choices
    from custom_components.spotnav.heat.config import HeatConfig

    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    assert await async_setup_component(hass, "homeassistant", {})
    for domain in DOMAINS:
        await _add(hass, domain)

    choices = pool_spa_choices(hass, HeatConfig())
    spa = {row["entity_id"]: row for row in choices["spa"]}["climate.balboa_spa_fake"]
    assert (spa["family"], spa["score"], spa["controllable"], spa["cloud"]) == ("balboa", 1.0, True, False)
    sauna = {row["entity_id"]: row for row in choices["sauna"]}["climate.saunum_leil_fake"]
    assert (sauna["family"], sauna["score"], sauna["lock_id"]) == ("saunum", 1.0, "binary_sensor.saunum_leil_fake_door")


async def test_the_fakes_take_writes_at_once(hass: HomeAssistant, fake_devices) -> None:
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    for domain in DOMAINS:
        await _add(hass, domain)

    # The models' ticks run on the event loop and redraw the states.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1))
    await hass.async_block_till_done()
    spa = hass.states.get("climate.balboa_spa_fake")
    assert (spa.state, spa.attributes["preset_mode"], spa.attributes["current_temperature"]) == ("heat", "ready", 30.0)
    assert spa.attributes["hvac_action"] == "heating"
    await hass.services.async_call("climate", "set_preset_mode", {"entity_id": spa.entity_id, "preset_mode": "rest"}, blocking=True)
    spa = hass.states.get(spa.entity_id)
    assert (spa.state, spa.attributes["preset_mode"], spa.attributes["hvac_action"]) == ("off", "rest", "idle")

    sauna = "climate.saunum_leil_fake"
    assert hass.states.get(sauna).state == "off"
    await hass.services.async_call("climate", "set_temperature", {"entity_id": sauna, "temperature": 85}, blocking=True)
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": sauna, "hvac_mode": "heat"}, blocking=True)
    state = hass.states.get(sauna)
    assert (state.state, state.attributes["temperature"], state.attributes["hvac_action"]) == ("heat", 85, "heating")
    # Opening the fake door ends the session and refuses a new one.
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.saunum_leil_fake_door_open_fake_control"}, blocking=True)
    assert hass.states.get(sauna).state == "off"
    assert hass.states.get("binary_sensor.saunum_leil_fake_door").state == "on"
    with pytest.raises(Exception, match="door is open"):
        await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": sauna, "hvac_mode": "heat"}, blocking=True)
