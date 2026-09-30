"""Every unresolved discovery ambiguity, as one list.

Detection refuses to guess which battery sensor is a device's state of charge, and which of its
charge-limit `number`s a write should use. Both are recorded as a confirmation in
`vehicles/discovery_decisions.py`, keyed by device id. This is the read side: live state, read
fresh on each call, never stored; a row disappears once its decision is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from homeassistant.core import HomeAssistant

from .vehicle_discovery import discover_ambiguous_charge_limits, discover_ambiguous_vehicles


#: The decision domain, not a display label: `soc` is resolved by `confirm_vehicle_soc`,
#: `charge_limit` by `confirm_charge_limit`.
ResolutionKind = Literal["soc", "charge_limit"]


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    """One decision a human has to make before a device can be used.

    `device_id` is the Home Assistant device id both confirm services take; `name` is display
    only. `candidate_entity_ids` is what there is to choose between, sorted.
    """

    kind: ResolutionKind
    device_id: str
    name: str
    candidate_entity_ids: list[str]

    def as_dict(self) -> dict[str, object]:
        """This decision as one entry of `resolve_required`'s JSON."""
        return {
            "kind": self.kind,
            "device_id": self.device_id,
            "name": self.name,
            "candidate_entity_ids": list(self.candidate_entity_ids),
        }


def resolve_required(hass: HomeAssistant) -> list[ResolutionDecision]:
    """Every decision still outstanding, sorted by `(kind, device_id)` so equal state gives equal order."""
    decisions = [
        ResolutionDecision(
            kind="soc",
            device_id=candidate.id,
            name=candidate.name,
            candidate_entity_ids=list(candidate.candidate_entity_ids),
        )
        for candidate in discover_ambiguous_vehicles(hass)
    ]
    decisions.extend(
        ResolutionDecision(
            kind="charge_limit",
            device_id=candidate.id,
            name=candidate.name,
            candidate_entity_ids=list(candidate.candidate_entity_ids),
        )
        for candidate in discover_ambiguous_charge_limits(hass)
    )
    return sorted(decisions, key=lambda decision: (decision.kind, decision.device_id))
