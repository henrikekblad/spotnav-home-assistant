"""The site flow confirms what detection found instead of showing the full details form."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import DOMAIN

from .helpers import create_ocpp_charger_device, make_entry, make_ocpp_config_entry
from .site_registry import materialize
from .test_site_detection import sigen



def _owner_world(hass: HomeAssistant, *, voltage: bool = True, chargers: int = 1) -> list[str]:
    registry = sigen()
    if not voltage:
        registry.entities = [e for e in registry.entities if not e.unique_id.endswith("_voltage")]
    materialize(hass, registry)
    entities = er.async_get(hass)
    for phase in "abc":
        # Disabled entities have a registry name and no state until they are enabled.
        entities.async_update_entity(
            f"sensor.sigen_plant_grid_phase_{phase}_active", name=f"Grid power {phase.upper()}"
        )
    owner = make_ocpp_config_entry(hass, entry_id="owner")
    ids = []
    for number in range(1, chargers + 1):
        switch = create_ocpp_charger_device(
            hass, ocpp_entry=owner, device_unique_id=f"halo_{number}",
            switch_object_id=f"halo_{number}",
            current_attributes={"L1": 6.0, "L2": 6.0, "L3": 6.0},
        )
        ids.append(
            make_entry(
                hass, entry_id=f"halo_{number}", charge_control=switch, current_limit=None,
                webhook_id=f"hook-{number}", title=f"Halo {number}",
            ).entry_id
        )
    return ids


async def _to_detected(hass: HomeAssistant, charger_ids: list[str]) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": charger_ids,
        },
    )
    assert result["step_id"] == "site_detected"
    return result


def _choice(result: dict[str, Any]) -> str:
    field = next(key for key in result["data_schema"].schema if str(key) == "choice")
    return field.default()


async def test_an_unambiguous_detection_is_confirmed_and_equals_the_form_path(hass: HomeAssistant) -> None:
    charger_ids = _owner_world(hass)
    result = await _to_detected(hass, charger_ids)
    choice = _choice(result)

    confirm = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": choice, "enable_disabled": True}
    )
    assert confirm["step_id"] == "site_confirm"
    summary = confirm["description_placeholders"]["summary"]
    assert "power per phase, current calculated" in summary
    assert "Reactive power: found" in summary
    assert "House battery:" in summary and "charging positive" in summary
    assert "Charger Halo 1: 3 phases, measured current from" in summary
    assert "L1: Grid power A" in summary and "sensor." not in summary
    created = await hass.config_entries.flow.async_configure(confirm["flow_id"], {})
    assert created["type"] is FlowResultType.CREATE_ENTRY
    data = dict(created["data"])
    assert data["measurement_mode"] == "derived_phase_current"
    assert data["battery_aggregate_power_entity"] == "sensor.sigen_plant_ess_power"
    wiring = data["phase_wiring"][charger_ids[0]]
    assert wiring["phases"] == 3
    source = wiring["measured_current_source"]
    assert source["kind"] == "attributes" and set(source["attributes"]) == {"L1", "L2", "L3"}
    assert source["attribute_unit_override"] == "A"
    assert data["derived_entities"]["L1"]["voltage"] == "sensor.sigen_inverter_phase_a_voltage"
    assert data["derived_entities"]["L1"]["reactive_power"] == "sensor.sigen_plant_grid_phase_a_reactive"
    assert data["main_fuse_a"] == 25 and data["safety_margin_a"] == 1
    await hass.config_entries.async_remove(created["result"].entry_id)

    # The same site through the full form, accepting its defaults and the discovered charger source.
    result = await _to_detected(hass, charger_ids)
    form = await hass.config_entries.flow.async_configure(result["flow_id"], {"choice": choice})
    form = await hass.config_entries.flow.async_configure(form["flow_id"], {"adjust": True})
    assert form["step_id"] == "site_details"
    form = await hass.config_entries.flow.async_configure(form["flow_id"], {})
    assert form["step_id"] == "site_charger_wiring"
    via_form = await hass.config_entries.flow.async_configure(
        form["flow_id"], {"measured_source": source_candidate(wiring, hass, charger_ids[0])}
    )
    form_data = dict(via_form["data"])
    for key in ("battery_aggregate_power_entity", "battery_discharge_power_entity", "battery_power_inverted"):
        data.pop(key, None)
    assert form_data == data


def source_candidate(wiring: dict[str, Any], hass: HomeAssistant, charger_id: str) -> str:
    return wiring["measured_current_source"]["entity_id"]


async def test_adjusting_shows_the_form(hass: HomeAssistant) -> None:
    result = await _to_detected(hass, _owner_world(hass))
    confirm = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": _choice(result), "enable_disabled": True}
    )

    form = await hass.config_entries.flow.async_configure(confirm["flow_id"], {"adjust": True})

    assert form["step_id"] == "site_details"


async def test_a_missing_voltage_goes_to_the_form(hass: HomeAssistant) -> None:
    charger_ids = _owner_world(hass, voltage=False)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home", "main_fuse_a": 25, "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current", "charger_entry_ids": charger_ids,
        },
    )
    # Without the inverter's voltage the meter is not offered at all, so nothing is confirmed.
    assert result["step_id"] in ("site_current_suggestions", "site_details")


async def test_a_charger_without_one_clear_measured_source_goes_to_the_form(hass: HomeAssistant) -> None:
    owner = make_ocpp_config_entry(hass, entry_id="owner_none")
    registry_world = _owner_world(hass, chargers=0)
    assert registry_world == []
    switch = create_ocpp_charger_device(
        hass, ocpp_entry=owner, device_unique_id="bare", switch_object_id="bare"
    )
    bare = make_entry(
        hass, entry_id="bare", charge_control=switch, current_limit=None, webhook_id="hook-bare", title="Bare"
    )
    result = await _to_detected(hass, [bare.entry_id])

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": _choice(result), "enable_disabled": True}
    )

    assert result["step_id"] == "site_details"


async def test_detected_meter_labels_are_human_readable(hass: HomeAssistant) -> None:
    result = await _to_detected(hass, _owner_world(hass))
    field = next(key for key in result["data_schema"].schema if str(key) == "choice")
    labels = [o["label"] for o in result["data_schema"].schema[field].config["options"]]

    assert labels[0] == "Sigen Plant – power per phase (current calculated)"
    assert all("(sigen" not in label and "derived_" not in label for label in labels)


async def test_the_first_form_has_sensible_defaults(hass: HomeAssistant) -> None:
    charger_ids = _owner_world(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    assert result["step_id"] == "site"
    fields = {str(key): key for key in result["data_schema"].schema}

    assert fields["main_fuse_a"].default is __import__("voluptuous").UNDEFINED
    assert fields["safety_margin_a"].default() == 1.0
    assert fields["name"].default() == "Site"
    assert fields["charger_entry_ids"].default() == charger_ids


async def test_the_first_form_name_follows_the_language(hass: HomeAssistant) -> None:
    hass.config.language = "sv"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    fields = {str(key): key for key in result["data_schema"].schema}

    assert fields["name"].default() == "Anläggning"
    assert fields["charger_entry_ids"].default() == []


async def test_the_summary_has_a_voltage_line(hass: HomeAssistant) -> None:
    result = await _to_detected(hass, _owner_world(hass))
    confirm = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": _choice(result), "enable_disabled": True}
    )

    assert "Voltage: L1 " in confirm["description_placeholders"]["summary"]
