"""Tests for advisory phase-count detection and its dashboard fields."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.site.phase_detection import (
    async_detect_phases,
    explicit_phase_count,
    present_phase_keys,
)

from .helpers import create_ocpp_charger_device, make_entry, make_ocpp_config_entry, webhook_dashboard

# --- Pure attribute-analysis helpers -----------------------------------


def test_present_phase_keys_matches_l1_l2_l3_case_insensitively() -> None:
    assert present_phase_keys({"L1": 230, "L2": 231, "L3": 229}) == {"l1", "l2", "l3"}
    assert present_phase_keys({"l1": 0, "l2": 0, "l3": 0}) == {"l1", "l2", "l3"}
    assert present_phase_keys({"L1": 230}) == {"l1"}


def test_present_phase_keys_ignores_unexpected_formats() -> None:
    # Case 10: unfamiliar attribute spellings and shapes must never be
    # guessed at as phase data.
    assert present_phase_keys({"l1_voltage": 230, "phase_a": 231}) == set()
    assert present_phase_keys({"voltage": 230, "unit_of_measurement": "V"}) == set()
    assert present_phase_keys("not-a-dict") == set()
    assert present_phase_keys(None) == set()
    assert present_phase_keys({1: "l1", "L2": 230}) == {"l2"}  # non-string key ignored


def test_explicit_phase_count_accepts_only_a_plain_integer_one_two_or_three() -> None:
    assert explicit_phase_count({"phases": 3}) == 3
    assert explicit_phase_count({"phases": 2}) == 2
    assert explicit_phase_count({"phases": 1}) == 1
    assert explicit_phase_count({"phases": 4}) is None
    assert explicit_phase_count({"phases": True}) is None  # bool is not a phase count
    assert explicit_phase_count({"phases": "3"}) is None  # no guessing from strings
    assert explicit_phase_count({}) is None
    assert explicit_phase_count(None) is None


# --- Detection order, via the registry-aware function -------------------


async def test_voltage_l1_l2_l3_detects_three_phases_high_confidence(
    hass: HomeAssistant,
) -> None:
    """Case 1."""
    ocpp_entry = make_ocpp_config_entry(hass, entry_id="ocpp_1")
    switch_id = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_entry,
        device_unique_id="charger_v3",
        switch_object_id="charger_v3",
        voltage_attributes={"L1": 230, "L2": 231, "L3": 229},
    )

    result = async_detect_phases(hass, switch_id)

    assert result.detected_phases == 3
    assert result.source == "voltage_attributes"
    assert result.confidence == "high"


async def test_current_l1_l2_l3_detects_three_phases_medium_confidence(
    hass: HomeAssistant,
) -> None:
    """Case 2. Documented decision: current-based three-phase evidence is one
    step less direct than voltage-based, so it is reported as `medium`
    confidence rather than `high` (see site/phase_detection.py's module comment).
    """
    ocpp_entry = make_ocpp_config_entry(hass, entry_id="ocpp_2")
    switch_id = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_entry,
        device_unique_id="charger_c3",
        switch_object_id="charger_c3",
        current_attributes={"L1": 6, "L2": 6, "L3": 6},
    )

    result = async_detect_phases(hass, switch_id)

    assert result.detected_phases == 3
    assert result.source == "current_attributes"
    assert result.confidence == "medium"


async def test_l1_only_detects_one_phase_low_confidence(hass: HomeAssistant) -> None:
    """Case 3."""
    ocpp_entry = make_ocpp_config_entry(hass, entry_id="ocpp_3")
    switch_id = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_entry,
        device_unique_id="charger_l1",
        switch_object_id="charger_l1",
        voltage_attributes={"L1": 230},
    )

    result = async_detect_phases(hass, switch_id)

    assert result.detected_phases == 1
    assert result.source == "l1_only"
    assert result.confidence == "low"


async def test_no_useful_attributes_is_unknown(hass: HomeAssistant) -> None:
    """Case 4."""
    ocpp_entry = make_ocpp_config_entry(hass, entry_id="ocpp_4")
    switch_id = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_entry,
        device_unique_id="charger_none",
        switch_object_id="charger_none",
        voltage_attributes={"voltage": 230},  # present, but not a phase key
    )

    result = async_detect_phases(hass, switch_id)

    assert result.detected_phases is None
    assert result.source == "unknown"
    assert result.confidence == "none"


async def test_zero_values_on_all_three_phases_still_detect_three_phases(
    hass: HomeAssistant,
) -> None:
    """Case 7: detection is by which keys exist, not their measured values —
    an idle charger reporting 0 A/0 V on every line is still wired for three
    phases.
    """
    ocpp_entry = make_ocpp_config_entry(hass, entry_id="ocpp_7")
    switch_id = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_entry,
        device_unique_id="charger_zero",
        switch_object_id="charger_zero",
        voltage_attributes={"L1": 0, "L2": 0, "L3": 0},
    )

    result = async_detect_phases(hass, switch_id)

    assert result.detected_phases == 3
    assert result.source == "voltage_attributes"
    assert result.confidence == "high"


async def test_three_phase_sensor_wins_over_earlier_l1_only_sensor(
    hass: HomeAssistant,
) -> None:
    """Registry order must not let weaker evidence hide a later full sensor."""
    ocpp_entry = make_ocpp_config_entry(hass, entry_id="ocpp_multiple_voltage")
    switch_id = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_entry,
        device_unique_id="charger_multiple_voltage",
        switch_object_id="charger_multiple_voltage",
        voltage_attributes={"L1": 230},
    )
    entity_registry = er.async_get(hass)
    control_entry = entity_registry.async_get(switch_id)
    full_voltage = entity_registry.async_get_or_create(
        "sensor",
        "ocpp",
        "charger_multiple_voltage_full",
        device_id=control_entry.device_id,
        config_entry=ocpp_entry,
        suggested_object_id="charger_multiple_voltage_full",
    )
    hass.states.async_set(
        full_voltage.entity_id,
        "230",
        {
            "device_class": "voltage",
            "unit_of_measurement": "V",
            "L1": 230,
            "L2": 231,
            "L3": 229,
        },
    )

    result = async_detect_phases(hass, switch_id)

    assert result.detected_phases == 3
    assert result.source == "voltage_attributes"
    assert result.confidence == "high"


async def test_explicit_metadata_on_the_charge_control_entity_wins(
    hass: HomeAssistant,
) -> None:
    """Detection order step 1, directly on the configured entity's own state."""
    hass.states.async_set("switch.explicit_charger", "off", {"phases": 1})

    result = async_detect_phases(hass, "switch.explicit_charger")

    assert result.detected_phases == 1
    assert result.source == "explicit_metadata"
    assert result.confidence == "high"


async def test_explicit_metadata_on_a_sibling_device_entity_wins_over_sensors(
    hass: HomeAssistant,
) -> None:
    """Explicit metadata is preferred even when voltage attributes on another
    entity of the same device would otherwise suggest three phases.
    """
    ocpp_entry = make_ocpp_config_entry(hass, entry_id="ocpp_explicit")
    switch_id = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_entry,
        device_unique_id="charger_explicit",
        switch_object_id="charger_explicit",
        voltage_attributes={"L1": 230, "L2": 231, "L3": 229},
    )
    hass.states.async_set(
        "sensor.charger_explicit_voltage",
        "230",
        {"device_class": "voltage", "L1": 230, "L2": 231, "L3": 229, "phases": 1},
    )

    result = async_detect_phases(hass, switch_id)

    assert result.detected_phases == 1
    assert result.source == "explicit_metadata"
    assert result.confidence == "high"


async def test_unregistered_entity_never_falls_back_to_a_global_scan(
    hass: HomeAssistant,
) -> None:
    """No entity_registry entry means no device link, so detection must not
    widen its search and risk reading an unrelated sensor.
    """
    hass.states.async_set("switch.floating_charger", "off")
    hass.states.async_set(
        "sensor.unrelated_voltage", "230", {"device_class": "voltage", "L1": 230, "L2": 231, "L3": 229}
    )

    result = async_detect_phases(hass, "switch.floating_charger")

    assert result.detected_phases is None
    assert result.source == "unknown"
    assert result.confidence == "none"


# --- Cross-charger isolation, via full webhook round trips --------------


async def test_two_devices_produce_different_results_without_leakage(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Case 5: two config entries on two different devices must each report
    their own detection result through their own webhook.
    """
    ocpp_entry = make_ocpp_config_entry(hass, entry_id="ocpp_isolation")
    switch_a = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_entry,
        device_unique_id="device_a",
        switch_object_id="isolation_charger_a",
        voltage_attributes={"L1": 230, "L2": 231, "L3": 229},
    )
    switch_b = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_entry,
        device_unique_id="device_b",
        switch_object_id="isolation_charger_b",
        voltage_attributes={"L1": 230},
    )

    entry_a = make_entry(
        hass,
        entry_id="entry_isolation_a",
        charge_control=switch_a,
        current_limit=None,
        webhook_id="webhook-isolation-a",
        title="Isolation A",
    )
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()

    entry_b = make_entry(
        hass,
        entry_id="entry_isolation_b",
        charge_control=switch_b,
        current_limit=None,
        webhook_id="webhook-isolation-b",
        title="Isolation B",
    )
    assert await hass.config_entries.async_setup(entry_b.entry_id)
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    status_a = await webhook_dashboard(client, "webhook-isolation-a")
    status_b = await webhook_dashboard(client, "webhook-isolation-b")

    assert status_a["detected_phases"] == 3
    assert status_a["phase_detection"] == {"source": "voltage_attributes", "confidence": "high"}
    assert status_b["detected_phases"] == 1
    assert status_b["phase_detection"] == {"source": "l1_only", "confidence": "low"}


async def test_charger_b_three_phase_sensor_never_changes_charger_as_result(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Case 6: charger B having strong three-phase evidence must never leak
    into charger A's own (weaker) result.
    """
    ocpp_entry = make_ocpp_config_entry(hass, entry_id="ocpp_no_leak")
    switch_a = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_entry,
        device_unique_id="no_leak_device_a",
        switch_object_id="no_leak_charger_a",
        voltage_attributes={"L1": 230},  # A: one phase only
    )
    switch_b = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_entry,
        device_unique_id="no_leak_device_b",
        switch_object_id="no_leak_charger_b",
        voltage_attributes={"L1": 230, "L2": 231, "L3": 229},  # B: three phases
    )

    entry_a = make_entry(
        hass,
        entry_id="entry_no_leak_a",
        charge_control=switch_a,
        current_limit=None,
        webhook_id="webhook-no-leak-a",
        title="No leak A",
    )
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()

    # entry_b intentionally not set up: charger A's result must not depend
    # on whether a second config entry for charger B even exists.
    result_a = async_detect_phases(hass, switch_a)
    assert result_a.detected_phases == 1
    assert result_a.source == "l1_only"

    client = await hass_client_no_auth()
    status_a = await webhook_dashboard(client, "webhook-no-leak-a")
    assert status_a["detected_phases"] == 1
    assert status_a["phase_detection"]["source"] == "l1_only"

    # switch_b is unused here beyond confirming it exists on a separate
    # device; referencing it keeps the "two devices" setup explicit.
    assert switch_b != switch_a


# --- Existing behavior stays intact --------------------------------------


async def test_generic_non_ocpp_charger_dashboard_still_works(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Case 9: a generic charger whose switch has no entity_registry entry
    at all (no integration link, no device) must still answer the dashboard
    successfully, just with an unknown phase result.
    """
    hass.states.async_set("switch.generic_charger", "off")
    entry = make_entry(
        hass,
        entry_id="entry_generic",
        charge_control="switch.generic_charger",
        current_limit=None,
        webhook_id="webhook-generic",
        title="Generic charger",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    status = await webhook_dashboard(client, "webhook-generic")

    assert status["ok"] is True
    assert status["detected_phases"] is None
    assert status["phase_detection"] == {"source": "unknown", "confidence": "none"}
    # The rest of the contract must be entirely unaffected by phase detection.
    assert status["live"]["charging"] is False
    assert status["charger"]["available"] is True
