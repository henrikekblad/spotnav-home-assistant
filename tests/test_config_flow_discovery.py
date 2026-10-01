"""Tests for the config-flow's discovery-suggestions step (site current)
and the per-charger measured-current-source field in site_details --
covering the "safe suggestion, never auto-applied" requirement end to end,
plus manual fallback and "configure later".
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.spotnav.const import (
    CONF_DIRECT_ENTITIES,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_PHASE_WIRING,
    CONF_SITE_CURRENT_SOURCE,
    DOMAIN,
)

from .helpers import create_ocpp_charger_device, make_entry, make_ocpp_config_entry, make_site_entry


def _grid_meter_with_attributes(hass: HomeAssistant, *, owner, unique_id: str) -> str:
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("ocpp", unique_id)}, name=unique_id
    )
    entry = entity_registry.async_get_or_create(
        "sensor", "ocpp", f"{unique_id}_current", device_id=device.id, config_entry=owner,
        suggested_object_id=unique_id,
    )
    hass.states.async_set(
        entry.entity_id, "unknown",
        {"device_class": "current", "unit_of_measurement": "A", "L1": 5.0, "L2": 6.0, "L3": 7.0},
    )
    return entry.entity_id


async def test_clear_site_current_candidate_can_be_accepted(hass: HomeAssistant) -> None:
    owner = make_ocpp_config_entry(hass, entry_id="owner_grid")
    entity_id = _grid_meter_with_attributes(hass, owner=owner, unique_id="grid_meter_flow")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
        },
    )
    assert result["step_id"] == "site_current_suggestions"

    registry = er.async_get(hass)
    candidate_id = registry.async_get(entity_id).entity_id
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": candidate_id}
    )
    assert result["step_id"] == "site_details"
    # No direct_L1/L2/L3 fields shown -- an empty submission must be enough.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    source = result["data"][CONF_SITE_CURRENT_SOURCE]
    assert source["kind"] == "attributes"
    assert source["entity_id"] == entity_id
    assert result["data"][CONF_DIRECT_ENTITIES] == {}


async def test_manual_choice_falls_back_to_the_guided_entity_pickers(hass: HomeAssistant) -> None:
    owner = make_ocpp_config_entry(hass, entry_id="owner_grid_manual")
    _grid_meter_with_attributes(hass, owner=owner, unique_id="grid_meter_manual")
    hass.states.async_set("sensor.manual_l1", "1", {"unit_of_measurement": "A"})
    hass.states.async_set("sensor.manual_l2", "1", {"unit_of_measurement": "A"})
    hass.states.async_set("sensor.manual_l3", "1", {"unit_of_measurement": "A"})

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"choice": "manual"})
    assert result["step_id"] == "site_details"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "direct_L1": "sensor.manual_l1",
            "direct_L2": "sensor.manual_l2",
            "direct_L3": "sensor.manual_l3",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SITE_CURRENT_SOURCE] is None
    assert result["data"][CONF_DIRECT_ENTITIES] == {
        "L1": "sensor.manual_l1", "L2": "sensor.manual_l2", "L3": "sensor.manual_l3",
    }


async def test_skip_choice_configures_the_site_current_later(hass: HomeAssistant) -> None:
    owner = make_ocpp_config_entry(hass, entry_id="owner_grid_skip")
    _grid_meter_with_attributes(hass, owner=owner, unique_id="grid_meter_skip")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
        },
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"choice": "skip"})
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SITE_CURRENT_SOURCE] is None
    assert result["data"][CONF_DIRECT_ENTITIES] == {}


async def test_no_site_current_candidates_still_offer_the_suggestions_form(
    hass: HomeAssistant,
) -> None:
    """A site with nothing discoverable is exactly the case the manual choices exist for: the step
    is still shown, with both manual shapes and "skip" as its only options. Submitting the default
    lands on `site_details` with the guided entity pickers.
    """
    hass.states.async_set("sensor.plain_l1", "1", {"unit_of_measurement": "A"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
        },
    )

    assert result["step_id"] == "site_current_suggestions"
    schema = result["data_schema"]
    field = next(key for key in schema.schema if str(key) == "choice")
    options = schema.schema[field].config["options"]
    assert [option["value"] for option in options] == ["manual", "manual_attributes", "skip"]
    # Two different manual meanings, so two different human labels -- and never
    # a raw stored value as one.
    labels = {option["value"]: option["label"] for option in options}
    assert labels["manual"] not in ("manual",)
    assert labels["manual_attributes"] not in ("manual_attributes",)
    assert labels["manual"] != labels["manual_attributes"]
    default = field.default
    assert (default() if callable(default) else default) == "manual"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "direct_L1": "sensor.plain_l1",
            "direct_L2": "sensor.plain_l1",
            "direct_L3": "sensor.plain_l1",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SITE_CURRENT_SOURCE] is None
    assert result["data"][CONF_DIRECT_ENTITIES] == {
        "L1": "sensor.plain_l1",
        "L2": "sensor.plain_l1",
        "L3": "sensor.plain_l1",
    }


async def test_charger_measured_current_candidate_can_be_selected_in_site_details(
    hass: HomeAssistant,
) -> None:
    ocpp_owner = make_ocpp_config_entry(hass, entry_id="owner_charger_measured")
    switch_entity_id = create_ocpp_charger_device(
        hass, ocpp_entry=ocpp_owner, device_unique_id="charger_flow_measured",
        switch_object_id="charger_flow_measured",
        current_attributes={"L1": 6.0, "L2": 6.0, "L3": 6.0},
    )
    charger = make_entry(
        hass, entry_id="charger_flow_measured", charge_control=switch_entity_id,
        current_limit=None, webhook_id="webhook-flow-measured", title="Flow measured",
    )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [charger.entry_id],
        },
    )
    # No site-current candidate exists (the only current-like entity belongs
    # to the charger's own device, which site discovery must exclude) -- the
    # suggestions step is shown all the same, and its default ("manual")
    # reaches the guided pickers.
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_details"

    registry = er.async_get(hass)
    candidate_entity_id = registry.async_get_entity_id(
        "sensor", "ocpp", "charger_flow_measured_current"
    )
    assert candidate_entity_id is not None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "direct_L1": "sensor.unused_l1",
            "direct_L2": "sensor.unused_l2",
            "direct_L3": "sensor.unused_l3",
        },
    )
    assert result["step_id"] == "site_charger_wiring"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phases": 3, "measured_source": candidate_entity_id}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    wiring = result["data"][CONF_PHASE_WIRING][charger.entry_id]
    assert wiring[CONF_MEASURED_CURRENT_SOURCE]["entity_id"] == candidate_entity_id


async def test_options_flow_roundtrip_preserves_a_generic_site_current_source(
    hass: HomeAssistant,
) -> None:
    """Editing an already-discovery-configured site via the options flow
    must not force the user back through the manual entity pickers, and
    must not silently drop the generic source: the stored source's own
    candidate comes back preselected, and saving unchanged keeps it.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_opts_source")
    entity_id = _grid_meter_with_attributes(hass, owner=owner, unique_id="grid_meter_opts")
    source = {
        "kind": "attributes", "entity_id": entity_id, "entity_ids": None,
        "attributes": {"L1": "L1", "L2": "L2", "L3": "L3"},
        "attribute_unit_override": "A", "trust_entity_unit_for_attributes": False,
    }
    entry = make_site_entry(
        hass, entry_id="site_opts_source", charger_entry_ids=[],
        direct_entities={}, site_current_source=source,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "main_fuse_a": 25.0,
            "safety_margin_a": 1.0,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
            "site_enabled": False,
            "change_measurement": True,
            "max_age_s": 120.0,
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    # Still discoverable, so it is offered back as the candidate it already is
    # -- not as either manual shape.
    field = next(key for key in result["data_schema"].schema if str(key) == "choice")
    default = field.default
    assert (default() if callable(default) else default) == "sensor.grid_meter_opts"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    updated_entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated_entry.data[CONF_SITE_CURRENT_SOURCE] == source


async def test_site_current_suggestion_dropdown_shows_a_rich_label_not_just_the_raw_id(
    hass: HomeAssistant,
) -> None:
    owner = make_ocpp_config_entry(hass, entry_id="owner_label")
    entity_id = _grid_meter_with_attributes(hass, owner=owner, unique_id="grid_meter_label")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
        },
    )
    assert result["step_id"] == "site_current_suggestions"

    schema = result["data_schema"]
    field = next(key for key in schema.schema if str(key) == "choice")
    select_selector = schema.schema[field]
    options = select_selector.config["options"]

    candidate_option = next(o for o in options if o["value"] == entity_id)
    # The stable candidate ID is the value that gets saved; the label is
    # only for display, and must be much more informative than the ID.
    assert candidate_option["value"] != candidate_option["label"]
    assert entity_id in candidate_option["label"]
    assert "attributes" in candidate_option["label"]
    assert "L1=" in candidate_option["label"] and "L2=" in candidate_option["label"]
    assert "high confidence" in candidate_option["label"]

    manual_option = next(o for o in options if o["value"] == "manual")
    skip_option = next(o for o in options if o["value"] == "skip")
    assert manual_option["label"] != "manual"
    assert skip_option["label"] != "skip"


async def test_charger_measured_source_dropdown_also_shows_a_rich_label(
    hass: HomeAssistant,
) -> None:
    ocpp_owner = make_ocpp_config_entry(hass, entry_id="owner_charger_label")
    switch_entity_id = create_ocpp_charger_device(
        hass, ocpp_entry=ocpp_owner, device_unique_id="charger_flow_label",
        switch_object_id="charger_flow_label",
        current_attributes={"L1": 6.0, "L2": 6.0, "L3": 6.0},
    )
    charger = make_entry(
        hass, entry_id="charger_flow_label", charge_control=switch_entity_id,
        current_limit=None, webhook_id="webhook-flow-label", title="Flow label",
    )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [charger.entry_id],
        },
    )
    # No site-current candidate exists (the only current-like entity belongs
    # to the charger's own device, which site discovery excludes); the
    # suggestions step is still shown, and its default ("manual", the guided
    # per-phase pickers) leads on to `site_details` exactly as before.
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"direct_L1": "sensor.unused_l1", "direct_L2": "sensor.unused_l2", "direct_L3": "sensor.unused_l3"},
    )
    assert result["step_id"] == "site_charger_wiring"
    assert result["description_placeholders"]["charger"] == "Flow label"

    schema = result["data_schema"]
    field_name = "measured_source"
    field = next(key for key in schema.schema if str(key) == field_name)
    select_selector = schema.schema[field]
    options = select_selector.config["options"]

    registry = er.async_get(hass)
    candidate_entity_id = registry.async_get_entity_id("sensor", "ocpp", "charger_flow_label_current")
    candidate_option = next(o for o in options if o["value"] == candidate_entity_id)
    assert candidate_option["value"] != candidate_option["label"]
    assert candidate_entity_id in candidate_option["label"]
    assert "high confidence" in candidate_option["label"]

    skip_option = next(o for o in options if o["value"] == "skip")
    assert skip_option["label"] != "skip"
    # A charger's own measured current can now be entered manually as well --
    # one entity on that charger's own device, with explicit per-phase
    # attribute names (see tests/test_config_flow_manual_measured_source.py).
    manual_option = next(o for o in options if o["value"] == "manual")
    assert manual_option["label"] != "manual"
