"""The charger's own current range: dashboard v7's `current_range` and the settings write that
refuses amps above it (`invalid_amps`, the contract's existing code), for a generic charger with a
current-limit number and for an OCPP 0.12 charger whose ceiling is `number.<cpid>_maximum_current`.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.planning.auto_settings import AutoSettings
from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CONF_WEBHOOK_ID,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    MODE_OCPP,
)
from tests.helpers import make_ocpp_config_entry
from tests.world import setup_charger
from tests.world import CPID, ocpp_entity, station_device, two_connector_charger
from tests.messages import update_settings_message
from tests.world import admin, ws_call
from tests.test_strategy_schema import body_of
from custom_components.spotnav.runtime import domain_data

pytestmark = pytest.mark.usefixtures("offline_relay")


async def _range(client, entry_id: str) -> dict[str, Any]:
    frame = await ws_call(
        client,
        {
            "type": "spotnav/get_dashboard",
            "api_version": 1,
            "charger_id": entry_id,
        },
    )
    return frame["result"]["current_range"]


async def _write_amps(hass: HomeAssistant, client, entry_id: str, amps: int) -> dict[str, Any]:
    store = domain_data(hass).auto_store
    before = await store.async_update(entry_id, mutate=lambda _c: AutoSettings())
    frame = await ws_call(
        client,
        update_settings_message(
            entry_id,
            before.revision,
            body_of(before, area_id="SE4", amps=amps, phases=3),
        ),
    )
    return frame["result"]


async def test_a_charger_that_states_no_ceiling_is_offered_the_default(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await setup_charger(hass)
    assert await _range(await admin(hass, hass_ws_client), entry.entry_id) == {
        "min_a": 6,
        "max_a": 32,
        "source": "default",
    }


async def test_the_current_limit_numbers_own_maximum_is_the_ceiling_and_the_write_obeys_it(
    hass: HomeAssistant, hass_ws_client, offline_relay
) -> None:
    hass.states.async_set(
        "number.wallbox_limit",
        "10",
        {"unit_of_measurement": "A", "min": 6, "max": 16},
    )
    entry = await setup_charger(hass, current_limit="number.wallbox_limit")
    client = await admin(hass, hass_ws_client)
    assert await _range(client, entry.entry_id) == {
        "min_a": 6,
        "max_a": 16,
        "source": "current_limit",
    }

    refused = await _write_amps(hass, client, entry.entry_id, 20)
    assert refused["ok"] is False and refused["error"] == "invalid_amps"
    assert domain_data(hass).auto_store.settings(entry.entry_id).amps is None

    accepted = await _write_amps(hass, client, entry.entry_id, 16)
    assert accepted["ok"] is True and accepted["settings"]["amps"] == 16


async def test_the_absolute_bound_still_refuses_above_eighty_whatever_the_charger_says(
    hass: HomeAssistant, hass_ws_client, offline_relay
) -> None:
    hass.states.async_set(
        "number.big_limit", "10", {"unit_of_measurement": "A", "min": 6, "max": 200}
    )
    entry = await setup_charger(hass, current_limit="number.big_limit")
    client = await admin(hass, hass_ws_client)
    # 200 A is not believed: the contract's 80 A bound applies to the stated ceiling too.
    assert (await _range(client, entry.entry_id))["max_a"] == 32
    assert (await _write_amps(hass, client, entry.entry_id, 81))["error"] == "invalid_amps"


@pytest.mark.parametrize(
    "attributes",
    [
        {"max": 16},  # no unit: never assumed to be amperes
        {"unit_of_measurement": "A", "max": "lots"},
        {"unit_of_measurement": "A", "max": 3},  # below the pilot floor
        {"unit_of_measurement": "kW", "max": 16},
    ],
)
async def test_an_unusable_maximum_falls_back_to_the_default(
    hass: HomeAssistant, hass_ws_client, attributes: dict[str, Any]
) -> None:
    hass.states.async_set("number.odd_limit", "10", attributes)
    entry = await setup_charger(hass, current_limit="number.odd_limit")
    assert (await _range(await admin(hass, hass_ws_client), entry.entry_id))["source"] == "default"


async def test_an_ocpp_012_charger_takes_its_ceiling_from_the_station_maximum(
    hass: HomeAssistant, hass_ws_client, offline_relay
) -> None:
    """The station's `number.<cpid>_maximum_current` (max 16) is a ceiling, and the connector's
    session limit (max 32 here) never raises it: the lowest stated maximum wins."""
    owner = make_ocpp_config_entry(hass, entry_id="entry_ocpp_owner")
    charger = two_connector_charger(hass, owner)
    hass.states.async_set(
        charger["session_1"], "unavailable", {"unit_of_measurement": "A", "min": 6, "max": 32}
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry_charger",
        title="Charger",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_WEBHOOK_ID: "webhook-charger",
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: charger["charge_control_1"],
            CONF_CURRENT_LIMIT: "",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_CHARGE_POINT_ID: CPID,
            CONF_OCPP_CONNECTOR_ID: 1,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await admin(hass, hass_ws_client)

    assert await _range(client, entry.entry_id) == {
        "min_a": 6,
        "max_a": 16,
        "source": "station_maximum",
    }
    assert (await _write_amps(hass, client, entry.entry_id, 20))["error"] == "invalid_amps"
    assert (await _write_amps(hass, client, entry.entry_id, 16))["ok"] is True


async def test_an_ocpp_012_charger_with_only_a_session_limit_uses_it(
    hass: HomeAssistant, hass_ws_client
) -> None:
    owner = make_ocpp_config_entry(hass, entry_id="entry_ocpp_owner")
    device_id = station_device(hass, owner, CPID)
    shared = {"owner": owner, "device_id": device_id, "cpid": CPID}
    control = ocpp_entity(
        hass, **shared, domain="switch", key="charge_control", connector=1, state="off"
    )
    ocpp_entity(
        hass,
        **shared,
        domain="number",
        key="session_current_limit",
        connector=1,
        state="10",
        attributes={"unit_of_measurement": "A", "min": 6, "max": 25},
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry_session",
        title="Session",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_WEBHOOK_ID: "webhook-session",
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: control,
            CONF_CURRENT_LIMIT: "",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_CHARGE_POINT_ID: CPID,
            CONF_OCPP_CONNECTOR_ID: 1,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await _range(await admin(hass, hass_ws_client), entry.entry_id) == {
        "min_a": 6,
        "max_a": 25,
        "source": "session_limit",
    }
