"""Review round 3: a smart-plug charger's entity configuration, as the card's focused dialogs read it."""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.core import HomeAssistant

from .messages import get_message
from .test_smart_plug import _plug_charger
from .world import admin, ws_call

OUT = Path(__file__).parent / "fixtures" / "entity_config" / "v1" / "review3_smart_plug.json"


async def test_a_smart_plug_charger_reports_no_automatically_found_register(hass: HomeAssistant, hass_ws_client) -> None:
    entry, power = await _plug_charger(hass)
    socket = await admin(hass, hass_ws_client)
    answer = (await ws_call(socket, get_message(entry.entry_id)))["result"]
    OUT.write_text(json.dumps(answer, indent=1))
    fields = {field["field"]: field for field in answer["config"]["fields"]}
    assert fields["power_entity"]["current"]["entity_id"] == power
    register = fields["energy_register_entity"]
    # SpotNav's own integrated-energy sensor stands in for the register: the card must not read it as a meter
    # found automatically (it then clears the plug's power entity on any Save of the charger's dialog).
    assert (register.get("effective") or {}).get("source") != "automatic", register.get("effective")
