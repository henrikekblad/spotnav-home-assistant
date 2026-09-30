"""The entity-configuration commands: `spotnav/get_entity_config` and
`spotnav/update_entity_config` (`api/entity_config.py`, validation in `api/entity_fields.py`).

Pinned through the real transport: a read, a charger write and a site write (each reloading
exactly its own entry), a stale `expected` as `conflict`, per-field error codes, `not_admin`,
`no_site`, direct versus derived sites, `active_control_enabled` never written -- and, in the
last test, what a reload does to a charge that is running (the options flow's own reload).
"""

from __future__ import annotations

import itertools

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_CURRENT_LIMIT,
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_MAIN_FUSE_A,
    CONF_MAX_AGE_S,
    CONF_MEASUREMENT_MODE,
    MEASUREMENT_MODE_DERIVED,
)

from .helpers import make_entry
from .world import admin, non_admin, ws_call
from .world import setup_charger_and_site
from .messages import field, get_message, register, update_entity_config_message
from .world import controller_of
from custom_components.spotnav.runtime import domain_data

_IDS = itertools.count(1)


async def test_get_reports_charger_and_direct_site_fields(hass: HomeAssistant, hass_ws_client) -> None:
    charger, site = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)

    result = (await ws_call(client, get_message(charger.entry_id)))["result"]

    assert result["ok"] is True and result["error"] is None
    site_block = result["config"]["site"]
    assert site_block["name"] == "Site" and site_block["charger_count"] == 1
    assert set(site_block) == {"name", "charger_count", "measurement", "warnings", "detection"}
    names = [item["field"] for item in result["config"]["fields"]]
    assert names == [
        "charge_control", "current_limit", "energy_register_entity", "vehicle_soc",
        "main_fuse_a", "measurement_mode", "direct_L1", "direct_L2", "direct_L3",
        "site_current_signed", "grid_power_inverted", "battery_aggregate_power_entity", "battery_discharge_power_entity",
        "battery_power_inverted", "max_age_s",
    ]
    control = field(result, "charge_control")
    assert control["required"] is True and control["scope"] == "charger"
    assert control["current"] == {"entity_id": "switch.entry_a_control", "friendly_name": "Control entry_a", "exists": True}
    assert control["allowed_domains"] == ["switch"]
    assert field(result, "vehicle_soc")["writable"] is False
    assert field(result, "direct_L1")["scope"] == "site"


async def test_get_reports_derived_site_fields_and_no_site(hass: HomeAssistant, hass_ws_client) -> None:
    charger, _ = await setup_charger_and_site(
        hass, measurement_mode=MEASUREMENT_MODE_DERIVED, derived_entities={}
    )
    lone, _ = await setup_charger_and_site(hass, "lone", site=False)
    client = await admin(hass, hass_ws_client)

    derived = (await ws_call(client, get_message(charger.entry_id)))["result"]
    names = [item["field"] for item in derived["config"]["fields"]]
    assert "derived_L1_power" in names and "derived_L3_voltage" in names
    assert not any(name.startswith("direct_") for name in names)

    alone = (await ws_call(client, get_message(lone.entry_id)))["result"]
    assert alone["config"]["site"] is None
    assert {item["scope"] for item in alone["config"]["fields"]} == {"charger"}


async def test_unknown_site_and_unloaded_chargers_have_stable_codes(hass: HomeAssistant, hass_ws_client) -> None:
    _, site = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)
    assert (await ws_call(client, get_message("nope")))["result"]["error"] == "spotnav_unknown_charger"
    assert (await ws_call(client, get_message(site.entry_id)))["result"]["error"] == "spotnav_site_not_charger"
    charger, _ = await setup_charger_and_site(hass, "unloaded", site=False)
    await hass.config_entries.async_unload(charger.entry_id)
    assert (await ws_call(client, get_message(charger.entry_id)))["result"]["error"] == "spotnav_charger_unloaded"


async def test_charger_write_updates_data_and_reloads_only_the_charger(hass: HomeAssistant, hass_ws_client) -> None:
    charger, site = await setup_charger_and_site(hass)
    limit = register(hass, "number", "limit", "Limit")
    register_sensor = register(hass, "sensor", "energy", "Energy", device_class="energy")
    site_controller = controller_of(hass, site.entry_id)
    client = await admin(hass, hass_ws_client)

    result = (
        await ws_call(
            client,
            update_entity_config_message(
                charger.entry_id,
                scope="charger",
                expected={"current_limit": ""},
                changes={"current_limit": limit, "energy_register_entity": register_sensor},
            ),
        )
    )["result"]

    assert result["ok"] is True
    data = hass.config_entries.async_get_entry(charger.entry_id).data
    assert data[CONF_CURRENT_LIMIT] == limit and data[CONF_ENERGY_REGISTER_ENTITY] == register_sensor
    assert field(result, "current_limit")["current"]["entity_id"] == limit
    assert controller_of(hass, site.entry_id) is site_controller, "a charger write leaves the site alone"


async def test_site_write_updates_data_and_reloads_only_the_site(hass: HomeAssistant, hass_ws_client) -> None:
    charger, site = await setup_charger_and_site(hass)
    charger_controller = controller_of(hass, charger.entry_id)
    battery = register(hass, "sensor", "battery", "Battery", device_class="power")
    l1 = register(hass, "sensor", "new_l1", "New L1", device_class="current")
    client = await admin(hass, hass_ws_client)

    result = (
        await ws_call(
            client,
            update_entity_config_message(
                charger.entry_id,
                scope="site",
                expected={"main_fuse_a": 25},
                changes={
                    "main_fuse_a": 32,
                    "max_age_s": 60,
                    "battery_aggregate_power_entity": battery,
                    "direct_L1": l1,
                },
            ),
        )
    )["result"]

    assert result["ok"] is True
    data = hass.config_entries.async_get_entry(site.entry_id).data
    assert data[CONF_MAIN_FUSE_A] == 32.0 and data[CONF_MAX_AGE_S] == 60.0
    assert data[CONF_DIRECT_ENTITIES]["L1"] == l1 and data[CONF_DIRECT_ENTITIES]["L2"] == "sensor.site_entry_a_l2"
    assert controller_of(hass, charger.entry_id) is charger_controller, "a site write leaves the charger alone"
    assert field(result, "main_fuse_a")["value"] == 32.0


async def test_switching_to_derived_needs_the_six_required_meters_and_keeps_direct(hass: HomeAssistant, hass_ws_client) -> None:
    charger, site = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)
    mode = {"measurement_mode": MEASUREMENT_MODE_DERIVED}

    refused = (await ws_call(client, update_entity_config_message(charger.entry_id, scope="site", changes=mode)))["result"]
    assert refused["error"] == "spotnav_invalid_value"
    assert len(refused["field_errors"]) == 6
    assert {item["code"] for item in refused["field_errors"]} == {"required"}

    changes = dict(mode)
    for phase in ("L1", "L2", "L3"):
        for sub, cls in (("power", "power"), ("reactive_power", "reactive_power"), ("voltage", "voltage")):
            changes[f"derived_{phase}_{sub}"] = register(hass, "sensor", f"d_{phase}_{sub}", device_class=cls)
    ok = (await ws_call(client, update_entity_config_message(charger.entry_id, scope="site", changes=changes)))["result"]
    assert ok["ok"] is True
    data = hass.config_entries.async_get_entry(site.entry_id).data
    assert data[CONF_MEASUREMENT_MODE] == MEASUREMENT_MODE_DERIVED
    assert data[CONF_DIRECT_ENTITIES]["L1"] == "sensor.site_entry_a_l1"
    assert data[CONF_DERIVED_ENTITIES]["L2"]["voltage"] == changes["derived_L2_voltage"]


async def test_field_errors_are_per_field_and_write_nothing(hass: HomeAssistant, hass_ws_client) -> None:
    charger, site = await setup_charger_and_site(hass)
    other, _ = await setup_charger_and_site(hass, "other", site=False)
    sensor = register(hass, "sensor", "a_sensor")
    before = dict(hass.config_entries.async_get_entry(charger.entry_id).data)
    client = await admin(hass, hass_ws_client)

    charger_result = (
        await ws_call(
            client,
            update_entity_config_message(
                charger.entry_id,
                scope="charger",
                changes={
                    "charge_control": sensor,
                    "current_limit": "number.ghost",
                    "vehicle_soc": sensor,
                },
            ),
        )
    )["result"]
    assert charger_result["ok"] is False and charger_result["error"] == "spotnav_invalid_value"
    assert {(e["field"], e["code"]) for e in charger_result["field_errors"]} == {
        ("charge_control", "wrong_domain"),
        ("current_limit", "entity_not_found"),
        ("vehicle_soc", "not_writable"),
    }
    assert dict(hass.config_entries.async_get_entry(charger.entry_id).data) == before

    clash = (
        await ws_call(
            client,
            update_entity_config_message(
                charger.entry_id,
                scope="charger",
                changes={"charge_control": "switch.other_control"},
            ),
        )
    )["result"]
    assert clash["field_errors"] == [{"field": "charge_control", "code": "charge_control_in_use"}]

    site_before = dict(hass.config_entries.async_get_entry(site.entry_id).data)
    site_result = (
        await ws_call(
            client,
            update_entity_config_message(
                charger.entry_id,
                scope="site",
                changes={
                    "main_fuse_a": 0,
                    "max_age_s": "x",
                    "measurement_mode": "bogus",
                    "direct_L1": "sensor.ghost",
                    "direct_L2": "",
                    "battery_aggregate_power_entity": "switch.entry_a_control",
                },
            ),
        )
    )["result"]
    assert {(e["field"], e["code"]) for e in site_result["field_errors"]} == {
        ("main_fuse_a", "invalid_value"),
        ("max_age_s", "invalid_value"),
        ("measurement_mode", "invalid_value"),
        ("direct_L1", "entity_not_found"),
        ("direct_L2", "required"),
        ("battery_aggregate_power_entity", "wrong_domain"),
    }
    assert dict(hass.config_entries.async_get_entry(site.entry_id).data) == site_before


async def test_stale_expected_is_a_conflict_with_current_values(hass: HomeAssistant, hass_ws_client) -> None:
    charger, site = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)

    result = (
        await ws_call(
            client,
            update_entity_config_message(
                charger.entry_id, scope="site", expected={"main_fuse_a": 16}, changes={"main_fuse_a": 32}
            ),
        )
    )["result"]

    assert result["ok"] is False and result["error"] == "spotnav_conflict"
    assert field(result, "main_fuse_a")["value"] == 25.0
    assert hass.config_entries.async_get_entry(site.entry_id).data[CONF_MAIN_FUSE_A] == 25.0


async def test_no_site_scope_and_never_active_control(hass: HomeAssistant, hass_ws_client) -> None:
    lone, _ = await setup_charger_and_site(hass, "lone", site=False)
    charger, site = await setup_charger_and_site(hass, active_control_enabled=False)
    client = await admin(hass, hass_ws_client)

    result = (
        await ws_call(client, update_entity_config_message(lone.entry_id, scope="site", changes={"main_fuse_a": 32}))
    )["result"]
    assert result["ok"] is False and result["error"] == "spotnav_no_site" and result["config"] is None

    refused = (
        await ws_call(
            client,
            update_entity_config_message(
                charger.entry_id,
                scope="site",
                changes={"active_control_enabled": True, "main_fuse_a": 32},
            ),
        )
    )["result"]
    assert refused["error"] == "spotnav_invalid_value"
    assert refused["field_errors"] == [{"field": "active_control_enabled", "code": "unknown_field"}]
    stored = hass.config_entries.async_get_entry(site.entry_id).data
    assert stored.get(CONF_ACTIVE_CONTROL_ENABLED, False) is False and stored[CONF_MAIN_FUSE_A] == 25.0


async def test_non_admin_is_refused_in_the_envelope_for_both_commands(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    charger, site = await setup_charger_and_site(hass)
    before = dict(hass.config_entries.async_get_entry(site.entry_id).data)
    client = await non_admin(hass, hass_ws_client, hass_read_only_access_token)

    for payload in (
        get_message(charger.entry_id),
        update_entity_config_message(charger.entry_id, scope="site", changes={"main_fuse_a": 32}),
    ):
        frame = await ws_call(client, payload)
        assert frame["success"] is True
        assert frame["result"]["error"] == "spotnav_not_admin" and frame["result"]["config"] is None
    assert dict(hass.config_entries.async_get_entry(site.entry_id).data) == before


async def test_unsupported_version_is_the_stable_refusal(hass: HomeAssistant, hass_ws_client) -> None:
    charger, _ = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)
    frame = await ws_call(client, get_message(charger.entry_id, api_version=2))
    assert frame["success"] is False and frame["error"]["code"] == "spotnav_unsupported_api_version"


async def test_a_reload_mid_charge_leaves_the_charge_and_stored_settings_alone(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """What the options flow's save already does, and therefore all this command does: it
    reloads the one entry it wrote. An unload cancels only local callbacks and timers -- it
    never calls the charge control, never clears the stored plan or the Auto settings
    (`async_unload_entry`); the reloaded controller restores from its stores. So a charge in
    progress (switch on) keeps running, no turn_off is sent, the Auto settings record is
    identical afterwards, and a *site* write does not even rebuild the charger's controller.
    """
    charger, site = await setup_charger_and_site(hass)
    hass.states.async_set("switch.entry_a_control", "on")
    turn_off = async_mock_service(hass, "switch", "turn_off")
    turn_on = async_mock_service(hass, "switch", "turn_on")
    store = domain_data(hass).auto_store
    settings_before = store.settings(charger.entry_id)
    charger_controller = controller_of(hass, charger.entry_id)
    limit = register(hass, "number", "limit2", "Limit 2")
    client = await admin(hass, hass_ws_client)

    site_result = (
        await ws_call(client, update_entity_config_message(charger.entry_id, scope="site", changes={"max_age_s": 90}))
    )["result"]
    assert site_result["ok"] is True
    assert controller_of(hass, charger.entry_id) is charger_controller

    charger_result = (
        await ws_call(client, update_entity_config_message(charger.entry_id, scope="charger", changes={"current_limit": limit}))
    )["result"]
    await hass.async_block_till_done()
    assert charger_result["ok"] is True

    assert controller_of(hass, charger.entry_id) is not charger_controller, "the charger entry reloaded"
    assert hass.states.get("switch.entry_a_control").state == "on"
    assert not turn_off and not turn_on, "a reload never commands the charger"
    assert store.settings(charger.entry_id) == settings_before


async def test_effective_reports_what_the_controller_really_reads(hass: HomeAssistant, hass_ws_client) -> None:
    """`effective` is the controller's own resolution: a configured entity that no longer exists
    falls back to the OCPP session limit, an unconfigured register is auto-resolved, and both
    say `automatic`; a configured, existing entity says `configured`."""
    from custom_components.spotnav.const import CONF_MODE, MODE_OCPP

    control = register(hass, "switch", "halo_charger_connector_1_charge_control", "halo_charger Connector 1 Charge Control")
    session = register(hass, "number", "halo_charger_connector_1_session_current_limit", "Session current limit")
    energy = register(
        hass, "sensor", "halo_charger_connector_1_energy_active_import_register", "Energy import register"
    )
    charger = make_entry(
        hass,
        entry_id="halo",
        charge_control=control,
        current_limit="number.halo_charger_connector_1_maximum_current",
        webhook_id="webhook-halo",
        title="halo",
        current_control="change_configuration",
    )
    hass.config_entries.async_update_entry(charger, data={**charger.data, CONF_MODE: MODE_OCPP})
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    client = await admin(hass, hass_ws_client)

    result = (await ws_call(client, get_message(charger.entry_id)))["result"]

    assert field(result, "charge_control")["effective"] == {
        "entity_id": control,
        "friendly_name": "halo_charger Connector 1 Charge Control",
        "source": "configured",
    }
    limit = field(result, "current_limit")
    assert limit["current"]["entity_id"] == "number.halo_charger_connector_1_maximum_current"
    assert limit["current"]["exists"] is False, "a stored id that no longer resolves is stated as gone"
    assert limit["effective"] == {
        "entity_id": session, "friendly_name": "Session current limit", "source": "automatic",
    }
    assert field(result, "energy_register_entity")["current"] is None
    assert field(result, "energy_register_entity")["effective"] == {
        "entity_id": energy, "friendly_name": "Energy import register", "source": "automatic",
    }


async def test_effective_is_null_when_nothing_is_read_and_configured_when_it_exists(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, _ = await setup_charger_and_site(hass, "plain", site=False)
    limit = register(hass, "number", "plain_limit", "Plain limit")
    hass.config_entries.async_update_entry(charger, data={**charger.data, CONF_CURRENT_LIMIT: limit})
    client = await admin(hass, hass_ws_client)

    result = (await ws_call(client, get_message(charger.entry_id)))["result"]

    assert field(result, "current_limit")["effective"]["source"] == "configured"
    assert field(result, "energy_register_entity")["effective"] is None
    assert field(result, "vehicle_soc")["effective"] is None
