"""The sauna, ranked from the registry (the owner's decision of 2026-11-23; routes read in each integration's source on
2026-11-23). Read only, from the pool's registry reading (`pool/detect.read_seen`); nothing written.

* 1.0: the climate of a sauna integration (`SAUNA_DOMAINS`): core `huum` (cloud, off/heat, a door sensor), core
  `saunum` (local, off/heat as a session, a door sensor and alarms), `harvia_sauna` (HACS, MyHarvia cloud, off/heat
  40-110 °C, door and safety-relay sensors, its own on time), `tylo_sauna` (HACS, local, off/heat with a standby,
  a door fault in the climate's attributes), `sentio` (HACS, local, off/heat).
* 0.5 (asked): a switch with "bastu" or "sauna" in its name (`family: relay`: a contactor, the heater's own thermostat
  holds the temperature; never controlled until the person declares the heater's own safety measure), or another
  climate reaching 90 °C or more with such a name (`family: generic`, watched only).

A door sensor on the same device (device class `door`, or a door's translation key) is proposed as the lock SpotNav
respects (`lock_id`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from ..pool.detect import ASK_BELOW, Seen
from .config import CLOUD, FAMILY_GENERIC, FAMILY_RELAY, INTEGRATIONS

SAUNA_DOMAINS: Final[dict[str, str]] = {family: family for family in INTEGRATIONS}
_SAUNA_WORDS: Final = re.compile(r"bastu|sauna|kiuas|badstue", re.IGNORECASE)
DOOR_KEYS: Final = frozenset({"door", "door_open"})


@dataclass(frozen=True, slots=True)
class SaunaCandidate:
    entity_id: str
    score: float
    family: str
    device_id: str | None = None
    lock_id: str | None = None

    @property
    def ask(self) -> bool:
        return self.score < ASK_BELOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "score": round(self.score, 2),
            "ask": self.ask,
            "family": self.family,
            "lock_id": self.lock_id,
            "cloud": self.family in CLOUD,
        }


def sauna_score(entity: Seen) -> tuple[float, str] | None:
    """`(score, family)` for an entity that may be a sauna, else `None`."""
    if entity.domain == "climate" and entity.platform in SAUNA_DOMAINS:
        return 1.0, SAUNA_DOMAINS[entity.platform]
    named = bool(_SAUNA_WORDS.search(entity.name))
    if entity.domain in ("switch", "input_boolean") and named:
        return 0.5, FAMILY_RELAY
    if entity.domain == "climate" and named and entity.max_c is not None and entity.max_c >= 90.0:
        return 0.5, FAMILY_GENERIC
    return None


def door_of(seen: Iterable[Seen], device_id: str | None) -> str | None:
    """The door sensor on the sauna's device, if it has one."""
    if device_id is None:
        return None
    return next(
        (
            item.entity_id
            for item in seen
            if item.device_id == device_id
            and item.domain == "binary_sensor"
            and (item.device_class == "door" or item.translation_key in DOOR_KEYS)
        ),
        None,
    )


def saunas(seen: Iterable[Seen]) -> list[SaunaCandidate]:
    """Every sauna candidate, best first, each with its door sensor where the device has one."""
    items = list(seen)
    found = []
    for item in items:
        scored = sauna_score(item)
        if scored is not None:
            found.append(SaunaCandidate(item.entity_id, scored[0], scored[1], item.device_id, door_of(items, item.device_id)))
    return sorted(found, key=lambda c: (-c.score, c.entity_id))
