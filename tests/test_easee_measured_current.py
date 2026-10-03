"""An Easee charger's measured current from the attributes of its `current` sensor (field case
2026-10-03: two Easee chargers on a SolaX site read as `charger_measurement: not_configured`).

easee_hass reports the input terminals as `state_inCurrentT2..T5`; on a TN network T3, T4, T5 are L1, L2,
L3 (T2 is the neutral), on an IT network T2, T3, T4 are.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    MEASUREMENT_MODE_DERIVED,
)
from custom_components.spotnav.execution.chargers.base import WRITE_REGULATOR
from custom_components.spotnav.execution.chargers.easee import EASEE_CONFIRM_AFTER_S
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.vehicles.discovery import (
    async_discover_charger_current_sources,
    REASON_ATTRIBUTES_PROFILE_MATCH,
)

from .charger_helpers import adapter_for, Clock, enable_easee_limit_sensor, set_easee_limit
from .charger_shapes import register_shape, SHAPES
from .helpers import make_entry, make_site_entry
from .test_charger_config_flow import _to_detected_entities
from .world import controller_of

TERMINALS = {
    "state_inCurrentT2": 0.02,
    "state_inCurrentT3": 5.0,
    "state_inCurrentT4": 6.0,
    "state_inCurrentT5": 7.0,
}
SOURCE_TN = {
    "kind": "attributes",
    "entity_ids": None,
    "entity_id": "sensor.easee_current",
    "attributes": {"L1": "state_inCurrentT3", "L2": "state_inCurrentT4", "L3": "state_inCurrentT5"},
    "attribute_unit_override": "A",
    "trust_entity_unit_for_attributes": False,
}


def _easee(hass: HomeAssistant, *, enable_current: bool = True, attributes: dict | None = None) -> dict:
    """An Easee device as easee_hass registers it: the `current` sensor is disabled by default."""
    ids = register_shape(hass, SHAPES["easee"])
    if enable_current:
        er.async_get(hass).async_update_entity("sensor.easee_current", disabled_by=None)
        hass.states.async_set(
            "sensor.easee_current",
            "7.0",
            {"unit_of_measurement": "A", "device_class": "current", **(TERMINALS if attributes is None else attributes)},
        )
    return ids


def _charger(hass: HomeAssistant, entry_id: str = "charger_easee"):
    return make_entry(
        hass,
        entry_id=entry_id,
        charge_control="sensor.easee_status",
        current_limit=None,
        webhook_id=f"hook-{entry_id}",
        title="Easee",
    )


def _site(hass: HomeAssistant, wiring: dict, **kwargs):
    return make_site_entry(
        hass,
        entry_id="site",
        charger_entry_ids=["charger_easee"],
        phase_wiring={"charger_easee": wiring},
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        **kwargs,
    )


# ------------------------------------------------------------------------- the terminal mapping


async def test_tn_reads_t3_t4_t5_as_l1_l2_l3(hass: HomeAssistant) -> None:
    ids = _easee(hass)

    (candidate,) = await async_discover_charger_current_sources(hass, charger_device_id=ids["device_id"])

    assert candidate.reason_code == REASON_ATTRIBUTES_PROFILE_MATCH and candidate.confidence == "high"
    assert candidate.mapping.kind == "attributes" and candidate.mapping.entity_id == "sensor.easee_current"
    assert dict(candidate.mapping.attributes) == {
        "L1": "state_inCurrentT3",
        "L2": "state_inCurrentT4",
        "L3": "state_inCurrentT5",
    }
    assert candidate.mapping.attribute_unit_override == "A"


async def test_it_reads_t2_t3_t4_as_l1_l2_l3(hass: HomeAssistant) -> None:
    ids = _easee(hass)

    (candidate,) = await async_discover_charger_current_sources(
        hass, charger_device_id=ids["device_id"], voltage_between_phases_v=230.0
    )

    assert dict(candidate.mapping.attributes) == {
        "L1": "state_inCurrentT2",
        "L2": "state_inCurrentT3",
        "L3": "state_inCurrentT4",
    }


async def test_a_disabled_current_sensor_offers_nothing(hass: HomeAssistant) -> None:
    ids = _easee(hass, enable_current=False)

    assert await async_discover_charger_current_sources(hass, charger_device_id=ids["device_id"]) == []


# ------------------------------------------------------------------------------ joining a site


async def test_a_new_easee_charger_joins_the_site_with_the_attribute_source(hass: HomeAssistant) -> None:
    make_site_entry(hass, entry_id="site", title="Home", measurement_mode=MEASUREMENT_MODE_DERIVED)
    ids = _easee(hass)

    result = await _to_detected_entities(hass, ids["device_id"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGE_CONTROL: "sensor.easee_status", CONF_CURRENT_CONTROL: "easee_dynamic_limit"}
    )
    assert result["step_id"] == "join_site"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    charger_id = result["result"].entry_id
    wiring = hass.config_entries.async_get_entry("site").data["phase_wiring"][charger_id]
    assert wiring["phases"] == 3 and wiring["measured_current_source"] == SOURCE_TN


# ------------------------------------------------------------- an existing site is filled in


async def test_an_existing_site_gets_the_source_once_and_logs_it_once(hass: HomeAssistant, caplog) -> None:
    _easee(hass)
    _charger(hass)
    site = _site(hass, {"phases": 3, "phase": None})

    with caplog.at_level(logging.INFO):
        assert await hass.config_entries.async_setup(site.entry_id)
        await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry("site").data["phase_wiring"]["charger_easee"] == {
        "phases": 3,
        "phase": None,
        "measured_current_source": SOURCE_TN,
    }
    assert sum("now reads its measured current" in record.message for record in caplog.records) == 1

    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert await hass.config_entries.async_reload(site.entry_id)
        await hass.async_block_till_done()
    assert not [record for record in caplog.records if "now reads its measured current" in record.message]


async def test_the_fill_in_follows_the_sites_it_voltage(hass: HomeAssistant) -> None:
    _easee(hass)
    _charger(hass)
    site = _site(hass, {"phases": 3, "phase": None}, extra_data={"voltage_between_phases_v": 230})

    assert await hass.config_entries.async_setup(site.entry_id)
    source = hass.config_entries.async_get_entry("site").data["phase_wiring"]["charger_easee"]["measured_current_source"]

    assert source["attributes"] == {"L1": "state_inCurrentT2", "L2": "state_inCurrentT3", "L3": "state_inCurrentT4"}


async def test_a_one_phase_charger_is_read_from_its_first_terminal_only(hass: HomeAssistant) -> None:
    _easee(hass)
    _charger(hass)
    site = _site(hass, {"phases": 1, "phase": "L2"})

    assert await hass.config_entries.async_setup(site.entry_id)
    source = hass.config_entries.async_get_entry("site").data["phase_wiring"]["charger_easee"]["measured_current_source"]

    assert source["attributes"] == {"L2": "state_inCurrentT3"}


async def test_what_a_person_set_is_kept(hass: HomeAssistant) -> None:
    _easee(hass)
    _charger(hass)
    mine = {
        "kind": "attributes",
        "entity_id": "sensor.my_own",
        "attributes": {"L1": "a", "L2": "b", "L3": "c"},
        "attribute_unit_override": "A",
    }
    site = _site(hass, {"phases": 3, "phase": None, "measured_current_source": mine})

    assert await hass.config_entries.async_setup(site.entry_id)

    assert hass.config_entries.async_get_entry("site").data["phase_wiring"]["charger_easee"]["measured_current_source"] == mine


async def test_a_source_removed_on_purpose_is_not_filled_in_again(hass: HomeAssistant) -> None:
    _easee(hass)
    _charger(hass)
    site = _site(hass, {"phases": 3, "phase": None, "measured_source_declined": True})

    assert await hass.config_entries.async_setup(site.entry_id)

    assert "measured_current_source" not in hass.config_entries.async_get_entry("site").data["phase_wiring"]["charger_easee"]


async def test_nothing_is_filled_in_while_the_current_sensor_is_disabled(hass: HomeAssistant) -> None:
    _easee(hass, enable_current=False)
    _charger(hass)
    site = _site(hass, {"phases": 3, "phase": None})

    assert await hass.config_entries.async_setup(site.entry_id)

    assert hass.config_entries.async_get_entry("site").data["phase_wiring"]["charger_easee"] == {"phases": 3, "phase": None}


# ---------------------------------------------------------------------- a healthy measurement


async def test_the_filled_in_charger_measurement_is_healthy_and_read_per_phase(hass: HomeAssistant) -> None:
    _easee(hass)
    _charger(hass)
    site = _site(hass, {"phases": 3, "phase": None})
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, site.entry_id)
    measured = controller.charger_measured_current("charger_easee")

    assert [measured.get(phase).value for phase in ("L1", "L2", "L3")] == [5.0, 6.0, 7.0]
    assert controller.capability_snapshot.charger_measurement["charger_easee"].health == "healthy"


# ------------------------------------------------------------ write against read-back (field case)


async def test_the_read_back_is_the_chargers_own_dynamic_limit_and_a_lag_is_not_yet_suspect(
    hass: HomeAssistant,
) -> None:
    """`dynamic_charger_limit` is `state.dynamicChargerCurrent` of the charger itself, the very field
    `set_charger_dynamic_limit` compares and sets, so a different value is the cloud not having reported
    back yet. Within `EASEE_CONFIRM_AFTER_S` that is lag; after it the cache is suspect, which the
    diagnostics show as it stands now (not as of the last write)."""
    ids = register_shape(hass, SHAPES["easee"])
    clock = Clock()
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=clock)
    async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    enable_easee_limit_sensor(hass, "32")

    await adapter.async_set_current(16, reason=WRITE_REGULATOR)
    state = adapter.current.describe_state()
    assert (state["last_written_a"], state["read_back_a"], state["cache_suspect"]) == (16, 32, False)

    clock.advance(EASEE_CONFIRM_AFTER_S + 1)
    assert adapter.current.describe_state()["cache_suspect"] is True

    set_easee_limit(hass, "16")
    assert adapter.current.describe_state()["cache_suspect"] is False


async def test_a_suspect_write_sends_one_amp_lower_first_so_the_service_cannot_skip_it(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    clock = Clock()
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=clock)
    limits = async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    enable_easee_limit_sensor(hass, "32")
    await adapter.async_set_current(16, reason=WRITE_REGULATOR)
    clock.advance(EASEE_CONFIRM_AFTER_S + 60)

    await adapter.async_set_current(16, reason=WRITE_REGULATOR)

    assert [call.data["current"] for call in limits] == [16, 15, 16]
