"""Every field a config-flow form shows has a label in each shipped language."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_ENERGY_REGISTER_ENTITY
from custom_components.spotnav.flows import SpotNavChargingConfigFlow

TRANSLATIONS = Path("custom_components/spotnav/translations")


@pytest.mark.parametrize("language", ["en", "sv"])
async def test_the_generic_charger_form_labels_every_field(hass: HomeAssistant, language: str) -> None:
    flow = SpotNavChargingConfigFlow()
    flow.hass = hass
    result = await flow.async_step_generic()
    fields = {str(key) for key in result["data_schema"].schema}
    step = json.loads((TRANSLATIONS / f"{language}.json").read_text())["config"]["step"]["generic"]

    assert fields <= set(step["data"])
    assert CONF_ENERGY_REGISTER_ENTITY in step["data_description"]
