"""The OCPP entities step answers itself for the common case and never guesses a write path."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
)

from pytest_homeassistant_custom_component.common import async_mock_service

from .helpers import make_ocpp_config_entry
from .world import ocpp_entity

AMPERE = {"unit_of_measurement": "A", "min": 6, "max": 16}


def _connector(
    hass: HomeAssistant, cpid: str = "halo_charger", *, session_number: bool = True
) -> tuple[str, str]:
    owner = make_ocpp_config_entry(hass, entry_id="owner")
    devices = dr.async_get(hass)
    station = devices.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("ocpp", cpid)}, name=cpid,
    )
    connector = devices.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("ocpp", f"{cpid}-1")},
        name=f"{cpid} Connector 1",
        via_device_id=station.id,
    )
    shared = {"owner": owner, "device_id": connector.id, "cpid": cpid, "connector": 1}
    switch = ocpp_entity(hass, **shared, domain="switch", key="charge_control", state="off")
    if session_number:
        ocpp_entity(
            hass, **shared, domain="number", key="session_current_limit", state="16", attributes=AMPERE
        )
    register = ocpp_entity(
        hass, **shared, domain="sensor", key="energy_active_import_register", state="1",
        attributes={"device_class": "energy", "unit_of_measurement": "kWh"},
    )
    assert switch == f"switch.{cpid}_connector_1_charge_control"
    return connector.id, register


def _probe_answers(hass: HomeAssistant, value: str = "1.16") -> None:
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": value})


async def _step(hass: HomeAssistant, device_id: str) -> dict[str, Any]:
    return await _automatic(hass, device_id)


async def test_a_connector_whose_charge_point_answers_the_probe_is_created_from_defaults_alone(hass: HomeAssistant) -> None:
    device_id, register = _connector(hass)
    _probe_answers(hass)

    result = await _step(hass, device_id)
    assert result["step_id"] == "ocpp_confirm"
    # The form behind "adjust" prefills the meter and defaults the current the same way.
    form = await hass.config_entries.flow.async_configure(result["flow_id"], {"adjust": True})
    assert form["step_id"] == "ocpp_entities"
    suggested = {
        str(key): (key.description or {}).get("suggested_value") for key in form["data_schema"].schema
    }
    assert suggested[CONF_ENERGY_REGISTER_ENTITY] == register

    result = await hass.config_entries.flow.async_configure(form["flow_id"], {})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHARGE_CONTROL] == "switch.halo_charger_connector_1_charge_control"
    assert result["data"][CONF_CURRENT_LIMIT] == ""
    assert result["data"][CONF_CURRENT_CONTROL] == "change_configuration"
    assert result["data"][CONF_ENERGY_REGISTER_ENTITY] == ""


async def test_a_not_supported_answer_with_a_session_number_uses_the_number_path(
    hass: HomeAssistant,
) -> None:
    device_id, _ = _connector(hass)
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": None})

    result = await _step(hass, device_id)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CURRENT_CONTROL] == "number"
    assert result["data"][CONF_CURRENT_LIMIT] == "number.halo_charger_connector_1_session_current_limit"


async def test_a_not_supported_answer_without_a_number_shows_the_form(hass: HomeAssistant) -> None:
    device_id, _ = _connector(hass, "acme_charger", session_number=False)
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": None})

    result = await _step(hass, device_id)

    assert result["step_id"] == "ocpp_entities"
    defaults = {str(k): k.default() for k in result["data_schema"].schema if callable(k.default)}
    assert defaults.get(CONF_CURRENT_CONTROL) == ""


async def test_a_probe_that_times_out_is_unreachable_and_never_picks_the_number(
    hass: HomeAssistant, monkeypatch
) -> None:
    async def slow(call):
        await asyncio.sleep(5)
        return {"value": "1.16"}

    hass.services.async_register("ocpp", "get_configuration", slow, supports_response=SupportsResponse.ONLY)
    monkeypatch.setattr("custom_components.spotnav.flows.flow.PROBE_TIMEOUT_S", 0.05)
    device_id, _ = _connector(hass)

    result = await _step(hass, device_id)

    assert result["step_id"] == "ocpp_entities"
    assert result["errors"] == {"base": "charger_no_answer"}


async def test_a_raising_probe_is_unreachable_then_reprobes_after_the_charger_connects(
    hass: HomeAssistant,
) -> None:
    from homeassistant.exceptions import HomeAssistantError

    async def down(call):
        raise HomeAssistantError("charge point not connected")

    hass.services.async_register(
        "ocpp", "get_configuration", down, supports_response=SupportsResponse.ONLY
    )
    device_id, _ = _connector(hass)  # has a session number that must NOT be picked

    result = await _automatic(hass, device_id)
    assert result["step_id"] == "ocpp_entities"
    assert result["errors"] == {"base": "charger_no_answer"}
    defaults = {str(k): k.default() for k in result["data_schema"].schema if callable(k.default)}
    assert defaults.get(CONF_CURRENT_CONTROL) == ""
    assert CONF_CURRENT_LIMIT not in defaults

    hass.services.async_remove("ocpp", "get_configuration")
    _probe_answers(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["step_id"] == "ocpp_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CURRENT_CONTROL] == "change_configuration"
    assert result["data"][CONF_CURRENT_LIMIT] == ""
    assert result["title"] == "halo_charger Connector 1"


async def test_a_probe_answer_without_this_connector_is_unsupported(hass: HomeAssistant) -> None:
    device_id, _ = _connector(hass, session_number=False)
    _probe_answers(hass, "2.16")

    result = await _step(hass, device_id)

    assert result["step_id"] == "ocpp_entities"


def test_the_flow_names_no_vendor() -> None:
    import pathlib

    for path in pathlib.Path("custom_components/spotnav/flows").glob("*.py"):
        assert "charge amps" not in path.read_text().lower(), path


async def _automatic(hass: HomeAssistant, device_id: str) -> dict[str, Any]:
    from custom_components.spotnav.const import MODE_DETECTED

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_MODE: MODE_DETECTED})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"device": device_id})


async def test_automatic_flow_confirms_an_unambiguous_connector(hass: HomeAssistant) -> None:
    device_id, _ = _connector(hass)
    _probe_answers(hass)

    result = await _automatic(hass, device_id)
    assert result["step_id"] == "ocpp_confirm"
    assert result["description_placeholders"]["energy_meter"] != "none found"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHARGE_CONTROL] == "switch.halo_charger_connector_1_charge_control"
    assert result["data"][CONF_CURRENT_LIMIT] == ""
    assert result["data"][CONF_CURRENT_CONTROL] == "change_configuration"
    assert result["data"][CONF_ENERGY_REGISTER_ENTITY] == ""


async def test_adjusting_from_the_confirmation_shows_the_form(hass: HomeAssistant) -> None:
    device_id, _ = _connector(hass)
    _probe_answers(hass)

    result = await _automatic(hass, device_id)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"adjust": True})

    assert result["step_id"] == "ocpp_entities"


async def test_automatic_flow_with_a_number_only_confirms_the_number_path(hass: HomeAssistant) -> None:
    device_id, _ = _connector(hass)
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": None})

    result = await _automatic(hass, device_id)
    assert result["step_id"] == "ocpp_confirm"
    assert "current number" in result["description_placeholders"]["current"]
    summary = result["description_placeholders"]["summary"]
    assert [line[:3] for line in summary.splitlines()] == ["- *"] * 3
    assert "**Current set via:** the charger's current number (" in summary


async def test_automatic_flow_asks_when_the_current_default_is_not_determined(hass: HomeAssistant) -> None:
    device_id, _ = _connector(hass, "acme_charger", session_number=False)

    result = await _automatic(hass, device_id)

    assert result["step_id"] == "ocpp_entities"
