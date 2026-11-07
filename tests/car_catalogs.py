"""Register a catalogued car as its integration would (`tests/fixtures/cars/<domain>.json`) and read SpotNav's picks.

Each catalog holds an integration's real entity shapes, read from its source at the recorded commit: keys, unique
ids, English names, device classes, units and options. The states are invented; only the shapes are real.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.vehicles import vehicle_discovery as vd
from custom_components.spotnav.vehicles.identification_sources import identification_sources

CATALOGS = Path(__file__).parent / "fixtures" / "cars"

VIN = "WF0XXXGCDX1234567"
_IDS = {
    "vin": VIN, "vin_lower": VIN.lower(), "vin_lowercase": VIN.lower(), "entry_id": "01JCARENTRY000000000000001",
    "plate": "ABC123", "vehicle_id": "a1b2c3d4-0000-4000-8000-000000000001",
    "vehicle.id": "a1b2c3d4-0000-4000-8000-000000000001", "profile_id": "p0001",
}
# What Home Assistant shows for an entity named by its device class only.
_DEVICE_CLASS_NAMES = {
    "battery": "Battery", "plug": "Plug", "battery_charging": "Charging", "distance": "Distance",
    "connectivity": "Connectivity", "power": "Power", "current": "Current", "energy": "Energy",
    "voltage": "Voltage", "door": "Door", "lock": "Lock", "energy_storage": "Stored energy",
}
# Device classes an integration sets at runtime (read in its source; the catalog holds the static description).
_RUNTIME_DEVICE_CLASS = {("fordpass", "soc"): "battery"}
_PLACEHOLDER = re.compile(r"\{([^}]*)\}")


def load(domain: str) -> dict[str, Any]:
    return json.loads((CATALOGS / f"{domain}.json").read_text())


def domains() -> list[str]:
    return sorted(path.stem for path in CATALOGS.glob("*.json"))


def literal_names(data: dict[str, Any]) -> bool:
    """The integration names its entities literally, with the car's make, model or nickname in front."""
    return any("{" in (entity.get("name_en") or "") for entity in data["entities"])


def _fill(pattern: str, fills: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda match: fills.get(match.group(1), "x1"), pattern)


def _name(entity: dict[str, Any], fills: dict[str, str]) -> str | None:
    name = entity.get("name_en")
    if not name or name.startswith("(device class name"):
        return _DEVICE_CLASS_NAMES.get(entity.get("device_class"))
    if name.startswith("(device name"):
        return None
    return _fill(name, fills)


def _device_name(data: dict[str, Any], fills: dict[str, str]) -> str:
    """A literal-named integration's device is called what its entity names start with."""
    for entity in data["entities"]:
        name = entity.get("name_en") or ""
        prefix = re.match(r"((?:\{[^}]*\}\s*)+)", name)
        if prefix:
            return _fill(prefix.group(1), fills).strip()
    return "My car"


def _device_class(domain: str, entity: dict[str, Any]) -> str | None:
    return _RUNTIME_DEVICE_CLASS.get((domain, entity["key"]), entity.get("device_class"))


def _attributes(domain: str, entity: dict[str, Any], fills: dict[str, str]) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    if device_class := _device_class(domain, entity):
        attributes["device_class"] = device_class
    if entity.get("unit"):
        attributes["unit_of_measurement"] = entity["unit"]
    if entity.get("state_class"):
        attributes["state_class"] = entity["state_class"]
    options = entity.get("options")
    if entity["platform"] == "number" and isinstance(options, dict):
        for field, default in (("min", 0.0), ("max", 100.0), ("step", 1.0)):
            try:
                attributes[field] = float(options.get(field))
            except (TypeError, ValueError):
                attributes[field] = default
    elif isinstance(options, list) and (
        entity["platform"] == "select" or (entity["platform"] == "sensor" and entity.get("device_class") == "enum")
    ):
        attributes["options"] = options
    if (name := _name(entity, fills)) is not None:
        attributes["friendly_name"] = name
    return attributes


def register(
    hass: HomeAssistant, data: dict[str, Any], *, all_enabled: bool, nickname: str = "Car"
) -> tuple[str, dict[str, str]]:
    """Register the car's device and entities with plausible states: (device id, entity id -> `platform:key`).

    `nickname` fills the make, model and nickname of a literal-named integration's names and device.
    """
    fills = {**_IDS, "make": nickname, "brandName": nickname, "modelName": nickname,
             "nickname_or_model": nickname, "nickname": nickname}
    literal = literal_names(data)
    entry = MockConfigEntry(domain=data["domain"])
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(data["domain"], VIN)}, name=_device_name(data, fills)
    )
    registry = er.async_get(hass)
    keys: dict[str, str] = {}
    for entity in data["entities"]:
        unique_id = _fill(entity.get("unique_id_pattern") or f"{VIN}_{entity['key']}", fills)
        if registry.async_get_entity_id(entity["platform"], data["domain"], unique_id):
            continue
        enabled = all_enabled or entity.get("enabled_by_default", True) is not False
        name = _name(entity, fills)
        registered = registry.async_get_or_create(
            entity["platform"], data["domain"], unique_id, device_id=device.id,
            suggested_object_id=slugify(f"{device.name} {name or entity['key']}"),
            translation_key=entity.get("translation_key"),
            original_name=name,
            has_entity_name=not literal,
            original_device_class=_device_class(data["domain"], entity),
            unit_of_measurement=entity.get("unit"),
            disabled_by=None if enabled else er.RegistryEntryDisabler.INTEGRATION,
        )
        keys[registered.entity_id] = f"{entity['platform']}:{entity['key']}"
        if enabled:
            state = entity.get("example_state")
            hass.states.async_set(
                registered.entity_id, "unknown" if state is None else str(state), _attributes(data["domain"], entity, fills)
            )
    return device.id, keys


def picks(hass: HomeAssistant, device_id: str, keys: dict[str, str]) -> dict[str, Any]:
    """SpotNav's picks for the car, as `platform:key` (`None`: nothing picked): whether it is a vehicle, its state
    of charge, the charge limit it writes, the charge target it reads, its plug source and its location source."""
    vehicle = next((item for item in vd.discover_vehicles(hass) if item.id == device_id), None)
    plug, location = identification_sources(hass, device_id)

    def key(entity_id: str | None) -> str | None:
        return None if entity_id is None else keys.get(entity_id, entity_id)

    target = vd._resolve_charge_limit(vd._device_entities(hass, er.async_get(hass), device_id), None)
    return {
        "vehicle": vehicle is not None,
        "soc": key(vd.vehicle_soc_entity_id(hass, device_id)),
        "limit": key(vd.vehicle_charge_limit_entity_id(hass, device_id)),
        # The charge target read, writable (the limit) or a read-only ceiling.
        "target": key(target.entity_id),
        "plug": key(plug.entity_id),
        "location": key(location.entity_id),
    }
