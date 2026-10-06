"""A charger's lifetime energy register is found after setup too, and a person's "none" is kept.

Detection used to run once, when the charger was added. An OCPP charger that was offline then has
registry entries without device or state class and no state, so its register was refused, and a register
the integration registered only later was never seen. The register is now accepted by the profile's own
key while nothing contradicts it, resolved again at every start and when a candidate appears, and stored
for a detected charger as detection would have stored it. A person who chose "none" is never overruled.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service, MockConfigEntry

from custom_components.spotnav.const import (
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_ENERGY_REGISTER_NONE,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    MODE_OCPP,
)
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.vehicles.ocpp_identity import (
    OcppConnectorTarget,
    energy_register_entity_for,
)
from tests.helpers import make_entry, make_ocpp_config_entry

from .charger_helpers import detected_config
from .charger_shapes import E, register_shape, SHAPES
from .world import controller_of, CPID, ocpp_entity, station_device

REGISTER_KEY = "energy_active_import_register"
PEBLAR_REGISTER = "sensor.peblar_energy_total"


@pytest.fixture(name="ocpp_owner")
def ocpp_owner_fixture(hass: HomeAssistant) -> MockConfigEntry:
    return make_ocpp_config_entry(hass, entry_id="entry_ocpp_owner")


def _peblar(*, register: E | None | str = "keep"):
    """Peblar's recorded shape, with its lifetime register as recorded, replaced, or left out."""
    shape = SHAPES["peblar"]
    entities = tuple(
        entity
        for entity in shape.entities
        if entity.key != "energy_total"
    )
    if register == "keep":
        entities = shape.entities
    elif register is not None:
        entities = (*entities, register)
    return replace(shape, entities=entities)


def _offline_register() -> E:
    """The lifetime register as an offline charger's integration registers it: no classes, no unit."""
    return E("sensor", "PB777_energy_total", "energy_total", "unknown", {}, translation_key=True)


# ------------------------------------------------------------------ detection by the profile's key


async def test_a_register_the_profile_names_is_found_without_a_state_or_classes(hass: HomeAssistant) -> None:
    ids = register_shape(hass, _peblar(register=_offline_register()))
    hass.states.async_remove(PEBLAR_REGISTER)

    found = detect_charger(hass, ids["device_id"])

    assert found.energy_register == PEBLAR_REGISTER


@pytest.mark.parametrize(
    "attrs",
    [
        {"device_class": "power", "unit_of_measurement": "W"},
        {"device_class": "energy", "state_class": "measurement", "unit_of_measurement": "kWh"},
    ],
)
async def test_a_named_register_whose_classes_contradict_is_refused(
    hass: HomeAssistant, attrs: dict[str, Any]
) -> None:
    ids = register_shape(
        hass, _peblar(register=E("sensor", "PB777_energy_total", "energy_total", "5", attrs, translation_key=True))
    )

    found = detect_charger(hass, ids["device_id"])

    assert found.energy_register is None


async def test_an_offline_ocpp_register_is_found_by_its_key(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    device_id = station_device(hass, ocpp_owner, CPID)
    energy = ocpp_entity(
        hass, owner=ocpp_owner, device_id=device_id, cpid=CPID, domain="sensor", key=REGISTER_KEY, connector=1
    )

    assert energy_register_entity_for(hass, OcppConnectorTarget(CPID, 1)) == energy


async def test_an_ocpp_register_whose_state_contradicts_is_refused(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    device_id = station_device(hass, ocpp_owner, CPID)
    ocpp_entity(
        hass,
        owner=ocpp_owner,
        device_id=device_id,
        cpid=CPID,
        domain="sensor",
        key=REGISTER_KEY,
        connector=1,
        state="1.2",
        attributes={"device_class": "power", "unit_of_measurement": "kW"},
    )

    assert energy_register_entity_for(hass, OcppConnectorTarget(CPID, 1)) is None


# ------------------------------------------------------------------ a detected charger, at start and later


async def _detected_charger(hass: HomeAssistant, ids: dict[str, str], **extra: Any) -> MockConfigEntry:
    found = detect_charger(hass, ids["device_id"])
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER,
        "webhook_id": "hook-peblar",
        **detected_config(found),
        CONF_ENERGY_REGISTER_ENTITY: "",
        **extra,
    }
    entry = MockConfigEntry(domain=DOMAIN, entry_id="det_peblar", title="peblar", data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_a_detected_charger_stored_without_its_register_finds_it_at_start(hass: HomeAssistant) -> None:
    entry = await _detected_charger(hass, register_shape(hass, _peblar()))

    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == PEBLAR_REGISTER
    controller = controller_of(hass, entry.entry_id)
    assert controller.energy_register_entity_id == PEBLAR_REGISTER
    assert controller.adapter.energy_entity_id == PEBLAR_REGISTER


async def test_a_register_that_appears_after_start_is_found_and_kept(hass: HomeAssistant) -> None:
    ids = register_shape(hass, _peblar(register=None))
    entry = await _detected_charger(hass, ids)
    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == ""
    assert controller_of(hass, entry.entry_id).energy_register_entity_id is None

    # The integration registers the register once the charger first reports, without classes yet.
    er.async_get(hass).async_get_or_create(
        "sensor",
        "peblar",
        "PB777_energy_total",
        suggested_object_id="peblar_energy_total",
        config_entry=hass.config_entries.async_get_entry("peblar"),
        device_id=ids["device_id"],
        translation_key="energy_total",
    )
    await hass.async_block_till_done()

    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == PEBLAR_REGISTER
    controller = controller_of(hass, entry.entry_id)
    assert controller.energy_register_entity_id == PEBLAR_REGISTER
    assert controller.adapter.energy_entity_id == PEBLAR_REGISTER


async def test_a_register_whose_first_state_makes_it_one_is_found(hass: HomeAssistant) -> None:
    """An energy sensor the profile does not name qualifies by its classes, which arrive with its state."""
    ids = register_shape(hass, _peblar(register=None))
    meter = er.async_get(hass).async_get_or_create(
        "sensor",
        "peblar",
        "PB777_meter_reading",
        suggested_object_id="peblar_meter_reading",
        config_entry=hass.config_entries.async_get_entry("peblar"),
        device_id=ids["device_id"],
    )
    entry = await _detected_charger(hass, ids)
    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == ""

    hass.states.async_set(
        meter.entity_id,
        "120.5",
        {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"},
    )
    await hass.async_block_till_done()

    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == meter.entity_id


async def test_a_persons_none_is_kept_at_start_and_when_a_register_appears(hass: HomeAssistant) -> None:
    ids = register_shape(hass, _peblar(register=None))
    entry = await _detected_charger(hass, ids, **{CONF_ENERGY_REGISTER_NONE: True})

    er.async_get(hass).async_get_or_create(
        "sensor",
        "peblar",
        "PB777_energy_total",
        suggested_object_id="peblar_energy_total",
        config_entry=hass.config_entries.async_get_entry("peblar"),
        device_id=ids["device_id"],
        translation_key="energy_total",
    )
    await hass.async_block_till_done()

    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == ""
    assert entry.data[CONF_ENERGY_REGISTER_NONE] is True
    assert controller_of(hass, entry.entry_id).energy_register_entity_id is None


async def test_a_smart_plug_charger_is_left_to_its_power(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.plug_power", "0", {"device_class": "power", "unit_of_measurement": "W"})
    entry = await _detected_charger(
        hass, register_shape(hass, _peblar()), power_entity="sensor.plug_power"
    )

    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == ""


# ------------------------------------------------------------------ an OCPP charger (automatic)


async def _ocpp_charger(hass: HomeAssistant, ocpp_owner: MockConfigEntry, **extra: Any) -> tuple[MockConfigEntry, str]:
    device_id = station_device(hass, ocpp_owner, CPID)
    control = ocpp_entity(
        hass, owner=ocpp_owner, device_id=device_id, cpid=CPID, domain="switch", key="charge_control", connector=1,
        state="off",
    )
    async_mock_service(hass, "switch", "turn_on")
    entry = make_entry(
        hass,
        entry_id="halo",
        charge_control=control,
        current_limit=None,
        webhook_id="webhook-halo",
        title="halo",
        ocpp_target=(CPID, 1),
        extra={CONF_MODE: MODE_OCPP, CONF_ENERGY_REGISTER_ENTITY: "", **extra},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, device_id


async def test_an_ocpp_register_registered_after_start_is_used_automatically(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    entry, device_id = await _ocpp_charger(hass, ocpp_owner)
    controller = controller_of(hass, entry.entry_id)
    assert controller.energy_register_entity_id is None

    energy = ocpp_entity(
        hass, owner=ocpp_owner, device_id=device_id, cpid=CPID, domain="sensor", key=REGISTER_KEY, connector=1
    )
    await hass.async_block_till_done()

    assert controller.energy_register_entity_id == energy
    assert controller.adapter.energy_entity_id == energy
    # An OCPP connector's register stays automatic: nothing is written into the entry.
    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == ""


async def test_an_ocpp_chargers_none_switches_the_automatic_register_off(
    hass: HomeAssistant, ocpp_owner: MockConfigEntry
) -> None:
    device_id = station_device(hass, ocpp_owner, CPID)
    ocpp_entity(
        hass, owner=ocpp_owner, device_id=device_id, cpid=CPID, domain="sensor", key=REGISTER_KEY, connector=1
    )
    entry, _ = await _ocpp_charger(hass, ocpp_owner, **{CONF_ENERGY_REGISTER_NONE: True})

    assert controller_of(hass, entry.entry_id).energy_register_entity_id is None
