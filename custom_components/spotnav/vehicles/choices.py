"""The dropdown choices shared by the config flow's vehicle resolution and the Repairs fix flow.

One definition keeps both flows offering the same choices, including the "not a vehicle" sentinel.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import selector

from .vehicle_discovery import AmbiguousVehicleCandidate


# Sentinel for "not a vehicle"; not an entity id, so it cannot collide with a candidate.
DISMISS_VEHICLE_CHOICE = "dismiss"


# Labels built in code so a device's name and candidate count can be included.
RESOLVE_VEHICLE_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "device": "{name} -- {count} possible battery sensors",
        "dismiss": "This is not a vehicle (stop reporting it)",
    },
    "sv": {
        "device": "{name} -- {count} möjliga batterisensorer",
        "dismiss": "Detta är inte ett fordon (sluta rapportera det)",
    },
}


def flow_language(hass) -> str:
    language = getattr(hass.config, "language", None) or "en"
    return "sv" if language.startswith("sv") else "en"


def entity_option(hass, entity_id: str) -> Any:
    """A dropdown entry for a candidate sensor: friendly name plus entity id (names can be near-identical)."""
    state = hass.states.get(entity_id)
    label = f"{state.name} ({entity_id})" if state is not None else entity_id
    return selector.SelectOptionDict(value=entity_id, label=label)


def ambiguous_vehicle_option(hass, candidate: AmbiguousVehicleCandidate) -> Any:
    """A dropdown entry for an ambiguous device: its name and how many sensors there are to choose between."""
    return selector.SelectOptionDict(
        value=candidate.id,
        label=RESOLVE_VEHICLE_TEXT[flow_language(hass)]["device"].format(
            name=candidate.name, count=len(candidate.candidate_entity_ids)
        ),
    )
