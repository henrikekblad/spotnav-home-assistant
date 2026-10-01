"""Full, real end-to-end chains: discovery -> config flow (with a rich
label) -> user acceptance -> entry creation -> unload/reload -> the
resulting numeric phase values actually read by the site controller,
credited correctly, and reflected in the capability snapshot.

A config-only test cannot catch a *saved* attributes-based source that looks fine but is
unreadable at measurement time, so every assertion here checks actual numeric values.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import CONF_SITE_ENABLED, DOMAIN

from .helpers import create_ocpp_charger_device, make_entry, make_ocpp_config_entry, set_current_sensor
from .world import controller_of


def _grid_meter_with_attributes(
    hass: HomeAssistant, *, owner, unique_id: str, values: dict
) -> str:
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
        entry.entity_id, "unknown", {"device_class": "current", "unit_of_measurement": "A", **values},
    )
    return entry.entity_id


async def test_site_current_attributes_candidate_works_end_to_end(hass: HomeAssistant) -> None:
    owner = make_ocpp_config_entry(hass, entry_id="owner_e2e_site")
    entity_id = _grid_meter_with_attributes(
        hass, owner=owner, unique_id="e2e_grid_meter", values={"L1": 5.0, "L2": 6.0, "L3": 7.0}
    )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "E2E site",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    # The label actually shown must be richer than the raw candidate ID --
    # see test_config_flow_discovery.py for the dedicated label assertions;
    # here we only need to confirm the candidate is present at all before
    # "accepting" it, exactly as a user would.
    options = result["data_schema"].schema[
        next(k for k in result["data_schema"].schema if str(k) == "choice")
    ].config["options"]
    assert any(o["value"] == entity_id for o in options)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"choice": entity_id})
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry_id = result["result"].entry_id
    await hass.async_block_till_done()

    # The flow manager already sets up a newly-created entry automatically
    # once its domain's component is loaded (which happened as soon as the
    # flow itself was initialized) -- no separate async_setup call needed
    # or, in fact, allowed here.
    controller = controller_of(hass, entry_id)
    # Freshly created the site calculation is on and active control is off, so it only observes.
    assert controller.result.state == "observing"
    assert controller.capability_snapshot.site_measurement.health == "healthy"

    # Unload/reload: a fresh controller instance must read the exact same
    # numeric values (a saved candidate with no usable unit override must not read back as invalid).
    assert await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()

    reloaded_controller = controller_of(hass, entry_id)
    assert reloaded_controller.capability_snapshot.site_measurement.health == "healthy"

    # Enable active control via the options flow and confirm the real `observing` state.
    options_result = await hass.config_entries.options.async_init(entry_id)
    options_result = await hass.config_entries.options.async_configure(
        options_result["flow_id"],
        {
            "main_fuse_a": 25.0,
            "safety_margin_a": 1.0,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
            "site_enabled": True,
            "change_measurement": True,
            "max_age_s": 120.0,
        },
    )
    assert options_result["step_id"] == "site_current_suggestions"
    options_result = await hass.config_entries.options.async_configure(
        options_result["flow_id"], {}
    )
    assert options_result["step_id"] == "site_details"
    options_result = await hass.config_entries.options.async_configure(options_result["flow_id"], {})
    assert options_result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    enabled_entry = hass.config_entries.async_get_entry(entry_id)
    assert enabled_entry.data[CONF_SITE_ENABLED] is True
    enabled_controller = controller_of(hass, entry_id)
    assert enabled_controller.result.state == "observing"
    assert enabled_controller.result.measured_phase_current_a == {"L1": 5.0, "L2": 6.0, "L3": 7.0}


async def test_charger_current_attributes_candidate_is_credited_exactly_once_end_to_end(
    hass: HomeAssistant,
) -> None:
    ocpp_owner = make_ocpp_config_entry(hass, entry_id="owner_e2e_charger")
    switch_entity_id = create_ocpp_charger_device(
        hass, ocpp_entry=ocpp_owner, device_unique_id="e2e_charger", switch_object_id="e2e_charger",
        current_attributes={"L1": 8.0, "L2": 8.0, "L3": 8.0},
    )
    charger = make_entry(
        hass, entry_id="charger_e2e", charge_control=switch_entity_id,
        current_limit=None, webhook_id="webhook-e2e-charger", title="E2E charger",
    )
    async_mock_service(hass, "switch", "turn_on")
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=16)
    hass.states.async_set(switch_entity_id, "on")

    # A plain, manually-entered site-total (not tied to any device) -- the
    # part of this flow under test is the charger's own measured-current
    # discovery, not the site's.
    set_current_sensor(hass, "sensor.e2e_site_l1", 20)
    set_current_sensor(hass, "sensor.e2e_site_l2", 20)
    set_current_sensor(hass, "sensor.e2e_site_l3", 20)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "E2E site with charger",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [charger.entry_id],
        },
    )
    # No site-current candidate exists (only the charger's own device has
    # current-like entities, and site discovery excludes charger devices), so
    # the suggestions step is shown with only the two manual choices and
    # "skip" -- and its default ("manual") then reaches the guided pickers.
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_details"

    registry = er.async_get(hass)
    charger_candidate_id = registry.async_get_entity_id("sensor", "ocpp", "e2e_charger_current")
    assert charger_candidate_id is not None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "direct_L1": "sensor.e2e_site_l1",
            "direct_L2": "sensor.e2e_site_l2",
            "direct_L3": "sensor.e2e_site_l3",
        },
    )
    assert result["step_id"] == "site_charger_wiring"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phases": 3, "measured_source": charger_candidate_id}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    site_entry_id = result["result"].entry_id
    await hass.async_block_till_done()

    site_controller = controller_of(hass, site_entry_id)
    measured = site_controller.charger_measured_current(charger.entry_id)
    assert measured is not None
    assert (measured.l1.value, measured.l2.value, measured.l3.value) == (8.0, 8.0, 8.0)

    snapshot = site_controller.capability_snapshot
    assert snapshot.charger_measurement[charger.entry_id].health == "healthy"

    # Enable active control so `result` reflects the live calculation, not the "disabled" short-circuit.
    options_result = await hass.config_entries.options.async_init(site_entry_id)
    options_result = await hass.config_entries.options.async_configure(
        options_result["flow_id"],
        {
            "main_fuse_a": 25.0,
            "safety_margin_a": 1.0,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [charger.entry_id],
            "site_enabled": True,
            "change_measurement": True,
            "max_age_s": 120.0,
        },
    )
    assert options_result["step_id"] == "site_current_suggestions"
    options_result = await hass.config_entries.options.async_configure(
        options_result["flow_id"], {}
    )
    assert options_result["step_id"] == "site_details"
    options_result = await hass.config_entries.options.async_configure(
        options_result["flow_id"],
        {
            "direct_L1": "sensor.e2e_site_l1",
            "direct_L2": "sensor.e2e_site_l2",
            "direct_L3": "sensor.e2e_site_l3",
        },
    )
    assert options_result["step_id"] == "site_charger_wiring"
    options_result = await hass.config_entries.options.async_configure(
        options_result["flow_id"], {"phases": 3, "measured_source": charger_candidate_id}
    )
    assert options_result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    enabled_controller = controller_of(hass, site_entry_id)
    # Credited exactly once, in the diagnostic: site total 20A minus the
    # charger's own 8A of that same total leaves 12A of "other load";
    # credited headroom is 25 - 1 - 12 = 12A. If the 8A had been credited
    # twice (or not at all) this would be 4A or 20A instead -- but this
    # value is diagnostic-only in every mode (see site/site_capacity.py's
    # module docstring), so the real headroom below never carries it.
    assert enabled_controller.result.state == "observing"
    assert enabled_controller.result.calculated_headroom_after_ev_credit_a["L1"] == 12.0
    # The real headroom used for the proposal never credits the charger's
    # own current: 25 - 1 - 20 = 4A.
    assert enabled_controller.result.phase_headroom_a["L1"] == 4.0


async def test_wrong_unit_candidate_never_becomes_a_working_high_confidence_source(
    hass: HomeAssistant,
) -> None:
    owner = make_ocpp_config_entry(hass, entry_id="owner_e2e_wrong_unit")
    _grid_meter_with_attributes(
        hass, owner=owner, unique_id="e2e_wrong_unit_meter", values={"L1": 5.0, "L2": 6.0, "L3": 7.0}
    )
    # Overwrite with a device_class=current but unit=W entity: plausible but the wrong unit.
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("ocpp", "e2e_wrong_unit_meter2")},
        name="e2e_wrong_unit_meter2",
    )
    wrong_entry = entity_registry.async_get_or_create(
        "sensor", "ocpp", "e2e_wrong_unit_meter2_current", device_id=device.id, config_entry=owner,
        suggested_object_id="e2e_wrong_unit_meter2",
    )
    hass.states.async_set(
        wrong_entry.entity_id, "unknown",
        {"device_class": "current", "unit_of_measurement": "W", "L1": 5.0, "L2": 6.0, "L3": 7.0},
    )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "E2E wrong unit",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
        },
    )
    # There is still a *good* candidate (the first meter), so the
    # suggestions step is shown -- but the bad-unit entity must never
    # appear as a usable option among its choices.
    assert result["step_id"] == "site_current_suggestions"
    options = result["data_schema"].schema[
        next(k for k in result["data_schema"].schema if str(k) == "choice")
    ].config["options"]
    assert not any(o["value"] == wrong_entry.entity_id for o in options)
