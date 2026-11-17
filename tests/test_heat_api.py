"""The heat card's commands end to end on the pilot's site: `spotnav/update_heat`'s compare-and-set, its judge, the
administrator rule, the meter setting it shares with the house card, and the diagnostics block."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.api.heat import websocket_get_heat, websocket_update_heat
from custom_components.spotnav.const import CONF_HEAT, CONF_HEAT_METER_ENTITY
from custom_components.spotnav.diagnostics import _site_diagnostics
from custom_components.spotnav.heat.view import HEAT_KEYS

from .test_demand_site import pilot_site

pytestmark = pytest.mark.usefixtures("offline_relay")


class _Connection:
    def __init__(self, admin: bool = True) -> None:
        self.answers: list[dict[str, Any]] = []
        self.user = SimpleNamespace(is_admin=admin)

    def send_result(self, message_id: int, result: Any) -> None:
        self.answers.append({"id": message_id, "success": True, "result": json.loads(json.dumps(result))})

    def send_error(self, message_id: int, code: str, message: str) -> None:
        self.answers.append({"id": message_id, "success": False, "error": {"code": code, "message": message}})


async def update(hass: HomeAssistant, admin: bool = True, **request: Any) -> dict[str, Any]:
    connection = _Connection(admin)
    websocket_update_heat(hass, connection, {"id": 1, "type": "spotnav/update_heat", **request})  # type: ignore[arg-type]
    await hass.async_block_till_done()
    (answer,) = connection.answers
    return answer


def _sensors(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.kok", "21.3", {"device_class": "temperature", "unit_of_measurement": "°C"})
    hass.states.async_set("sensor.ovanvaning", "20.5", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.cpu_load", "12", {"unit_of_measurement": "%"})
    hass.states.async_set("climate.golvvarme", "heat", {"current_temperature": 20.4, "temperature": 17})
    hass.states.async_set("switch.larm_rele_1", "off")
    hass.states.async_set("sensor.varme_effekt", "1128", {"device_class": "power", "unit_of_measurement": "W"})


async def test_a_write_is_stored_without_a_reload_and_answered_reread(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer)
    _sensors(hass)
    answer = await update(
        hass,
        api_version=1,
        site_id=site.entry_id,
        changes={
            "target_c": 21.5,
            "indoor_sensors": ["sensor.kok", "sensor.ovanvaning"],
            "zones": [{"entity_id": "climate.golvvarme", "offset_c": -0.5}],
            "control_path": "relay",
            "control_entity": "switch.larm_rele_1",
            "heat_meter_entity": "sensor.varme_effekt",
        },
        expected={"target_c": 21.0, "heat_meter_entity": None},
    )
    result = answer["result"]
    assert (result["ok"], result["error"], result["field_errors"]) == (True, None, [])
    config = result["config"]
    assert config["target_c"] == 21.5 and config["indoor_sensors"] == ["sensor.kok", "sensor.ovanvaning"]
    assert config["zones"] == [{"entity_id": "climate.golvvarme", "offset_c": -0.5}]
    assert config["heat_meter_entity"] == "sensor.varme_effekt"
    assert site.data[CONF_HEAT]["control_entity"] == "switch.larm_rele_1"
    assert site.data[CONF_HEAT_METER_ENTITY] == "sensor.varme_effekt"
    # By a charger in the site, the same settings; a stale `expected` writes nothing.
    stale = await update(hass, api_version=1, charger_id="entry_a", changes={"target_c": 22.0}, expected={"target_c": 21.0})
    assert stale["result"]["error"] == "spotnav_conflict" and stale["result"]["config"]["target_c"] == 21.5
    cleared = await update(hass, api_version=1, site_id=site.entry_id, changes={"heat_meter_entity": ""})
    assert cleared["result"]["ok"] and CONF_HEAT_METER_ENTITY not in site.data

    diagnostics = _site_diagnostics(hass, site)["heat_settings"]
    assert diagnostics["read"]["target_c"] == 21.5 and diagnostics["stored"]["target_c"] == 21.5


async def test_field_errors_and_refusals(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer)
    _sensors(hass)
    bad = await update(
        hass,
        api_version=1,
        site_id=site.entry_id,
        changes={"indoor_sensors": ["sensor.cpu_load"], "heat_meter_entity": "sensor.kok", "target_c": 30},
    )
    assert bad["result"]["error"] == "spotnav_invalid_value"
    assert bad["result"]["field_errors"] == [
        {"field": "target_c", "code": "out_of_range"},
        {"field": "indoor_sensors", "code": "wrong_kind"},
        {"field": "heat_meter_entity", "code": "wrong_kind"},
    ]
    assert CONF_HEAT not in site.data
    for changes, code in (({}, "invalid_changes"), ({"colour": 1}, "unknown_field")):
        answer = await update(hass, api_version=1, site_id=site.entry_id, changes=changes)
        assert answer["result"]["field_errors"][0]["code"] == code
    # Someone who is not an administrator may turn the dial and go away, nothing else.
    user = await update(hass, False, api_version=1, site_id=site.entry_id, changes={"target_c": 20.0})
    assert user["result"]["ok"]
    until = (dt_util.utcnow() + timedelta(days=2)).isoformat()
    away = await update(hass, False, api_version=1, site_id=site.entry_id, changes={"mode": "away", "away_until": until})
    assert away["result"]["config"]["mode"] == "away"
    refused = await update(hass, False, api_version=1, site_id=site.entry_id, changes={"indoor_sensors": ["sensor.kok"]})
    assert refused["result"]["error"] == "spotnav_not_admin"
    for request, code in (
        ({"site_id": site.entry_id}, "spotnav_unsupported_api_version"),
        ({"api_version": 1}, "spotnav_charger_required"),
        ({"api_version": 1, "site_id": "nope"}, "spotnav_unknown_site"),
    ):
        answer = await update(hass, **request, changes={"target_c": 20.0})
        assert not answer["success"] and answer["error"]["code"] == code


async def read(hass: HomeAssistant, admin: bool = True, **request: Any) -> dict[str, Any]:
    connection = _Connection(admin)
    websocket_get_heat(hass, connection, {"id": 1, "type": "spotnav/get_heat", **request})  # type: ignore[arg-type]
    await hass.async_block_till_done()
    (answer,) = connection.answers
    return answer


async def test_the_read_on_the_pilot_site(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer)
    _sensors(hass)
    hass.states.async_set(
        "climate.luft", "heat", {"current_temperature": 21.5, "temperature": 21, "fan_modes": ["auto"], "hvac_action": "heating"}
    )
    written = await update(
        hass,
        api_version=1,
        site_id=site.entry_id,
        changes={
            "indoor_sensors": ["sensor.kok", "sensor.ovanvaning"],
            "zones": [{"entity_id": "climate.golvvarme"}, {"entity_id": "climate.luft", "offset_c": -1.0}],
        },
    )
    assert written["result"]["ok"], written
    answer = await read(hass, api_version=1, charger_id="entry_a")
    assert answer["success"], answer
    result = answer["result"]
    assert tuple(result) == HEAT_KEYS and result["shadow"] is True and result["writable"] is True
    assert (result["indoor"]["value_c"], result["indoor"]["coldest"]) == (20.5, "sensor.ovanvaning")
    assert [(zone["kind"], zone["aim_c"]) for zone in result["zones"]] == [("floor", 21.0), ("air", 20.0)]
    assert result["zones"][1]["heating"] is True
    choices = result["choices"]
    assert [item["entity_id"] for item in choices["indoor"]][:2] == ["sensor.kok", "sensor.ovanvaning"]
    assert "sensor.cpu_load" not in [item["entity_id"] for item in choices["indoor"]]
    assert {item["entity_id"]: item["kind"] for item in choices["zones"]} == {"climate.golvvarme": "floor", "climate.luft": "air"}
    relays = [item["entity_id"] for item in choices["relay"]]
    assert "switch.larm_rele_1" in relays and not any(entity_id.startswith("switch.spotnav") for entity_id in relays)
    assert len(result["band"]["hours"]) == 24 and result["pump"] is None
    # No recorder here: the indoor curve is there, empty.
    assert result["indoor_day"]["points"] == [] and result["indoor_day"]["step_min"] == 15
    # Someone who is not an administrator reads the same without the choices.
    user = (await read(hass, False, api_version=1, site_id=site.entry_id))["result"]
    assert user["writable"] is False and user["choices"] is None
    diagnostics = _site_diagnostics(hass, site)["heat_settings"]
    assert diagnostics["shadow_log"], diagnostics
    for request, code in (({"site_id": site.entry_id}, "spotnav_unsupported_api_version"), ({"api_version": 1, "site_id": "x"}, "spotnav_unknown_site")):
        refused = await read(hass, **request)
        assert not refused["success"] and refused["error"]["code"] == code


async def test_the_indoor_curve_is_read_once_per_five_minute_row(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.spotnav.api import heat as heat_api

    site = await pilot_site(hass, freezer)
    _sensors(hass)
    await update(hass, api_version=1, site_id=site.entry_id, changes={"indoor_sensors": ["sensor.kok"]})
    reads: list[tuple[Any, ...]] = []

    async def means(_hass: HomeAssistant, entity_ids: tuple[str, ...], start: Any, end: Any, period: str) -> dict[str, Any]:
        reads.append((entity_ids, period))
        return {"sensor.kok": {start: 21.2, start + timedelta(minutes=5): 21.4}}

    monkeypatch.setattr(heat_api, "async_temperature_means", means)
    first = (await read(hass, api_version=1, site_id=site.entry_id))["result"]["indoor_day"]
    assert first["points"][0]["mean_c"] == 21.3 and reads == [(("sensor.kok",), "5minute")]
    await read(hass, api_version=1, site_id=site.entry_id)
    assert len(reads) == 1
    freezer.tick(timedelta(minutes=5))
    await read(hass, api_version=1, site_id=site.entry_id)
    assert len(reads) == 2


async def test_a_zone_or_meter_write_drops_the_kept_energy_reads(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """The zones and the meter decide what the flow and the pies count as heat: a write reads them again at once,
    a write of anything else keeps them."""
    from custom_components.spotnav.api import site_settings

    site = await pilot_site(hass, freezer)
    _sensors(hass)
    dropped: list[str] = []
    monkeypatch.setattr(site_settings, "_forget_energy_reads", lambda _hass, entry_id: dropped.append(entry_id))
    await update(hass, api_version=1, site_id=site.entry_id, changes={"target_c": 21.5})
    assert dropped == []
    await update(hass, api_version=1, site_id=site.entry_id, changes={"zones": [{"entity_id": "climate.golvvarme", "offset_c": 0.0}]})
    assert dropped == [site.entry_id]
    await update(hass, api_version=1, site_id=site.entry_id, changes={"heat_meter_entity": "sensor.varme_effekt"})
    assert dropped == [site.entry_id, site.entry_id]
