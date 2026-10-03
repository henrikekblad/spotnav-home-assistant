"""The site flow confirms what detection found instead of showing the full details form."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import DOMAIN, MEASUREMENT_MODE_DERIVED
from custom_components.spotnav.flows.labels import PHASES
from custom_components.spotnav.flows.site_confirm import charger_found_summary, md_escape, site_confirm_summary
from custom_components.spotnav.site.site_detection import BatteryCandidate, MeterCandidate

from .helpers import create_ocpp_charger_device, make_entry, make_ocpp_config_entry
from .site_registry import materialize
from .test_site_detection import sigen



def _owner_world(
    hass: HomeAssistant, *, voltage: bool = True, chargers: int = 1, registry: Any = None
) -> list[str]:
    registry = sigen() if registry is None else registry
    if not voltage:
        registry.entities = [e for e in registry.entities if not e.unique_id.endswith("_voltage")]
    materialize(hass, registry)
    entities = er.async_get(hass)
    for phase in "abc":
        # Disabled entities have a registry name and no state until they are enabled.
        if entities.async_get(f"sensor.sigen_plant_grid_phase_{phase}_active") is not None:
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
    blocks = summary.split("\n\n")
    assert blocks[0].startswith("**Grid meter** – power per phase, current calculated")
    assert "**Reactive power:** found" in blocks
    assert any(b.startswith("**House battery:**") and "(charging positive)" in b for b in blocks)
    assert any(b.startswith("**Charger:** Halo 1 – 3 phases, measured current from") for b in blocks)
    assert "- **L1:** Grid power A" in summary and "sensor." not in summary
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

    summary = confirm["description_placeholders"]["summary"]
    rows = [line for line in summary.splitlines() if line.startswith("- **L")]
    assert [row.split(":**")[0] for row in rows] == ["- **L1", "- **L2", "- **L3"]
    assert all(" · " in row for row in rows)


async def test_the_detected_total_grid_power_is_stored_with_the_meter(hass: HomeAssistant) -> None:
    """A Tibber Pulse is a direct site; its consumption and production sensors become the total that
    solar and hybrid read, stored with the meter the user confirmed through the form."""
    from .test_site_detection import tibber_pulse_with_production

    charger_ids = _owner_world(hass, registry=tibber_pulse_with_production())
    result = await _to_detected(hass, charger_ids)
    choice = _choice(result)

    form = await hass.config_entries.flow.async_configure(result["flow_id"], {"choice": choice})
    form = await hass.config_entries.flow.async_configure(form["flow_id"], {"adjust": True})
    assert form["step_id"] == "site_details"
    form = await hass.config_entries.flow.async_configure(form["flow_id"], {})
    assert form["step_id"] == "site_charger_wiring"
    schema_keys = {str(key): key for key in form["data_schema"].schema}
    measured = schema_keys["measured_source"].default()
    created = await hass.config_entries.flow.async_configure(form["flow_id"], {"measured_source": measured})

    assert created["type"] is FlowResultType.CREATE_ENTRY
    data = dict(created["data"])
    assert data["measurement_mode"] == "direct_phase_current"
    assert data["grid_power_source"] == {
        "power": "sensor.tibber_power",
        "power_export": "sensor.tibber_power_production",
    }


async def test_a_meter_without_a_total_stores_none(hass: HomeAssistant) -> None:
    charger_ids = _owner_world(hass)
    result = await _to_detected(hass, charger_ids)
    confirm = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": _choice(result), "enable_disabled": True}
    )
    created = await hass.config_entries.flow.async_configure(confirm["flow_id"], {})

    assert "grid_power_source" not in dict(created["data"])


def _meter() -> MeterCandidate:
    return MeterCandidate(
        candidate_id="m",
        integration="x",
        title="Meter",
        mode=MEASUREMENT_MODE_DERIVED,
        confidence=None,
        derived_entities={
            phase: {"power": f"sensor.p{n}", "voltage": f"sensor.v{n}"} for n, phase in enumerate(PHASES, 1)
        },
    )


async def test_names_are_markdown_escaped_in_both_summaries(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.p1", "1", {"friendly_name": "A*B_C [x] <i>"})
    hass.states.async_set("sensor.bat", "1", {"friendly_name": "Bat_tery *1*"})
    battery = BatteryCandidate(candidate_id="b", integration="x", title="B", entity_id="sensor.bat")

    summary = site_confirm_summary(hass, candidate=_meter(), battery=battery, chargers=[])

    assert "- **L1:** A\\*B\\_C \\[x\\] \\<i\\> · " in summary
    assert "**House battery:** Bat\\_tery \\*1\\* (charging positive)" in summary
    found = charger_found_summary(hass, charge_control="Switch *1*", current_number=None, energy_meter="E_1")
    assert found.splitlines() == [
        "- **Charge control:** Switch \\*1\\*",
        "- **Current set via:** OCPP ChangeConfiguration",
        "- **Energy meter:** E\\_1 (found automatically)",
    ]
    assert md_escape("plain name 1.5") == "plain name 1.5"
