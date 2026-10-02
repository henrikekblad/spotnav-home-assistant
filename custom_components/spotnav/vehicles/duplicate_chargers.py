"""Detect two SpotNav charger entries that are one physical charger.

`entity_conflicts.py` refuses the same charge-control or current entity in two entries. A charger can
still be listed twice through different entities (one entry via OCPP, a leftover one via the vendor's
integration), so two entries are one charger when they share any of: a Home Assistant device (charge
control, current limit or measured current), a measured-current entity, an OCPP charge point and
connector, or an Easee device id. Nothing here changes an entry: the card and Repairs say so, the setup
flow refuses it, and a person removes one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import (
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_CURRENT_ENTITIES,
    CONF_CONTROL_PATH,
    CONF_CURRENT_LIMIT,
    CONF_ENTRY_TYPE,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CONF_PHASE_WIRING,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    ENTRY_TYPE_SITE,
)

#: What two entries share, as the card's conflict `label`.
SHARED_DEVICE: Final = "device"
SHARED_MEASURED_CURRENT: Final = "measured_current"
SHARED_OCPP: Final = "ocpp"
SHARED_EASEE: Final = "easee"

#: Looked up under config.error / options.error in the translation files.
DUPLICATE_CHARGER_ERROR: Final = "duplicate_charger"


@dataclass(frozen=True, slots=True)
class ChargerIdentity:
    """Everything that says which physical charger an entry (or a candidate) is."""

    devices: frozenset[str] = frozenset()
    measured: frozenset[str] = frozenset()
    ocpp: tuple[str, str] | None = None
    easee: str | None = None


@dataclass(frozen=True, slots=True)
class DuplicateCharger:
    """Another charger entry that is the same physical charger, and what they share."""

    entry_id: str
    title: str
    shared: str
    what: str


def _measured_entities_of_source(source: Any) -> list[str]:
    if not isinstance(source, dict):
        return []
    found: list[str] = []
    entity_id = source.get("entity_id")
    if isinstance(entity_id, str) and entity_id:
        found.append(entity_id)
    entity_ids = source.get("entity_ids")
    if isinstance(entity_ids, dict):
        found.extend(value for value in entity_ids.values() if isinstance(value, str) and value)
    return found


def _site_measured_entities(hass: HomeAssistant, charger_entry_id: str) -> list[str]:
    """The measured-current entities the sites' wiring stores for this charger."""
    found: list[str] = []
    for site in hass.config_entries.async_entries(DOMAIN):
        if site.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SITE:
            continue
        wiring = (site.data.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id)
        if isinstance(wiring, dict):
            found.extend(_measured_entities_of_source(wiring.get(CONF_MEASURED_CURRENT_SOURCE)))
    return found


def identity_of(
    hass: HomeAssistant,
    *,
    charge_control: str | None,
    current_limit: str | None = None,
    measured: Iterable[str] = (),
    ocpp: tuple[str, str] | None = None,
    easee: str | None = None,
) -> ChargerIdentity:
    """The identity of a charger from its entity ids (devices read from the entity registry)."""
    registry = er.async_get(hass)
    measured_set = frozenset(entity_id for entity_id in measured if entity_id)
    devices: set[str] = set()
    for entity_id in (charge_control, current_limit, *measured_set):
        if not entity_id:
            continue
        registered = registry.async_get(entity_id)
        if registered is not None and registered.device_id:
            devices.add(registered.device_id)
    return ChargerIdentity(frozenset(devices), measured_set, ocpp, easee)


def candidate_identity(
    hass: HomeAssistant,
    *,
    charge_control: str,
    current_limit: str | None,
    measured: Iterable[str] = (),
    ocpp: tuple[str, str] | None = None,
    easee: str | None = None,
) -> ChargerIdentity:
    """A candidate's identity; a missing OCPP identity is derived from its charge-control entity."""
    if ocpp is None:
        from .ocpp_identity import resolve_target

        target = resolve_target(hass, charge_control=charge_control).target
        if target is not None:
            ocpp = (str(target.charge_point_id), str(target.connector_id))
    return identity_of(
        hass,
        charge_control=charge_control,
        current_limit=current_limit,
        measured=measured,
        ocpp=ocpp,
        easee=easee,
    )


def identity_of_entry(hass: HomeAssistant, entry: ConfigEntry) -> ChargerIdentity:
    """The identity a stored charger entry describes."""
    data = entry.data
    current_entities = data.get(CONF_CHARGER_CURRENT_ENTITIES)
    measured = [item for item in current_entities if isinstance(item, str)] if isinstance(
        current_entities, (list, tuple)
    ) else []
    measured.extend(_site_measured_entities(hass, entry.entry_id))
    charge_point = data.get(CONF_OCPP_CHARGE_POINT_ID)
    connector = data.get(CONF_OCPP_CONNECTOR_ID)
    path = data.get(CONF_CONTROL_PATH)
    easee = path.get("device_id") if isinstance(path, dict) and path.get("kind") == "easee" else None
    return identity_of(
        hass,
        charge_control=data.get(CONF_CHARGE_CONTROL) or None,
        current_limit=data.get(CONF_CURRENT_LIMIT) or None,
        measured=measured,
        ocpp=(str(charge_point), str(connector)) if charge_point and connector else None,
        easee=str(easee) if easee else None,
    )


def shared_between(first: ChargerIdentity, second: ChargerIdentity) -> tuple[str, str] | None:
    """`(what, identifier)` the two identities share, or `None`; the most specific fact first."""
    if first.ocpp is not None and first.ocpp == second.ocpp:
        return SHARED_OCPP, f"{first.ocpp[0]}:{first.ocpp[1]}"
    if first.easee is not None and first.easee == second.easee:
        return SHARED_EASEE, first.easee
    common = sorted(first.measured & second.measured)
    if common:
        return SHARED_MEASURED_CURRENT, common[0]
    # Two connectors of one charge point are two chargers, even when they share a device.
    different_connectors = (
        first.ocpp is not None
        and second.ocpp is not None
        and first.ocpp[0] == second.ocpp[0]
        and first.ocpp[1] != second.ocpp[1]
    )
    common_devices = sorted(first.devices & second.devices)
    if common_devices and not different_connectors:
        return SHARED_DEVICE, common_devices[0]
    return None


def charger_entries_except(hass: HomeAssistant, exclude_entry_ids: Iterable[str] = ()) -> list[ConfigEntry]:
    excluded = set(exclude_entry_ids)
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.entry_id not in excluded and entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_CHARGER
    ]


def find_duplicate(
    hass: HomeAssistant, identity: ChargerIdentity, *, exclude_entry_id: str | None = None
) -> DuplicateCharger | None:
    """The first existing charger entry that is the same physical charger as `identity`."""
    for other in charger_entries_except(hass, (exclude_entry_id,) if exclude_entry_id else ()):
        shared = shared_between(identity, identity_of_entry(hass, other))
        if shared is not None:
            return DuplicateCharger(other.entry_id, other.title, shared[0], shared[1])
    return None


def duplicates_of(
    hass: HomeAssistant, entry: ConfigEntry, exclude_entry_ids: Iterable[str] = ()
) -> list[DuplicateCharger]:
    """Every other charger entry that is the same physical charger as `entry`."""
    identity = identity_of_entry(hass, entry)
    found: list[DuplicateCharger] = []
    for other in charger_entries_except(hass, (entry.entry_id, *exclude_entry_ids)):
        shared = shared_between(identity, identity_of_entry(hass, other))
        if shared is not None:
            found.append(DuplicateCharger(other.entry_id, other.title, shared[0], shared[1]))
    return found


def duplicate_pairs(
    hass: HomeAssistant, exclude_entry_ids: Iterable[str] = ()
) -> list[tuple[ConfigEntry, ConfigEntry, DuplicateCharger]]:
    """Each duplicate pair once, ordered by entry id: `(first, second, what the second shares)`."""
    entries = sorted(charger_entries_except(hass, exclude_entry_ids), key=lambda entry: entry.entry_id)
    identities = {entry.entry_id: identity_of_entry(hass, entry) for entry in entries}
    pairs: list[tuple[ConfigEntry, ConfigEntry, DuplicateCharger]] = []
    for index, first in enumerate(entries):
        for second in entries[index + 1 :]:
            shared = shared_between(identities[first.entry_id], identities[second.entry_id])
            if shared is not None:
                pairs.append(
                    (first, second, DuplicateCharger(second.entry_id, second.title, shared[0], shared[1]))
                )
    return pairs
