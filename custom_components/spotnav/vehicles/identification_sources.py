"""A car's own "plugged in" and "where am I" entities, read only to tell which car is at a charger.

Vehicle detection (`vehicle_discovery.py`) rejects plug sensors and trackers on purpose: they are never a
state of charge, a range or a way to control a charge. They are read here, and only by vehicle
identification (`identification.py`), never written and never used to wake a car.

* **Plug.** Ranked by kind, best first: a `binary_sensor` with device class `plug`; one with device class
  `connectivity` (or none) whose key names a cable, a plug or a charger connection; a text `sensor` (no unit)
  whose key names a plug, a connector or a connection. A lock or latch and the charge port's door, flap or lid
  are never one; the car's own online state and a charging sensor are not, unless the key names the cable
  ("charging cable"). Exactly one of the best kind found is detected; several are a
  choice a person makes.
* **Location.** The device's `device_tracker`: exactly one is detected, several are a choice.
* **A person's choice** is a discovery decision (`DECISION_DOMAIN_VEHICLE_IDENTIFICATION`) per vehicle:
  `{"plug": entity id | None, "location": entity id | None}`, a key only once chosen. `None` is "none": the
  car has no such source. A chosen entity that is no longer registered is no source, and the decision is kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from homeassistant.const import STATE_HOME, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..runtime import domain_data
from .discovery_decisions import DECISION_DOMAIN_VEHICLE_IDENTIFICATION
from .entity_keys import EntityKey, entity_key

SOURCE_PLUG: Final = "plug"
SOURCE_LOCATION: Final = "location"
SOURCES: Final = (SOURCE_PLUG, SOURCE_LOCATION)
#: What a person sends to say the car has no such source.
CHOICE_NONE: Final = "none"

# Never a plug signal, whatever the device class: a lock, a door or flap, the car's online state, charging.
# Never a plug signal, whatever the key or device class: a lock or latch, and the charge port's door, flap or lid
# (Kia's `ev_charge_port` is the port door, open while charging or not; its `switch` opens it).
_PLUG_EXCLUDED_PHRASES: Final = (
    "lock", "locked", "latch", "door", "flap", "lid", "cover", "window", "port",
)
# Not a plug signal unless the key also names the cable or plug ("charging cable" is one, "charging" is not):
# the car's online state and a charging-in-progress sensor.
_PLUG_SOFT_EXCLUDED_PHRASES: Final = ("online", "cloud", "internet", "charging")
# A connectivity (or class-less) binary sensor counts when its key says it is the cable or plug.
_PLUG_PHRASES: Final = ("plug", "plugged", "cable", "gun", "connector", "coupler", "inlet")
_PLUG_FRAGMENTS: Final = ("chargerconnected", "chargercable", "conncharge", "plugged", "chargecable")
# A text sensor counts when its key names the plug or the connection.
_PLUG_TEXT_FRAGMENTS: Final = ("plug", "connection", "connector", "coupler", "cable", "gunstate")

# Text values, squashed (lowercase, no separators). Checked in this order: nothing, unplugged, plugged.
_PLUG_UNKNOWN_FRAGMENTS: Final = ("error", "fault", "unknown", "invalid", "unavailable")
_PLUG_OFF_FRAGMENTS: Final = ("unplug", "disconnect", "notplug", "notconnect", "unconnect", "noplug", "nocable")
_PLUG_ON_FRAGMENTS: Final = ("plug", "connect")


@dataclass(frozen=True, slots=True)
class IdentificationSource:
    """One of a vehicle's identification sources.

    `entity_id` is what is read (`None`: none, or a choice not yet made); `chosen` says a person decided it
    (a chosen `None` is "none"); `candidates` are what can be chosen, sorted.
    """

    entity_id: str | None
    chosen: bool
    candidates: tuple[str, ...]


def _squashed(text: str) -> str:
    return "".join(character for character in text.lower() if character.isalnum())


def plug_reading(state: State | None) -> bool | None:
    """Whether a plug entity says the car is plugged in (`True`), unplugged (`False`), or nothing (`None`)."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    if state.domain == "binary_sensor":
        return {"on": True, "off": False}.get(state.state)
    value = _squashed(state.state)
    if not value or any(fragment in value for fragment in _PLUG_UNKNOWN_FRAGMENTS):
        return None
    if any(fragment in value for fragment in _PLUG_OFF_FRAGMENTS):
        return False
    if any(fragment in value for fragment in _PLUG_ON_FRAGMENTS):
        return True
    return None


def location_reading(state: State | None) -> bool | None:
    """Whether a tracker says the car is at home (`True`), elsewhere (`False`), or nothing (`None`).

    Home Assistant's own zone test decides the state (`home` is inside the home zone, give or take the
    reported accuracy); this only reads it.
    """
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    return state.state == STATE_HOME


def _plug_rank(entry: er.RegistryEntry, state: State) -> int | None:
    """0 for the best plug signal, higher for weaker ones, `None` for none."""
    key: EntityKey = entity_key(entry)
    if key.has_any(_PLUG_EXCLUDED_PHRASES):
        return None
    names_plug = key.has_any(_PLUG_PHRASES) or key.contains_any(_PLUG_FRAGMENTS)
    if key.has_any(_PLUG_SOFT_EXCLUDED_PHRASES) and not names_plug:
        return None
    device_class = state.attributes.get("device_class")
    if entry.domain == "binary_sensor":
        if device_class == "plug":
            return 0
        if device_class in ("connectivity", None) and (
            key.has_any(_PLUG_PHRASES) or key.contains_any(_PLUG_FRAGMENTS)
        ):
            return 1
        return None
    if entry.domain == "sensor":
        if state.attributes.get("unit_of_measurement") or device_class not in (None, "enum"):
            return None
        if key.contains_any(_PLUG_TEXT_FRAGMENTS):
            return 2
    return None


def _device_entities(hass: HomeAssistant, vehicle_id: str) -> list[tuple[er.RegistryEntry, State]]:
    registry = er.async_get(hass)
    return [
        (entry, state)
        for entry in sorted(er.async_entries_for_device(registry, vehicle_id), key=lambda item: item.entity_id)
        if (state := hass.states.get(entry.entity_id)) is not None
    ]


def _plug_candidates(entities: list[tuple[er.RegistryEntry, State]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(every plug candidate, the best-ranked ones), each sorted."""
    ranked = [(rank, entry.entity_id) for entry, state in entities if (rank := _plug_rank(entry, state)) is not None]
    if not ranked:
        return (), ()
    best = min(rank for rank, _ in ranked)
    return tuple(sorted(e for _, e in ranked)), tuple(sorted(e for rank, e in ranked if rank == best))


def _location_candidates(entities: list[tuple[er.RegistryEntry, State]]) -> tuple[str, ...]:
    return tuple(sorted(entry.entity_id for entry, _ in entities if entry.domain == "device_tracker"))


def _chosen(hass: HomeAssistant, vehicle_id: str) -> dict[str, str | None]:
    store = domain_data(hass).decision_store
    payload = None if store is None else store.confirmed_payload(DECISION_DOMAIN_VEHICLE_IDENTIFICATION, vehicle_id)
    return {} if payload is None else {key: payload[key] for key in SOURCES if key in payload}


def _source(
    hass: HomeAssistant, chosen: dict[str, str | None], kind: str, all_: tuple[str, ...], best: tuple[str, ...]
) -> IdentificationSource:
    if kind in chosen:
        entity_id = chosen[kind]
        if entity_id is not None and er.async_get(hass).async_get(entity_id) is None:
            entity_id = None
        return IdentificationSource(entity_id, True, all_)
    return IdentificationSource(best[0] if len(best) == 1 else None, False, all_)


def identification_sources(
    hass: HomeAssistant, vehicle_id: str
) -> tuple[IdentificationSource, IdentificationSource]:
    """This vehicle's plug source and location source, as chosen or detected now."""
    entities = _device_entities(hass, vehicle_id)
    chosen = _chosen(hass, vehicle_id)
    plugs, best_plugs = _plug_candidates(entities)
    trackers = _location_candidates(entities)
    return (
        _source(hass, chosen, SOURCE_PLUG, plugs, best_plugs),
        _source(hass, chosen, SOURCE_LOCATION, trackers, trackers),
    )


def _name(hass: HomeAssistant, entity_id: str | None) -> str | None:
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    return None if state is None else state.name


def sources_block(hass: HomeAssistant, vehicle_id: str) -> dict[str, dict[str, Any]]:
    """A vehicle row's `identification`: per source its `entity_id` and `name` (`null`: none, or a choice not
    made), `chosen` (a person decided it; a chosen `null` is "none") and the `candidates` to choose from."""
    block: dict[str, dict[str, Any]] = {}
    for kind, source in zip(SOURCES, identification_sources(hass, vehicle_id), strict=True):
        block[kind] = {
            "entity_id": source.entity_id,
            "name": _name(hass, source.entity_id),
            "chosen": source.chosen,
            "candidates": [{"entity_id": item, "name": _name(hass, item)} for item in source.candidates],
        }
    return block


async def async_choose_source(hass: HomeAssistant, vehicle_id: str, kind: str, entity_id: str | None) -> None:
    """Record a person's choice of one source: a candidate entity, `"none"`, or `None` for automatic.

    Raises `ValueError` for an unknown vehicle, kind or entity; nothing is written then.
    """
    if kind not in SOURCES:
        raise ValueError("unknown identification source")
    store = domain_data(hass).decision_store
    if store is None or not isinstance(vehicle_id, str) or dr.async_get(hass).async_get(vehicle_id) is None:
        raise ValueError("unknown vehicle")
    plug, location = identification_sources(hass, vehicle_id)
    candidates = (plug if kind == SOURCE_PLUG else location).candidates
    if entity_id is not None and entity_id != CHOICE_NONE and entity_id not in candidates:
        raise ValueError("not one of the vehicle's candidates")
    chosen = _chosen(hass, vehicle_id)
    if entity_id is None:
        chosen.pop(kind, None)
    else:
        chosen[kind] = None if entity_id == CHOICE_NONE else entity_id
    if chosen:
        await store.async_confirm(DECISION_DOMAIN_VEHICLE_IDENTIFICATION, vehicle_id, chosen)
    else:
        await store.async_unconfirm(DECISION_DOMAIN_VEHICLE_IDENTIFICATION, vehicle_id)
