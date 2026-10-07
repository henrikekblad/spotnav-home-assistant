"""The dropdown choices shared by the config flow's vehicle resolution and the Repairs fix flow.

One definition keeps both flows offering the same choices, including the "not a vehicle" sentinel.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import selector

from ..texts import language_of, table
from .vehicle_discovery import AmbiguousVehicleCandidate


# Sentinel for "not a vehicle"; not an entity id, so it cannot collide with a candidate.
DISMISS_VEHICLE_CHOICE = "dismiss"


def flow_language(hass) -> str:
    """The language of Home Assistant's configuration, one of SpotNav's `i18n/<lang>.json` files."""
    return language_of(getattr(hass.config, "language", None))


def resolve_vehicle_text(hass, key: str) -> str:
    """A label built in code (a device's name and candidate count go in): `device` or `dismiss`."""
    return table(flow_language(hass), "resolve_vehicle")[key]


def entity_option(hass, entity_id: str) -> Any:
    """A dropdown entry for a candidate sensor: friendly name plus entity id (names can be near-identical)."""
    state = hass.states.get(entity_id)
    label = f"{state.name} ({entity_id})" if state is not None else entity_id
    return selector.SelectOptionDict(value=entity_id, label=label)


def ambiguous_vehicle_option(hass, candidate: AmbiguousVehicleCandidate) -> Any:
    """A dropdown entry for an ambiguous device: its name and how many sensors there are to choose between."""
    return selector.SelectOptionDict(
        value=candidate.id,
        label=resolve_vehicle_text(hass, "device").format(
            name=candidate.name, count=len(candidate.candidate_entity_ids)
        ),
    )
