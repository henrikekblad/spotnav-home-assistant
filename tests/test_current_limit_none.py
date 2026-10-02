"""The card's "None" for the current limit: `update_entity_config` with `current_limit: "none"` stores an
explicit opt-out (`CONF_CURRENT_LIMIT_NONE`) that clears the entity and the current control, switches the
automatic OCPP session-limit lookup off, and leaves a charger that starts and stops only.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_CURRENT_LIMIT_NONE,
    CONF_MODE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    CURRENT_CONTROL_NUMBER,
    MODE_OCPP,
)
from custom_components.spotnav.execution.controller import ChargingController

from .helpers import make_entry
from .messages import field, get_message, register, update_entity_config_message
from .world import admin, controller_of, setup_charger_and_site, ws_call


async def _ocpp_charger(hass: HomeAssistant, control_mode: str = CURRENT_CONTROL_CHANGE_CONFIGURATION):
    control = register(hass, "switch", "halo_charger_connector_1_charge_control", "halo_charger Connector 1 Charge Control")
    session = register(hass, "number", "halo_charger_connector_1_session_current_limit", "Session current limit")
    hass.states.async_set(session, "16", {"min": 6, "max": 16, "friendly_name": "Session current limit"})
    charger = make_entry(
        hass,
        entry_id="halo",
        charge_control=control,
        current_limit=None,
        webhook_id="webhook-halo",
        title="halo",
        current_control=control_mode,
    )
    hass.config_entries.async_update_entry(charger, data={**charger.data, CONF_MODE: MODE_OCPP})
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    return charger, session


def _auto(session: str) -> dict[str, str]:
    return {"entity_id": session, "friendly_name": "Session current limit"}


async def _set_limit(hass, client, charger, value, expected):
    return (
        await ws_call(
            client,
            update_entity_config_message(
                charger.entry_id, scope="charger", expected={"current_limit": expected}, changes={"current_limit": value}
            ),
        )
    )["result"]


async def test_descriptor_offers_none_and_reports_the_automatic_value(hass: HomeAssistant, hass_ws_client) -> None:
    charger, session = await _ocpp_charger(hass)
    client = await admin(hass, hass_ws_client)

    limit = field((await ws_call(client, get_message(charger.entry_id)))["result"], "current_limit")

    assert limit["current"] is None
    assert limit["effective"]["entity_id"] == session and limit["effective"]["source"] == "automatic"
    assert limit["none"] == {"allowed": True, "chosen": False, "automatic": _auto(session)}


async def test_none_is_not_offered_while_the_current_is_set_through_the_number(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, session = await _ocpp_charger(hass, CURRENT_CONTROL_NUMBER)
    client = await admin(hass, hass_ws_client)

    result = (await ws_call(client, get_message(charger.entry_id)))["result"]
    assert field(result, "current_limit")["none"] == {"allowed": False, "chosen": False, "automatic": _auto(session)}

    refused = await _set_limit(hass, client, charger, "none", "")
    assert refused["ok"] is False
    assert refused["field_errors"] == [{"field": "current_limit", "code": "invalid_value"}]
    assert CONF_CURRENT_LIMIT_NONE not in hass.config_entries.async_get_entry(charger.entry_id).data


async def test_choosing_none_stores_the_opt_out_and_reads_back_as_none(hass: HomeAssistant, hass_ws_client) -> None:
    charger, session = await _ocpp_charger(hass)
    client = await admin(hass, hass_ws_client)

    result = await _set_limit(hass, client, charger, "none", "")

    assert result["ok"] is True
    data = hass.config_entries.async_get_entry(charger.entry_id).data
    assert data[CONF_CURRENT_LIMIT_NONE] is True
    assert data[CONF_CURRENT_LIMIT] == "" and data[CONF_CURRENT_CONTROL] == ""
    limit = field(result, "current_limit")
    assert limit["current"] is None and limit["effective"] is None, "the automatic lookup is off"
    assert limit["none"] == {"allowed": True, "chosen": True, "automatic": _auto(session)}, "still offered again"
    # The stored choice is what a later save compares against.
    again = await _set_limit(hass, client, charger, "", "none")
    assert again["ok"] is True
    data = hass.config_entries.async_get_entry(charger.entry_id).data
    assert CONF_CURRENT_LIMIT_NONE not in data, "automatic again drops the opt-out"
    assert field(again, "current_limit")["effective"]["source"] == "automatic"


async def test_choosing_an_entity_after_none_ends_the_opt_out(hass: HomeAssistant, hass_ws_client) -> None:
    charger, session = await _ocpp_charger(hass)
    other = register(hass, "number", "halo_other_limit", "Other limit")
    client = await admin(hass, hass_ws_client)
    await _set_limit(hass, client, charger, "none", "")

    result = await _set_limit(hass, client, charger, other, "none")

    assert result["ok"] is True
    data = hass.config_entries.async_get_entry(charger.entry_id).data
    assert data[CONF_CURRENT_LIMIT] == other and CONF_CURRENT_LIMIT_NONE not in data
    assert field(result, "current_limit")["none"] == {"allowed": True, "chosen": False, "automatic": _auto(session)}


async def test_a_generic_charger_can_choose_none_without_an_automatic_value(hass: HomeAssistant, hass_ws_client) -> None:
    charger, _ = await setup_charger_and_site(hass, "plain", site=False)
    client = await admin(hass, hass_ws_client)

    result = await _set_limit(hass, client, charger, "none", "")

    assert result["ok"] is True
    assert field(result, "current_limit")["none"] == {"allowed": True, "chosen": True, "automatic": None}


async def test_the_controller_then_never_writes_a_current(hass: HomeAssistant, hass_ws_client) -> None:
    charger, session = await _ocpp_charger(hass)
    client = await admin(hass, hass_ws_client)
    await _set_limit(hass, client, charger, "none", "")
    controller = controller_of(hass, charger.entry_id)
    assert controller.current_limit_none and controller.current_control == ""
    assert controller.adapter.current_enabled is False
    assert controller.adapter.capabilities.set_current is False
    assert controller._current_limit_entity_value() is None, "the session limit is no longer read"  # noqa: SLF001
    assert controller.current_range()["source"] != "session_limit"

    hass.states.async_set(controller.charge_control, "off")
    async_mock_service(hass, "switch", "turn_on")
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    number_calls = async_mock_service(hass, "number", "set_value")
    await controller.async_start(amps=10)

    assert configure_calls == [] and number_calls == []
    assert controller.requested_current_a == 10, "the request is only recorded"


async def test_stored_control_is_ignored_while_the_opt_out_is_set(hass: HomeAssistant) -> None:
    """Even a hand-edited entry that has both keeps to start and stop."""
    hass.states.async_set("switch.a", "off")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.16,2.10"})
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    controller = ChargingController(
        hass,
        "entry_a",
        {
            "charge_control": "switch.a",
            CONF_CURRENT_LIMIT: "number.x",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_CURRENT_LIMIT_NONE: True,
            CONF_OCPP_CHARGE_POINT_ID: "charger",
            CONF_OCPP_CONNECTOR_ID: 1,
        },
    )
    await controller.async_initialize()

    await controller.async_start(amps=10)

    assert configure_calls == [] and controller.current_limit is None
    await controller.async_shutdown()
