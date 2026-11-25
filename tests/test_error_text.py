"""A failed write's warning always says why (`util.error_text`): an exception without text shows its type, so a line
never ends in "failed: " with nothing after it."""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.spotnav.util import error_text

from .test_heat_zones_control import FLOOR, _running

pytestmark = pytest.mark.usefixtures("offline_relay")


def test_the_text_where_there_is_one_and_the_type_where_there_is_none() -> None:
    assert error_text(RuntimeError("Not connected")) == "Not connected"
    assert error_text(HomeAssistantError()) == "HomeAssistantError"
    assert error_text(TimeoutError()) == "TimeoutError"
    assert error_text(ValueError("  ")) == "ValueError"


async def test_a_zone_write_that_fails_without_text_names_the_error(hass: HomeAssistant, freezer, caplog) -> None:
    _entry, zones, _thermostat, now = await _running(hass, freezer, {0: "lower", 1: "lower"})

    async def refuse(_call: ServiceCall) -> None:
        raise HomeAssistantError

    hass.services.async_register("climate", "set_temperature", refuse)
    with caplog.at_level(logging.WARNING):
        await zones.async_look(now)
    lines = [record.getMessage() for record in caplog.records if FLOOR in record.getMessage() and "failed" in record.getMessage()]
    assert lines and all(line.endswith("failed: HomeAssistantError") for line in lines)
