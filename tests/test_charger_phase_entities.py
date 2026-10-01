"""A charger's measured current as one entity per phase (Charge Amps and the like), and the unit check
on manually picked site sensors that state no device class."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.spotnav.const import CONF_MEASURED_CURRENT_SOURCE, CONF_PHASE_WIRING, DOMAIN
from custom_components.spotnav.vehicles.discovery import (
    async_discover_charger_current_sources,
    REASON_SEPARATE_ENTITIES_PROFILE_MATCH,
)

from .helpers import make_entry, make_site_entry
from .world import controller_of

PHASES = ("L1", "L2", "L3")


def _chargeamps(hass: HomeAssistant, name: str = "ca1", values: tuple[float, ...] = (3.0, 4.0, 5.0)):
    """A Charge Amps device: its switch and one current sensor per phase, named so that only the
    platform profile (not the name-based scan) can tell the phase."""
    config = MockConfigEntry(domain="chargeamps", entry_id=f"owner_{name}")
    config.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config.entry_id, identifiers={("chargeamps", name)}, name=name
    )
    registry = er.async_get(hass)
    switch = registry.async_get_or_create(
        "switch", "chargeamps", f"{name}_enable", device_id=device.id, config_entry=config,
        suggested_object_id=name,
    )
    hass.states.async_set(switch.entity_id, "off")
    sensors = []
    for index, value in enumerate(values, start=1):
        entry = registry.async_get_or_create(
            "sensor", "chargeamps", f"{name}_l{index}_current", device_id=device.id,
            config_entry=config, suggested_object_id=f"{name}_l{index}_current",
        )
        hass.states.async_set(
            entry.entity_id, str(value), {"device_class": "current", "unit_of_measurement": "A"}
        )
        sensors.append(entry.entity_id)
    charger = make_entry(
        hass, entry_id=f"charger_{name}", charge_control=switch.entity_id, current_limit=None,
        webhook_id=f"hook-{name}", title=f"Charger {name}",
    )
    return charger, device.id, sensors


async def _to_wiring(hass: HomeAssistant, charger_id: str) -> dict[str, Any]:
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await flow.async_configure(
        result["flow_id"],
        {"name": "Home", "main_fuse_a": 25, "safety_margin_a": 1,
         "measurement_mode": "direct_phase_current", "charger_entry_ids": [charger_id]},
    )
    result = await flow.async_configure(result["flow_id"], {"choice": "manual"})
    assert result["step_id"] == "site_details"
    result = await flow.async_configure(
        result["flow_id"], {"direct_L1": "sensor.g1", "direct_L2": "sensor.g2", "direct_L3": "sensor.g3"}
    )
    assert result["step_id"] == "site_charger_wiring"
    return result


async def test_chargeamps_discovery_offers_its_own_per_phase_entities(hass: HomeAssistant) -> None:
    _, device_id, sensors = _chargeamps(hass)

    candidates = await async_discover_charger_current_sources(hass, charger_device_id=device_id)

    assert len(candidates) == 1
    assert candidates[0].reason_code == REASON_SEPARATE_ENTITIES_PROFILE_MATCH
    assert candidates[0].mapping.kind == "separate_entities"
    assert dict(candidates[0].mapping.entity_ids) == dict(zip(PHASES, sensors))
    assert candidates[0].confidence == "high"


async def test_the_chargeamps_candidate_is_selectable_in_the_wiring_step(hass: HomeAssistant) -> None:
    charger, _, sensors = _chargeamps(hass)
    result = await _to_wiring(hass, charger.entry_id)

    options = result["data_schema"].schema[
        next(key for key in result["data_schema"].schema if str(key) == "measured_source")
    ].config["options"]
    values = [option["value"] for option in options]
    assert "|".join(sorted(sensors)) in values
    assert {"manual", "manual_entities", "skip"} <= set(values)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phases": 3, "measured_source": "|".join(sorted(sensors))}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    stored = result["data"][CONF_PHASE_WIRING][charger.entry_id][CONF_MEASURED_CURRENT_SOURCE]
    assert stored["kind"] == "separate_entities" and stored["entity_ids"] == dict(zip(PHASES, sensors))


async def test_three_separate_entities_are_stored_and_read_at_runtime(hass: HomeAssistant) -> None:
    charger, _, sensors = _chargeamps(hass)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    result = await _to_wiring(hass, charger.entry_id)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phases": 3, "measured_source": "manual_entities"}
    )
    assert result["step_id"] == "charger_manual_entities"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {f"entity_{phase}": sensor for phase, sensor in zip(PHASES, sensors)}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    stored = result["data"][CONF_PHASE_WIRING][charger.entry_id][CONF_MEASURED_CURRENT_SOURCE]
    assert stored["kind"] == "separate_entities"
    assert stored["entity_ids"] == dict(zip(PHASES, sensors))
    await hass.async_block_till_done()
    measured = controller_of(hass, result["result"].entry_id).charger_measured_current(charger.entry_id)
    assert measured is not None
    assert (measured.l1.value, measured.l2.value, measured.l3.value) == (3.0, 4.0, 5.0)


async def test_the_manual_entities_step_checks_device_unit_and_distinctness(hass: HomeAssistant) -> None:
    charger, _, sensors = _chargeamps(hass)
    _, _, other = _chargeamps(hass, name="ca2")
    hass.states.async_set(sensors[1], "4", {"unit_of_measurement": "W"})
    hass.states.async_set(sensors[2], "5", {"unit_of_measurement": "mA"})
    result = await _to_wiring(hass, charger.entry_id)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phases": 3, "measured_source": "manual_entities"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity_L1": other[0], "entity_L2": sensors[1], "entity_L3": sensors[2]}
    )
    assert result["step_id"] == "charger_manual_entities"
    assert result["errors"] == {
        "entity_L1": "manual_source_entity_wrong_device",
        "entity_L2": "unit_expected_current",
    }

    hass.states.async_set(sensors[1], "4", {"unit_of_measurement": "A"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity_L1": sensors[0], "entity_L2": sensors[0], "entity_L3": sensors[2]}
    )
    assert result["errors"] == {
        "entity_L1": "manual_entities_duplicate",
        "entity_L2": "manual_entities_duplicate",
    }

    # Milliamperes are accepted, and the form pre-fills what was submitted.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {f"entity_{phase}": sensor for phase, sensor in zip(PHASES, sensors)}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_an_existing_separate_entities_source_comes_back_as_manual_entities(
    hass: HomeAssistant,
) -> None:
    charger, _, sensors = _chargeamps(hass)
    stored = {"kind": "separate_entities", "entity_ids": dict(zip(PHASES, sensors)), "entity_id": None,
              "attributes": None, "attribute_unit_override": None, "trust_entity_unit_for_attributes": False}
    # Discovery would match this one; make it differ so only the stored shape decides.
    stored["entity_ids"] = {"L1": sensors[0], "L2": sensors[1], "L3": sensors[1]}
    site = make_site_entry(
        hass, entry_id="site_pf", charger_entry_ids=[charger.entry_id],
        phase_wiring={charger.entry_id: {"phases": 3, "phase": None, CONF_MEASURED_CURRENT_SOURCE: stored}},
    )
    result = await hass.config_entries.options.async_init(site.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"main_fuse_a": 25.0, "safety_margin_a": 1.0, "measurement_mode": "direct_phase_current",
         "charger_entry_ids": [charger.entry_id], "site_enabled": True, "change_measurement": True},
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"choice": "manual"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"direct_L1": "sensor.g1", "direct_L2": "sensor.g2", "direct_L3": "sensor.g3"}
    )
    schema = result["data_schema"]
    marker = next(key for key in schema.schema if str(key) == "measured_source")
    assert marker.default() == "manual_entities"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "charger_manual_entities"
    entity_l2 = next(key for key in result["data_schema"].schema if str(key) == "entity_L2")
    assert entity_l2.description["suggested_value"] == sensors[1]


# --- Site pickers: any sensor, the unit decides ------------------------------------------------


def _bare_sensor(hass: HomeAssistant, entity_id: str, unit: str | None) -> str:
    """A sensor without a device class, like a DIY ESPHome P1 reader."""
    attributes = {"unit_of_measurement": unit} if unit else {}
    hass.states.async_set(entity_id, "1.0", attributes)
    return entity_id


async def _site_details(hass: HomeAssistant, mode: str) -> dict[str, Any]:
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await flow.async_configure(
        result["flow_id"],
        {"name": "Home", "main_fuse_a": 25, "safety_margin_a": 1, "measurement_mode": mode,
         "charger_entry_ids": []},
    )
    if result["step_id"] == "site_current_suggestions":
        result = await flow.async_configure(result["flow_id"], {"choice": "manual"})
    assert result["step_id"] == "site_details"
    return result


async def test_a_sensor_without_a_device_class_is_accepted_for_direct_current(hass: HomeAssistant) -> None:
    result = await _site_details(hass, "direct_phase_current")
    # The picker no longer filters by device class.
    for key, selector in result["data_schema"].schema.items():
        if str(key).startswith("direct_"):
            assert "device_class" not in selector.config

    payload = {f"direct_{phase}": _bare_sensor(hass, f"sensor.diy_{phase.lower()}", "A") for phase in PHASES}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], payload)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["direct_entities"] == {phase: payload[f"direct_{phase}"] for phase in PHASES}


async def test_a_wrong_unit_on_a_direct_current_sensor_is_refused_with_a_field_error(
    hass: HomeAssistant,
) -> None:
    result = await _site_details(hass, "direct_phase_current")
    payload = {
        "direct_L1": _bare_sensor(hass, "sensor.diy_l1", "A"),
        "direct_L2": _bare_sensor(hass, "sensor.diy_l2", "W"),
        "direct_L3": _bare_sensor(hass, "sensor.diy_l3", None),
    }

    result = await hass.config_entries.flow.async_configure(result["flow_id"], payload)

    assert result["step_id"] == "site_details"
    assert result["errors"] == {"direct_L2": "unit_expected_current", "direct_L3": "unit_expected_current"}

    payload["direct_L2"] = _bare_sensor(hass, "sensor.diy_l2", "mA")
    payload["direct_L3"] = _bare_sensor(hass, "sensor.diy_l3", "A")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], payload)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_derived_site_sensors_are_checked_by_their_quantity(hass: HomeAssistant) -> None:
    result = await _site_details(hass, "derived_phase_current")
    payload: dict[str, str] = {}
    for phase in PHASES:
        payload[f"derived_{phase}_power"] = _bare_sensor(hass, f"sensor.p_{phase.lower()}", "kW")
        payload[f"derived_{phase}_voltage"] = _bare_sensor(hass, f"sensor.v_{phase.lower()}", "V")
    payload["derived_L1_reactive_power"] = _bare_sensor(hass, "sensor.q1", "W")
    payload["derived_L2_apparent_power"] = _bare_sensor(hass, "sensor.s2", "kVA")
    payload["derived_L3_current"] = _bare_sensor(hass, "sensor.i3", "V")
    payload["derived_L1_power_export"] = _bare_sensor(hass, "sensor.e1", "W")
    payload["derived_L2_voltage"] = _bare_sensor(hass, "sensor.v_l2", "W")

    result = await hass.config_entries.flow.async_configure(result["flow_id"], payload)

    assert result["step_id"] == "site_details"
    assert result["errors"] == {
        "derived_L1_reactive_power": "unit_expected_reactive_power",
        "derived_L3_current": "unit_expected_current",
        "derived_L2_voltage": "unit_expected_voltage",
    }
    # What the user typed is kept on the form.
    marker = next(key for key in result["data_schema"].schema if str(key) == "derived_L1_power")
    assert marker.description["suggested_value"] == "sensor.p_l1"

    payload["derived_L1_reactive_power"] = _bare_sensor(hass, "sensor.q1", "kvar")
    payload["derived_L3_current"] = _bare_sensor(hass, "sensor.i3", "mA")
    payload["derived_L2_voltage"] = _bare_sensor(hass, "sensor.v_l2", "V")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], payload)
    assert result["type"] is FlowResultType.CREATE_ENTRY
