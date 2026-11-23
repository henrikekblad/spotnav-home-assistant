"""The spa, ranked from the registry (spec section 3; research `plans/research_integrations/spa_control_2026-11-23.md`
section 3). Read only, from the same registry reading as the pool's (`pool/detect.read_seen`); nothing written.

* 1.0: a climate or water heater of a spa integration (`SPA_DOMAINS`), `family` from its domain.
* 0.9: a `tuya_local` water heater whose device's manufacturer is Intex (`family: tuya_intex`).
* 0.5 (asked): any other climate or water heater with a spa's range (max 40 °C or less, min 8 to 27 °C) and "spa",
  "hot tub", "bubbel" or "jacuzzi" in its name, or the `mdi:hot-tub` icon (`family: generic`, watched only).

Gecko gives one spa a climate and a water heater on one device: the water heater is the spa. `control_my_spa`,
`arctic_spa` and Joyonway are found with their family and are never controllable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from ..pool.detect import ASK_BELOW, Seen
from .config import (
    CLOUD,
    FAMILY_ARCTIC_SPA,
    FAMILY_BALBOA,
    FAMILY_BESTWAY,
    FAMILY_CONTROL_MY_SPA,
    FAMILY_GECKO,
    FAMILY_GENERIC,
    FAMILY_INTEX,
    FAMILY_JOYONWAY,
    FAMILY_MSPA,
    FAMILY_SMARTTUB,
    FAMILY_TUYA_INTEX,
    CONTROLLABLE,
)

#: A spa integration's domain and the family it gives (the Joyonway domain is the HACS repository's, unverified).
SPA_DOMAINS: Final[dict[str, str]] = {
    "balboa": FAMILY_BALBOA,
    "smarttub": FAMILY_SMARTTUB,
    "gecko": FAMILY_GECKO,
    "bestway": FAMILY_BESTWAY,
    "intex_spa": FAMILY_INTEX,
    "mspa": FAMILY_MSPA,
    "control_my_spa": FAMILY_CONTROL_MY_SPA,
    "arctic_spa": FAMILY_ARCTIC_SPA,
    "joyonway": FAMILY_JOYONWAY,
}
_SPA_WORDS: Final = re.compile(r"spa|hot ?tub|bubbel|jacuzzi|badtunna", re.IGNORECASE)
HOT_TUB_ICON: Final = "mdi:hot-tub"


@dataclass(frozen=True, slots=True)
class SpaCandidate:
    entity_id: str
    score: float
    family: str
    device_id: str | None = None

    @property
    def ask(self) -> bool:
        return self.score < ASK_BELOW

    @property
    def controllable(self) -> bool:
        return self.family in CONTROLLABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "score": round(self.score, 2),
            "ask": self.ask,
            "family": self.family,
            "controllable": self.controllable,
            "cloud": self.family in CLOUD,
        }


def spa_score(entity: Seen) -> tuple[float, str] | None:
    """`(score, family)` for a climate or water heater that may be a spa, else `None`."""
    if entity.domain not in ("climate", "water_heater"):
        return None
    family = SPA_DOMAINS.get(entity.platform)
    if family is not None:
        return 1.0, family
    if entity.platform == "tuya_local" and entity.domain == "water_heater" and "intex" in (entity.manufacturer or "").casefold():
        return 0.9, FAMILY_TUYA_INTEX
    ranged = (
        entity.max_c is not None and entity.min_c is not None and entity.max_c <= 40.5 and 8.0 <= entity.min_c <= 27.0
    )
    named = bool(_SPA_WORDS.search(entity.name)) or entity.icon == HOT_TUB_ICON
    if ranged and named:
        return 0.5, FAMILY_GENERIC
    return None


def spas(seen: Iterable[Seen]) -> list[SpaCandidate]:
    """Every spa candidate, best first; one per device (Gecko's water heater over its climate)."""
    by_device: dict[str, SpaCandidate] = {}
    loose: list[SpaCandidate] = []
    for item in seen:
        found = spa_score(item)
        if found is None:
            continue
        candidate = SpaCandidate(item.entity_id, found[0], found[1], item.device_id)
        if item.device_id is None:
            loose.append(candidate)
            continue
        held = by_device.get(item.device_id)
        better = held is None or candidate.score > held.score or (
            candidate.score == held.score and item.domain == "water_heater" and held.entity_id.startswith("climate.")
        )
        if better:
            by_device[item.device_id] = candidate
    return sorted([*by_device.values(), *loose], key=lambda c: (-c.score, c.entity_id))
