"""The create flow's detected-meter step: offered only when the registry holds a grid meter, applied
only on confirmation, pre-filling the details form, and enabling disabled entities only when the site
is created."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import (
    CONF_DERIVED_ENTITIES,
    CONF_GRID_POWER_INVERTED,
    CONF_MEASUREMENT_MODE,
    CONF_SITE_CURRENT_SIGNED,
    DOMAIN,
    MEASUREMENT_MODE_DERIVED,
)

from .site_registry import materialize
from .test_site_detection import huawei_solar, tibber_pulse

BASIC = {
    "name": "Home",
    "main_fuse_a": 25,
    "safety_margin_a": 1,
    "measurement_mode": "direct_phase_current",
    "charger_entry_ids": [],
}


async def to_basic_submitted(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    return await hass.config_entries.flow.async_configure(result["flow_id"], dict(BASIC))


def default_of(schema, key: str):
    field = next(field for field in schema.schema if str(field) == key)
    default = field.default
    return default() if callable(default) else default


async def test_without_a_detected_meter_the_flow_is_unchanged(hass: HomeAssistant) -> None:
    result = await to_basic_submitted(hass)

    assert result["step_id"] == "site_current_suggestions"


async def test_a_detected_meter_is_offered_before_anything_is_applied(hass: HomeAssistant) -> None:
    made = materialize(hass, huawei_solar())

    result = await to_basic_submitted(hass)

    assert result["step_id"] == "site_detected"
    assert default_of(result["data_schema"], "choice").startswith("huawei_solar:")
    assert "enable_disabled" in {str(key) for key in result["data_schema"].schema}
    assert er.async_get(hass).async_get(made["meter_current_a"]).disabled_by == er.RegistryEntryDisabler.INTEGRATION


async def test_choosing_the_meter_sets_the_mode_prefills_the_form_and_creates_the_site_with_its_signs(
    hass: HomeAssistant,
) -> None:
    made = materialize(hass, huawei_solar())
    result = await to_basic_submitted(hass)
    choice = default_of(result["data_schema"], "choice")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": choice, "enable_disabled": True}
    )

    assert result["step_id"] == "site_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"adjust": True})
    assert result["step_id"] == "site_details"
    keys = {str(key) for key in result["data_schema"].schema}
    assert "derived_L1_power" in keys and "derived_L1_current" in keys and "direct_L1" not in keys
    assert default_of(result["data_schema"], "derived_L2_voltage") == made["meter_voltage_b"]
    assert default_of(result["data_schema"], CONF_GRID_POWER_INVERTED) is True
    assert default_of(result["data_schema"], CONF_SITE_CURRENT_SIGNED) is True
    # Nothing is enabled until the site is created.
    assert er.async_get(hass).async_get(made["meter_current_a"]).disabled_by == er.RegistryEntryDisabler.INTEGRATION

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_MEASUREMENT_MODE] == MEASUREMENT_MODE_DERIVED
    assert data[CONF_GRID_POWER_INVERTED] is True and data[CONF_SITE_CURRENT_SIGNED] is True
    assert data[CONF_DERIVED_ENTITIES]["L1"] == {
        "power": made["meter_power_a"],
        "voltage": made["meter_voltage_a"],
        "current": made["meter_current_a"],
    }
    assert er.async_get(hass).async_get(made["meter_current_a"]).disabled_by is None


async def test_declining_to_enable_leaves_the_disabled_entities_alone(hass: HomeAssistant) -> None:
    made = materialize(hass, huawei_solar())
    result = await to_basic_submitted(hass)
    choice = default_of(result["data_schema"], "choice")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": choice, "enable_disabled": False}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert er.async_get(hass).async_get(made["meter_current_a"]).disabled_by == er.RegistryEntryDisabler.INTEGRATION


async def test_a_direct_meter_keeps_direct_mode(hass: HomeAssistant) -> None:
    made = materialize(hass, tibber_pulse())
    result = await to_basic_submitted(hass)
    assert result["step_id"] == "site_detected"
    # Nothing here ships disabled, so no enabling is offered.
    assert "enable_disabled" not in {str(key) for key in result["data_schema"].schema}
    choice = default_of(result["data_schema"], "choice")

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"choice": choice})
    assert result["step_id"] == "site_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"adjust": True})
    assert result["step_id"] == "site_details"
    assert default_of(result["data_schema"], "direct_L3") == made["tibber_current_l3"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["data"][CONF_MEASUREMENT_MODE] == "direct_phase_current"
    assert result["data"]["direct_entities"]["L1"] == made["tibber_current_l1"]


async def test_manual_continues_with_the_mode_chosen_on_the_first_step(hass: HomeAssistant) -> None:
    materialize(hass, huawei_solar())
    result = await to_basic_submitted(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"choice": "manual"})

    assert result["step_id"] == "site_current_suggestions"


async def test_the_options_flow_keeps_a_stored_optional_source_and_flags(hass: HomeAssistant) -> None:
    from .helpers import make_site_entry
    from .world import set_derived_site_entities

    derived = set_derived_site_entities(hass, "opt", 1000.0)
    derived["L1"]["current"] = "sensor.opt_current_l1"
    hass.states.async_set("sensor.opt_current_l1", "4.4", {"unit_of_measurement": "A"})
    entry = make_site_entry(
        hass,
        entry_id="site_opt",
        charger_entry_ids=[],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived,
        extra_data={CONF_GRID_POWER_INVERTED: True},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "main_fuse_a": 25.0,
            "safety_margin_a": 1.0,
            "measurement_mode": MEASUREMENT_MODE_DERIVED,
            "charger_entry_ids": [],
            "site_enabled": True,
            "change_measurement": True,
            "max_age_s": 120.0,
        },
    )
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = hass.config_entries.async_get_entry(entry.entry_id).data
    assert data[CONF_DERIVED_ENTITIES]["L1"]["current"] == "sensor.opt_current_l1"
    assert data[CONF_DERIVED_ENTITIES]["L1"]["reactive_power"] == derived["L1"]["reactive_power"]
    assert data[CONF_GRID_POWER_INVERTED] is True
    assert CONF_SITE_CURRENT_SIGNED not in data
