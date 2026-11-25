"""A home battery mirrored from another Home Assistant by Remote Home-Assistant: its brand found by the adapters' own
keys in the mirrored entity ids, read only, never controlled (`battery/mirrored.py`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.battery.commands import control_for
from custom_components.spotnav.battery.facts import EntityFact
from custom_components.spotnav.battery.mirrored import (
    BATTERY_NOT_POWER,
    MATCHED,
    MIRROR_PLATFORM,
    NEED_MIRRORED_READ_ONLY,
    NO_BRAND_MATCH,
    READ_ONLY_MIRRORED,
    match_adapter,
    mirror_key,
    mirrored_facts,
)
from custom_components.spotnav.battery.runtime import read_battery_facts
from custom_components.spotnav.const import CONF_BATTERY_AGGREGATE_POWER_ENTITY, CONF_BATTERY_POWER_INVERTED
from custom_components.spotnav.runtime import site_controller_for

from tests.world import setup_charger_and_site

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "battery" / "mirrored_solax.json").read_text(encoding="utf-8")
)
REMOTE = FIXTURE["remote_uuid"][:16]


def _fact(row: dict[str, Any], *, prefix_from: str = FIXTURE["prefix"], prefix_to: str = FIXTURE["prefix"]) -> EntityFact:
    """A row as Remote Home-Assistant registers it: its own platform, `<remote>_<local entity id>`, no device."""
    domain, object_id = row["entity_id"].split(".", 1)
    entity_id = f"{domain}.{prefix_to}{object_id.removeprefix(prefix_from)}"
    return EntityFact(
        entity_id=entity_id, platform=MIRROR_PLATFORM, unique_id=f"{REMOTE}_{entity_id}", state=row["state"],
        unit=row["unit"], device_class=row["device_class"],
    )


def _set(*, without: tuple[str, ...] = (), prefix: str = FIXTURE["prefix"]) -> tuple[EntityFact, list[EntityFact]]:
    entities = [_fact(row, prefix_to=prefix) for row in FIXTURE["entities"] if row["entity_id"] not in without]
    battery_id = f"sensor.{prefix}{FIXTURE['battery'].split('.', 1)[1].removeprefix(FIXTURE['prefix'])}"
    return next(entity for entity in entities if entity.entity_id == battery_id), entities


def test_the_mirrored_solax_battery_is_read_as_solax_with_its_level_size_and_power_and_never_controlled() -> None:
    battery, entities = _set()

    facts = mirrored_facts(battery, entities, prefix="grid_", power_kw=-1.243)

    assert facts.brand == "solax" and facts.integration == MIRROR_PLATFORM
    assert facts.read_only == READ_ONLY_MIRRORED
    assert facts.soc_pct == 62.0, "the SolaX level, not the BMS' battery 1"
    assert facts.capacity_kwh == pytest.approx(11.52)
    assert facts.power_kw == pytest.approx(-1.243)
    assert facts.charged_total_kwh == pytest.approx(1520.3) and facts.discharged_total_kwh == pytest.approx(1402.9)
    assert facts.management.state == "free"
    assert facts.hold is None and facts.grid_charge is None
    assert facts.control_brand is None and control_for(facts.control_brand) is None
    assert control_for(facts.brand) is not None, "the brand has a recipe; the mirror is what refuses it"
    assert facts.mirror is not None and facts.mirror["reason"] == MATCHED and facts.mirror["prefix"] == "grid_"
    assert facts.mirror["missing"] == ["rated_charge", "rated_discharge"]
    # The tester's battery entity is not SolaX's own battery power key: its sign is the site's setting alone.
    assert facts.mirror["power_sign"] == "unknown"
    assert facts.as_dict()["read_only"] == READ_ONLY_MIRRORED


def test_without_the_size_mirrored_the_size_is_left_to_be_learnt_and_said_so() -> None:
    """solax_modbus's size is disabled by default: then it is not on the other instance and not mirrored."""
    battery, entities = _set(without=("sensor.grid_solax1_battery_system_installed_capacity",))

    facts = mirrored_facts(battery, entities, prefix="grid_")

    assert facts.brand == "solax" and facts.capacity_kwh is None
    assert facts.mirror is not None and "capacity" in facts.mirror["learnt_if_missing"]


@pytest.mark.parametrize(("inverted", "expected"), [(False, "as_brand"), (True, "differs_from_brand")])
def test_the_brand_s_own_power_key_is_checked_against_the_site_s_sign(inverted: bool, expected: str) -> None:
    """SolaX's `battery_power_charge` is charge-positive (`site_detection.BATTERY_ROWS`): not negated."""
    _battery, entities = _set()
    battery = next(entity for entity in entities if entity.entity_id.endswith("_battery_power_charge"))

    facts = mirrored_facts(battery, entities, prefix="grid_", power_inverted=inverted)

    assert facts.brand == "solax" and facts.mirror is not None and facts.mirror["power_sign"] == expected


@pytest.mark.parametrize("prefix", ["", "remote_", "Grid_"])
def test_any_prefix_of_the_entry_is_taken_off_before_the_keys_are_read(prefix: str) -> None:
    lowered = prefix.lower()
    battery, entities = _set(prefix=lowered)

    match = match_adapter(battery, entities, prefix)

    assert match.adapter is not None and match.adapter.brand == "solax"
    assert match.found["soc"] == f"sensor.{lowered}solax1_battery_capacity"


def test_the_key_is_the_remote_s_object_id_from_the_unique_id_even_after_a_rename_here() -> None:
    unique_id = f"{REMOTE}_sensor.grid_solax1_battery_capacity"

    assert mirror_key("sensor.my_battery_level", unique_id, "grid_") == "solax1_battery_capacity"
    assert mirror_key("sensor.grid_x", "not-of-the-shape", "grid_") == "x"


def test_a_remote_set_without_a_battery_is_not_matched() -> None:
    """Lights, a temperature and a power meter: no brand's level, so no adapter."""
    rows = [
        {"entity_id": "sensor.grid_heat_pump_power", "state": "800", "unit": "W", "device_class": "power"},
        {"entity_id": "sensor.grid_heat_pump_energy", "state": "1200", "unit": "kWh", "device_class": "energy"},
        {"entity_id": "sensor.grid_living_room_temperature", "state": "21", "unit": "°C", "device_class": "temperature"},
        {"entity_id": "light.grid_kitchen", "state": "on", "unit": None, "device_class": None},
    ]
    entities = [_fact(row) for row in rows]

    facts = mirrored_facts(entities[0], entities, prefix="grid_")

    assert facts.brand is None and facts.soc_pct is None and facts.read_only == READ_ONLY_MIRRORED
    assert facts.mirror is not None and facts.mirror["reason"] == NO_BRAND_MATCH
    assert facts.management.reason == "no_adapter"


def test_a_level_in_a_key_many_brands_share_is_ambiguous_and_not_matched() -> None:
    """`battery_soc` and `battery_power` are several brands' keys: the battery stays unknown, and why is said."""
    rows = [
        {"entity_id": "sensor.grid_inverter_battery_power", "state": "100", "unit": "W", "device_class": "power"},
        {"entity_id": "sensor.grid_inverter_battery_soc", "state": "50", "unit": "%", "device_class": "battery"},
    ]
    entities = [_fact(row) for row in rows]

    facts = mirrored_facts(entities[0], entities, prefix="grid_")

    assert facts.brand is None and facts.soc_pct is None
    assert facts.mirror is not None and facts.mirror["reason"].startswith("ambiguous:")
    assert len(facts.mirror["reason"].removeprefix("ambiguous:").split(",")) >= 2


def test_a_level_that_is_not_a_percentage_does_not_count() -> None:
    battery, entities = _set()
    entities = [
        entity if not entity.entity_id.endswith("solax1_battery_capacity") else EntityFact(
            entity_id=entity.entity_id, platform=entity.platform, unique_id=entity.unique_id, state="11.5", unit="kWh",
        )
        for entity in entities
    ]

    match = match_adapter(battery, entities, "grid_")

    assert match.adapter is None or match.found.get("soc") != "sensor.grid_solax1_battery_capacity"


def test_a_battery_entity_that_reads_no_power_is_not_matched() -> None:
    _battery, entities = _set()
    level = next(entity for entity in entities if entity.entity_id.endswith("solax1_battery_capacity"))

    assert match_adapter(level, entities, "grid_").reason == BATTERY_NOT_POWER


# ---- in Home Assistant


def _register(hass: HomeAssistant, prefix: str = FIXTURE["prefix"]) -> str:
    """The fixture's entities as Remote Home-Assistant registers them (no config entry, no device), its entry with
    the prefix option, and their states."""
    MockConfigEntry(
        domain=MIRROR_PLATFORM, unique_id=FIXTURE["remote_uuid"], options={"entity_prefix": prefix},
    ).add_to_hass(hass)
    registry = er.async_get(hass)
    for row in FIXTURE["entities"]:
        domain, object_id = row["entity_id"].split(".", 1)
        entity_id = f"{domain}.{object_id}"
        entry = registry.async_get_or_create(
            domain, MIRROR_PLATFORM, f"{REMOTE}_{entity_id}", suggested_object_id=object_id,
        )
        assert entry.entity_id == entity_id and entry.config_entry_id is None and entry.device_id is None
        attributes: dict[str, Any] = {"unique_id": entry.unique_id}
        if row["unit"]:
            attributes["unit_of_measurement"] = row["unit"]
        if row["device_class"]:
            attributes["device_class"] = row["device_class"]
        hass.states.async_set(entity_id, row["state"], attributes)
    return FIXTURE["battery"]


async def test_read_from_the_registry_the_mirrored_battery_is_solax_and_read_only(hass: HomeAssistant) -> None:
    battery = _register(hass)

    facts = read_battery_facts(
        hass, {CONF_BATTERY_AGGREGATE_POWER_ENTITY: battery, CONF_BATTERY_POWER_INVERTED: False}, -1.243
    )

    assert facts.brand == "solax" and facts.read_only == READ_ONLY_MIRRORED
    assert facts.soc_pct == 62.0 and facts.capacity_kwh == pytest.approx(11.52)
    assert facts.sources["soc"] == "sensor.grid_solax1_battery_capacity"
    assert facts.mirror is not None and facts.mirror["prefix"] == "grid_"
    # Read again with nothing changed: the same answer from the kept match.
    assert read_battery_facts(
        hass, {CONF_BATTERY_AGGREGATE_POWER_ENTITY: battery, CONF_BATTERY_POWER_INVERTED: False}, -1.243
    ) == facts


async def test_on_a_site_the_card_is_told_read_only_and_control_refuses(hass: HomeAssistant) -> None:
    battery = _register(hass)
    _charger, site = await setup_charger_and_site(hass, battery_aggregate_power_entity=battery)
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    control = controller.battery_control
    control.facts = control._read_facts()

    status = control.status()

    assert status["battery_found"] is True and status["battery_read_only"] == READ_ONLY_MIRRORED
    assert status["needs"] == [NEED_MIRRORED_READ_ONLY]
    assert control.recipe() is None
    assert control.hardware_test_refusal() in {"starting", "no_battery_control"}
    assert status["allowed_actions"] == []
