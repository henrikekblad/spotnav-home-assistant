"""The site's sign options and optional derived sources as entity-config fields: what
`spotnav/get_entity_config` lists, what `spotnav/update_entity_config` accepts, and what it refuses."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import (
    CONF_BATTERY_POWER_INVERTED,
    CONF_DERIVED_ENTITIES,
    CONF_GRID_POWER_INVERTED,
    CONF_SITE_CURRENT_SIGNED,
    MEASUREMENT_MODE_DERIVED,
)

from .messages import get_message, register, update_entity_config_message
from .world import admin, setup_charger_and_site, ws_call

PHASES = ("L1", "L2", "L3")


def derived(hass: HomeAssistant, prefix: str) -> dict:
    mapping = {}
    for phase in PHASES:
        low = phase.lower()
        mapping[phase] = {
            "power": register(hass, "sensor", f"{prefix}_p_{low}", device_class="power"),
            "voltage": register(hass, "sensor", f"{prefix}_v_{low}", device_class="voltage"),
            "reactive_power": register(hass, "sensor", f"{prefix}_q_{low}", device_class="reactive_power"),
        }
    return mapping


def fields_of(result: dict) -> dict[str, dict]:
    return {item["field"]: item for item in result["result"]["config"]["fields"]}


async def test_derived_mode_lists_the_required_and_optional_sources_and_both_sign_flags(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, _ = await setup_charger_and_site(
        hass, measurement_mode=MEASUREMENT_MODE_DERIVED, derived_entities=derived(hass, "a")
    )
    client = await admin(hass, hass_ws_client)

    fields = fields_of(await ws_call(client, get_message(charger.entry_id)))

    for phase in PHASES:
        assert fields[f"derived_{phase}_power"]["required"] is True
        assert fields[f"derived_{phase}_voltage"]["required"] is True
        for optional in ("power_export", "reactive_power", "apparent_power", "current"):
            assert fields[f"derived_{phase}_{optional}"]["required"] is False
    assert fields["derived_L1_apparent_power"]["allowed_device_classes"] == ["apparent_power"]
    assert fields["derived_L1_current"]["allowed_device_classes"] == ["current"]
    assert fields["site_current_signed"] == {
        "field": "site_current_signed",
        "scope": "site",
        "kind": "flag",
        "required": False,
        "writable": True,
        "value": False,
    }
    assert fields["grid_power_inverted"]["kind"] == "flag"
    assert fields["battery_power_inverted"]["kind"] == "flag"


async def test_direct_mode_lists_both_flags_so_a_switch_to_derived_can_set_the_power_sign(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, _ = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)

    fields = fields_of(await ws_call(client, get_message(charger.entry_id)))

    assert fields["grid_power_inverted"]["kind"] == "flag"
    assert fields["site_current_signed"]["kind"] == "flag"


async def test_the_flags_are_written_and_compared_as_booleans(hass: HomeAssistant, hass_ws_client) -> None:
    charger, site = await setup_charger_and_site(
        hass, measurement_mode=MEASUREMENT_MODE_DERIVED, derived_entities=derived(hass, "b")
    )
    client = await admin(hass, hass_ws_client)

    ok = await ws_call(
        client,
        update_entity_config_message(
            charger.entry_id,
            scope="site",
            expected={"site_current_signed": False, "grid_power_inverted": False},
            changes={"site_current_signed": True, "grid_power_inverted": True, "battery_power_inverted": True},
        ),
    )

    assert ok["result"]["ok"] is True
    data = hass.config_entries.async_get_entry(site.entry_id).data
    assert data[CONF_SITE_CURRENT_SIGNED] is True
    assert data[CONF_GRID_POWER_INVERTED] is True
    assert data[CONF_BATTERY_POWER_INVERTED] is True
    assert fields_of(ok)["site_current_signed"]["value"] is True

    stale = await ws_call(
        client,
        update_entity_config_message(
            charger.entry_id, scope="site", expected={"site_current_signed": False}, changes={"site_current_signed": False}
        ),
    )
    assert stale["result"]["error"] == "spotnav_conflict"


async def test_a_flag_that_is_not_a_boolean_is_refused(hass: HomeAssistant, hass_ws_client) -> None:
    charger, site = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)

    refused = await ws_call(
        client, update_entity_config_message(charger.entry_id, scope="site", changes={"site_current_signed": "yes"})
    )

    assert refused["result"]["error"] == "spotnav_invalid_value"
    assert refused["result"]["field_errors"] == [{"field": "site_current_signed", "code": "invalid_value"}]
    assert CONF_SITE_CURRENT_SIGNED not in hass.config_entries.async_get_entry(site.entry_id).data


async def test_an_optional_source_can_be_added_and_cleared_but_a_required_one_cannot(
    hass: HomeAssistant, hass_ws_client
) -> None:
    mapping = derived(hass, "c")
    charger, site = await setup_charger_and_site(
        hass, measurement_mode=MEASUREMENT_MODE_DERIVED, derived_entities=mapping
    )
    client = await admin(hass, hass_ws_client)
    apparent = register(hass, "sensor", "c_s_l1", device_class="apparent_power")

    added = await ws_call(
        client,
        update_entity_config_message(charger.entry_id, scope="site", changes={"derived_L1_apparent_power": apparent}),
    )
    assert added["result"]["ok"] is True
    stored = hass.config_entries.async_get_entry(site.entry_id).data[CONF_DERIVED_ENTITIES]
    assert stored["L1"]["apparent_power"] == apparent and stored["L1"]["reactive_power"]

    cleared = await ws_call(
        client,
        update_entity_config_message(
            charger.entry_id,
            scope="site",
            expected={"derived_L1_reactive_power": mapping["L1"]["reactive_power"]},
            changes={"derived_L1_reactive_power": ""},
        ),
    )
    assert cleared["result"]["ok"] is True
    stored = hass.config_entries.async_get_entry(site.entry_id).data[CONF_DERIVED_ENTITIES]
    assert "reactive_power" not in stored["L1"]

    required = await ws_call(
        client, update_entity_config_message(charger.entry_id, scope="site", changes={"derived_L1_power": ""})
    )
    assert required["result"]["field_errors"] == [{"field": "derived_L1_power", "code": "required"}]


async def test_a_site_without_reactive_power_gets_an_estimated_state_in_the_config_block(
    hass: HomeAssistant, hass_ws_client
) -> None:
    mapping = derived(hass, "d")
    for phase in PHASES:
        mapping[phase].pop("reactive_power")
        hass.states.async_set(mapping[phase]["power"], "2300", {"unit_of_measurement": "W"})
        hass.states.async_set(mapping[phase]["voltage"], "230", {"unit_of_measurement": "V"})
    charger, _ = await setup_charger_and_site(
        hass, measurement_mode=MEASUREMENT_MODE_DERIVED, derived_entities=mapping
    )
    client = await admin(hass, hass_ws_client)

    measurement = (await ws_call(client, get_message(charger.entry_id)))["result"]["config"]["site"]["measurement"]

    assert measurement["mode"] == MEASUREMENT_MODE_DERIVED
    assert measurement["current_estimated"] is True
    assert measurement["assumed_power_factor"] == 0.9
    assert measurement["basis"] == {phase: "estimated" for phase in PHASES}
