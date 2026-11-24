"""The sauna's settings block inside the heat settings (`heat/config.py`, field `sauna`). Pure.

The sauna is the spa's deadline heat load (`spa/plan.py`) without an idle temperature: off between sessions, heated up
for a session the person booked. Stored shape (every key optional but `entity_id`):

    {entity_id: climate id (an integration's) | switch / input_boolean id (a relay or contactor),
     family: one of `FAMILIES`,
     target_c: 40..110 in whole degrees (80), sessions: [{weekdays: [1..7] (Monday 1), time: "HH:MM"}] (up to 4),
     session_once: ISO instant with offset | null, heater_kw: 2..15 | null (entered where no power sensor is found),
     lock_id: binary sensor id | null (the door or safety switch SpotNav respects; detected on the device),
     safety_declared: bool (false: a relay's person says the heater has its own safety measure for a remote start),
     control: bool (false)}

**Safety** (EN 60335-2-53 asks for a safety measure on a remote start, and SpotNav never bypasses one): nothing is ever
started but for a session the person booked or a "Bada nu" press; a readable lock (a door reading open) keeps it from
starting; a relay without `safety_declared` is never controlled (SpotNav only says when to start it); `generic` is
watched only. Nothing in this release writes to the sauna.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final

from ..core.parse import finite_number
from ..spa.config import (
    ERR_INVALID,
    ERR_RANGE,
    ERR_STEP,
    ERR_UNKNOWN_ENTITY,
    ERR_UNKNOWN_FIELD,
    ERR_WATCH_ONLY,
    ERR_WRONG_KIND,
    MAX_BATHS,
    Bath,
    _bath,
    judge_bath_once,
    judge_baths,
    parse_instant,
)

F_SAUNA: Final = "sauna"

FAMILY_HUUM: Final = "huum"
FAMILY_SAUNUM: Final = "saunum"
FAMILY_HARVIA: Final = "harvia_sauna"
FAMILY_TYLO: Final = "tylo_sauna"
FAMILY_SENTIO: Final = "sentio"
FAMILY_RELAY: Final = "relay"
FAMILY_GENERIC: Final = "generic"
INTEGRATIONS: Final = (FAMILY_HUUM, FAMILY_SAUNUM, FAMILY_HARVIA, FAMILY_TYLO, FAMILY_SENTIO)
FAMILIES: Final = (*INTEGRATIONS, FAMILY_RELAY, FAMILY_GENERIC)
#: Read through the maker's cloud: slow planned actions only.
CLOUD: Final = frozenset({FAMILY_HUUM, FAMILY_HARVIA})
CLIMATE_DOMAINS: Final = ("climate",)
RELAY_DOMAINS: Final = ("switch", "input_boolean")
LOCK_DOMAINS: Final = ("binary_sensor",)

TARGET_MIN_C: Final = 40.0
TARGET_MAX_C: Final = 110.0
DEFAULT_TARGET_C: Final = 80.0
HEATER_KW: Final = (2.0, 15.0)

KEYS: Final = frozenset(
    {"entity_id", "family", "target_c", "sessions", "session_once", "heater_kw", "lock_id", "safety_declared", "control"}
)
#: A relay's control without the person's word that the heater has its own safety measure for a remote start.
ERR_SAFETY_REQUIRED: Final = "safety_required"

EntityCheck = Callable[[str, str], bool | None]


def domains_of(family: str) -> tuple[str, ...]:
    if family == FAMILY_RELAY:
        return RELAY_DOMAINS
    if family == FAMILY_GENERIC:
        return (*CLIMATE_DOMAINS, *RELAY_DOMAINS)
    return CLIMATE_DOMAINS


@dataclass(frozen=True, slots=True)
class SaunaConfig:
    entity_id: str
    family: str = FAMILY_GENERIC
    target_c: float = DEFAULT_TARGET_C
    sessions: tuple[Bath, ...] = ()
    session_once: str | None = None
    heater_kw: float | None = None
    lock_id: str | None = None
    safety_declared: bool = False
    control: bool = False

    @property
    def controllable(self) -> bool:
        """Whether SpotNav could ever steer it: an integration's sauna, or a relay whose safety measure is declared."""
        return self.family in INTEGRATIONS or (self.family == FAMILY_RELAY and self.safety_declared)

    def session_once_at(self) -> datetime | None:
        return parse_instant(self.session_once)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "family": self.family,
            "target_c": self.target_c,
            "sessions": [session.as_dict() for session in self.sessions],
            "session_once": self.session_once,
            "heater_kw": self.heater_kw,
            "lock_id": self.lock_id,
            "safety_declared": self.safety_declared,
            "control": self.control,
        }


def _entity(value: Any, domains: tuple[str, ...]) -> str | None:
    if isinstance(value, str) and "." in value and value.split(".", 1)[0] in domains:
        return value
    return None


def _whole_in(value: Any, low: float, high: float) -> float | None:
    number = finite_number(value)
    if number is None or not low <= number <= high or number != int(number):
        return None
    return number


def read_sauna(raw: Any) -> SaunaConfig | None:
    """The stored block, every missing or broken key read as its default."""
    if not isinstance(raw, Mapping):
        return None
    family = raw.get("family") if raw.get("family") in FAMILIES else FAMILY_GENERIC
    entity_id = _entity(raw.get("entity_id"), domains_of(family))
    if entity_id is None:
        return None
    target = _whole_in(raw.get("target_c"), TARGET_MIN_C, TARGET_MAX_C)
    kw = finite_number(raw.get("heater_kw"))
    stored = raw.get("sessions")
    sessions = tuple(b for b in (_bath(item, stored=True) for item in stored[:MAX_BATHS]) if b is not None) if isinstance(stored, list) else ()
    config = SaunaConfig(
        entity_id=entity_id,
        family=family,
        target_c=DEFAULT_TARGET_C if target is None else target,
        sessions=sessions,
        session_once=raw.get("session_once") if parse_instant(raw.get("session_once")) is not None else None,
        heater_kw=kw if kw is not None and HEATER_KW[0] <= kw <= HEATER_KW[1] else None,
        lock_id=_entity(raw.get("lock_id"), LOCK_DOMAINS),
        safety_declared=raw.get("safety_declared") is True and family == FAMILY_RELAY,
    )
    return replace(config, control=raw.get("control") is True and config.controllable)


def apply_sauna(
    current: SaunaConfig | None, raw: Any, check: EntityCheck, *, now: datetime
) -> tuple[SaunaConfig | None, list[tuple[str, str]]]:
    """`current` with the block `raw` applied, and every `(field, code)` error (nothing applies unless there is none).
    Field names are `sauna.<key>`. Another entity starts watched and undeclared."""
    if raw is None:
        return None, []
    if not isinstance(raw, Mapping):
        return current, [(F_SAUNA, ERR_INVALID)]
    errors: list[tuple[str, str]] = [(f"{F_SAUNA}.{key}", ERR_UNKNOWN_FIELD) for key in raw if key not in KEYS]
    if errors:
        return current, errors
    family = raw.get("family", FAMILY_GENERIC if current is None else current.family)
    if family not in FAMILIES:
        return current, [(f"{F_SAUNA}.family", ERR_INVALID)]
    entity_id = raw.get("entity_id", None if current is None else current.entity_id)
    name = f"{F_SAUNA}.entity_id"
    if _entity(entity_id, domains_of(family)) is None:
        return current, [(name, ERR_WRONG_KIND if isinstance(entity_id, str) else ERR_INVALID)]
    fits = check(entity_id, "sauna")
    if fits is None:
        return current, [(name, ERR_UNKNOWN_ENTITY)]
    if not fits:
        return current, [(name, ERR_WRONG_KIND)]
    moved = current is None or current.entity_id != entity_id
    updated = SaunaConfig(str(entity_id)) if current is None else replace(current, entity_id=str(entity_id))
    updated = replace(updated, family=family)
    if moved:
        updated = replace(updated, control=False, safety_declared=False, lock_id=None)

    if "target_c" in raw:
        number = finite_number(raw["target_c"])
        if number is None:
            errors.append((f"{F_SAUNA}.target_c", ERR_INVALID))
        elif not TARGET_MIN_C <= number <= TARGET_MAX_C:
            errors.append((f"{F_SAUNA}.target_c", ERR_RANGE))
        elif number != int(number):
            errors.append((f"{F_SAUNA}.target_c", ERR_STEP))
        else:
            updated = replace(updated, target_c=number)
    if "heater_kw" in raw:
        value = raw["heater_kw"]
        number = finite_number(value)
        if value is None:
            updated = replace(updated, heater_kw=None)
        elif number is None:
            errors.append((f"{F_SAUNA}.heater_kw", ERR_INVALID))
        elif not HEATER_KW[0] <= number <= HEATER_KW[1]:
            errors.append((f"{F_SAUNA}.heater_kw", ERR_RANGE))
        else:
            updated = replace(updated, heater_kw=number)
    if "sessions" in raw:
        sessions, code = judge_baths(raw["sessions"])
        if code is not None:
            errors.append((f"{F_SAUNA}.sessions", code))
        elif sessions is not None:
            updated = replace(updated, sessions=sessions)
    if "session_once" in raw:
        once, code = judge_bath_once(raw["session_once"], now)
        if code is not None:
            errors.append((f"{F_SAUNA}.session_once", code.replace("bath", "session")))
        else:
            updated = replace(updated, session_once=once)
    if "lock_id" in raw:
        value = raw["lock_id"]
        if value is None:
            updated = replace(updated, lock_id=None)
        elif _entity(value, LOCK_DOMAINS) is None:
            errors.append((f"{F_SAUNA}.lock_id", ERR_WRONG_KIND if isinstance(value, str) else ERR_INVALID))
        elif (known := check(value, "sauna_lock")) is None:
            errors.append((f"{F_SAUNA}.lock_id", ERR_UNKNOWN_ENTITY))
        elif not known:
            errors.append((f"{F_SAUNA}.lock_id", ERR_WRONG_KIND))
        else:
            updated = replace(updated, lock_id=str(value))
    for key in ("safety_declared", "control"):
        if key in raw:
            if not isinstance(raw[key], bool):
                errors.append((f"{F_SAUNA}.{key}", ERR_INVALID))
            else:
                updated = replace(updated, **{key: raw[key]})
    if not errors:
        if updated.safety_declared and updated.family != FAMILY_RELAY:
            updated = replace(updated, safety_declared=False)
        if updated.control and updated.family == FAMILY_RELAY and not updated.safety_declared:
            errors.append((f"{F_SAUNA}.control", ERR_SAFETY_REQUIRED))
        elif updated.control and not updated.controllable:
            errors.append((f"{F_SAUNA}.control", ERR_WATCH_ONLY))
    if errors:
        return current, list(dict.fromkeys(errors))
    return updated, []
