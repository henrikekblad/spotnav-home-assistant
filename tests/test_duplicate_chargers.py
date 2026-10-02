"""Two SpotNav charger entries that are one physical charger: found through any shared device,
measured current, OCPP connector or Easee device; shown on both chargers in the card and in Repairs;
refused in the setup flow with the same sentence; never removed."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.entity_fields import charger_control_descriptor
from custom_components.spotnav.const import (
    CONF_CHARGER_CURRENT_ENTITIES,
    CONF_CONTROL_PATH,
    CONF_ENTRY_TYPE,
    CONF_MEASURED_CURRENT_SOURCE,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    MODE_GENERIC,
)
from custom_components.spotnav.repairs import async_sync_resolution_repairs
from custom_components.spotnav.site.measurement_source import PhaseMeasurementSource, source_to_dict
from custom_components.spotnav.vehicles.duplicate_chargers import (
    SHARED_DEVICE,
    SHARED_EASEE,
    SHARED_MEASURED_CURRENT,
    SHARED_OCPP,
    duplicate_pairs,
    duplicates_of,
)

from .helpers import create_ocpp_switch_and_number, make_entry, make_ocpp_config_entry, make_site_entry


def _device_of(hass: HomeAssistant, entity_id: str) -> str:
    registered = er.async_get(hass).async_get(entity_id)
    assert registered is not None and registered.device_id is not None
    return registered.device_id


def _second_device_entity(hass: HomeAssistant, ocpp_entry: MockConfigEntry, device_id: str, name: str) -> str:
    """A second entity (a measured current) on an existing device."""
    registered = er.async_get(hass).async_get_or_create(
        "sensor", "ocpp", f"{name}_current", device_id=device_id, config_entry=ocpp_entry, suggested_object_id=name
    )
    hass.states.async_set(registered.entity_id, "0", {"unit_of_measurement": "A"})
    return registered.entity_id


def _entry(hass: HomeAssistant, name: str, charge_control: str, **kwargs):
    hass.states.async_set(charge_control, "off")
    return make_entry(
        hass,
        entry_id=f"entry_{name}",
        charge_control=charge_control,
        current_limit=kwargs.pop("current_limit", None),
        webhook_id=f"webhook-{name}",
        title=name.title(),
        **kwargs,
    )


async def test_two_entries_on_one_device_through_different_entities_are_one_charger(hass: HomeAssistant) -> None:
    ocpp = make_ocpp_config_entry(hass, entry_id="ocpp_dup_1")
    switch, number = create_ocpp_switch_and_number(
        hass, ocpp_entry=ocpp, device_unique_id="box_1", switch_object_id="box_1_switch", number_object_id="box_1_limit"
    )
    first = _entry(hass, "first", switch)
    # The leftover entry controls a plain switch of its own but measures on the same device.
    measured = _second_device_entity(hass, ocpp, _device_of(hass, switch), "box_1_import")
    other_switch = _entry(hass, "second", "switch.leftover")
    hass.config_entries.async_update_entry(
        other_switch, data={**other_switch.data, CONF_CHARGER_CURRENT_ENTITIES: [measured]}
    )

    found = duplicates_of(hass, first)
    assert [(item.entry_id, item.shared) for item in found] == [(other_switch.entry_id, SHARED_DEVICE)]
    assert [(item.entry_id, item.shared) for item in duplicates_of(hass, other_switch)] == [
        (first.entry_id, SHARED_DEVICE)
    ]
    assert number is not None


async def test_two_entries_measuring_one_sensor_are_one_charger(hass: HomeAssistant) -> None:
    sensor = "sensor.easee_garage_ocpp_current_import"
    hass.states.async_set(sensor, "0", {"unit_of_measurement": "A"})
    first = _entry(hass, "first", "switch.garage_a")
    second = _entry(hass, "second", "switch.garage_b")
    site = make_site_entry(
        hass,
        entry_id="site_dup",
        charger_entry_ids=[first.entry_id, second.entry_id],
        phase_wiring={
            first.entry_id: {
                CONF_MEASURED_CURRENT_SOURCE: source_to_dict(
                    PhaseMeasurementSource(kind="attributes", entity_id=sensor, attributes={"L1": "a", "L2": "b", "L3": "c"})
                )
            },
            second.entry_id: {
                CONF_MEASURED_CURRENT_SOURCE: source_to_dict(
                    PhaseMeasurementSource(kind="attributes", entity_id=sensor, attributes={"L1": "a", "L2": "b", "L3": "c"})
                )
            },
        },
    )
    assert site is not None
    [pair] = duplicate_pairs(hass)
    assert (pair[0].entry_id, pair[1].entry_id) == (first.entry_id, second.entry_id)
    assert (pair[2].shared, pair[2].what) == (SHARED_MEASURED_CURRENT, sensor)


async def test_two_entries_on_one_ocpp_connector_or_easee_device_are_one_charger(hass: HomeAssistant) -> None:
    ocpp_a = _entry(hass, "ocpp_a", "switch.ocpp_a", ocpp_target=("CP1", 1))
    ocpp_b = _entry(hass, "ocpp_b", "switch.ocpp_b", ocpp_target=("CP1", 1))
    easee_a = _entry(hass, "easee_a", "switch.easee_a", extra={CONF_CONTROL_PATH: {"kind": "easee", "device_id": "dev1"}})
    easee_b = _entry(hass, "easee_b", "switch.easee_b", extra={CONF_CONTROL_PATH: {"kind": "easee", "device_id": "dev1"}})
    found = {(a.entry_id, b.entry_id, c.shared) for a, b, c in duplicate_pairs(hass)}
    assert found == {
        (ocpp_a.entry_id, ocpp_b.entry_id, SHARED_OCPP),
        (easee_a.entry_id, easee_b.entry_id, SHARED_EASEE),
    }


async def test_two_connectors_of_one_charge_point_are_two_chargers(hass: HomeAssistant) -> None:
    ocpp = make_ocpp_config_entry(hass, entry_id="ocpp_dup_2")
    switch_1, _ = create_ocpp_switch_and_number(
        hass, ocpp_entry=ocpp, device_unique_id="box_2", switch_object_id="box_2_c1"
    )
    # Connector two sits on the same device and is a different charger.
    registered = er.async_get(hass).async_get_or_create(
        "switch", "ocpp", "box_2_c2_charge_control", device_id=_device_of(hass, switch_1), config_entry=ocpp,
        suggested_object_id="box_2_c2",
    )
    _entry(hass, "c1", switch_1, ocpp_target=("CP2", 1))
    _entry(hass, "c2", registered.entity_id, ocpp_target=("CP2", 2))
    assert duplicate_pairs(hass) == []


async def test_unrelated_chargers_are_not_duplicates(hass: HomeAssistant) -> None:
    _entry(hass, "a", "switch.a")
    _entry(hass, "b", "switch.b")
    assert duplicate_pairs(hass) == []


async def test_the_setup_flow_refuses_a_second_entry_on_one_device_naming_the_first(hass: HomeAssistant) -> None:
    ocpp = make_ocpp_config_entry(hass, entry_id="ocpp_dup_3")
    switch, _ = create_ocpp_switch_and_number(
        hass, ocpp_entry=ocpp, device_unique_id="box_3", switch_object_id="box_3_switch"
    )
    # A leftover entry already controls the same box through a different, plain switch.
    leftover = _entry(hass, "leftover", "switch.leftover_box_3")
    hass.config_entries.async_update_entry(
        leftover,
        data={**leftover.data, CONF_CHARGER_CURRENT_ENTITIES: [_second_device_entity(hass, ocpp, _device_of(hass, switch), "box_3_import")]},
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mode": MODE_GENERIC})
    assert result["step_id"] == "generic"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"charge_control": switch})
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"charge_control": "duplicate_charger"}
    assert result["description_placeholders"] == {"other": "Leftover"}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_the_card_and_the_status_name_the_other_entry_on_both_chargers(hass: HomeAssistant) -> None:
    from .world import setup_charger

    first = await setup_charger(hass, entry_id="entry_a", webhook_id="wa", charge_control="switch.garage_a", title="Garage")
    second = await setup_charger(hass, entry_id="entry_b", webhook_id="wb", charge_control="switch.garage_b", title="Garage OCPP")
    for entry, other in ((first, "Garage OCPP"), (second, "Garage")):
        assert duplicates_of(hass, entry) == []
    # The leftover shares the box: both now carry the conflict.
    for entry in (first, second):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_CHARGER_CURRENT_ENTITIES: ["sensor.shared_import"]}
        )
    for entry, other in ((first, "Garage OCPP"), (second, "Garage")):
        control = charger_control_descriptor(hass, entry)
        mine = [item for item in control["conflicts"] if item["kind"] == "duplicate_charger"]
        assert mine == [
            {"kind": "duplicate_charger", "entity_id": "sensor.shared_import", "label": SHARED_MEASURED_CURRENT, "state": other}
        ]
        # The status says it too (the composer words it beside the headline, see test_status_compose).
        facts = dashboard_api.status_facts(dashboard_api.capture_dashboard(hass, entry))
        assert facts.duplicate_chargers == (other,)
    # Nothing was removed.
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_repairs_name_both_entries_and_clear_when_one_is_gone(hass: HomeAssistant) -> None:
    first = _entry(hass, "first", "switch.a", ocpp_target=("CPX", 1))
    second = _entry(hass, "second", "switch.b", ocpp_target=("CPX", 1))
    await async_sync_resolution_repairs(hass)
    issues = [issue for (domain, _), issue in ir.async_get(hass).issues.items() if domain == DOMAIN]
    assert [(issue.translation_key, issue.is_fixable) for issue in issues] == [("duplicate_charger", False)]
    assert issues[0].translation_placeholders == {"charger": "First", "other": "Second"}
    await async_sync_resolution_repairs(hass, (second.entry_id,))
    assert [key for key in ir.async_get(hass).issues if key[0] == DOMAIN] == []
    assert first.entry_id in {entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)}
