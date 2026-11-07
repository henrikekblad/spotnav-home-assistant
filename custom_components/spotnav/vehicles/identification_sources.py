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
  Every other `binary_sensor`, and every unit-less `sensor` of device class none or `enum`, on the car's
  device is offered as `others`: detection may miss or reject a sensor, a person is never stuck.
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

# Never a plug signal, whatever the key or device class: a lock or latch, and the charge port's door, flap or lid.
_PLUG_EXCLUDED_PHRASES: Final = (
    "lock", "locked", "latch", "door", "flap", "lid", "cover", "window",
)
# Not a plug signal unless the key also names the cable or plug ("charging cable" is one, "charging" is not):
# the car's online state, a charging-in-progress sensor, and the charge port itself (Kia's `ev_charge_port` is
# the port door, open while charging or not; BMW CarData's `chargingPort.status`, "Charging Port plug state",
# names the plug and is one).
_PLUG_SOFT_EXCLUDED_PHRASES: Final = ("online", "cloud", "internet", "charging", "port")
# A text sensor for the DC inlet only says nothing of a car on an AC charger: ranked after the others.
_PLUG_DC_PHRASES: Final = ("dc",)
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
    (a chosen `None` is "none"); `candidates` are what detection found, sorted. `others` (plug only) are the
    car's other entities that could carry a plug reading, which a person may choose too, sorted by name.
    """

    entity_id: str | None
    chosen: bool
    candidates: tuple[str, ...]
    others: tuple[str, ...] = ()


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
            # One that names the plug before one that names only a connection, and the DC inlet's last.
            rank = 2 if names_plug else 3
            return rank + 2 if key.has_any(_PLUG_DC_PHRASES) else rank
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


def _plug_others(entities: list[tuple[er.RegistryEntry, State]], candidates: tuple[str, ...]) -> tuple[str, ...]:
    """The car's other entities that could carry a plug reading, sorted by name: a `binary_sensor` of any
    device class, or a `sensor` with no unit and device class none or `enum`. Detection may reject a sensor
    (or miss it); a person is never stuck because of that."""
    found = []
    for entry, state in entities:
        if entry.entity_id in candidates:
            continue
        if entry.domain == "binary_sensor" or (
            entry.domain == "sensor"
            and not state.attributes.get("unit_of_measurement")
            and state.attributes.get("device_class") in (None, "enum")
        ):
            found.append(entry.entity_id)
    return tuple(sorted(found, key=lambda item: ((_name_of(entities, item) or item).casefold(), item)))


def _name_of(entities: list[tuple[er.RegistryEntry, State]], entity_id: str) -> str | None:
    return next((state.name for entry, state in entities if entry.entity_id == entity_id), None)


# A tracker for the navigation's destination (Tesla Fleet's `route`) or the route's start (Teslemetry's
# `origin`), not where the car is.
_LOCATION_EXCLUDED_PHRASES: Final = ("route", "destination", "navigation", "origin")
# A last known or parked position: a tracker that may lag where the car is, used only when it is the only kind.
_LOCATION_DEMOTED_PHRASES: Final = ("parked", "last", "previous")


def _location_candidates(entities: list[tuple[er.RegistryEntry, State]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(every tracker that may be the car's position, the preferred ones), each sorted."""
    trackers = [
        entry
        for entry, _ in entities
        if entry.domain == "device_tracker" and not entity_key(entry).has_any(_LOCATION_EXCLUDED_PHRASES)
    ]
    live = [entry for entry in trackers if not entity_key(entry).has_any(_LOCATION_DEMOTED_PHRASES)]
    return tuple(sorted(entry.entity_id for entry in trackers)), tuple(
        sorted(entry.entity_id for entry in live or trackers)
    )


def _chosen(hass: HomeAssistant, vehicle_id: str) -> dict[str, str | None]:
    store = domain_data(hass).decision_store
    payload = None if store is None else store.confirmed_payload(DECISION_DOMAIN_VEHICLE_IDENTIFICATION, vehicle_id)
    return {} if payload is None else {key: payload[key] for key in SOURCES if key in payload}


def _source(
    hass: HomeAssistant,
    chosen: dict[str, str | None],
    kind: str,
    all_: tuple[str, ...],
    best: tuple[str, ...],
    others: tuple[str, ...] = (),
) -> IdentificationSource:
    if kind in chosen:
        entity_id = chosen[kind]
        if entity_id is not None and er.async_get(hass).async_get(entity_id) is None:
            entity_id = None
        return IdentificationSource(entity_id, True, all_, others)
    return IdentificationSource(best[0] if len(best) == 1 else None, False, all_, others)


def identification_sources(
    hass: HomeAssistant, vehicle_id: str
) -> tuple[IdentificationSource, IdentificationSource]:
    """This vehicle's plug source and location source, as chosen or detected now."""
    entities = _device_entities(hass, vehicle_id)
    chosen = _chosen(hass, vehicle_id)
    plugs, best_plugs = _plug_candidates(entities)
    trackers, best_trackers = _location_candidates(entities)
    return (
        _source(hass, chosen, SOURCE_PLUG, plugs, best_plugs, _plug_others(entities, plugs)),
        _source(hass, chosen, SOURCE_LOCATION, trackers, best_trackers),
    )


def _name(hass: HomeAssistant, entity_id: str | None) -> str | None:
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    return None if state is None else state.name


def _state_text(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    return None if state is None else state.state


_READING_WORDS: Final = {True: "plugged", False: "unplugged", None: "nothing"}


def _row(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    return {"entity_id": entity_id, "name": _name(hass, entity_id), "state": _state_text(hass, entity_id)}


def sources_block(hass: HomeAssistant, vehicle_id: str) -> dict[str, dict[str, Any]]:
    """A vehicle row's `identification`: per source its `entity_id` and `name` (`null`: none, or a choice not
    made), `chosen` (a person decided it; a chosen `null` is "none") and the `candidates` to choose from, each
    with its current `state`. The plug source adds `others` (the car's other entities a person may choose,
    same shape) and `reading`: what the source says now, `plugged`, `unplugged` or `nothing` (`null`: no
    source)."""
    block: dict[str, dict[str, Any]] = {}
    for kind, source in zip(SOURCES, identification_sources(hass, vehicle_id), strict=True):
        block[kind] = {
            "entity_id": source.entity_id,
            "name": _name(hass, source.entity_id),
            "chosen": source.chosen,
            "candidates": [_row(hass, item) for item in source.candidates],
        }
        if kind == SOURCE_PLUG:
            block[kind]["others"] = [_row(hass, item) for item in source.others]
            block[kind]["reading"] = (
                None
                if source.entity_id is None
                else _READING_WORDS[plug_reading(hass.states.get(source.entity_id))]
            )
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
    source = plug if kind == SOURCE_PLUG else location
    candidates = (*source.candidates, *source.others)
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
