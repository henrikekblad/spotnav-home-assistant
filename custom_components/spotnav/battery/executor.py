"""The battery's write path: turns a slot's planned action into the brand's commands, safely. Built, not reachable.

Nothing runs it in production: `shadow.ON_REACHABLE` is off, no service or setting starts it, and the shell
(`control.py`) refuses to write while it is off. Tests drive it against fake entities. A later, explicit opt-in will
use it. It is the one battery write route: since wave 7 the shadow hold for the car (`hold.py`) is a request for
this executor's action `hold`, and what it logs comes from `preview`, which runs the executor's own start rules and
commands on a throw-away instance and returns the writes and the hand-back without writing anything.

How it acts (`step`, one look every few seconds; pure, the shell does the writes it returns):

* **Edge-triggered.** It writes only when the wanted state changes (a new action, a new rate past the hysteresis,
  a brand's watchdog to renew) and only the roles whose reading differs from it, one at a time in the brand's order
  (`commands.py`). Each write goes through the confirmed-write ledger (`ledger.py`): it counts only when the entity
  reads it back within `READBACK_DEADLINE_S`; the next role is written only after that. A write that does not read
  back (a read-only integration drops it silently, an entity went away) hands the battery back and blocks the
  control until it is reset: SpotNav never repeats a write the device did not take.
* **The monitor judges the effect** (`monitor.py`): after its grace, three readings that show no effect hand the
  battery back and block the control (a hold that does not hold, a charge that does not charge).
* **Restore first, on every exit path.** Before the first write the value of every role it may write is kept
  (`snapshot`) and the shell persists it before it writes. The battery is handed back (every role to its snapshot,
  in the brand's restore order, each read back) when: the part is stopped or unloaded; the plan wants plain
  self-consumption, has no slot now or names an action the brand cannot do; a reading is stale or the site's
  measurement is lost; the plant leaves the grid; the fuse headroom falls under `RESTORE_HEADROOM_A` (or a grid
  charge cannot keep `MIN_RATE_KW` within it); the battery's management changes (its own optimiser, another planner,
  an app taking Remote EMS back) or the site's battery is no longer that brand; someone else changes a control
  SpotNav owns (that control is given up and not written back over the other system, as the ledger says); a write
  is not taken; the monitor sees no effect; the day's write cap is reached. After a restart a stored snapshot is
  restored before anything else. A role SpotNav wrote is restored by writing its snapshot and reading it back even
  when it reads as it was found (`ExecState.written`): Sigenergy 2.0.1 shows a never-set limit as `unknown`, which
  after SpotNav's own write proves nothing; the snapshot is then written as the rating, never as `unknown`, and an
  `unknown` read-back is never a confirmed write (the ledger waits for a real value until its deadline). A restore write that does not read back is retried with backoff
  (`RESTORE_RETRY_S`), up to `RESTORE_ATTEMPTS` times each; a role still not restored then is left with its
  snapshot kept (the control blocks, and the next start tries again), while the other roles are handed back. A role
  that meanwhile needs no write (it reads its restore value, or its select is idle once Remote EMS is off) drops its
  retry, and a write made for the action that is still awaited when the hand-back begins is no restore retry: every
  hand-back ends.
* **Fuse safety.** A grid charge is clamped by the site's live headroom: what the battery already draws from the
  grid (at most its charge, the commanded rate and the site's import) plus the headroom less `HEADROOM_MARGIN_A`.
  Unlike evcc, the battery's charge counts against the main fuse. With the site's solar priority "car first" a
  charger held below what it asked makes the battery yield (`YIELD_STEP_KW` per review); with "battery first" the
  battery keeps its rate and the site's load balancing backs the car off (the battery is house load to it).
  The rate goes down at once and up only by `RATE_STEP_KW` after `RATE_UP_DWELL_S`.
* **Peak cover under a hold.** A hold stops the battery covering the house, so a peak (a kettle, the oven, one
  phase) lands on the fuse. When the least phase headroom falls under `PEAK_COVER_A` the executor first lifts the
  hold: one write, the brand's `peak_first` roles back to the value found (Sigenergy: the discharge limit, which
  covers a one-phase peak on that phase at once), Remote EMS left on. If the headroom is not back at or above
  `PEAK_COVER_A` within `PEAK_RECOVER_S` (or later stays under it that long), the battery is handed back in full.
  Under `RESTORE_HEADROOM_A` it is handed back at once, as before, and every hand-back of a hold or a grid charge
  writes the `peak_first` roles first (a grid charge: the mode that stops it). A hand-back for the fuse writes all
  its roles in one look, without waiting for each read-back (each is still read back, and one not confirmed is
  retried as any restore); other hand-backs keep one write at a time. The cover's write is never held back
  by the day's write cap. A covered hold holds again only after `PEAK_REHOLD_S`, and only when the start rule
  allows it (the headroom keeps `HEADROOM_MARGIN_A` once the battery stops covering); otherwise it stays covered
  until the plan changes the action.
* **The export limit** (`commands.EXPORT_CONTROLS`, kind `export`): the same executor, ledger and rules run the
  site's export limit as its own instance, for the one action `curtail_export` (the limit at 0 kW while the sell price
  is below zero, `export_curtail.py`); anything else hands it back to the value found. It needs no battery reading
  and no fuse headroom (a lower export limit never adds import), but every other rule holds: management (a battery in
  its own optimiser is only watched), off-grid, the site's measurement, read-back, a change by someone else given up,
  the restore. A never-set limit (`unknown`) is restored as the plant's maximum output. The day's write cap is the
  site's, shared with the battery's instance (`ExecInputs.other_writes_today`). A plant whose mode reads SpotNav's
  own battery control (Remote EMS that the battery instance runs, `ExecInputs.sibling_active`) is taken; one in
  Remote EMS of someone else's is not. The two instances never write each other's roles: the battery's control
  does not read the export limit, and Sigenergy's export limit needs no Remote EMS, so the battery's hand-back
  (Remote EMS off) takes nothing from it. **Obeyed?** The monitor judges a curtail on the site's grid power: after
  `monitor.CURTAIL_GRACE_S`, three readings exporting more than `monitor.EXPORT_TOLERANCE_KW` hand the limit back
  and block it (`not_obeyed`).

Pure: no Home Assistant import; times are aware datetimes from the caller. State survives a restart
(`ExecState.as_dict`, `from_dict`); the ledger and the monitor keep their own.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from .commands import (
    ACTION_CURTAIL_EXPORT,
    NORMAL,
    SNAPSHOT,
    WATCHDOG_RENEW_S,
    WATCHDOG_S,
    BrandControl,
    Native,
    Role,
    reading,
)
from .facts import BatteryFacts, EntityFact, find_entity
from .ledger import Ledger, same
from .monitor import Capability, Command, Monitor, Reading
from .plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
)
from .shadow import parse_aware_datetime

#: A battery reading older than this is stale (s).
BATTERY_MAX_AGE_S: Final = 120.0
#: The headroom kept under the fuse when a grid charge is sized (A per phase).
HEADROOM_MARGIN_A: Final = 2.0
#: Below this headroom (A) any action that adds import is handed back at once.
RESTORE_HEADROOM_A: Final = 1.0
#: Under a hold, below this headroom (A) the hold is lifted (the battery covers the peak) before anything else.
#: Between `RESTORE_HEADROOM_A` and `HEADROOM_MARGIN_A`: a hold starts only with `HEADROOM_MARGIN_A` left, so a
#: hold that just started does not lift at once.
PEAK_COVER_A: Final = 1.5
#: A lifted hold whose headroom stays under `PEAK_COVER_A` this long (s) is handed back in full.
PEAK_RECOVER_S: Final = 30.0
#: A lifted hold holds again no sooner than this (s): its own lift is why the headroom recovered.
PEAK_REHOLD_S: Final = 900.0
#: A grid charge below this is not worth running (kW).
MIN_RATE_KW: Final = 0.5
#: The rate goes up only by this much (kW), after this long since its last change (s).
RATE_STEP_KW: Final = 0.5
RATE_UP_DWELL_S: Final = 60.0
#: A rate is lowered when the clamp is under it by more than this (kW).
RATE_TOLERANCE_KW: Final = 0.1
#: With "car first", how much the battery's grid charge gives up per review while a car is held (kW).
YIELD_STEP_KW: Final = 1.0
#: After a hand-back, no new action for this long (s).
REARM_S: Final = 120.0
#: A management reading may be unknown this long (Sigenergy's Remote EMS entities blink, issue #427) (s).
MANAGEMENT_GRACE_S: Final = 60.0
#: Writes a day outside restores (flash wear is unknown for most brands).
DAILY_WRITE_CAP: Final = 96
#: A restore write that does not read back is retried after these delays (s), then given up.
RESTORE_RETRY_S: Final = (30.0, 60.0, 120.0, 300.0, 600.0)
RESTORE_ATTEMPTS: Final = len(RESTORE_RETRY_S)

Phase = Literal["idle", "active", "restoring", "blocked"]
_PHASES: Final = frozenset({"idle", "active", "restoring", "blocked"})
#: Hand-back reasons that block the control until it is reset (or, for the cap, until the next day).
_BLOCKING_PREFIXES: Final = (
    "write_not_taken", "changed_outside_spotnav", "no_effect", "management_changed", "not_obeyed",
)
#: States that are no value: never a snapshot to write back.
_NO_VALUE: Final = frozenset({"", "unavailable", "unknown", "none"})
_RAISES_IMPORT: Final = frozenset({ACTION_HOLD, ACTION_GRID_CHARGE})
#: Hand-backs for the fuse: every role is written at once, without waiting for the last one's read-back.
_FUSE_REASONS: Final = frozenset({"fuse_headroom"})
_MONITORED: Final = {ACTION_HOLD: "hold", ACTION_GRID_CHARGE: "charge", ACTION_DISCHARGE_HOUSE: "discharge",
                     ACTION_EXPORT_DISCHARGE: "discharge", ACTION_CURTAIL_EXPORT: "curtail"}


@dataclass(frozen=True, slots=True)
class RoleRead:
    """A role's entity as found now."""

    entity_id: str | None = None
    state: str | None = None
    found: bool = False
    enabled: bool = False
    #: The entity's step (number entities), for the read-back.
    step: float | None = None
    #: The device a service is called for (Huawei's battery).
    device_id: str | None = None


def role_reads(control: BrandControl, entities: Sequence[EntityFact], battery: EntityFact) -> dict[str, RoleRead]:
    """Each role's entity among the battery integration's entities, found by the integration's unique-id key."""
    reads: dict[str, RoleRead] = {}
    for role in control.roles:
        found = find_entity(role.match, entities, battery)
        if found is None:
            continue
        reads[role.name] = RoleRead(
            entity_id=found.entity_id, state=found.state, found=True, enabled=not found.disabled, step=found.step,
            device_id=found.device_id,
        )
    return reads


@dataclass(frozen=True, slots=True)
class ExecInputs:
    now: datetime
    facts: BatteryFacts
    reads: Mapping[str, RoleRead]
    #: The plan's action for the running slot (`None`: no plan now), its battery power (kW, magnitude) and end.
    action: str | None = None
    rate_kw: float = 0.0
    slot_end: datetime | None = None
    battery_age_s: float | None = None
    measurement_ok: bool = True
    #: The least headroom on any phase now (A), `None` when unknown.
    headroom_a: float | None = None
    volts_between_phases: float = 400.0
    #: The site's grid import now (kW, + import), `None` when unknown.
    grid_import_kw: float | None = None
    #: The site's solar priority: `True` for "car first" (the battery yields to a held car).
    car_first: bool = True
    #: A SpotNav charger is held below what it asked by the site's load balancing.
    car_held: bool = False
    #: The house's own deficit (kW) for judging a discharge; `None`: a discharge is not judged.
    house_deficit_kw: float | None = None
    #: Hand the battery back now (`stop`, `unload`, ...).
    stopping: str | None = None
    #: Roles whose last write raised (the shell's service call failed).
    failed: tuple[str, ...] = ()
    #: Writes the site's other control instance made today (battery and export limit share `DAILY_WRITE_CAP`).
    other_writes_today: int = 0
    #: SpotNav's other control instance on this plant runs it now (the battery control's Remote EMS): the modes it
    #: leaves (`BrandControl.own_states`) are SpotNav's own, not the plant's own management, for this instance too.
    sibling_active: bool = False


@dataclass(frozen=True, slots=True)
class Op:
    """One write for the shell to make."""

    role: str
    method: str
    entity_id: str | None
    value: Native
    device_id: str | None = None
    restore: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "method": self.method, "entity_id": self.entity_id, "value": self.value,
                "restore": self.restore}


@dataclass(frozen=True, slots=True)
class ExecState:
    """What the executor remembers; persisted (the snapshot first of all)."""

    phase: Phase = "idle"
    brand: str | None = None
    #: The action being run and its rate (kW), and the wanted value per role (native, snapshot resolved).
    action: str | None = None
    rate_kw: float = 0.0
    target: dict[str, Native] = field(default_factory=dict)
    #: The value per role found before the first write (as read), the native value a restore writes, and where.
    snapshot: dict[str, str] = field(default_factory=dict)
    restore_values: dict[str, Native] = field(default_factory=dict)
    entities: dict[str, str] = field(default_factory=dict)
    device_id: str | None = None
    snapshot_at: datetime | None = None
    #: The role written and not yet read back.
    pending: str | None = None
    #: In a fuse hand-back (`_FUSE_REASONS`): the roles written at once and not yet read back (each still read back,
    #: a role not confirmed retried as any restore).
    in_flight: tuple[str, ...] = ()
    #: Buttons pressed and roles renewed for the current target.
    sent: tuple[str, ...] = ()
    renew: tuple[str, ...] = ()
    reason: str | None = None
    blocked: str | None = None
    given_up: tuple[str, ...] = ()
    entered_at: datetime | None = None
    rate_changed_at: datetime | None = None
    watchdog_at: datetime | None = None
    released_at: datetime | None = None
    management_unknown_since: datetime | None = None
    #: Restore writes not read back: per role, the attempts made and when to try again.
    retries: dict[str, tuple[int, datetime]] = field(default_factory=dict)
    #: Roles whose restore was given up after every attempt (the snapshot is kept for them).
    unrestored: tuple[str, ...] = ()
    #: Roles SpotNav wrote since the snapshot was taken: their restore always writes the snapshot back and waits
    #: for its read-back (a read that only looks like the value found, Sigenergy's `unknown`, is not enough).
    written: tuple[str, ...] = ()
    #: A hold lifted for a peak (`peak_cover`): since when, and since when the headroom has been under the mark.
    covered_at: datetime | None = None
    peak_low_since: datetime | None = None
    day: str | None = None
    writes_today: int = 0

    @property
    def holds_snapshot(self) -> bool:
        return bool(self.snapshot) or bool(self.entities)

    def as_dict(self) -> dict[str, Any]:
        def iso(moment: datetime | None) -> str | None:
            return None if moment is None else moment.isoformat()

        return {
            "phase": self.phase,
            "brand": self.brand,
            "action": self.action,
            "rate_kw": self.rate_kw,
            "target": dict(self.target),
            "snapshot": dict(self.snapshot),
            "restore_values": dict(self.restore_values),
            "entities": dict(self.entities),
            "device_id": self.device_id,
            "snapshot_at": iso(self.snapshot_at),
            "pending": self.pending,
            "in_flight": list(self.in_flight),
            "sent": list(self.sent),
            "renew": list(self.renew),
            "reason": self.reason,
            "blocked": self.blocked,
            "given_up": list(self.given_up),
            "entered_at": iso(self.entered_at),
            "rate_changed_at": iso(self.rate_changed_at),
            "watchdog_at": iso(self.watchdog_at),
            "released_at": iso(self.released_at),
            "management_unknown_since": iso(self.management_unknown_since),
            "retries": {name: [count, at.isoformat()] for name, (count, at) in self.retries.items()},
            "unrestored": list(self.unrestored),
            "written": list(self.written),
            "covered_at": iso(self.covered_at),
            "peak_low_since": iso(self.peak_low_since),
            "day": self.day,
            "writes_today": self.writes_today,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ExecState:
        """A stored state; anything broken loads as idle, but a stored snapshot is always kept so it is restored."""
        if not isinstance(data, Mapping):
            return cls()

        def text(key: str) -> str | None:
            value = data.get(key)
            return value if isinstance(value, str) else None

        def natives(key: str) -> dict[str, Native]:
            raw = data.get(key)
            if not isinstance(raw, Mapping):
                return {}
            # A stored "unknown"/"unavailable" is no value: it is never written back.
            return {
                str(name): value for name, value in raw.items()
                if (isinstance(value, str) and value.strip().lower() not in _NO_VALUE) or isinstance(value, bool)
                or (isinstance(value, (int, float)) and math.isfinite(value))
            }

        def texts(key: str) -> dict[str, str]:
            raw = data.get(key)
            return {str(k): v for k, v in raw.items() if isinstance(v, str)} if isinstance(raw, Mapping) else {}

        def names(key: str) -> tuple[str, ...]:
            raw = data.get(key)
            return tuple(item for item in raw if isinstance(item, str)) if isinstance(raw, list) else ()

        phase = data.get("phase")
        rate = data.get("rate_kw")
        writes = data.get("writes_today")
        retries: dict[str, tuple[int, datetime]] = {}
        raw_retries = data.get("retries")
        if isinstance(raw_retries, Mapping):
            for name, row in raw_retries.items():
                if isinstance(row, list) and len(row) == 2 and isinstance(row[0], int) and not isinstance(row[0], bool):
                    at = parse_aware_datetime(row[1])
                    if at is not None and row[0] >= 0:
                        retries[str(name)] = (row[0], at)
        return cls(
            phase=phase if phase in _PHASES else "idle",
            brand=text("brand"),
            action=text("action"),
            rate_kw=float(rate) if isinstance(rate, (int, float)) and not isinstance(rate, bool) and math.isfinite(rate) else 0.0,
            target=natives("target"),
            snapshot=texts("snapshot"),
            restore_values=natives("restore_values"),
            entities=texts("entities"),
            device_id=text("device_id"),
            snapshot_at=parse_aware_datetime(data.get("snapshot_at")),
            pending=text("pending"),
            in_flight=names("in_flight"),
            sent=names("sent"),
            renew=names("renew"),
            reason=text("reason"),
            blocked=text("blocked"),
            given_up=names("given_up"),
            entered_at=parse_aware_datetime(data.get("entered_at")),
            rate_changed_at=parse_aware_datetime(data.get("rate_changed_at")),
            watchdog_at=parse_aware_datetime(data.get("watchdog_at")),
            released_at=parse_aware_datetime(data.get("released_at")),
            management_unknown_since=parse_aware_datetime(data.get("management_unknown_since")),
            retries=retries,
            unrestored=names("unrestored"),
            written=names("written"),
            covered_at=parse_aware_datetime(data.get("covered_at")),
            peak_low_since=parse_aware_datetime(data.get("peak_low_since")),
            day=text("day"),
            writes_today=writes if isinstance(writes, int) and not isinstance(writes, bool) and writes >= 0 else 0,
        )


@dataclass(frozen=True, slots=True)
class StepResult:
    state: ExecState
    ops: tuple[Op, ...] = ()
    #: Log rows: what happened and why.
    events: tuple[dict[str, Any], ...] = ()
    #: The state must be stored before the ops are made (a new snapshot).
    persist_first: bool = False


# --- fuse ----------------------------------------------------------------------------------------------------------


def _kw(amps: float, volts: float) -> float:
    """Amps per phase over three phases as kW (signed)."""
    return amps * math.sqrt(3.0) * volts / 1000.0


def grid_charge_cap_kw(inputs: ExecInputs, commanded_kw: float) -> float | None:
    """The most the battery may charge from the grid now (kW), `None` when the headroom is unknown.

    What the battery already takes from the grid is counted back (it is in the measured import): at most its own
    charge, the rate commanded and the site's import, and nothing when the import is unknown, so the figure is never
    above the truth. The headroom keeps `HEADROOM_MARGIN_A` under the fuse."""
    if inputs.headroom_a is None or inputs.volts_between_phases <= 0:
        return None
    battery = inputs.facts.power_kw
    own = 0.0
    if inputs.grid_import_kw is not None and battery is not None:
        own = max(min(commanded_kw, battery, inputs.grid_import_kw), 0.0)
    cap = own + _kw(inputs.headroom_a - HEADROOM_MARGIN_A, inputs.volts_between_phases)
    if inputs.car_first and inputs.car_held:
        cap = min(cap, own - YIELD_STEP_KW)
    return cap


def hold_headroom_ok(inputs: ExecInputs) -> bool:
    """Whether a hold may start (or a lifted one hold again): the headroom keeps `HEADROOM_MARGIN_A` once the
    battery stops covering what it discharges now."""
    if inputs.headroom_a is None or inputs.volts_between_phases <= 0:
        return False
    extra_a = 0.0
    power = inputs.facts.power_kw
    if power is not None and power < 0:
        extra_a = -power * 1000.0 / (math.sqrt(3.0) * inputs.volts_between_phases)
    return inputs.headroom_a - extra_a >= HEADROOM_MARGIN_A


# --- helpers -------------------------------------------------------------------------------------------------------


def _fresh(inputs: ExecInputs) -> bool:
    age = inputs.battery_age_s
    return inputs.facts.power_kw is not None and age is not None and age <= BATTERY_MAX_AGE_S


def _rating(facts: BatteryFacts, kind: str | None) -> float | None:
    if kind == "charge":
        return facts.rated_charge_kw
    if kind == "discharge":
        return facts.rated_discharge_kw
    if kind == "export":
        # A never-set export limit holds none: the most the plant can put out is the most it can export.
        return facts.plant_max_kw
    return None


def _unset(role: Role, state: str | None) -> bool:
    """Whether a number reads the register's "never set" value (Sigenergy's all-ones U32, shown as `unknown` by
    2.0.1 and as 4294967.295 before): it cannot be written back, so the rating is (research §1.4). A real value, even
    at or above the rating, is written back exactly."""
    if role.unset_as_rating is None:
        return False
    if state is not None and state.strip().lower() == "unknown":
        return role.unknown_is_unset
    try:
        value = float(state) / role.scale if state is not None else None
    except ValueError:
        return False
    return value is not None and value >= 4_000_000.0


def _restore_value(role: Role, state: str | None, facts: BatteryFacts) -> Native | None:
    """What a restore writes back for a role found reading `state`; `None` when that cannot be known."""
    if role.method == "number":
        if _unset(role, state):
            rating = _rating(facts, role.unset_as_rating)
            return None if rating is None else round(rating * role.scale, 6)
        text = reading(role, state)
        try:
            return None if text is None else float(text)
        except ValueError:
            return None
    return reading(role, state)


def management_problem(facts: BatteryFacts, control: BrandControl, active: bool) -> str | None:
    """Why the battery is not SpotNav's to run now; `unknown` when a setting cannot be read."""
    management = facts.management
    if management.state == "free":
        return None
    if management.state == "externally_controlled" or management.reason.startswith("planner:") or not active:
        # Another system drives it (`outside_control.py`): SpotNav only watches, whatever the modes read.
        return f"{management.state}:{management.reason}"
    for rule in control.adapter.modes:
        if rule.label not in management.settings:
            continue
        state = management.settings[rule.label]
        if state is None or state.strip().lower() in ("unavailable", "unknown", ""):
            return "unknown"
        if state in rule.free or state in control.own_states.get(rule.label, frozenset()):
            continue
        return f"self_managed:{rule.label}:{state}"
    return None if management.settings else f"{management.state}:{management.reason}"


def restore_order(control: BrandControl, action: str | None) -> tuple[str, ...]:
    """The order a hand-back writes the roles in: for a hold or a grid charge the roles that let the battery cover
    a peak first (`BrandControl.peak_first`), then the brand's restore order."""
    first = control.peak_first.get(action or "", ())
    return (*first, *(name for name in control.restore_order if name not in first))


def _event(now: datetime, kind: str, reason: str, **detail: Any) -> dict[str, Any]:
    return {"at": now.isoformat(), "kind": kind, "reason": reason} | detail


def _blocking(reason: str) -> bool:
    return reason.startswith(_BLOCKING_PREFIXES)


def _without(names: tuple[str, ...], name: str) -> tuple[str, ...]:
    return tuple(item for item in names if item != name)


def _writes_today(state: ExecState, inputs: ExecInputs) -> int:
    """The site's writes today: this instance's and the other's (the cap is shared)."""
    return state.writes_today + max(inputs.other_writes_today, 0)


class Executor:
    """One battery's write path: the brand's control, the ledger and the monitor, and the state."""

    def __init__(
        self, control: BrandControl, ledger: Ledger | None = None, monitor: Monitor | None = None,
        state: ExecState | None = None, *, watchdog_s: int = WATCHDOG_S,
    ) -> None:
        self.control = control
        self.ledger = ledger or Ledger()
        self.monitor = monitor or Monitor()
        self.state = state or ExecState()
        self.watchdog_s = watchdog_s

    @property
    def _export(self) -> bool:
        """Whether this instance runs the export limit (`commands.EXPORT_CONTROLS`)."""
        return self.control.kind == "export"

    # --- life ---

    def restart(self, now: datetime) -> list[dict[str, Any]]:
        """After a restart: a stored snapshot is restored before anything else."""
        state = self.state
        if not state.holds_snapshot:
            if state.phase in ("active", "restoring"):
                self.state = replace(state, phase="idle", pending=None)
            return []
        # A write in flight when HA stopped is no longer awaited: the restore reads every role afresh.
        self.state = replace(state, pending=None, in_flight=(), retries={}, unrestored=())
        return self._begin_restore(now, "restart")

    def reset(self) -> None:
        """Lift a block (the part's next explicit decision); never while a snapshot waits to be restored."""
        if self.state.phase == "blocked" and not self.state.holds_snapshot:
            self.state = replace(self.state, phase="idle", blocked=None, reason=None, given_up=())

    # --- one look ---

    def step(self, inputs: ExecInputs) -> StepResult:
        now = inputs.now
        events: list[dict[str, Any]] = []
        today = now.date().isoformat()
        if self.state.day != today:
            self.state = replace(self.state, day=today, writes_today=0)
            if self.state.phase == "blocked" and self.state.blocked == "daily_write_cap" and not self.state.holds_snapshot:
                self.state = replace(self.state, phase="idle", blocked=None)

        events += self._observe(inputs)
        if self.state.phase == "active":
            reason = self._exit_reason(inputs)
            if reason is not None:
                events += self._begin_restore(now, reason)
        persist_first = False
        if self.state.phase == "idle" and inputs.stopping is None:
            started, start_events = self._try_start(inputs)
            events += start_events
            persist_first = started
        elif self.state.phase == "active":
            events += self._cover(inputs)
            if self.state.phase == "active":
                events += self._retarget(inputs)
        self._settle_retries(inputs)
        ops: list[Op] = []
        while True:
            events += self._precheck(inputs)
            batch = self._progress(inputs)
            ops += batch
            if not batch or not self._fuse_hand_back():
                break
        events += [_event(now, "write", op.role, entity_id=op.entity_id, value=op.value, restore=op.restore)
                   for op in ops]
        if not ops:
            events += self._finish(inputs)
        return StepResult(self.state, tuple(ops), tuple(events), persist_first)

    # --- observing ---

    def _observe(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        now = inputs.now
        events: list[dict[str, Any]] = []
        for role_name in inputs.failed:
            if self.state.pending == role_name or role_name in self.state.in_flight:
                entity_id = self.state.entities.get(role_name)
                if entity_id is not None:
                    self.ledger.release(entity_id)
                self.state = replace(self.state, pending=None if self.state.pending == role_name else self.state.pending,
                                     in_flight=_without(self.state.in_flight, role_name))
                events += self._not_taken(now, role_name, "service_failed")
        for role in self.control.roles:
            entity_id = self.state.entities.get(role.name)
            if entity_id is None or self.ledger.claim(entity_id) is None:
                continue
            read = inputs.reads.get(role.name)
            event = self.ledger.observe(entity_id, reading(role, None if read is None else read.state), now)
            if event is None:
                continue
            if event.kind == "confirmed":
                if self.state.pending == role.name or role.name in self.state.in_flight:
                    retries = {name: row for name, row in self.state.retries.items() if name != role.name}
                    self.state = replace(
                        self.state, pending=None if self.state.pending == role.name else self.state.pending,
                        in_flight=_without(self.state.in_flight, role.name), retries=retries,
                    )
                events.append(_event(now, "confirmed", role.name, entity_id=entity_id, read=event.read))
            elif event.kind == "unconfirmed":
                if self.state.pending == role.name:
                    self.state = replace(self.state, pending=None)
                self.state = replace(self.state, in_flight=_without(self.state.in_flight, role.name))
                if self.state.phase == "restoring" and event.intended != self.state.target.get(role.name):
                    # A write made for the action, still awaited when the hand-back began: no restore to retry,
                    # the role is judged afresh against its restore value.
                    events.append(_event(now, "not_taken", role.name, why="superseded", read=event.read))
                else:
                    events += self._not_taken(now, role.name, "no_read_back", read=event.read)
            else:
                events += self._gave_up(now, role.name, entity_id, event.read)
        if self.state.phase == "active" and self._export:
            # The export limit is judged on the site's grid power: the export must stop.
            grid = inputs.grid_import_kw
            fresh = inputs.measurement_ok and grid is not None and math.isfinite(grid)
            judged = self.monitor.observe(Reading(grid, 0.0 if fresh else None), Capability(), now)
            if judged is not None and judged.kind == "failure":
                events.append(_event(now, "monitor", judged.outcome, detail=judged.as_dict()))
                events += self._begin_restore(now, f"not_obeyed:{self.state.action}:{judged.outcome}")
        elif self.state.phase == "active":
            facts = inputs.facts
            capability = Capability(
                rated_charge_kw=facts.max_charge_kw,
                rated_discharge_kw=facts.max_discharge_kw,
                soc_min_pct=facts.reserve_soc_pct or 0.0,
                site_cap_kw=inputs.house_deficit_kw if self.state.action == ACTION_DISCHARGE_HOUSE else None,
            )
            judged = self.monitor.observe(Reading(facts.power_kw, inputs.battery_age_s, facts.soc_pct), capability, now)
            if judged is not None and judged.kind == "failure":
                events.append(_event(now, "monitor", judged.outcome, detail=judged.as_dict()))
                if judged.outcome == "no_effect":
                    events += self._begin_restore(now, f"no_effect:{self.state.action}")
        return events

    def _gave_up(self, now: datetime, role: str, entity_id: str, read: str | None, **detail: Any) -> list[dict[str, Any]]:
        """A role someone else changed: given up (never fought); an active part hands the rest back."""
        self.state = replace(
            self.state, given_up=(*self.state.given_up, role),
            pending=None if self.state.pending == role else self.state.pending,
            in_flight=_without(self.state.in_flight, role),
        )
        events = [_event(now, "external", role, entity_id=entity_id, read=read, **detail)]
        if self.state.phase == "active":
            events += self._begin_restore(now, f"changed_outside_spotnav:{role}")
        return events

    def _precheck(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """Re-read the role about to be written: if it is not what SpotNav expects (the value it owns, or for a role
        not written yet the value found at enter) someone else changed it: give it up instead of writing over it.
        A reading that is no reading (unknown, unavailable) is no evidence and never stops a write."""
        events: list[dict[str, Any]] = []
        for _ in self.control.roles:
            state = self.state
            if state.phase not in ("active", "restoring") or state.pending is not None:
                break
            name = self._next_role(state, inputs)
            if name is None:
                break
            if self._capped(state, inputs, name):
                break
            role = self.control.role(name)
            read = inputs.reads.get(name)
            entity_id = state.entities.get(name)
            if role.method == "button" or read is None or entity_id is None:
                break
            claim = self.ledger.claim(entity_id)
            if claim is not None and claim.owned is not None:
                expected: str | None = claim.owned
            elif name not in state.written and name in state.snapshot:
                expected = reading(role, state.snapshot[name])
            else:
                break
            if expected is None:
                break
            now_read = reading(role, read.state)
            if same(expected, now_read, read.step or role.step) is not False:
                break
            if claim is not None:
                self.ledger.give_up(entity_id, now_read, inputs.now, state.target.get(name, expected))
            events += self._gave_up(
                inputs.now, name, entity_id, now_read, expected=expected, why="changed_before_write"
            )
        return events

    def _not_taken(self, now: datetime, role: str, why: str, **detail: Any) -> list[dict[str, Any]]:
        events = [_event(now, "not_taken", role, why=why, **detail)]
        state = self.state
        if state.phase == "restoring":
            attempts = state.retries.get(role, (0, now))[0] + 1
            if attempts > RESTORE_ATTEMPTS:
                # Every retry spent: the role is left; its snapshot stays and is tried again after a restart.
                retries = {name: row for name, row in state.retries.items() if name != role}
                self.state = replace(state, retries=retries, unrestored=(*state.unrestored, role))
                events.append(_event(now, "restore_failed", role))
            else:
                at = now + timedelta(seconds=RESTORE_RETRY_S[attempts - 1])
                self.state = replace(state, retries=state.retries | {role: (attempts, at)})
        elif state.phase == "active":
            events += self._begin_restore(now, f"write_not_taken:{role}:{why}")
        return events

    # --- leaving ---

    def _action_problem(self, inputs: ExecInputs, action: str) -> str | None:
        """Why the brand cannot run this action now (an AC charge policy that forbids grid charging)."""
        if not self.control.supports(action):
            return f"action_not_supported:{action}"
        for act, name, forbidden, why in self.control.action_requires:
            read = inputs.reads.get(name)
            if act == action and (read is None or read.state is None or read.state in forbidden):
                return why
        return None

    def _exit_reason(self, inputs: ExecInputs) -> str | None:
        facts = inputs.facts
        action = inputs.action
        if inputs.stopping is not None:
            return inputs.stopping
        if action is None:
            return "no_plan_now"
        if action == ACTION_SELF_CONSUME:
            return "curtail_ended" if self._export else "plan_self_consume"
        if facts.brand != self.control.brand:
            return "battery_changed"
        problem = self._action_problem(inputs, action)
        if problem is not None:
            return problem
        if facts.on_grid is False:
            return "off_grid"
        if not inputs.measurement_ok or (not self._export and not _fresh(inputs)):
            return "stale_data"
        if inputs.headroom_a is None and not self._export:
            return "headroom_unknown"
        if (action in _RAISES_IMPORT or self.state.action in _RAISES_IMPORT) and inputs.headroom_a < RESTORE_HEADROOM_A:
            return "fuse_headroom"
        # The battery's own instance runs the Remote EMS it turned on; the export instance only while the battery's
        # runs it (`sibling_active`): one taken by another controller (the app, a VPP) ends the curtail.
        management = management_problem(facts, self.control, active=inputs.sibling_active if self._export else True)
        if management == "unknown":
            since = self.state.management_unknown_since or inputs.now
            self.state = replace(self.state, management_unknown_since=since)
            if (inputs.now - since).total_seconds() >= MANAGEMENT_GRACE_S:
                return "management_changed:unknown"
        elif management is not None:
            return f"management_changed:{management}"
        else:
            self.state = replace(self.state, management_unknown_since=None)
        if _writes_today(self.state, inputs) >= DAILY_WRITE_CAP:
            return "daily_write_cap"
        return None

    def _begin_restore(self, now: datetime, reason: str) -> list[dict[str, Any]]:
        state = self.state
        if state.phase == "restoring" or (state.phase != "active" and not state.holds_snapshot):
            return []
        self.monitor.stop(now)
        target: dict[str, Native] = {}
        normal = NORMAL.get(self.control.brand, {})
        for role in self.control.roles:
            if role.name in state.given_up:
                continue
            if role.restored and role.name in state.restore_values:
                target[role.name] = state.restore_values[role.name]
            elif role.name in normal and role.name in state.entities:
                target[role.name] = normal[role.name]
        blocked = reason if _blocking(reason) else state.blocked
        if reason in _FUSE_REASONS and state.pending is not None:
            # A write still awaiting its read-back (the peak cover's lift) does not hold the rest back.
            state = replace(state, pending=None, in_flight=(*_without(state.in_flight, state.pending), state.pending))
        self.state = replace(
            state, phase="restoring", target=target, reason=reason, blocked=blocked, sent=(), renew=(),
            retries={}, unrestored=(), management_unknown_since=None, covered_at=None, peak_low_since=None,
        )
        return [_event(now, "hand_back", reason)]

    def _settle_retries(self, inputs: ExecInputs) -> None:
        """Drop the retry of a restore write that is no longer needed: the role now reads what the hand-back wants
        (Sigenergy's mode select goes unavailable once Remote EMS is off, before its own write reads back), or it
        is no longer the hand-back's to write. Its claim, if any, is released. Without this the hand-back would wait
        for a retry `_next_role` never makes."""
        state = self.state
        if state.phase != "restoring" or not state.retries:
            return
        retries = dict(state.retries)
        for name in list(retries):
            if name == state.pending or name in state.in_flight:
                continue
            wanted = state.target.get(name)
            if wanted is not None and name not in state.given_up and not self._satisfied(
                self.control.role(name), wanted, inputs.reads.get(name), restoring=True
            ):
                continue
            del retries[name]
            entity_id = state.entities.get(name)
            if entity_id is not None:
                self.ledger.release(entity_id)
        if retries != state.retries:
            self.state = replace(state, retries=retries)

    def _finish(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A restore with nothing left to write ends: claims released, the snapshot dropped (kept for a role whose
        restore was given up)."""
        state = self.state
        if state.phase != "restoring" or state.pending is not None or state.in_flight or state.retries:
            return []
        if self._next_role(state, inputs) is not None:
            return []
        now = inputs.now
        for name, entity_id in state.entities.items():
            if name not in state.unrestored:
                self.ledger.release(entity_id)
        if state.unrestored:
            kept = set(state.unrestored)
            self.state = replace(
                state, phase="blocked", blocked=f"restore_failed:{','.join(state.unrestored)}", target={},
                snapshot={k: v for k, v in state.snapshot.items() if k in kept},
                restore_values={k: v for k, v in state.restore_values.items() if k in kept},
                entities={k: v for k, v in state.entities.items() if k in kept},
                written=tuple(name for name in state.written if name in kept),
                sent=(), renew=(), released_at=now,
            )
            return [_event(now, "restore_failed", ",".join(state.unrestored))]
        blocked = state.blocked
        if blocked is not None and blocked.startswith("restore_failed"):
            blocked = None
        if state.reason == "daily_write_cap":
            blocked = "daily_write_cap"
        self.state = replace(
            state, phase="blocked" if blocked else "idle", blocked=blocked, action=None, rate_kw=0.0, target={},
            snapshot={}, restore_values={}, entities={}, device_id=None, snapshot_at=None, sent=(), renew=(),
            released_at=now, given_up=state.given_up if blocked else (), written=(),
        )
        return [_event(now, "handed_back", state.reason or "")]

    # --- entering ---

    def _wanted(self, action: str, rate_kw: float, restore_values: Mapping[str, Native]) -> dict[str, Native]:
        wanted = self.control.wanted(action, rate_kw, self.watchdog_s)
        return {
            name: (restore_values[name] if value == SNAPSHOT else value)
            for name, value in wanted.items()
            if not (value == SNAPSHOT and name not in restore_values)
        }

    def _rate(self, inputs: ExecInputs, commanded_kw: float) -> tuple[float, str | None]:
        """The rate to run the planned action at, and why it cannot run."""
        facts = inputs.facts
        action = inputs.action
        rate = abs(inputs.rate_kw)
        if action == ACTION_GRID_CHARGE:
            limit = facts.max_charge_kw
            if limit is not None:
                rate = min(rate, limit)
            cap = grid_charge_cap_kw(inputs, commanded_kw)
            if cap is None:
                return 0.0, "headroom_unknown"
            rate = min(rate, cap)
            if rate < MIN_RATE_KW:
                return 0.0, "fuse_headroom"
        elif action == ACTION_EXPORT_DISCHARGE:
            limit = facts.max_discharge_kw
            if limit is not None:
                rate = min(rate, limit)
            if rate < MIN_RATE_KW:
                return 0.0, "rate_too_small"
        return round(rate, 3), None

    def _blocker(self, inputs: ExecInputs) -> str | None:
        facts = inputs.facts
        action = inputs.action
        state = self.state
        if action is None or action == ACTION_SELF_CONSUME:
            return "nothing_to_do"
        problem = self._action_problem(inputs, action)
        if problem is not None:
            return problem
        if facts.brand != self.control.brand:
            return "brand_mismatch"
        management = management_problem(facts, self.control, active=inputs.sibling_active)
        if management is not None:
            return management
        if facts.on_grid is False:
            return "off_grid"
        if not inputs.measurement_ok or (not self._export and not _fresh(inputs)):
            return "stale_data"
        if inputs.headroom_a is None and not self._export:
            return "headroom_unknown"
        if action == ACTION_HOLD and not hold_headroom_ok(inputs):
            return "headroom_too_small"
        if action == ACTION_GRID_CHARGE and inputs.headroom_a < HEADROOM_MARGIN_A:
            return "headroom_too_small"
        if _writes_today(state, inputs) >= DAILY_WRITE_CAP:
            return "daily_write_cap"
        if state.released_at is not None and (inputs.now - state.released_at).total_seconds() < REARM_S:
            return "rearm"
        needed = set(self.control.wanted(action, 1.0, self.watchdog_s)) | {name for name, _ok, _why in self.control.requires}
        for name in sorted(needed):
            read = inputs.reads.get(name)
            role = self.control.role(name)
            if read is None or not read.found or read.entity_id is None:
                return f"control_not_found:{name}"
            if role.method != "service" and not read.enabled:
                return f"control_disabled:{name}"
        for name, allowed, why in self.control.requires:
            if reading(self.control.role(name), inputs.reads[name].state) not in allowed:
                return why
        return None

    def _try_start(self, inputs: ExecInputs) -> tuple[bool, list[dict[str, Any]]]:
        now = inputs.now
        blocker = self._blocker(inputs)
        if blocker is not None:
            return False, ([] if blocker in ("nothing_to_do", "rearm") else [_event(now, "not_started", blocker)])
        rate, why = self._rate(inputs, 0.0)
        if why is not None:
            return False, [_event(now, "not_started", why)]
        snapshot: dict[str, str] = {}
        restore_values: dict[str, Native] = {}
        entities: dict[str, str] = {}
        device_id: str | None = None
        for role in self.control.roles:
            read = inputs.reads.get(role.name)
            if read is None or read.entity_id is None:
                continue
            entities[role.name] = read.entity_id
            device_id = device_id or read.device_id
            if not role.restored:
                continue
            value = _restore_value(role, read.state, inputs.facts)
            if value is None:
                return False, [_event(now, "not_started", f"no_restore_value:{role.name}", read=read.state)]
            snapshot[role.name] = read.state or ""
            restore_values[role.name] = value
        action = inputs.action
        assert action is not None
        self.state = replace(
            self.state, phase="active", brand=self.control.brand, action=action, rate_kw=rate,
            target=self._wanted(action, rate, restore_values), snapshot=snapshot, restore_values=restore_values,
            entities=entities, device_id=device_id, snapshot_at=now, entered_at=now, rate_changed_at=now,
            watchdog_at=now, sent=(), renew=(), reason=None, given_up=(), management_unknown_since=None, written=(),
            covered_at=None, peak_low_since=None,
        )
        self._watch(inputs, action, rate)
        return True, [_event(now, "enter", action, rate_kw=rate, snapshot=snapshot)]

    def _watch(self, inputs: ExecInputs, action: str, rate: float) -> None:
        """The monitor watches the new command; a discharge for the house only when the house's deficit is known."""
        kind = _MONITORED.get(action)
        if kind is None or (action == ACTION_DISCHARGE_HOUSE and inputs.house_deficit_kw is None):
            self.monitor.stop(inputs.now)
            return
        if action == ACTION_DISCHARGE_HOUSE:
            rate = max(inputs.house_deficit_kw or 0.0, 0.0)
        before = inputs.grid_import_kw if kind == "curtail" else inputs.facts.power_kw
        self.monitor.start(Command(kind, inputs.now, rate, before_kw=before, written=True))

    def _retarget(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A new action, a new rate past the hysteresis, or a watchdog to renew."""
        now = inputs.now
        state = self.state
        action = inputs.action
        assert action is not None
        rate, why = self._rate(inputs, state.rate_kw)
        if why is not None:
            return self._begin_restore(now, why)
        events: list[dict[str, Any]] = []
        if action != state.action:
            self.state = replace(
                state, action=action, rate_kw=rate, target=self._wanted(action, rate, state.restore_values),
                sent=(), renew=(), rate_changed_at=now, watchdog_at=now, covered_at=None, peak_low_since=None,
            )
            self._watch(inputs, action, rate)
            return [_event(now, "change", action, rate_kw=rate)]
        if action in (ACTION_GRID_CHARGE, ACTION_EXPORT_DISCHARGE):
            since = (now - (state.rate_changed_at or now)).total_seconds()
            down = rate < state.rate_kw - RATE_TOLERANCE_KW
            up = rate >= state.rate_kw + RATE_STEP_KW and since >= RATE_UP_DWELL_S
            if down or up:
                self.state = replace(
                    state, rate_kw=rate, target=self._wanted(action, rate, state.restore_values), sent=(),
                    rate_changed_at=now,
                )
                self._watch(inputs, action, rate)
                events.append(_event(now, "rate", action, rate_kw=rate, was_kw=state.rate_kw))
        state = self.state
        if (
            self.control.watchdog_roles and state.watchdog_at is not None and state.pending is None
            and (now - state.watchdog_at).total_seconds() >= WATCHDOG_RENEW_S
        ):
            self.state = replace(state, renew=self.control.watchdog_roles, watchdog_at=now)
            events.append(_event(now, "renew", ",".join(self.control.watchdog_roles)))
        return events

    # --- the peak cover ---

    def _normal_value(self, name: str) -> Native | None:
        """What a hand-back writes to a role: its snapshot, or the brand's normal value for one without."""
        role = self.control.role(name)
        if role.restored and name in self.state.restore_values:
            return self.state.restore_values[name]
        if name in self.state.entities:
            return NORMAL.get(self.control.brand, {}).get(name)
        return None

    def _covering(self, state: ExecState, name: str) -> bool:
        """Whether `name` is a write of a lifted hold (never held back: the battery must cover the peak)."""
        return state.covered_at is not None and name in self.control.peak_first.get(state.action or "", ())

    def _capped(self, state: ExecState, inputs: ExecInputs, name: str) -> bool:
        """Whether the day's write cap holds this write back (a restore and a peak cover never are)."""
        if state.phase == "restoring" or self._covering(state, name):
            return False
        return _writes_today(state, inputs) >= DAILY_WRITE_CAP

    def _cover(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """Under a hold: lift it when the headroom falls under `PEAK_COVER_A`, hand back in full when it does not
        recover within `PEAK_RECOVER_S`, hold again after `PEAK_REHOLD_S` when the start rule allows it."""
        state = self.state
        now = inputs.now
        headroom = inputs.headroom_a
        first = self.control.peak_first.get(ACTION_HOLD, ())
        if state.action != ACTION_HOLD or inputs.action != ACTION_HOLD or headroom is None or not first:
            return []
        if state.covered_at is None:
            if headroom >= PEAK_COVER_A:
                return []
            target = dict(state.target)
            for name in first:
                value = self._normal_value(name)
                if value is not None and name in target:
                    target[name] = value
            # The battery is meant to discharge now: the hold's monitor would read that as a hold not taken.
            self.monitor.stop(now)
            self.state = replace(
                state, target=target, covered_at=now, peak_low_since=now,
                sent=tuple(name for name in state.sent if name not in first),
            )
            return [_event(now, "peak_cover", ACTION_HOLD, headroom_a=round(headroom, 2), roles=list(first))]
        if headroom < PEAK_COVER_A:
            since = state.peak_low_since or now
            if (now - since).total_seconds() >= PEAK_RECOVER_S:
                return self._begin_restore(now, "fuse_headroom")
            if state.peak_low_since is None:
                self.state = replace(state, peak_low_since=since)
            return []
        if state.peak_low_since is not None:
            self.state = state = replace(state, peak_low_since=None)
        if (now - state.covered_at).total_seconds() >= PEAK_REHOLD_S and hold_headroom_ok(inputs):
            self.state = replace(
                state, target=self._wanted(ACTION_HOLD, 0.0, state.restore_values), covered_at=None,
                sent=tuple(name for name in state.sent if name not in first),
            )
            self._watch(inputs, ACTION_HOLD, 0.0)
            return [_event(now, "peak_rehold", ACTION_HOLD, headroom_a=round(headroom, 2))]
        return []

    # --- writing ---

    def _satisfied(self, role: Role, wanted: Native, read: RoleRead | None, *, restoring: bool) -> bool:
        if role.method == "button":
            return role.name in self.state.sent
        state = None if read is None else read.state
        if restoring and role.idle_when and state in role.idle_when:
            return True
        snapshot = self.state.snapshot.get(role.name)
        if (
            snapshot is not None and state == snapshot and wanted == self.state.restore_values.get(role.name)
            and role.name not in self.state.written
        ):
            # Never written and still as found (a never-set limit reads "unknown"): nothing to write back. A role
            # SpotNav wrote is restored by writing its snapshot and reading it back, whatever it reads now.
            return True
        return bool(same(wanted, reading(role, state), None if read is None else (read.step or role.step)))

    def _next_role(self, state: ExecState, inputs: ExecInputs) -> str | None:
        """The first role, in the brand's order, that does not read what it should (or must be renewed)."""
        restoring = state.phase == "restoring"
        order = restore_order(self.control, state.action) if restoring else self.control.enter_order
        for name in order:
            if name not in state.target or name in state.given_up or name in state.unrestored or name in state.in_flight:
                continue
            retry = state.retries.get(name)
            if retry is not None and inputs.now < retry[1]:
                continue
            if name in state.renew:
                return name
            entity_id = state.entities.get(name)
            claim = None if entity_id is None else self.ledger.claim(entity_id)
            if claim is not None and claim.owned is not None and claim.intended == state.target[name]:
                # Written and read back: a later difference is the ledger's to judge (another system, or a
                # gap), never a reason to write it again.
                continue
            if not self._satisfied(self.control.role(name), state.target[name], inputs.reads.get(name), restoring=restoring):
                return name
        return None

    def _progress(self, inputs: ExecInputs) -> list[Op]:
        state = self.state
        if state.phase not in ("active", "restoring") or state.pending is not None:
            return []
        restoring = state.phase == "restoring"
        name = self._next_role(state, inputs)
        if name is None or self._capped(state, inputs, name):
            return []
        role = self.control.role(name)
        value = state.target[name]
        entity_id = state.entities.get(name)
        read = inputs.reads.get(name)
        renew = tuple(item for item in state.renew if item != name)
        sent = state.sent if name in state.sent else (*state.sent, name)
        writes = state.writes_today + (0 if restoring else 1)
        written = state.written if name in state.written else (*state.written, name)
        if role.method == "button":
            # A button has no read-back: pressed once per target; the monitor judges what it did.
            self.state = replace(state, sent=sent, renew=renew, writes_today=writes, written=written)
        else:
            if entity_id is not None:
                self.ledger.intend(entity_id, value, inputs.now, step=None if read is None else (read.step or role.step))
            if self._fuse_hand_back():
                self.state = replace(state, in_flight=(*state.in_flight, name), sent=sent, renew=renew,
                                     writes_today=writes, written=written)
            else:
                self.state = replace(state, pending=name, sent=sent, renew=renew, writes_today=writes, written=written)
        return [Op(name, role.method, entity_id, value, device_id=state.device_id, restore=restoring)]

    def _fuse_hand_back(self) -> bool:
        """A hand-back for the fuse: its roles are written at once, each read back afterwards."""
        return self.state.phase == "restoring" and self.state.reason in _FUSE_REASONS

    # --- what it shows ---

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state.as_dict(), "ledger": self.ledger.as_dict(), "monitor": self.monitor.as_dict()}


# --- the dry run -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Preview:
    """What the executor would do if the action were asked for now, from a fresh state: why it would not start, or
    the writes it would make (in the brand's order, each read back in turn) and what handing back would write."""

    blocker: str | None
    rate_kw: float = 0.0
    writes: tuple[Op, ...] = ()
    restores: tuple[Op, ...] = ()
    #: The value found per restored role (as read) and the native value a restore would write.
    snapshot: Mapping[str, str] = field(default_factory=dict)
    restore_values: Mapping[str, Native] = field(default_factory=dict)


def preview(
    control: BrandControl, inputs: ExecInputs, *, writes_today: int = 0, watchdog_s: int = WATCHDOG_S
) -> Preview:
    """The executor's own start rules and commands for `inputs.action`, run on a throw-away executor: nothing is
    written, nothing is kept. The shadow parts (`hold.py`) use it so that what they log as "would write" is exactly
    what the write path would write, with the same refusals (management, controls found and enabled, fuse, stale
    data, the day's cap) and the same snapshot rules (a never-set limit is restored as the rating)."""
    executor = Executor(
        control, state=ExecState(day=inputs.now.date().isoformat(), writes_today=writes_today), watchdog_s=watchdog_s
    )
    started, events = executor._try_start(inputs)
    if not started:
        return Preview(events[0]["reason"] if events else "nothing_to_do")
    state = executor.state
    writes: list[Op] = []
    for name in control.enter_order:
        if name not in state.target:
            continue
        role = control.role(name)
        if not executor._satisfied(role, state.target[name], inputs.reads.get(name), restoring=False):
            writes.append(Op(name, role.method, state.entities.get(name), state.target[name], device_id=state.device_id))
    written = tuple(op.role for op in writes)
    executor.state = replace(state, written=written)
    executor._begin_restore(inputs.now, "preview")
    back = executor.state.target
    restores = tuple(
        Op(name, control.role(name).method, state.entities.get(name), back[name], device_id=state.device_id, restore=True)
        for name in restore_order(control, state.action)
        if name in back and name in written
    )
    return Preview(None, state.rate_kw, tuple(writes), restores, dict(state.snapshot), dict(state.restore_values))
