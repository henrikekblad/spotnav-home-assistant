"""A car's own "plugged in" and "where am I" entities, read only to tell which car is at a charger.

Vehicle detection (`vehicle_discovery.py`) rejects plug sensors and trackers on purpose: they are never a
state of charge, a range or a way to control a charge. They are read here, and only by vehicle
identification (`identification.py`), never written and never used to wake a car.

* **Plug.** Ranked by kind, best first: a `binary_sensor` with device class `plug`; one with device class
  `connectivity` (or none) whose key names a cable, a plug or a charger connection; a text `sensor` (no unit)
  whose key names a plug, a connector or a connection, and that can say both: its declared options hold a value
  read as plugged and one read as unplugged (a number is never one). A lock or latch and the charge port's door,
  flap or lid are never one; the car's own online state and a charging sensor are not, unless the key names the
  cable ("charging cable"). Weakest of all, and so only when nothing else is found: a charging-state enum whose
  options hold exactly one unplugged value (`connect_cable`, `disconnected`); read through it, that value is
  unplugged and every other declared value but an unknown one is plugged (`charging`, `conserving`).
  Exactly one of the best kind found is detected; among equals, one whose key says "any" or "all" (a car with
  a plug sensor per port and one for the whole car); otherwise several are a choice a person makes.
  Every other `binary_sensor`, and every unit-less `sensor` of device class none or `enum`, on the car's
  device is offered as `others`: detection may miss or reject a sensor, a person is never stuck.
* **Location.** The device's `device_tracker`, never one for a route's origin or destination: exactly one is
  detected; of several, the one that is not a last or parked position (Polestar's `parked_location` beside its
  live `location`); otherwise several are a choice.
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
from .entity_keys import EntityKey, entity_key, without_device_name

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
_PLUG_FRAGMENTS: Final = (
    "chargerconnected", "chargerconnection", "chargercable", "conncharge", "plugged", "chargecable",
)
# A text sensor counts when its key names the plug or the connection.
_PLUG_TEXT_FRAGMENTS: Final = ("plug", "connection", "connector", "coupler", "cable", "gunstate")

# Text values, squashed (lowercase, no separators). Checked in this order: nothing, unplugged, plugged.
_PLUG_UNKNOWN_FRAGMENTS: Final = ("error", "fault", "unknown", "invalid", "unavailable")
# `connect_cable` is the car asking for the cable (Skoda's charging state with nothing plugged in).
_PLUG_OFF_FRAGMENTS: Final = (
    "unplug", "disconnect", "notplug", "notconnect", "unconnect", "noplug", "nocable", "connectcable",
)
_PLUG_ON_FRAGMENTS: Final = ("plug", "connect")

# The weakest plug kind: a charging-state enum (its key names charging or the charge state) with exactly one
# unplugged value among its options. Ranked after every other kind.
_CHARGING_STATE_PHRASES: Final = ("charging", "charge state")
_CHARGING_STATE_RANK: Final = 6
# Through a charging-state source these say nothing, as the unknown fragments do ("none": no data).
_CHARGING_STATE_UNKNOWN_VALUES: Final = ("none",)
# A tie between equally good plug sensors: the one for the whole car ("anyPosition" beside one per port).
_PLUG_WHOLE_CAR_PHRASES: Final = ("any", "all")


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


def _text_reading(value: str) -> bool | None:
    """How a squashed text value reads: plugged, unplugged or nothing."""
    if not value or any(fragment in value for fragment in _PLUG_UNKNOWN_FRAGMENTS):
        return None
    if any(fragment in value for fragment in _PLUG_OFF_FRAGMENTS):
        return False
    if any(fragment in value for fragment in _PLUG_ON_FRAGMENTS):
        return True
    return None


def _declared_options(state: State) -> list[str]:
    options = state.attributes.get("options")
    return [str(option) for option in options] if isinstance(options, (list, tuple)) else []


def _charging_state_unplugged(state: State) -> str | None:
    """The squashed unplugged value of a charging-state enum, when its options hold exactly one, and at least one
    other value that is not an unknown one; otherwise `None`."""
    values = [_squashed(option) for option in _declared_options(state)]
    unplugged = [value for value in values if _text_reading(value) is False]
    if len(unplugged) != 1:
        return None
    others = [value for value in values if value != unplugged[0] and not _charging_state_unknown(value)]
    return unplugged[0] if others else None


def _charging_state_unknown(value: str) -> bool:
    return (
        not value
        or value in _CHARGING_STATE_UNKNOWN_VALUES
        or any(fragment in value for fragment in _PLUG_UNKNOWN_FRAGMENTS)
    )


def plug_reading(state: State | None, *, charging_state: bool = False) -> bool | None:
    """Whether a plug entity says the car is plugged in (`True`), unplugged (`False`), or nothing (`None`).

    `charging_state`: the entity is a source of the charging-state kind (`is_charging_state_source`). Its one
    unplugged value is unplugged, and every other declared value but an unknown one is plugged.
    """
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    if state.domain == "binary_sensor":
        return {"on": True, "off": False}.get(state.state)
    value = _squashed(state.state)
    if charging_state and (unplugged := _charging_state_unplugged(state)) is not None:
        if value == unplugged:
            return False
        if _charging_state_unknown(value):
            return None
        return True if value in {_squashed(option) for option in _declared_options(state)} else None
    return _text_reading(value)


def is_charging_state_source(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether this entity is a plug source of the charging-state kind, read by its own rule."""
    entry = er.async_get(hass).async_get(entity_id)
    state = hass.states.get(entity_id)
    if entry is None or state is None:
        return False
    device = None if entry.device_id is None else dr.async_get(hass).async_get(entry.device_id)
    return _plug_rank(without_device_name(entry, None if device is None else device.name), state) == (
        _CHARGING_STATE_RANK
    )


def source_plug_reading(hass: HomeAssistant, state: State | None) -> bool | None:
    """What a car's plug source says (`plug_reading`), by the rule of its kind."""
    if state is None:
        return None
    return plug_reading(state, charging_state=is_charging_state_source(hass, state.entity_id))


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
    rank = _plug_kind_rank(entry, state, key)
    if rank is None and _is_charging_state(entry, state, key):
        return _CHARGING_STATE_RANK
    return rank


def _is_charging_state(entry: er.RegistryEntry, state: State, key: EntityKey) -> bool:
    """A charging-state enum with exactly one unplugged value: the weakest plug kind."""
    return (
        entry.domain == "sensor"
        and state.attributes.get("device_class") == "enum"
        and not state.attributes.get("unit_of_measurement")
        and key.has_any(_CHARGING_STATE_PHRASES)
        and _charging_state_unplugged(state) is not None
    )


def _reads_both_ways(state: State) -> bool:
    """A text plug candidate can say plugged and unplugged: its declared options hold one value of each (none
    declared: any text will do), and a number is never one."""
    if _is_number(state.state):
        return False
    options = _declared_options(state)
    if not options:
        return True
    return {True, False} <= {_text_reading(_squashed(option)) for option in options}


def _is_number(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _plug_kind_rank(entry: er.RegistryEntry, state: State, key: EntityKey) -> int | None:
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
        if key.contains_any(_PLUG_TEXT_FRAGMENTS) and _reads_both_ways(state):
            # One that names the plug before one that names only a connection, and the DC inlet's last.
            rank = 2 if names_plug else 3
            return rank + 2 if key.has_any(_PLUG_DC_PHRASES) else rank
    return None


def _device_entities(hass: HomeAssistant, vehicle_id: str) -> list[tuple[er.RegistryEntry, State]]:
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get(vehicle_id)
    device_name = None if device is None else device.name
    return [
        (without_device_name(entry, device_name), state)
        for entry in sorted(er.async_entries_for_device(registry, vehicle_id), key=lambda item: item.entity_id)
        if (state := hass.states.get(entry.entity_id)) is not None
    ]


def _plug_candidates(entities: list[tuple[er.RegistryEntry, State]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(every plug candidate, the best-ranked ones), each sorted. Among equals, the ones whose key says "any" or
    "all" (the whole car, beside one per port) are the best."""
    ranked = [(rank, entry) for entry, state in entities if (rank := _plug_rank(entry, state)) is not None]
    if not ranked:
        return (), ()
    best_rank = min(rank for rank, _ in ranked)
    best = [entry for rank, entry in ranked if rank == best_rank]
    whole_car = [entry for entry in best if entity_key(entry).has_any(_PLUG_WHOLE_CAR_PHRASES)]
    if len(best) > 1 and whole_car:
        best = whole_car
    return tuple(sorted(entry.entity_id for _, entry in ranked)), tuple(sorted(entry.entity_id for entry in best))


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
                else _READING_WORDS[source_plug_reading(hass, hass.states.get(source.entity_id))]
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
