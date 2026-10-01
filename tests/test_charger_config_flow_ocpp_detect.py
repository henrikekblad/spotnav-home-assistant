"""The automatic charger flow offers OCPP devices and reuses the OCPP path for them."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_OCPP_CONNECTOR_ID,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    MODE_DETECTED,
    MODE_OCPP,
)

from pytest_homeassistant_custom_component.common import async_mock_service

from .charger_shapes import register_shape, SHAPES
from .helpers import make_ocpp_config_entry
from .world import CPID, ocpp_entity, two_connector_charger

OWNER = "entry_ocpp_owner"


async def _to_type_step(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER}
    )
    assert result["step_id"] == "charger_type"
    return result["flow_id"]


async def _detected(hass: HomeAssistant, device_id: str | None) -> dict[str, Any]:
    flow_id = await _to_type_step(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_MODE: MODE_DETECTED})
    if device_id is None:
        return result
    assert result["step_id"] == "detect_device"
    return await hass.config_entries.flow.async_configure(flow_id, {"device": device_id})


def _listed(result: dict[str, Any]) -> set[str]:
    return {option["value"] for option in result["data_schema"].schema["device"].config["options"]}


def _one_connector(hass: HomeAssistant) -> tuple[str, str]:
    owner = make_ocpp_config_entry(hass, entry_id=OWNER)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("ocpp", CPID)}, name=CPID
    )
    switch = ocpp_entity(
        hass, owner=owner, device_id=device.id, cpid=CPID, domain="switch", key="charge_control", state="off"
    )
    return device.id, switch


async def test_automatic_flow_with_only_an_ocpp_charger_lists_it_and_builds_the_ocpp_entry(
    hass: HomeAssistant,
) -> None:
    device_id, switch = _one_connector(hass)
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": None})

    result = await _detected(hass, None)
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "detect_device"
    assert _listed(result) == {device_id}

    result = await _detected(hass, device_id)
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "ocpp_entities"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGE_CONTROL: switch}
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODE] == MODE_OCPP
    assert result["data"][CONF_CHARGE_CONTROL] == switch
    assert result["data"][CONF_OCPP_CONNECTOR_ID] == 1
    assert result["data"]["current_limit"] == "" and result["data"]["current_control"] == ""


async def test_automatic_flow_lists_only_the_connectors_of_a_real_ocpp_install(
    hass: HomeAssistant,
) -> None:
    owner = make_ocpp_config_entry(hass, entry_id=OWNER)
    devices = dr.async_get(hass)
    central = devices.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("ocpp", "central")}, name="central"
    )
    station = devices.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("ocpp", CPID)},
        name=CPID,
        via_device_id=central.id,
    )
    ocpp_entity(hass, owner=owner, device_id=station.id, cpid=CPID, domain="switch", key="availability", state="on")
    ocpp_entity(
        hass, owner=owner, device_id=station.id, cpid=CPID, domain="number", key="maximum_current",
        state="16", attributes={"unit_of_measurement": "A"},
    )
    switches = {}
    connector_devices = {}
    for number in (1, 2):
        connector = devices.async_get_or_create(
            config_entry_id=owner.entry_id,
            identifiers={("ocpp", f"{CPID}-{number}")},
            name=f"{CPID} Connector {number}",
            via_device_id=station.id,
        )
        connector_devices[number] = connector.id
        switches[number] = ocpp_entity(
            hass,
            owner=owner,
            device_id=connector.id,
            cpid=CPID,
            domain="switch",
            key="charge_control",
            connector=number,
            state="off",
        )
        ocpp_entity(
            hass, owner=owner, device_id=connector.id, cpid=CPID, domain="switch",
            key="availability", connector=number, state="on",
        )

    result = await _detected(hass, None)
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": None})
    assert _listed(result) == set(connector_devices.values())
    assert central.id not in _listed(result) and station.id not in _listed(result)
    labels = [option["label"] for option in result["data_schema"].schema["device"].config["options"]]
    assert labels == sorted(labels, key=str.casefold)

    # A connector device selects exactly that connector.
    result = await _detected(hass, connector_devices[2])
    assert result["step_id"] == "ocpp_entities"
    options = result["data_schema"].schema[CONF_CHARGE_CONTROL].container
    assert set(options) == {switches[2]}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGE_CONTROL: switches[2]}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_OCPP_CONNECTOR_ID] == 2


async def test_automatic_flow_builds_the_ocpp_entry_on_a_two_connector_charger(
    hass: HomeAssistant,
) -> None:
    owner = make_ocpp_config_entry(hass, entry_id=OWNER)
    charger = two_connector_charger(hass, owner)
    device_id = dr.async_get(hass).async_get_device_by_identifier(("ocpp", CPID), OWNER).id

    result = await _detected(hass, device_id)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGE_CONTROL: charger["charge_control_1"]}
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHARGE_CONTROL] == charger["charge_control_1"]
    assert result["data"][CONF_OCPP_CONNECTOR_ID] == 1


async def test_automatic_flow_still_handles_a_wallbox_shaped_device(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])

    result = await _detected(hass, ids["device_id"])

    assert result["type"] == FlowResultType.FORM and result["step_id"] == "detected_entities"


async def test_automatic_flow_with_no_supported_device_says_so(hass: HomeAssistant) -> None:
    result = await _detected(hass, None)

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_supported_charger"


async def test_automatic_flow_hides_a_non_ocpp_hub_device_without_entities(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["peblar"])
    hub = dr.async_get(hass).async_get_or_create(
        config_entry_id=_config_entry_of(hass, ids["device_id"]), identifiers={("peblar", "hub")}, name="Hub"
    )

    result = await _detected(hass, None)

    assert _listed(result) == {ids["device_id"]}
    assert hub.id not in _listed(result)


def _config_entry_of(hass: HomeAssistant, device_id: str) -> str:
    return next(iter(dr.async_get(hass).async_get(device_id).config_entries))


async def test_automatic_flow_tolerates_a_device_with_a_three_part_identifier(hass: HomeAssistant) -> None:
    """Some integrations register `(domain, id, extra)` identifiers; listing devices must not fail."""
    ids = register_shape(hass, SHAPES["peblar"])
    odd = dr.async_get(hass).async_get_or_create(
        config_entry_id=_config_entry_of(hass, ids["device_id"]),
        identifiers={("peblar", "odd", "extra")},
        name="Odd",
    )

    result = await _detected(hass, None)

    assert ids["device_id"] in _listed(result)
    assert odd.id not in _listed(result)  # no entities of its own, so not a charger to choose
