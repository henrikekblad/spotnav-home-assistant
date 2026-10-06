"""The OCPP charger's control identity, kept apart from the entities that control it.

The OCPP integration's 0.12 entity model has three separate facts:

* `number.<cpid>_maximum_current` is one station-wide safety ceiling and names no connector;
* `switch.<cpid>_connector_<n>_charge_control` and
  `number.<cpid>_connector_<n>_session_current_limit` are per connector (the session limit only
  matters while a transaction runs).

Identity is therefore derived only from entities that are about a connector, never from the
station ceiling: a station-wide entity answers `None` rather than connector 1, because guessing
the connector is how a two-connector charger gets the wrong cable tightened. Nothing here calls
a service or writes a state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from ..const import CONF_OCPP_CHARGE_POINT_ID, CONF_OCPP_CONNECTOR_ID, CONF_OCPP_TARGET_UNRESOLVED


OCPP_DOMAIN = "ocpp"
"""The OCPP integration's domain, which names the entities this module reads."""

CHARGE_CONTROL_KEY = "charge_control"
SESSION_LIMIT_KEY = "session_current_limit"
#: The OCPP measurand `Energy.Active.Import.Register` as the integration spells its sensor key
#: (e.g. `sensor.<cpid>_connector_1_energy_active_import_register`, `total_increasing`): the
#: connector's cumulative energy meter, read by `auto_controller._read_energy_register`.
ENERGY_REGISTER_KEY = "energy_active_import_register"

_CHARGE_CONTROL_SUFFIX = "_charge_control"
_SESSION_LIMIT_SUFFIX = "_session_current_limit"
_STATION_MAXIMUM_SUFFIX = "_maximum_current"
_ENERGY_REGISTER_SUFFIX = "_energy_active_import_register"
_CONNECTOR_MARKER = "_connector_"

# Keys whose unique id may be connector-scoped or flat. The OCPP integration emits the flat form
# exactly when the charge point has one connector, so a flat role entity means "one connector".
_CONNECTOR_SCOPED_KEYS = (CHARGE_CONTROL_KEY, SESSION_LIMIT_KEY)

@dataclass(frozen=True)
class OcppConnectorTarget:
    """Which connector of which charge point an OCPP write is about.

    Both fields are the integration's own identifiers (the `<cpid>` and `<n>` in
    `<cpid>_connector_<n>_...`). Every OCPP path is given this rather than re-deriving a
    connector from a current-slider entity id.
    """

    charge_point_id: str
    connector_id: int

    @property
    def devid(self) -> str:
        """The charge point id in the form the OCPP services take it (`devid`)."""
        return self.charge_point_id


@dataclass(frozen=True)
class OcppCurrentControls:
    """The OCPP roles around one target.

    `station_maximum_entity` is a ceiling and diagnostic, never an actuator;
    `session_limit_entity` is a possible actuator for a running transaction. Either may be
    `None`; neither is derived from the other.
    """

    target: OcppConnectorTarget
    station_maximum_entity: str | None = None
    session_limit_entity: str | None = None

def _object_id(entity_id: str, domain: str) -> str | None:
    prefix = f"{domain}."
    return entity_id[len(prefix) :] if entity_id.startswith(prefix) else None


def _connector_scoped(
    object_id: str | None, suffix: str, *, flattened_means_single: bool
) -> OcppConnectorTarget | None:
    """`<cpid>_connector_<n><suffix>`, and (where the role allows) the flat `<cpid><suffix>`."""
    if not object_id or not object_id.endswith(suffix):
        return None
    stem = object_id[: -len(suffix)]
    if not stem:
        return None
    head, marker, tail = stem.rpartition(_CONNECTOR_MARKER)
    if marker:
        if not head or not tail.isdigit():
            return None
        connector_id = int(tail)
        return OcppConnectorTarget(head, connector_id) if connector_id >= 1 else None
    if not flattened_means_single:
        return None
    return OcppConnectorTarget(stem, 1)


def charge_control_target_from_entity_id(entity_id: str) -> OcppConnectorTarget | None:
    """The connector a charge-control switch belongs to (flat form means one connector)."""
    return _connector_scoped(
        _object_id(entity_id, "switch"), _CHARGE_CONTROL_SUFFIX, flattened_means_single=True
    )


def session_limit_target_from_entity_id(entity_id: str) -> OcppConnectorTarget | None:
    """The connector a session-current-limit number belongs to (flat form means one connector)."""
    return _connector_scoped(
        _object_id(entity_id, "number"), _SESSION_LIMIT_SUFFIX, flattened_means_single=True
    )


def energy_register_target_from_entity_id(entity_id: str) -> OcppConnectorTarget | None:
    """The connector an energy-import-register sensor belongs to (flat form means one connector)."""
    return _connector_scoped(
        _object_id(entity_id, "sensor"), _ENERGY_REGISTER_SUFFIX, flattened_means_single=True
    )


def _energy_register_target_from_unique_id(unique_id: str | None) -> OcppConnectorTarget | None:
    """The connector an energy-import-register sensor's unique id names.

    Same shapes as `target_from_unique_id`, restricted to `ENERGY_REGISTER_KEY` so it never
    matches a charge-control or session-limit entity.
    """
    if not isinstance(unique_id, str):
        return None
    parts = unique_id.split(".")
    # A sensor's unique id has the platform last (`ocpp.<cpid>.conn1.<key>.sensor`), unlike a
    # switch's. Rotate it to the front; otherwise real ids parse to nothing and only the
    # entity-id fallback works, which a renamed entity breaks.
    if len(parts) >= 4 and parts[0] == OCPP_DOMAIN:
        parts = [parts[-1], *parts[:-1]]
    if len(parts) < 4 or parts[1] != OCPP_DOMAIN or not parts[2]:
        return None
    connector_part = parts[3]
    if len(parts) == 5:
        key = parts[4]
        if key != ENERGY_REGISTER_KEY or not connector_part.startswith("conn") or not connector_part[4:].isdigit():
            return None
        connector_id = int(connector_part[4:])
        return OcppConnectorTarget(parts[2], connector_id) if connector_id >= 1 else None
    if len(parts) == 4 and connector_part == ENERGY_REGISTER_KEY:
        return OcppConnectorTarget(parts[2], 1)
    return None


def energy_register_target_from_registry(hass: HomeAssistant, entity_id: str) -> OcppConnectorTarget | None:
    """The target of an energy-import-register sensor (registry unique id first, entity id second)."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None:
        from_unique = _energy_register_target_from_unique_id(entry.unique_id)
        if from_unique is not None:
            return from_unique
    return energy_register_target_from_entity_id(entity_id)


def _classes_allow_register(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether nothing known about the entity says it is not a cumulative energy meter.

    The key already says what the entity is; classes that are missing (an offline charger's entities
    are registered without them, and have no state) are no reason to refuse it. A device or state class
    that is there and names something else is.
    """
    entry = er.async_get(hass).async_get(entity_id)
    state = hass.states.get(entity_id)
    attributes = state.attributes if state is not None else {}
    device_class = attributes.get("device_class") or (
        (entry.device_class or entry.original_device_class) if entry is not None else None
    )
    state_class = attributes.get("state_class") or (
        (entry.capabilities or {}).get("state_class") if entry is not None else None
    )
    return device_class in (None, "energy") and state_class in (None, "total_increasing")


def energy_register_entity_for(hass: HomeAssistant, target: OcppConnectorTarget) -> str | None:
    """The target's energy-import-register sensor entity id, or `None` if none is exposed.

    Matches each OCPP entity's registry identity against `target`; never constructs an entity id.
    """
    for entity_id in ocpp_entity_ids(hass):
        if energy_register_target_from_registry(hass, entity_id) == target and _classes_allow_register(
            hass, entity_id
        ):
            return entity_id
    return None


def target_from_entity_id(entity_id: str) -> OcppConnectorTarget | None:
    """The connector an OCPP control entity names: the charge control's, else the session limit's."""
    return charge_control_target_from_entity_id(entity_id) or session_limit_target_from_entity_id(
        entity_id
    )


def is_station_maximum(entity_id: str) -> bool:
    """Whether an entity is the station-wide ceiling rather than any connector's own entity."""
    object_id = _object_id(entity_id, "number")
    if not object_id or not object_id.endswith(_STATION_MAXIMUM_SUFFIX):
        return False
    stem = object_id[: -len(_STATION_MAXIMUM_SUFFIX)]
    return _CONNECTOR_MARKER not in stem


def target_from_unique_id(unique_id: str | None) -> OcppConnectorTarget | None:
    """The connector an OCPP entity's unique id names.

    Shapes: `<platform>.ocpp.<cpid>.conn<n>.<key>` (multi-connector) and
    `<platform>.ocpp.<cpid>.<key>` (single connector). The station ceiling names no
    connector and answers `None`.
    """
    if not isinstance(unique_id, str):
        return None
    parts = unique_id.split(".")
    if len(parts) < 4 or parts[1] != OCPP_DOMAIN or not parts[2]:
        return None
    connector_part, key = parts[3], parts[4] if len(parts) == 5 else None
    if len(parts) == 5:
        if not connector_part.startswith("conn") or not connector_part[4:].isdigit():
            return None
        connector_id = int(connector_part[4:])
        if connector_id < 1 or key not in _CONNECTOR_SCOPED_KEYS:
            return None
        return OcppConnectorTarget(parts[2], connector_id)
    if len(parts) == 4 and connector_part in _CONNECTOR_SCOPED_KEYS:
        return OcppConnectorTarget(parts[2], 1)
    return None


def target_from_registry(hass: HomeAssistant, entity_id: str | None) -> OcppConnectorTarget | None:
    """The target of a configured entity: registry unique id first, entity id shape second.

    The entity-id shape is the integration's own naming and survives a missing registry entry.
    """
    if not isinstance(entity_id, str) or not entity_id:
        return None
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None:
        from_unique = target_from_unique_id(entry.unique_id)
        if from_unique is not None:
            return from_unique
    return target_from_entity_id(entity_id)


@dataclass(frozen=True)
class TargetResolution:
    """What a resolution established, and from which fact; an empty result is a real answer."""

    target: OcppConnectorTarget | None
    source: str | None

    @property
    def resolved(self) -> bool:
        return self.target is not None


TARGET_SOURCE_CHARGE_CONTROL = "charge_control"


def resolve_target(hass: HomeAssistant, *, charge_control: str | None) -> TargetResolution:
    """Establish the connector target from the Charge Control entity's metadata, and nothing else.

    An unresolvable entity stays unresolved so the caller can mark the entry incomplete.
    """
    from_charge_control = target_from_registry(hass, charge_control)
    if from_charge_control is not None:
        return TargetResolution(from_charge_control, TARGET_SOURCE_CHARGE_CONTROL)
    return TargetResolution(None, None)


def _station_matches(entity_id: str, charge_point_id: str) -> bool:
    """Whether a station-wide ceiling entity belongs to [charge_point_id]."""
    object_id = _object_id(entity_id, "number")
    if not object_id or not object_id.endswith(_STATION_MAXIMUM_SUFFIX):
        return False
    stem = object_id[: -len(_STATION_MAXIMUM_SUFFIX)]
    return stem in (charge_point_id, slugify(charge_point_id))


def ocpp_entity_ids(hass: HomeAssistant) -> list[str]:
    """Every entity the OCPP integration owns: the registry first, the states as a fallback.

    The state machine is used only when the registry holds no OCPP entity at all (e.g. a
    stripped test registry).
    """
    registry = er.async_get(hass)
    owned = sorted(
        entry.entity_id for entry in registry.entities.values() if entry.platform == OCPP_DOMAIN
    )
    if owned:
        return owned
    return sorted(
        entity_id
        for entity_id in hass.states.async_entity_ids()
        if target_from_entity_id(entity_id) is not None
        or is_station_maximum(entity_id)
        or energy_register_target_from_entity_id(entity_id) is not None
    )


def apply_target(data: dict[str, Any], resolution: TargetResolution) -> None:
    """Write a resolution into a config entry's data, in place.

    A resolved target replaces stored data and clears the incomplete marker; an unresolved one
    keeps old data and marks the entry only when it has no target, so a failed registry read
    cannot drop a working identity or leave half a target.
    """
    if resolution.resolved:
        data[CONF_OCPP_CHARGE_POINT_ID] = resolution.target.charge_point_id
        data[CONF_OCPP_CONNECTOR_ID] = resolution.target.connector_id
        data.pop(CONF_OCPP_TARGET_UNRESOLVED, None)
        return
    if not data.get(CONF_OCPP_CHARGE_POINT_ID) or not data.get(CONF_OCPP_CONNECTOR_ID):
        data[CONF_OCPP_TARGET_UNRESOLVED] = True


def discover_controls(hass: HomeAssistant, target: OcppConnectorTarget) -> OcppCurrentControls:
    """The roles around one target, whatever their current state.

    Session Current Limit is found even while `unavailable`: the entity's existence, not its
    state, identifies the role. The station ceiling is never an actuator or a session substitute.
    """
    station: str | None = None
    session: str | None = None
    for entity_id in ocpp_entity_ids(hass):
        if station is None and _station_matches(entity_id, target.charge_point_id):
            station = entity_id
            continue
        if session is None and session_limit_target_from_entity_id(entity_id) == target:
            session = entity_id
    return OcppCurrentControls(
        target=target, station_maximum_entity=station, session_limit_entity=session
    )
