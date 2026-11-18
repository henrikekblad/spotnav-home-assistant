"""The heat control's paths beside the relay, pure (plan `plans/v3_heating_plan.md` section 4, levels A and D): how
each is read as SpotNav's two contacts and what is written to it. The shell is `heat/control.py`, whose rules, check,
read-back and foreign-writer pause are the relay's own (`heat/relay_control.py`), unchanged.

**None of these three paths has run on a real heat pump yet** (`UNTESTED`): the registers and their ranges are read
from the integrations' sources, the meaning of each value from the makers' manuals, and the card says so.

**A contact** is what the heat control holds: A lowers, B raises, never both. On the relay paths a contact is a
switch. On the paths here both contacts are one entity read two ways, named by a key (`contact_key`:
`<entity id>#<kind>:<role>`; a switch's key is its entity id, so the kept state of the relay paths reads as before):

* **`sg_ready_api`, NIBE S series over the local `nibe_heatpump`** (library nibe 2.26.0, `nibe/data/s1155_s1255.json`,
  read 2026-11-18): number 46009 "Requested operating mode (SG Ready)", 0..3 with the default 1, acts while switch
  43033 "Activate SG Ready via API" is on. The modes, by SG Ready's own order and NIBE's menu 4.2.3 (blocking,
  normal, low price, overcapacity), with the library's default as the anchor for normal: `MODE_BLOCK` 0 is (1,0),
  `MODE_NORMAL` 1 is (0,0), `MODE_RAISE` 2 is (0,1), `MODE_FORCED` 3 is (1,1). The library does not name the values
  (the coil has no mappings): **the mapping is the first thing a field test confirms.** A is closed at 0, B at 2, and
  3 reads as both closed, which SpotNav never writes (`mode_to_write`) and never takes over. Every opening writes 1,
  and the shell's dead time between an opening and the other contact's closing makes each change go through normal.
  The library's data says nothing of where either coil is kept: both are plain writable holding registers with a
  default, as the stored settings beside them are, so they are written as **stored settings** (`CLASS_SETTING`), a
  few times a day. The switch is turned on before the first closing and off again when SpotNav hands back, after
  the mode reads normal (`heat/control.py`), and only if SpotNav turned it on.
* **`offset`, a heat-curve offset number.** Lowering writes the value found less the depth, raising the value found
  plus the raise step, each read back as exactly what was written (a value someone else set, even a deeper one, is
  theirs: the lowering has ended and nothing is undone).
  * **Stored** (NIBE F 47011 "Heat Offset S1" and NIBE S 40031 "Heating offset climate system 1", -10..10 in whole
    steps; any other number a person chooses): the value found is kept before the lowering is written and written
    back when it ends, at Home Assistant's stop and at the next start (`Snapshot`). `CLASS_SETTING`, at most
    `OFFSET_WRITES_DAY` a day (the plan's section 8).
  * **Volatile** (CTC `ctc_bms` 1023 "Curve adjustment override", -10..10 °C; dklemm/home-assistant-ctc-bms 0.6.0,
    `controls.py` and `hold.py`, read 2026-11-18): the controller forgets the value five minutes after the last
    write. The entity reads `unknown` while nothing is held (read as open) and takes 0 as "release": the integration
    then stops writing and the controller returns to its own curve within those five minutes. The integration also
    rewrites a held value every minute by itself while Home Assistant runs. SpotNav writes the value again every
    `RENEW` as its own watchdog (`CLASS_SETPOINT`, free of write cycles by CTC's manual) and hands back by writing
    the release. What ends an override without SpotNav is Home Assistant or the link stopping: the dead-man is the
    controller's. `ctc_ecozenith` (beolink/HA-CTC-Local-Easyconf 0.30.0) has no curve adjustment among its `ctl_`
    keys, so it has no offset path.

**The depth** (`Depth`, in the entity's own unit; no setting). The plan lets the house move half a degree around its
target (`config.BAND_C`) in runs of at most three hours (`relay_control.MAX_RUN`), and asks for whole steps, one step
at most below -5 °C outdoors and no raise above one step (section 5), within three steps of the value found (the
owner's ±3, section 15). One NIBE offset step moves the room's aim about 1 °C once the house has settled; a house
follows with a time constant of 15 hours (light) to 50 (heavy), so three hours at two steps move the room
`2·(1−e^(−3/15))` ≈ 0.36 °C in the light house and 0.12 °C in the heavy one: inside the band, where three steps would
leave it (0.54 °C). Two steps is also the shallow end of what the plan suggests for the relay's shift in the pump
(-2 to -3). So NIBE lowers 2 steps (1 when cold) and raises 1. CTC's adjustment is in °C of supply: at a radiator
curve a degree of room is 2 to 3 °C of supply, so -3 °C is about one NIBE step in the room, shallower than NIBE's
two on purpose (the path is new), yet twice the fall the check must see on the calculated supply
(`relay_control.SUPPLY_DROP_C`), and -2 °C when cold still shows. It raises 2 °C (the raise check's 1 °C). A number
SpotNav does not know is treated as NIBE's.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from .config import PATH_OFFSET, PATH_RELAY, PATH_SG_READY, PATH_SG_READY_API, HeatConfig
from .relay_control import MAX_WRITES_DAY

KIND_SWITCH: Final = "switch"
KIND_MODE: Final = "mode"
KIND_OFFSET: Final = "offset"
ROLE_LOWER: Final = "lower"
ROLE_RAISE: Final = "raise"

#: The paths that have not run on a real heat pump yet: the card says so under the switch.
UNTESTED: Final = frozenset({PATH_SG_READY_API, PATH_OFFSET})

CLASS_SETTING: Final = "setting"
CLASS_SETPOINT: Final = "setpoint"

#: Closings and openings a day on a path whose every change starts or stops the compressor's work (a relay, an SG
#: Ready block): `relay_control.MAX_WRITES_DAY`.
SWITCH_WRITES_DAY: Final = MAX_WRITES_DAY
#: The same on a curve offset (the plan's section 8: at most 24 writes a day, whole steps).
OFFSET_WRITES_DAY: Final = 24
#: SG Ready's activation switch turned on at most this often a day (each hand-back turns it off again).
ACTIVATION_WRITES_DAY: Final = 4
#: A volatile value is written again this often: well inside the controller's five minutes.
RENEW: Final = timedelta(minutes=2)
#: After this long without a write that took, the controller has forgotten a volatile value.
EXPIRY: Final = timedelta(minutes=5)
#: Renewals a day at most (one every `RENEW` for the six lowered and six raised hours a day is 360): a runaway guard.
RENEWALS_DAY: Final = 1000

MODE_BLOCK: Final = 0
MODE_NORMAL: Final = 1
MODE_RAISE: Final = 2
#: SG Ready's forced raise, (1,1): read, never written.
MODE_FORCED: Final = 3
#: What a volatile offset is set to in order to let it go (ctc_bms: nothing is written, the controller forgets).
RELEASE: Final = 0.0

#: Outdoors below this a lowering is one step only (the plan's section 5).
COLD_BELOW_C: Final = -5.0
#: SpotNav never moves an offset further than this from the value found (the owner's ±3, in the entity's unit).
MAX_SHIFT: Final = 3.0


@dataclass(frozen=True, slots=True)
class Depth:
    """How far an offset is moved, in the entity's unit (see the module)."""

    lower: float
    lower_cold: float
    raise_by: float


DEPTH_NIBE: Final = Depth(2.0, 1.0, 1.0)
DEPTH_CTC: Final = Depth(3.0, 2.0, 2.0)
#: Per adapter brand (`adapters.Adapter.brand`); a number SpotNav does not know is treated as NIBE's.
DEPTHS: Final[dict[str, Depth]] = {"nibe_f": DEPTH_NIBE, "nibe_s": DEPTH_NIBE, "ctc_bms": DEPTH_CTC}
#: The adapters whose offset SpotNav offers by itself (the others' are read through a cloud, or are no plain shift).
OFFSET_BRANDS: Final = frozenset(DEPTHS)


def depth_of(brand: str | None) -> Depth:
    return DEPTHS.get(brand or "", DEPTH_NIBE)


# ------------------------------------------------------------------------------------------------ contacts


def contact_key(entity_id: str, kind: str, role: str) -> str:
    return entity_id if kind == KIND_SWITCH else f"{entity_id}#{kind}:{role}"


def parse(key: str) -> tuple[str, str, str]:
    """`(entity id, kind, role)` of a contact's key; a plain entity id is a switch (its role is not in the key)."""
    entity_id, mark, rest = key.partition("#")
    if not mark:
        return key, KIND_SWITCH, ""
    kind, _colon, role = rest.partition(":")
    return entity_id, kind, role


def entity_of(key: str) -> str:
    return key.partition("#")[0]


def lower_contact(config: HeatConfig) -> str | None:
    """Contact A (the lowering) of the settings' control path, `None` where there is nothing to steer yet."""
    entity_id = config.control_entity
    if entity_id is None:
        return None
    if config.control_path in (PATH_RELAY, PATH_SG_READY):
        return entity_id
    if config.control_path == PATH_SG_READY_API:
        # The mode acts only through its activation switch: without it there is no path.
        return None if config.control_entity_b is None else contact_key(entity_id, KIND_MODE, ROLE_LOWER)
    if config.control_path == PATH_OFFSET:
        return contact_key(entity_id, KIND_OFFSET, ROLE_LOWER)
    return None


def raise_contact(config: HeatConfig) -> str | None:
    """Contact B (the raise): SG Ready's second contact, the API's low-price mode, an offset above the value found;
    `None` where the path cannot raise."""
    if lower_contact(config) is None:
        return None
    entity_id = config.control_entity
    assert entity_id is not None
    if config.control_path == PATH_SG_READY:
        return config.control_entity_b
    if config.control_path == PATH_SG_READY_API:
        return contact_key(entity_id, KIND_MODE, ROLE_RAISE)
    if config.control_path == PATH_OFFSET:
        return contact_key(entity_id, KIND_OFFSET, ROLE_RAISE)
    return None


def activation_switch(config: HeatConfig) -> str | None:
    """The switch that lets the SG Ready API's mode act (43033), `None` on any other path."""
    if config.control_path == PATH_SG_READY_API and config.control_entity is not None:
        return config.control_entity_b
    return None


def max_writes(key: str | None) -> int:
    """A day's closings and openings on a contact's path."""
    return OFFSET_WRITES_DAY if key is not None and parse(key)[1] == KIND_OFFSET else SWITCH_WRITES_DAY


def write_class(key: str, volatile: bool = False) -> str:
    return CLASS_SETPOINT if parse(key)[1] == KIND_OFFSET and volatile else CLASS_SETTING


# ------------------------------------------------------------------------------------------------ values


def number_of(text: Any) -> float | None:
    """A number entity's state as a figure, `None` when it is none."""
    if not isinstance(text, str):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _same(a: float, b: float) -> bool:
    return abs(a - b) < 1e-6


def mode_of(text: Any) -> int | None:
    """The SG Ready API's mode from the number's state, `None` when it is none of the four."""
    value = number_of(text)
    if value is None or not value.is_integer() or not MODE_BLOCK <= value <= MODE_FORCED:
        return None
    return int(value)


def mode_closed(mode: int, role: str) -> bool:
    """Whether a contact reads closed at a mode: A at blocked, B at the raise, both at the forced raise."""
    if role == ROLE_LOWER:
        return mode in (MODE_BLOCK, MODE_FORCED)
    return mode in (MODE_RAISE, MODE_FORCED)


def mode_to_write(role: str, closed: bool) -> int:
    """The mode a write sets: blocked or the raise for a closing, normal for every opening. Never `MODE_FORCED`."""
    if not closed:
        return MODE_NORMAL
    return MODE_BLOCK if role == ROLE_LOWER else MODE_RAISE


@dataclass(frozen=True, slots=True)
class Snapshot:
    """An offset SpotNav moved: the value found (`None`: a volatile one nobody held), the value written, whether the
    controller forgets it by itself, and when. Kept before the write goes out, so a stop or a crash right after it
    still knows what to put back."""

    found: float | None
    written: float
    volatile: bool
    at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {"found": self.found, "written": self.written, "volatile": self.volatile, "at": self.at.isoformat()}

    @classmethod
    def from_dict(cls, raw: Any) -> Snapshot | None:
        if not isinstance(raw, dict):
            return None
        found, written = raw.get("found"), raw.get("written")
        if isinstance(written, bool) or not isinstance(written, (int, float)) or not math.isfinite(written):
            return None
        if found is not None and (isinstance(found, bool) or not isinstance(found, (int, float)) or not math.isfinite(found)):
            return None
        try:
            at = datetime.fromisoformat(raw.get("at"))
        except (TypeError, ValueError):
            return None
        if at.tzinfo is None:
            return None
        return cls(None if found is None else float(found), float(written), raw.get("volatile") is True, at)

    def restore(self) -> float | None:
        """What an opening writes: the release for a volatile value, else the value found."""
        return RELEASE if self.volatile else self.found


def offset_closed(value: float | None, role: str, snapshot: Snapshot | None, volatile: bool) -> bool | None:
    """Whether an offset's contact reads closed. `value` is the number as read, `None` where it reads none (a
    volatile one then holds nothing: open; a stored one is unreadable). With SpotNav's own move kept: closed only at
    exactly the value written. Without: a stored offset is open whatever it reads (that value is what would be
    found), a volatile one is closed by someone else when it holds a lowering (A) or a raise (B)."""
    if value is None:
        return False if volatile else None
    if snapshot is not None:
        return _same(value, snapshot.written) and not _same(value, snapshot.found if snapshot.found is not None else RELEASE)
    if volatile:
        return value < 0.0 if role == ROLE_LOWER else value > 0.0
    return False


def offset_target(
    role: str,
    found: float | None,
    depth: Depth,
    *,
    cold: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
    step: float | None = None,
) -> float | None:
    """The value a closing writes: the value found less the depth (A) or plus the raise step (B), on the entity's
    step, inside its range and within `MAX_SHIFT` of the value found; `None` where that leaves no move at all (an
    offset already at the end of its range)."""
    base = RELEASE if found is None else found
    move = min(MAX_SHIFT, depth.raise_by if role == ROLE_RAISE else depth.lower_cold if cold else depth.lower)
    target = base + move if role == ROLE_RAISE else base - move
    if minimum is not None:
        target = max(target, minimum)
    if maximum is not None:
        target = min(target, maximum)
    if step is not None and step > 0:
        # Towards the value found, so a step never makes the move deeper than asked.
        steps = (target - base) / step
        target = base + (math.floor(steps + 1e-9) if steps > 0 else math.ceil(steps - 1e-9)) * step
    target = round(target, 4)
    if _same(target, base) or (target > base) != (role == ROLE_RAISE):
        return None
    return target


def detected_paths(pump: Any) -> list[dict[str, Any]]:
    """The control paths a pump's own entities offer (`facts.HeatPumpFacts`), the one that both lowers and blocks
    first: `[{path, control_entity, control_entity_b}]`. An entity still disabled in its integration counts (the
    opt-in enables it, `heat/optin.py`). None through a cloud (read only, plan section 4), and none where the pump
    has two of an entity (which climate system is not SpotNav's to guess)."""
    if pump is None or pump.cloud:
        return []

    def found(kind: str, method: str) -> list[str]:
        return [item.entity_id for item in pump.capabilities if item.kind == kind and item.method == method]

    out: list[dict[str, Any]] = []
    if pump.brand == "nibe_s":
        modes, switches = found("sg_ready", "number"), found("sg_ready", "switch")
        if len(modes) == 1 and len(switches) == 1:
            out.append({"path": PATH_SG_READY_API, "control_entity": modes[0], "control_entity_b": switches[0]})
    if pump.brand in OFFSET_BRANDS:
        offsets = found("offset", "number")
        if len(offsets) == 1:
            out.append({"path": PATH_OFFSET, "control_entity": offsets[0], "control_entity_b": None})
    return out


__all__ = [
    "ACTIVATION_WRITES_DAY",
    "CLASS_SETPOINT",
    "CLASS_SETTING",
    "COLD_BELOW_C",
    "DEPTHS",
    "EXPIRY",
    "KIND_MODE",
    "KIND_OFFSET",
    "KIND_SWITCH",
    "MAX_SHIFT",
    "MODE_BLOCK",
    "MODE_FORCED",
    "MODE_NORMAL",
    "MODE_RAISE",
    "OFFSET_BRANDS",
    "OFFSET_WRITES_DAY",
    "RELEASE",
    "RENEW",
    "RENEWALS_DAY",
    "ROLE_LOWER",
    "ROLE_RAISE",
    "SWITCH_WRITES_DAY",
    "UNTESTED",
    "Depth",
    "Snapshot",
    "activation_switch",
    "contact_key",
    "depth_of",
    "detected_paths",
    "entity_of",
    "lower_contact",
    "max_writes",
    "mode_closed",
    "mode_of",
    "mode_to_write",
    "number_of",
    "offset_closed",
    "offset_target",
    "parse",
    "raise_contact",
    "write_class",
]
