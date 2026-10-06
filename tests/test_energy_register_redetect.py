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
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.vehicles.ocpp_identity import (
    OcppConnectorTarget,
    energy_register_entity_for,
)
from tests.helpers import make_ocpp_config_entry

from .charger_shapes import E, register_shape, SHAPES
from .world import CPID, ocpp_entity, station_device

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
