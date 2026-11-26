"""What one look of the executor is given and what it answers: a role's reading, the look's inputs, a write, the
state it keeps (as stored) and a step's result."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ...core.electric import TN_VOLTS_BETWEEN_PHASES
from ..commands import Native, Phases
from ..facts import BatteryFacts
from ..shadow import parse_aware_datetime
from .const import _NO_VALUE, _PHASES, Phase


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
    #: The phases the plant spreads a balanced grid charge over (`BrandControl.grid_charge_phases`, or the plant's
    #: Output Type): the live clamp counts the least headroom on that many phases, as the HA-down cap does.
    charge_phases: int = 3
    #: The plant puts out the same on each of its phases (`BrandControl.balanced_output`): on three phases
    #: (`charge_phases`) what its discharge covers comes back a third on each when it stops (`cover_a`).
    output_balanced: bool = False
    volts_between_phases: float = TN_VOLTS_BETWEEN_PHASES
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
    #: Those of `failed` whose service call was given up on (no answer in time): a slow integration, not a refusal.
    timed_out: tuple[str, ...] = ()
    #: Writes the site's other control instance made today (battery and export limit share `DAILY_WRITE_CAP`).
    other_writes_today: int = 0
    #: SpotNav's other control instance on this plant runs it now (the battery control's Remote EMS): the modes it
    #: leaves (`BrandControl.own_states`) are SpotNav's own, not the plant's own management, for this instance too.
    sibling_active: bool = False
    #: The person acknowledged that SpotNav may take the plant over from its own optimiser (`BrandControl.takeover`,
    #: Sigenergy's AI mode); without it such a plant is only watched.
    takeover: bool = False
    #: The HA-down cap of a grid charge on L1, L2 and L3 (kW, `gate.grid_charge_phase_caps_kw`). Given, a control
    #: that runs targets per phase (`BrandControl.phased`) charges each phase on its own; `None`: the brand's own
    #: balanced charge.
    phase_caps_kw: Phases | None = None
    #: The most a hold that follows a sun surplus (`BrandControl.hold_surplus`) may leave its charge limit at (kW):
    #: the HA-down grid charge cap, since a limit left standing grid-charges once the sun goes; `math.inf` where the
    #: plant's own import limit guards the fuse instead. 0 (no follow) unless the shell gives it.
    hold_charge_cap_kw: float = 0.0
    #: Each phase's headroom now (A), `None` when one is unknown.
    phase_headroom_a: Phases | None = None
    #: The plant's own power on each phase now (kW, + output; the adapter's `plant_phase_power`), `None` when it is
    #: not read or stale.
    plant_phase_kw: Phases | None = None
    #: The plant's phases A, B and C are shown to be the meter's L1, L2 and L3 (the verification's mode `per_phase`
    #: passed on this firmware, `gate.per_phase`). Without it nothing pairs a phase of `plant_phase_kw` with a phase
    #: of `phase_headroom_a`: the cover per phase then takes its largest off the least headroom.
    plant_phases_mapped: bool = False
    #: The site meter's power on each phase now (kW, + import), `None` when one is unknown: what a charge per phase
    #: is judged on where the recipe's targets are the grid's (`PhaseControl.target_kind`).
    grid_phase_kw: Phases | None = None
    #: The verification's probe: the charge to command on each phase as it is (kW, negative for a discharge), a
    #: charge among them still within its phase's cap and room.
    phase_probe_kw: Phases | None = None
    #: For a recipe whose targets per phase are the grid's with sensors at their point
    #: (`commands.PhaseControl.grid_phases`): the load behind each phase's point and the power at the point now (kW,
    #: the point's + from the grid); `None` when not read. The plant's own power per phase (`plant_phase_kw`) is
    #: then the first less the second.
    phase_behind_kw: Phases | None = None
    phase_point_kw: Phases | None = None
    #: The verification's expiry test (`verify.MODE_EXPIRY`) runs on the active command: no renewal round starts,
    #: nothing else is written for it (a change of action or rate is refused for the span; a peak cover and every
    #: hand-back still write), and a renewed role (`BrandControl.watchdog_roles`) that reads the device's documented
    #: fallback (`Expiry.fallback_values`) is its own (`expired`), never someone else's change; one that reads
    #: anything else is another writer (`diverged`), left to the ledger as any outside change. The effect is not judged meanwhile (the test judges it),
    #: except a charge per phase over its targets.
    expiry_probe: bool = False
    #: What the plant may import by itself on the most loaded phase now (A, `commands.AutonomousImport`), only while
    #: its trigger may fire: taken off the headroom a hold starts on and its peak cover reads.
    own_import_a: float = 0.0
    #: For the export limit: what the site's battery charges and its chargers draw now (kW), where read: an import
    #: up to it under a curtail is theirs (`monitor`'s `import_rose`).
    excused_import_kw: float | None = None


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
    #: Since when a running action's battery reading has been missing or stale (`SILENCE_GRACE_S`); not stored.
    silent_since: datetime | None = None
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
    #: The hold variant this executor runs (`BrandControl.variant`), stored so a restart hands back with it.
    variant: str | None = None
    #: Since when an idle hold's battery has charged (`HOLD_CHARGE_S`); not stored.
    charging_since: datetime | None = None
    #: Since when a balanced grid charge has taken more from the grid than commanded (`CHARGE_OVER_S`); not stored.
    over_since: datetime | None = None
    #: Roles written before the brand's enter order after a change of action: the grid charge's stop
    #: (`BrandControl.peak_first`), so no limit is loosened while the plant still charges from the grid; not stored.
    lead: tuple[str, ...] = ()
    #: Roles that go idle with the switch (`Role.idle_when`) whose hand-back write was never read back (a fuse
    #: hand-back writes the switch off at once): the plant may still hold the command. The next enter primes first.
    unsure: tuple[str, ...] = ()
    #: A silence SpotNav wrote (`Expiry.silence`, EcoFlow's Modbus Control off) still stands, or is not yet undone
    #: and read back: every hand-back first takes the plant back as the inverter's own self-consumption (the
    #: discharge to the house, the integration renewing again) and only then hands back, so a dead-man that did not act
    #: leaves no command standing. Stored: a restart in the silence takes the plant back too.
    retake: bool = False
    #: An enter that writes the brand's `prime` values first, until every role reads its target; not stored (a
    #: restart hands back, and `unsure` primes the next enter again).
    priming: bool = False
    #: Roles read back during this hand-back; not stored.
    confirmed: tuple[str, ...] = ()
    #: Whether the control runs the brand's targets per phase (`BrandControl.phased`), stored so a restart hands
    #: back with those roles; and the charge commanded on L1, L2, L3 while a grid charge runs per phase (kW,
    #: negative for the verification's discharge), `None` otherwise.
    phased: bool = False
    phase_kw: Phases | None = None
    #: The run of failing readings of a charge per phase (`monitor.judge_phases`) and when it was last judged; not
    #: stored.
    phase_bad: tuple[str, ...] = ()
    phase_judged_at: datetime | None = None
    #: The targets before the last change and when a phase was last raised; the count of readings in a row with a
    #: phase over its target and when that was last judged (never reset by a change of the targets); not stored.
    phase_was_kw: Phases | None = None
    phase_raised_at: datetime | None = None
    phase_over: int = 0
    phase_over_at: datetime | None = None
    #: Writes today to setpoints (`commands.WriteClass`), counted apart from the settings' (`writes_today`).
    setpoint_writes_today: int = 0
    #: Renewal rounds today (`BrandControl.renewal_cap`) and commands that lapsed by themselves today
    #: (`BrandControl.command_role`, `LAPSES_A_DAY`).
    renewals_today: int = 0
    lapses_today: int = 0
    #: The command role whose value was set from outside under SpotNav's own command (`Executor._gave_up`): nothing
    #: is written while it stands, but a hand-back for the fuse still ends SpotNav's own command. Not stored.
    altered: str | None = None
    #: The value SpotNav last wrote per role for an action (not a restore), stored: what a refused restore shows.
    wrote: dict[str, Native] = field(default_factory=dict)
    #: After a hand-back, the roles watched for a restore not held (`BrandControl.settle_s`): per role the state found,
    #: its restore value, SpotNav's value and the entity; until when; and the restores written again so far.
    settle: dict[str, tuple[str, Native, Native, str]] = field(default_factory=dict)
    settle_until: datetime | None = None
    settle_misses: int = 0
    #: The house's net load the running action's values were written for (`BrandControl.house_states`); the load
    #: behind each phase's point in the targets written (a `grid` target per phase) and the one before the last
    #: change. Not stored: a restart hands back.
    house_kw: float | None = None
    phase_behind_kw: Phases | None = None
    phase_was_behind_kw: Phases | None = None

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
            "variant": self.variant,
            "unsure": list(self.unsure),
            "retake": self.retake,
            "phased": self.phased,
            "phase_kw": None if self.phase_kw is None else list(self.phase_kw),
            "setpoint_writes_today": self.setpoint_writes_today,
        } | self._settle_dict() | self._renewal_dict()

    def _renewal_dict(self) -> dict[str, Any]:
        """The day's renewal rounds and lapses, stored only when there are any: a recipe without them stores what it
        always did."""
        out: dict[str, Any] = {}
        if self.renewals_today:
            out["renewals_today"] = self.renewals_today
        if self.lapses_today:
            out["lapses_today"] = self.lapses_today
        return out

    def _settle_dict(self) -> dict[str, Any]:
        """The values written and the watch after a hand-back (`BrandControl.settle_s`), stored only when there are
        any: a recipe without the watch stores what it always did."""
        if not (self.wrote or self.settle or self.settle_misses):
            return {}
        return {
            "wrote": dict(self.wrote),
            "settle": {name: list(row) for name, row in self.settle.items()},
            "settle_until": None if self.settle_until is None else self.settle_until.isoformat(),
            "settle_misses": self.settle_misses,
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
        raw_phases = data.get("phase_kw")
        phase_kw: Phases | None = None
        if isinstance(raw_phases, list) and len(raw_phases) == 3 and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in raw_phases
        ):
            phase_kw = (float(raw_phases[0]), float(raw_phases[1]), float(raw_phases[2]))
        writes = data.get("writes_today")
        setpoints = data.get("setpoint_writes_today")
        misses = data.get("settle_misses")

        def count(key: str) -> int:
            value = data.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
        settle: dict[str, tuple[str, Native, Native, str]] = {}
        raw_settle = data.get("settle")
        if isinstance(raw_settle, Mapping):
            for name, row in raw_settle.items():
                if (
                    isinstance(row, list) and len(row) == 4 and isinstance(row[0], str) and isinstance(row[3], str)
                    and all(isinstance(value, (str, bool, int, float)) for value in row[1:3])
                ):
                    settle[str(name)] = (row[0], row[1], row[2], row[3])
        retries: dict[str, tuple[int, datetime]] = {}
        raw_retries = data.get("retries")
        if isinstance(raw_retries, Mapping):
            for name, row in raw_retries.items():
                if isinstance(row, list) and len(row) == 2 and isinstance(row[0], int) and not isinstance(row[0], bool):
                    at = parse_aware_datetime(row[1])
                    if at is not None and row[0] >= 0:
                        retries[str(name)] = (row[0], at)
        return cls(
            phase=phase if isinstance(phase, str) and phase in _PHASES else "idle",
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
            variant=text("variant"),
            unsure=names("unsure"),
            retake=data.get("retake") is True,
            phased=data.get("phased") is True,
            phase_kw=phase_kw,
            setpoint_writes_today=(
                setpoints if isinstance(setpoints, int) and not isinstance(setpoints, bool) and setpoints >= 0 else 0
            ),
            wrote=natives("wrote"),
            settle=settle,
            settle_until=parse_aware_datetime(data.get("settle_until")),
            settle_misses=misses if isinstance(misses, int) and not isinstance(misses, bool) and misses >= 0 else 0,
            renewals_today=count("renewals_today"),
            lapses_today=count("lapses_today"),
        )


@dataclass(frozen=True, slots=True)
class StepResult:
    state: ExecState
    ops: tuple[Op, ...] = ()
    #: Log rows: what happened and why.
    events: tuple[dict[str, Any], ...] = ()
    #: The state must be stored before the ops are made (a new snapshot).
    persist_first: bool = False
