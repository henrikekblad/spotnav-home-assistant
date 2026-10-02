"""Registry detection through Home Assistant: the snapshot of the real registries, the detection and
warning blocks of `spotnav/get_entity_config`, and applying a detected meter or battery with
`apply_detection` -- which enables only entities their integration ships disabled."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import (
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_BATTERY_DISCHARGE_POWER_ENTITY,
    CONF_BATTERY_POWER_INVERTED,
    CONF_DERIVED_ENTITIES,
    CONF_GRID_POWER_INVERTED,
    CONF_MEASUREMENT_MODE,
    CONF_SITE_CURRENT_SIGNED,
    CONF_SITE_CURRENT_SOURCE,
    MEASUREMENT_MODE_DERIVED,
    MEASUREMENT_MODE_DIRECT,
)
from custom_components.spotnav.site.site_detection import (
    detect_site_from_hass,
    find_own_load_balancing_from_hass,
    snapshot_registry,
)

from .messages import audit_privacy, get_message, update_entity_config_message
from .site_registry import materialize
from .test_site_detection import easee_equalizer, huawei_solar, sigen, sma_with_inverter, tibber_pulse
from .world import admin, setup_charger_and_site, ws_call


def block(result: dict) -> dict:
    return result["result"]["config"]["site"]


async def test_the_snapshot_reads_disabled_and_enabled_entities_and_devices(hass: HomeAssistant) -> None:
    made = materialize(hass, huawei_solar())

    entities, devices = snapshot_registry(hass)

    by_id = {entity.entity_id: entity for entity in entities}
    assert by_id[made["meter_current_a"]].disabled_by == "integration"
    assert by_id[made["meter_power_a"]].disabled_by is None
    assert by_id[made["meter_power_a"]].device_class == "power"
    assert any(device.model == "DTSU666-H" for device in devices)


async def test_detection_runs_over_the_real_registry_and_skips_a_chargers_device(hass: HomeAssistant) -> None:
    materialize(hass, huawei_solar())

    assert [c.integration for c in detect_site_from_hass(hass).meters] == ["huawei_solar"]
    devices = {d.id for d in snapshot_registry(hass)[1] if d.model == "DTSU666-H"}
    assert detect_site_from_hass(hass, excluded_device_ids=devices).meters == ()


async def test_get_entity_config_lists_candidates_measurement_and_warnings(
    hass: HomeAssistant, hass_ws_client
) -> None:
    materialize(hass, huawei_solar())
    charger, _site = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)

    result = await ws_call(client, get_message(charger.entry_id))

    audit_privacy(result["result"])
    site = block(result)
    [meter] = site["detection"]["meters"]
    assert meter["integration"] == "huawei_solar"
    assert meter["mode"] == MEASUREMENT_MODE_DERIVED
    assert meter["current_signed"] is True and meter["power_inverted"] is True
    assert meter["applied"] is False
    assert len(meter["disabled_entities"]) == 6
    assert {item["role"] for item in meter["entities"]} == {"power", "voltage", "current"}
    assert [b["inverted"] for b in site["detection"]["batteries"]] == [False]
    assert site["measurement"]["mode"] == MEASUREMENT_MODE_DIRECT
    assert site["warnings"] == []


async def test_apply_detection_writes_the_setup_and_enables_only_what_the_integration_disabled(
    hass: HomeAssistant, hass_ws_client
) -> None:
    made = materialize(hass, huawei_solar())
    registry = er.async_get(hass)
    charger, site_entry = await setup_charger_and_site(hass)
    # A person's own choice to disable an entity stays.
    registry.async_update_entity(made["inverter_active_power"], disabled_by=er.RegistryEntryDisabler.USER)
    client = await admin(hass, hass_ws_client)
    candidate_id = block(await ws_call(client, get_message(charger.entry_id)))["detection"]["meters"][0]["id"]

    result = await ws_call(
        client,
        update_entity_config_message(
            charger.entry_id, scope="site", changes={"apply_detection": candidate_id}
        ),
    )

    assert result["result"]["ok"] is True
    data = hass.config_entries.async_get_entry(site_entry.entry_id).data
    assert data[CONF_MEASUREMENT_MODE] == MEASUREMENT_MODE_DERIVED
    assert data[CONF_SITE_CURRENT_SIGNED] is True and data[CONF_GRID_POWER_INVERTED] is True
    assert data[CONF_DERIVED_ENTITIES]["L1"] == {
        "power": made["meter_power_a"],
        "voltage": made["meter_voltage_a"],
        "current": made["meter_current_a"],
    }
    assert registry.async_get(made["meter_current_a"]).disabled_by is None
    assert registry.async_get(made["meter_voltage_c"]).disabled_by is None
    assert registry.async_get(made["inverter_active_power"]).disabled_by == er.RegistryEntryDisabler.USER
    # The card is told the stored setup now is the detected one.
    [meter] = block(result)["detection"]["meters"]
    assert meter["applied"] is True


async def test_apply_detection_for_a_battery_sets_the_sign_and_the_pair(hass: HomeAssistant, hass_ws_client) -> None:
    made = materialize(hass, sma_with_inverter())
    charger, site_entry = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)
    site = block(await ws_call(client, get_message(charger.entry_id)))
    [battery] = site["detection"]["batteries"]

    result = await ws_call(
        client,
        update_entity_config_message(charger.entry_id, scope="site", changes={"apply_detection": battery["id"]}),
    )

    assert result["result"]["ok"] is True
    data = hass.config_entries.async_get_entry(site_entry.entry_id).data
    assert data[CONF_BATTERY_AGGREGATE_POWER_ENTITY] == made["sma_battery_charge"]
    assert data[CONF_BATTERY_DISCHARGE_POWER_ENTITY] == made["sma_battery_discharge"]
    assert data[CONF_BATTERY_POWER_INVERTED] is False


async def test_apply_detection_refuses_an_unknown_id_or_extra_changes_and_writes_nothing(
    hass: HomeAssistant, hass_ws_client
) -> None:
    materialize(hass, huawei_solar())
    charger, site_entry = await setup_charger_and_site(hass)
    before = dict(hass.config_entries.async_get_entry(site_entry.entry_id).data)
    client = await admin(hass, hass_ws_client)
    candidate_id = block(await ws_call(client, get_message(charger.entry_id)))["detection"]["meters"][0]["id"]

    unknown = await ws_call(
        client, update_entity_config_message(charger.entry_id, scope="site", changes={"apply_detection": "made:up"})
    )
    extra = await ws_call(
        client,
        update_entity_config_message(
            charger.entry_id, scope="site", changes={"apply_detection": candidate_id, "main_fuse_a": 16}
        ),
    )

    for result in (unknown, extra):
        assert result["result"]["error"] == "spotnav_invalid_value"
        assert {"field": "apply_detection", "code": "invalid_value"} in result["result"]["field_errors"]
    assert dict(hass.config_entries.async_get_entry(site_entry.entry_id).data) == before


async def test_a_direct_candidate_applies_as_direct_mode_and_keeps_the_derived_entities(
    hass: HomeAssistant, hass_ws_client
) -> None:
    made = materialize(hass, tibber_pulse())
    charger, site_entry = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)
    candidate_id = block(await ws_call(client, get_message(charger.entry_id)))["detection"]["meters"][0]["id"]

    result = await ws_call(
        client, update_entity_config_message(charger.entry_id, scope="site", changes={"apply_detection": candidate_id})
    )

    assert result["result"]["ok"] is True
    data = hass.config_entries.async_get_entry(site_entry.entry_id).data
    assert data[CONF_MEASUREMENT_MODE] == MEASUREMENT_MODE_DIRECT
    assert data["direct_entities"]["L1"] == made["tibber_current_l1"]


async def test_an_easee_equalizer_warns_and_applies_an_attributes_source(hass: HomeAssistant, hass_ws_client) -> None:
    made = materialize(hass, easee_equalizer())
    charger, site_entry = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)
    site = block(await ws_call(client, get_message(charger.entry_id)))

    assert [w["code"] for w in site["warnings"]] == ["own_load_balancing"]
    assert site["warnings"][0]["integration"] == "easee"
    [meter] = site["detection"]["meters"]
    assert "own_load_balancing" in meter["warnings"]

    await ws_call(
        client, update_entity_config_message(charger.entry_id, scope="site", changes={"apply_detection": meter["id"]})
    )
    data = hass.config_entries.async_get_entry(site_entry.entry_id).data
    assert data[CONF_SITE_CURRENT_SOURCE]["entity_id"] == made["easee_equalizer_current"]
    assert er.async_get(hass).async_get(made["easee_equalizer_current"]).disabled_by is None


async def test_own_load_balancing_devices_are_found_in_the_real_device_registry(hass: HomeAssistant) -> None:
    materialize(hass, easee_equalizer())

    assert [(o.integration, o.device_name) for o in find_own_load_balancing_from_hass(hass)] == [
        ("easee", "Easee Equalizer")
    ]


async def test_a_slow_integration_is_warned_about_with_its_option(hass: HomeAssistant, hass_ws_client) -> None:
    from .test_site_detection import solaredge_modbus_multi

    made = materialize(hass, solaredge_modbus_multi())
    charger, site_entry = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)
    await ws_call(
        client,
        update_entity_config_message(
            charger.entry_id,
            scope="site",
            changes={"apply_detection": block(await ws_call(client, get_message(charger.entry_id)))["detection"]["meters"][0]["id"]},
        ),
    )

    site = block(await ws_call(client, get_message(charger.entry_id)))

    [warning] = [w for w in site["warnings"] if w["code"] == "update_interval_exceeds_max_age"]
    assert warning["integration"] == "solaredge_modbus_multi"
    assert warning["interval_s"] == 300.0
    assert "Polling" in warning["option"]
    assert made  # the entities the warning is about


async def test_the_sigen_candidate_keeps_the_owners_three_field_shape(hass: HomeAssistant, hass_ws_client) -> None:
    made = materialize(hass, sigen())
    charger, site_entry = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)
    [meter] = block(await ws_call(client, get_message(charger.entry_id)))["detection"]["meters"]

    await ws_call(
        client, update_entity_config_message(charger.entry_id, scope="site", changes={"apply_detection": meter["id"]})
    )

    data = hass.config_entries.async_get_entry(site_entry.entry_id).data
    assert data[CONF_DERIVED_ENTITIES]["L1"] == {
        "power": made["sigen_plant_grid_phase_a_active"],
        "reactive_power": made["sigen_plant_grid_phase_a_reactive"],
        "voltage": made["sigen_inverter_phase_a_voltage"],
    }


async def test_a_site_stored_without_the_sign_flags_still_reads_the_same_meter_as_applied(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """A site created before the sign flags existed has no such keys; absent means off, so the
    detected meter it already uses must not be offered again."""
    materialize(hass, sigen())
    charger, site_entry = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)
    candidate_id = block(await ws_call(client, get_message(charger.entry_id)))["detection"]["meters"][0]["id"]
    await ws_call(
        client,
        update_entity_config_message(charger.entry_id, scope="site", changes={"apply_detection": candidate_id}),
    )
    entry = hass.config_entries.async_get_entry(site_entry.entry_id)
    assert entry.data[CONF_SITE_CURRENT_SIGNED] is False and entry.data[CONF_GRID_POWER_INVERTED] is False
    older = {key: value for key, value in entry.data.items() if key not in (CONF_SITE_CURRENT_SIGNED, CONF_GRID_POWER_INVERTED)}
    hass.config_entries.async_update_entry(entry, data=older)

    [meter] = block(await ws_call(client, get_message(charger.entry_id)))["detection"]["meters"]

    assert meter["applied"] is True
