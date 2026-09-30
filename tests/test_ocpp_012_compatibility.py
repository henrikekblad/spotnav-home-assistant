"""The OCPP 0.12 compatibility surface: identity, migration, roles and the gates around them.

Every identifier is invented, and the fixtures are *registry-shaped*: entities and devices registered with
the entity-id **and** unique-id shapes OCPP 0.12.0 uses:

    switch.<cpid>_connector_<n>_charge_control           switch.ocpp.<cpid>.conn<n>.charge_control
    number.<cpid>_connector_<n>_session_current_limit    number.ocpp.<cpid>.conn<n>.session_current_limit
    number.<cpid>_maximum_current                        number.ocpp.<cpid>.maximum_current    (station)
    switch.<cpid>_charge_control                         switch.ocpp.<cpid>.charge_control     (one connector)

and the 0.11 shape the migration reads:

    number.<cpid>_connector_<n>_maximum_current          number.ocpp.<cpid>.conn<n>.maximum_current
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CONF_OCPP_TARGET_UNRESOLVED,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    CURRENT_CONTROL_NUMBER,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    MODE_GENERIC,
    MODE_OCPP,
)
from custom_components.spotnav.flows import SpotNavChargingConfigFlow
from custom_components.spotnav.execution.controller import (
    ChargingController,
    rewrite_assigned_current,
)
from custom_components.spotnav.vehicles.ocpp_identity import (
    OcppConnectorTarget,
    discover_controls,
    energy_register_entity_for,
    is_station_maximum,
    resolve_target,
    target_from_entity_id,
)
from tests.helpers import make_ocpp_config_entry
from .world import CPID, ocpp_entity, station_device, two_connector_charger

OWNER_ENTRY_ID = "entry_ocpp_owner"
AMPERE = {"unit_of_measurement": "A", "min": 6, "max": 16}
ENERGY_KWH = {
    "unit_of_measurement": "kWh",
    "device_class": "energy",
    "state_class": "total_increasing",
}


@pytest.fixture(name="ocpp_owner")
def ocpp_owner_fixture(hass: HomeAssistant) -> MockConfigEntry:
    """A stand-in for the OCPP integration's own config entry, which owns the devices."""
    return make_ocpp_config_entry(hass, entry_id=OWNER_ENTRY_ID)


async def test_a_single_connector_topology_resolves_from_the_flat_shapes(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    """§7.2: the flat 0.12 shapes mean one connector, and resolve to connector 1.

    The integration writes the flat form exactly when `num_connectors == 1`, so this is the integration's own
    statement rather than this integration's guess -- and it is the shape a new install gets.
    """
    device_id = station_device(hass, ocpp_owner, CPID)
    charge_control = ocpp_entity(
        hass,
        owner=ocpp_owner,
        device_id=device_id,
        cpid=CPID,
        domain="switch",
        key="charge_control",
        state="off",
    )
    session = ocpp_entity(
        hass,
        owner=ocpp_owner,
        device_id=device_id,
        cpid=CPID,
        domain="number",
        key="session_current_limit",
        state="unavailable",
        attributes=AMPERE,
    )

    resolution = resolve_target(hass, charge_control=charge_control)

    assert resolution.target == OcppConnectorTarget(CPID, 1)
    controls = discover_controls(hass, resolution.target)
    assert controls.session_limit_entity == session
    assert target_from_entity_id(session) == OcppConnectorTarget(CPID, 1)


async def test_a_multi_connector_charger_takes_the_configured_charge_controls_connector(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    """§7.3: the connector comes from the Charge Control the person configured, not from connector 1."""
    charger = two_connector_charger(hass, ocpp_owner)

    resolution = resolve_target(hass, charge_control=charger["charge_control_2"])

    assert resolution.target == OcppConnectorTarget(CPID, 2)
    controls = discover_controls(hass, resolution.target)
    assert controls.session_limit_entity == charger["session_2"]
    assert controls.station_maximum_entity == charger["station"]
    # Connector 2's session limit is *not* connector 1's, and neither is the station ceiling.
    assert charger["session_1"] != charger["session_2"]
    assert target_from_entity_id(charger["session_1"]) == OcppConnectorTarget(CPID, 1)


async def test_a_session_limit_is_discovered_while_it_is_unavailable(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    """§7.5: the role is discovered from the registry, not from a parseable state.

    0.12 reports Session Current Limit as `unavailable` whenever no transaction runs -- which is most of the
    time -- so hiding it because its state cannot be parsed would hide it exactly when it matters.
    """
    charger = two_connector_charger(hass, ocpp_owner)
    assert hass.states.get(charger["session_1"]).state == "unavailable"

    controls = discover_controls(hass, OcppConnectorTarget(CPID, 1))

    assert controls.session_limit_entity == charger["session_1"]
    assert controls.session_limit_entity is not None


async def test_the_station_ceiling_is_never_a_target_or_an_actuator(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    """§7.4 and §7.7: the station ceiling names no connector, and a marked entry never guesses one.

    `number.<cpid>_maximum_current` is a persistent ceiling for the whole charger. Commanding it would change
    a safety value rather than one session's current, so it is refused as a target -- and an entry marked
    unresolved stays unresolved *even though* a connector-shaped charge control exists,
    because resolving it here would be exactly the silent guess the marker exists to prevent.
    """
    charger = two_connector_charger(hass, ocpp_owner)
    assert is_station_maximum(charger["station"]) is True
    assert is_station_maximum(charger["session_1"]) is False
    assert target_from_entity_id(charger["station"]) is None
    assert resolve_target(hass, charge_control=charger["station"]).resolved is False

    async_mock_service(hass, "switch", "turn_on")
    set_value_calls = async_mock_service(hass, "number", "set_value")
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    controller = ChargingController(
        hass,
        "entry_ceiling",
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: charger["charge_control_1"],
            CONF_CURRENT_LIMIT: charger["station"],
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_TARGET_UNRESOLVED: True,
        },
    )
    await controller.async_initialize()

    await controller.async_start(amps=10)

    assert controller.ocpp_target is None
    assert controller.ocpp_target_source == "unresolved"
    assert configure_calls == []
    assert set_value_calls == []
    assert controller.requested_current_a == 10
    await controller.async_shutdown()


async def test_a_stored_target_is_all_it_takes_to_apply_a_current(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    """§7.6: the stored target is all it takes -- there is no per-connector `maximum_current` entity.

    The read-modify-write still goes out through the charger's own service, and the facts a
    diagnostics dump reports keep the ceiling and the session limit apart.
    """
    charger = two_connector_charger(hass, ocpp_owner)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.16,2.10"})
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    controller = ChargingController(
        hass,
        "entry_migrated",
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: charger["charge_control_1"],
            CONF_CURRENT_LIMIT: "",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_CHARGE_POINT_ID: CPID,
            CONF_OCPP_CONNECTOR_ID: 1,
        },
    )
    await controller.async_initialize()

    await controller.async_start(amps=10)

    assert [call.data for call in configure_calls] == [
        {"devid": CPID, "ocpp_key": "AssignedCurrent", "value": "1.10,2.10"}
    ]
    assert controller.ocpp_target == OcppConnectorTarget(CPID, 1)
    assert controller.ocpp_target_source == "stored"
    facts = controller.ocpp_control_facts()
    assert facts["station_maximum_present"] is True
    assert facts["session_limit_present"] is True
    assert facts["session_limit_state"] == "unavailable"
    await controller.async_shutdown()


async def test_a_refused_configure_reports_not_applied_and_writes_nothing_else(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    """§7.10: a failure is reported honestly, and no other command is sent instead of it."""
    charger = two_connector_charger(hass, ocpp_owner)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.16,2.10"})
    async_mock_service(
        hass, "ocpp", "configure", raise_exception=HomeAssistantError("refused")
    )
    set_value_calls = async_mock_service(hass, "number", "set_value")
    controller = ChargingController(
        hass,
        "entry_refused",
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: charger["charge_control_2"],
            CONF_CURRENT_LIMIT: "",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_CHARGE_POINT_ID: CPID,
            CONF_OCPP_CONNECTOR_ID: 2,
        },
    )
    await controller.async_initialize()

    await controller.async_start(amps=9)

    assert controller.requested_current_a == 9
    assert set_value_calls == []
    assert controller.setpoint_current_a is None
    await controller.async_shutdown()


async def test_two_chargers_and_two_connectors_stay_isolated(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    """§7.13: each target writes its own charge point and connector, and leaves the other's entry alone."""
    charger = two_connector_charger(hass, ocpp_owner)
    other_device = station_device(hass, ocpp_owner, "monet")
    other_control = ocpp_entity(
        hass,
        owner=ocpp_owner,
        device_id=other_device,
        cpid="monet",
        domain="switch",
        key="charge_control",
        connector=2,
        state="off",
    )
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.16,2.10"})
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    first = ChargingController(
        hass,
        "entry_first",
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: charger["charge_control_1"],
            CONF_CURRENT_LIMIT: "",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_CHARGE_POINT_ID: CPID,
            CONF_OCPP_CONNECTOR_ID: 1,
        },
    )
    second = ChargingController(
        hass,
        "entry_second",
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: other_control,
            CONF_CURRENT_LIMIT: "",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_CHARGE_POINT_ID: "monet",
            CONF_OCPP_CONNECTOR_ID: 2,
        },
    )
    await first.async_initialize()
    await second.async_initialize()

    await first.async_start(amps=8)
    await second.async_start(amps=7)

    assert [call.data for call in configure_calls] == [
        {"devid": CPID, "ocpp_key": "AssignedCurrent", "value": "1.8,2.10"},
        {"devid": "monet", "ocpp_key": "AssignedCurrent", "value": "1.16,2.7"},
    ]
    await first.async_shutdown()
    await second.async_shutdown()


def test_rewriting_one_connector_preserves_every_other_entry() -> None:
    """§7.9: the slot is rewritten whole, so the connectors this call is not about keep their values."""
    assert rewrite_assigned_current("1.16,2.10,3.6", 2, 8) == "1.16,2.8,3.6"
    assert rewrite_assigned_current("1.16", 2, 8) == "1.16,2.8"


async def test_the_ocpp_flow_offers_connector_roles_and_never_the_station_ceiling(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    """§5: the picker offers the connector's own roles, and the ceiling is not one of them.

    The ceiling is excluded because choosing it could not establish a connector target and writing it would
    change a persistent safety value for the whole charger; both session limits *are* offered, `unavailable`
    state and all, because a role is discovered from the registry rather than from a parseable state.
    """
    charger = two_connector_charger(hass, ocpp_owner)
    registry = er.async_get(hass)
    device_id = registry.async_get(charger["charge_control_1"]).device_id
    flow = SpotNavChargingConfigFlow()
    flow.hass = hass
    flow.flow_id = "ocpp-roles-direct-step"
    flow.handler = DOMAIN
    flow._device_id = device_id

    switches, numbers = flow._entities_for_device(device_id)

    assert charger["charge_control_1"] in switches
    assert set(numbers) == {charger["session_1"], charger["session_2"]}
    assert charger["station"] not in numbers


# `ChargingController.energy_register_entity_id` resolves a connector's own cumulative `Energy.Active.Import.Register` sensor the same way
# every other 0.12 role is resolved -- by the device/entity registry and the entity's own key,
# never by constructing an entity id (see `energy_register_entity_for`'s own docstring).


async def test_energy_register_resolves_automatically_for_an_ocpp_charger(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    """§ delivered-energy accounting: no override at all, and the register is still found."""
    device_id = station_device(hass, ocpp_owner, CPID)
    shared = {"owner": ocpp_owner, "device_id": device_id, "cpid": CPID}
    charge_control = ocpp_entity(
        hass, **shared, domain="switch", key="charge_control", connector=1, state="off"
    )
    energy = ocpp_entity(
        hass,
        **shared,
        domain="sensor",
        key="energy_active_import_register",
        connector=1,
        state="6727.452",
        attributes=ENERGY_KWH,
    )

    assert energy_register_entity_for(hass, OcppConnectorTarget(CPID, 1)) == energy

    async_mock_service(hass, "switch", "turn_on")
    controller = ChargingController(
        hass,
        "entry_energy_auto",
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: charge_control,
            CONF_CURRENT_LIMIT: "",
        },
    )
    await controller.async_initialize()

    assert controller.ocpp_target == OcppConnectorTarget(CPID, 1)
    assert controller.energy_register_entity_id == energy
    await controller.async_shutdown()


async def test_an_explicit_energy_register_override_wins(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    """The manual override is for the rare charger whose automatic resolution is wrong, or
    whose integration exposes the register some other way -- it must take precedence, not
    merely fill a gap."""
    device_id = station_device(hass, ocpp_owner, CPID)
    shared = {"owner": ocpp_owner, "device_id": device_id, "cpid": CPID}
    charge_control = ocpp_entity(
        hass, **shared, domain="switch", key="charge_control", connector=1, state="off"
    )
    ocpp_entity(
        hass,
        **shared,
        domain="sensor",
        key="energy_active_import_register",
        connector=1,
        state="6727.452",
        attributes=ENERGY_KWH,
    )
    hass.states.async_set("sensor.a_different_meter", "42.0", ENERGY_KWH)

    async_mock_service(hass, "switch", "turn_on")
    controller = ChargingController(
        hass,
        "entry_energy_override",
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: charge_control,
            CONF_CURRENT_LIMIT: "",
            CONF_ENERGY_REGISTER_ENTITY: "sensor.a_different_meter",
        },
    )
    await controller.async_initialize()

    assert controller.energy_register_entity_id == "sensor.a_different_meter"
    await controller.async_shutdown()


async def test_a_non_ocpp_charger_without_an_override_has_no_energy_register(
    hass: HomeAssistant,
) -> None:
    """A generic charger has no connector identity to resolve one from at all -- `None`, never
    a guess, so `manual_kwh`'s own accounting reports `delivered_energy_trustworthy=False`
    rather than pretending to see progress it cannot."""
    hass.states.async_set("switch.generic_charger", "off")
    async_mock_service(hass, "switch", "turn_on")
    controller = ChargingController(
        hass,
        "entry_generic_no_register",
        {
            CONF_MODE: MODE_GENERIC,
            CONF_CHARGE_CONTROL: "switch.generic_charger",
            CONF_CURRENT_LIMIT: "",
        },
    )
    await controller.async_initialize()

    assert controller.ocpp_target is None
    assert controller.energy_register_entity_id is None
    await controller.async_shutdown()



def test_the_real_ocpp_012_register_unique_id_parses_without_the_entity_id_fallback() -> None:
    """Taken verbatim from a real OCPP 0.12 entity registry. A sensor's unique id
    puts its platform *last*, unlike a switch's; parsing it must not depend on
    the entity id, which a person may have renamed."""
    from custom_components.spotnav.vehicles.ocpp_identity import (
        OcppConnectorTarget,
        _energy_register_target_from_unique_id,
        target_from_unique_id,
    )

    register = "ocpp.halo_charger.conn1.energy_active_import_register.sensor"
    switch = "switch.ocpp.halo_charger.conn1.charge_control"

    assert _energy_register_target_from_unique_id(register) == OcppConnectorTarget("halo_charger", 1)
    # The same charger and connector the charge control names.
    assert _energy_register_target_from_unique_id(register) == target_from_unique_id(switch)
    # A single-connector charge point, in the same trailing-platform form.
    assert _energy_register_target_from_unique_id(
        "ocpp.station_a.energy_active_import_register.sensor"
    ) == OcppConnectorTarget("station_a", 1)
    # And still restricted to the register key.
    assert _energy_register_target_from_unique_id(
        "ocpp.halo_charger.conn1.energy_session.sensor"
    ) is None


async def test_the_options_flow_of_an_ocpp_charger_keeps_a_session_number_current_control(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    """An entry stored with the number kind round-trips through Configure unchanged."""
    from homeassistant.data_entry_flow import FlowResultType

    charger = two_connector_charger(hass, ocpp_owner)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: charger["charge_control_1"],
            CONF_CURRENT_LIMIT: charger["session_1"],
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_NUMBER,
            CONF_ENERGY_REGISTER_ENTITY: "",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    field = next(key for key in schema if key == CONF_CURRENT_CONTROL)
    offered = {option["value"] for option in schema[field].config["options"]}
    assert CURRENT_CONTROL_NUMBER in offered

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CHARGE_CONTROL: charger["charge_control_1"],
            CONF_CURRENT_LIMIT: charger["session_1"],
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_NUMBER,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_CURRENT_CONTROL] == CURRENT_CONTROL_NUMBER
    assert entry.data[CONF_CURRENT_LIMIT] == charger["session_1"]
