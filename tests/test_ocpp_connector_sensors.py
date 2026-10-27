"""A connector's OCPP sensors (status, current import, transaction) are found by what they are, not by a
name built from the charge point id: the registry's unique id first, the connector's device second, the
integration's default name last. A one-connector charge point has flat names; one with several has a
connector in every connector sensor's name and flat station-wide sensors that are never a connector's.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_MODE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    MODE_OCPP,
)
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.vehicles.ocpp_identity import (
    connector_sensor_entity_id,
    CURRENT_IMPORT_KEY,
    OcppConnectorTarget,
    STATUS_CONNECTOR_KEY,
    TRANSACTION_ID_KEY,
)


def _register(
    hass: HomeAssistant, domain: str, unique_id: str, object_id: str, *, device_id: str | None = None, translation_key: str | None = None
) -> str:
    return er.async_get(hass).async_get_or_create(
        domain,
        "ocpp",
        unique_id,
        suggested_object_id=object_id,
        device_id=device_id,
        translation_key=translation_key,
    ).entity_id


def _device(hass: HomeAssistant, name: str) -> str:
    entry = MockConfigEntry(domain="ocpp")
    entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("ocpp", name)}
    ).id


async def test_a_multi_connector_charge_point_finds_each_connectors_own_sensor_even_renamed(hass: HomeAssistant) -> None:
    _register(hass, "switch", "switch.ocpp.cp.conn1.charge_control", "cp_connector_1_charge_control")
    _register(hass, "switch", "switch.ocpp.cp.conn2.charge_control", "cp_connector_2_charge_control")
    _register(hass, "sensor", "ocpp.cp.conn1.current_import.sensor", "garage_left_current")
    _register(hass, "sensor", "ocpp.cp.conn2.current_import.sensor", "garage_right_current")
    # The station-wide sensor of the same key names no connector.
    _register(hass, "sensor", "ocpp.cp.current_import.sensor", "cp_current_import")

    assert connector_sensor_entity_id(hass, OcppConnectorTarget("cp", 1), CURRENT_IMPORT_KEY) == "sensor.garage_left_current"
    assert connector_sensor_entity_id(hass, OcppConnectorTarget("cp", 2), CURRENT_IMPORT_KEY) == "sensor.garage_right_current"


async def test_a_multi_connector_charge_point_never_takes_a_station_wide_sensor_for_a_connectors(
    hass: HomeAssistant,
) -> None:
    _register(hass, "switch", "switch.ocpp.cp.conn1.charge_control", "cp_connector_1_charge_control")
    _register(hass, "sensor", "ocpp.cp.status_connector.sensor", "cp_status_connector")
    hass.states.async_set("sensor.cp_status_connector", "Charging")

    assert (
        connector_sensor_entity_id(hass, OcppConnectorTarget("cp", 1), STATUS_CONNECTOR_KEY)
        == "sensor.cp_connector_1_status_connector"
    )


async def test_a_single_connector_charge_point_finds_its_flat_sensors(hass: HomeAssistant) -> None:
    _register(hass, "switch", "switch.ocpp.halo.charge_control", "halo_charge_control")
    _register(hass, "sensor", "ocpp.halo.status_connector.sensor", "halo_status_connector")
    _register(hass, "sensor", "ocpp.halo.current_import.sensor", "halo_current_import")
    target = OcppConnectorTarget("halo", 1)

    assert connector_sensor_entity_id(hass, target, STATUS_CONNECTOR_KEY) == "sensor.halo_status_connector"
    assert connector_sensor_entity_id(hass, target, CURRENT_IMPORT_KEY) == "sensor.halo_current_import"


async def test_a_connector_scoped_sensor_is_found_on_a_single_connector_charge_point_too(hass: HomeAssistant) -> None:
    _register(hass, "switch", "switch.ocpp.halo.charge_control", "halo_charge_control")
    _register(hass, "sensor", "ocpp.halo.conn1.transaction_id.sensor", "halo_connector_1_transaction_id")

    assert (
        connector_sensor_entity_id(hass, OcppConnectorTarget("halo", 1), TRANSACTION_ID_KEY)
        == "sensor.halo_connector_1_transaction_id"
    )


async def test_the_connectors_device_finds_a_sensor_by_its_translation_key(hass: HomeAssistant) -> None:
    device = _device(hass, "halo")
    _register(hass, "switch", "switch.ocpp.halo.charge_control", "halo_charge_control", device_id=device)
    _register(
        hass, "sensor", "halo-status-v2", "wallbox_status", device_id=device, translation_key=STATUS_CONNECTOR_KEY
    )

    assert connector_sensor_entity_id(hass, OcppConnectorTarget("halo", 1), STATUS_CONNECTOR_KEY) == "sensor.wallbox_status"


async def test_on_a_shared_device_another_connectors_sensor_is_never_taken(hass: HomeAssistant) -> None:
    device = _device(hass, "cp")
    _register(hass, "switch", "switch.ocpp.cp.conn1.charge_control", "cp_connector_1_charge_control", device_id=device)
    _register(
        hass, "sensor", "cp.conn2.status", "cp_right_status", device_id=device, translation_key=STATUS_CONNECTOR_KEY
    )
    _register(
        hass, "sensor", "cp.conn1.status", "cp_left_status", device_id=device, translation_key=STATUS_CONNECTOR_KEY
    )

    assert connector_sensor_entity_id(hass, OcppConnectorTarget("cp", 1), STATUS_CONNECTOR_KEY) == "sensor.cp_left_status"


async def test_without_registry_entries_the_default_names_are_used(hass: HomeAssistant) -> None:
    target = OcppConnectorTarget("cp", 1)
    assert connector_sensor_entity_id(hass, target, CURRENT_IMPORT_KEY) == "sensor.cp_connector_1_current_import"

    # A one-connector charge point by its states alone: the flat name.
    hass.states.async_set("switch.cp_charge_control", "on")
    hass.states.async_set("sensor.cp_current_import", "7.5")
    assert connector_sensor_entity_id(hass, target, CURRENT_IMPORT_KEY) == "sensor.cp_current_import"

    # A connector-scoped sensor of that name wins.
    hass.states.async_set("sensor.cp_connector_1_current_import", "7.0")
    assert connector_sensor_entity_id(hass, target, CURRENT_IMPORT_KEY) == "sensor.cp_connector_1_current_import"


async def test_the_controller_reads_a_single_connector_charge_points_flat_sensors(hass: HomeAssistant) -> None:
    _register(hass, "switch", "switch.ocpp.halo.charge_control", "halo_charge_control")
    _register(hass, "sensor", "ocpp.halo.status_connector.sensor", "halo_status_connector")
    _register(hass, "sensor", "ocpp.halo.current_import.sensor", "halo_current_import")
    hass.states.async_set("switch.halo_charge_control", "on")
    hass.states.async_set("sensor.halo_status_connector", "Charging")
    hass.states.async_set("sensor.halo_current_import", "9.5", {"unit_of_measurement": "A"})
    controller = ChargingController(
        hass,
        "entry_halo",
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: "switch.halo_charge_control",
            CONF_CURRENT_LIMIT: "",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_CHARGE_POINT_ID: "halo",
            CONF_OCPP_CONNECTOR_ID: 1,
        },
    )
    await controller.async_initialize()

    assert controller.measured_current_a() == 9.5
    facts = controller.charge_progress_facts()
    assert (facts.connector_status, facts.current_import_a) == ("Charging", 9.5)
    await controller.async_shutdown()


async def test_the_controller_reads_a_renamed_multi_connector_sensor(hass: HomeAssistant) -> None:
    _register(hass, "switch", "switch.ocpp.cp.conn2.charge_control", "cp_connector_2_charge_control")
    _register(hass, "sensor", "ocpp.cp.conn2.current_import.sensor", "garage_right_current")
    hass.states.async_set("switch.cp_connector_2_charge_control", "on")
    hass.states.async_set("sensor.garage_right_current", "12.0", {"unit_of_measurement": "A"})
    controller = ChargingController(
        hass,
        "entry_cp2",
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: "switch.cp_connector_2_charge_control",
            CONF_CURRENT_LIMIT: "",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_CHARGE_POINT_ID: "cp",
            CONF_OCPP_CONNECTOR_ID: 2,
        },
    )
    await controller.async_initialize()

    assert controller.measured_current_a() == 12.0
    await controller.async_shutdown()
