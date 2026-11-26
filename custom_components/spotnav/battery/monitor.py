"""The control-failure monitor: did a command to the battery take, judged from what the battery measures.

After a command (Predbat's `control_monitor.py`, `plans/research_integrations/predbat_lowhanging_2026-11-13.md`
item 5) the monitor waits `GRACE_S`, then reads the battery's power at most every `READING_GAP_S` and compares each
reading with what the battery can deliver: the command's rate capped by its rated power and by the site's limit
(the fuse headroom for a charge, the house's draw for a discharge), and not judged at all near its SoC limits
(a charge tapers near full, a discharge stops near empty). Three consecutive failing readings are one failure,
`no_effect` (all three far off) or `partial`, counted once per command (per slot when the command names its end).
A reading that is stale, missing or cannot be judged breaks the run of three: the expensive mistake is a false
alarm. Nothing is judged after the slot ends. A failure is a counter, never a notification; `writes` counts the
commands written per day.

What each command expects:

* `hold` (no discharge): discharge at most `FLOOR_KW`. Still discharging is `no_effect` while it is at least
  `1 - FAIL_FRACTION` of the discharge before the command, `partial` below that. An idle hold (`Command.idle`,
  Sigenergy's mode 4 with the charge limit at 0) is judged on the battery's own power alone: a charge of more than
  `HOLD_CHARGE_TOLERANCE_KW` is `no_effect` (mode 4 is a charge at the charge limit, from the grid once the sun is
  short). A hold that stores the sun is `partial` when the site's grid power is known (`Reading.grid_kw`) and the
  site exports more than `EXPORT_TOLERANCE_KW` while the battery does not charge (below `CHARGE_TAPER_SOC_PCT`):
  the hold stopped the sun charging too.
* `curtail` (the site's export limit at 0 kW, judged on the site's grid power, + import): export at most
  `EXPORT_TOLERANCE_KW`, after `CURTAIL_GRACE_S`. Still exporting at least `1 - FAIL_FRACTION` of the export before
  the command is `no_effect`, less is `partial`. A site that exported before the command and now **imports** more
  than `EXPORT_TOLERANCE_KW` beyond what its battery charges and its chargers draw (`Reading.excused_kw`, where
  read) is `import_rose`: the limit capped the inverter's own output instead of the export, so the house is fed from
  the grid. Judged only for `IMPORT_ROSE_WINDOW_S` after the command (such a cap acts at once; a load that starts
  later is the house's own).
* `charge`/`discharge` at a rate: below `FAIL_FRACTION` of what the battery can deliver (at least `FLOOR_KW`) is
  `no_effect`, below `PARTIAL_FRACTION` is `partial`. A deliverable rate under `FLOOR_KW` is not judged.

* a charge **per phase** (`judge_phases`, a plant that takes a target per phase, `commands.PhaseControl`): judged
  on the plant's own power per phase (the adapter's `plant_phase_power`, + output), each phase against its own
  target. A phase that takes more than its target by `PHASE_OVER_KW` (or a quarter of the target) is `over`: that is
  the one that matters for the fuse, since each target is sized to its phase's headroom. Every phase with a target
  below `FAIL_FRACTION` of it is `no_effect`, one below `PARTIAL_FRACTION` is `partial`. The executor counts
  `CONFIRM_READINGS` in a row, `PHASE_GAP_S` apart, after `PHASE_GRACE_S` (a target settles in seconds).

**A command shown to act** (`proves`, the event `proved`). A written command's readings that are "ok" are no failure,
and on their own say little: a battery left alone often does what a command asks. `CONFIRM_READINGS` in a row that
are "ok" **and** could not have read so without the command are told once per command (`MonitorEvent.kind`
`proved`), so that the shell can count the plan's own command as that mode shown on the plant's firmware
(`gate.plan_proved`). A charge or discharge: what the battery could deliver is at least `PROVE_MIN_KW`, and for a
charge the battery was not charging before the command and the site's grid power is known and shows the site
drawing from the grid (a battery left alone charges only from a surplus of sun, with nothing drawn: sun that came
out after the command is not the command acting). A hold: the battery was discharging before the command
(above `FLOOR_KW`, what the verification asks before it judges a hold), it is not near its floor and at least at
`PROVE_HOLD_SOC_MIN_PCT` (a floor of the battery's own that is not read stops it too), and the site's
grid power is known and shows the house drawing from the grid (so there is something the battery would cover if it
were not held). An export limit never. The run is broken by any reading that is not such a one, and is not kept
over a restart. A failure is still only counted: it is never written to a report from here.

**In shadow** nothing was commanded, so the monitor cannot tell whether a command would have taken: "a hold of 0 kW
would not have taken, the battery kept discharging" is unknowable without writing. A dry run (`Command.written`
false) runs the same windows on the real readings and reports, once, either that it could not have judged (and
why: no rating, SoC near a limit, stale readings, the command ended inside the grace) or the three readings it
would have judged, with what the battery did without the command (`baseline`). A dry run never counts a failure.

Pure: no Home Assistant import; times are aware datetimes from the caller. State survives a restart (`as_dict`,
`from_dict`); the run of readings does not, so a restart never completes a run of three.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final, Literal

from .shadow import parse_aware_datetime

GRACE_S: Final = 300.0
#: An export limit acts within seconds (it is the inverter's own loop): judged sooner.
CURTAIL_GRACE_S: Final = 120.0
#: The export (kW) a curtailed site may still show: meter noise and the inverter's regulation around 0.
EXPORT_TOLERANCE_KW: Final = 0.5
CONFIRM_READINGS: Final = 3
#: Readings judged are at least this far apart.
READING_GAP_S: Final = 60.0
#: A reading older than this is not judged.
READING_MAX_AGE_S: Final = 120.0
#: Below this the battery counts as idle (kW).
FLOOR_KW: Final = 0.2
FAIL_FRACTION: Final = 0.25
PARTIAL_FRACTION: Final = 0.5
#: A charge is not judged at or above this SoC (it tapers), a discharge not at or below the floor plus this margin.
CHARGE_TAPER_SOC_PCT: Final = 90.0
DISCHARGE_MARGIN_PCT: Final = 5.0
COUNTER_DAYS: Final = 30
COUNTERS: Final[tuple[str, ...]] = ("writes", "no_effect", "partial")
#: How long after a curtail an import that rose is read as the limit's doing (s).
IMPORT_ROSE_WINDOW_S: Final = 600.0

#: What an idle hold's battery may charge (kW) before a reading counts as a hold not taken.
HOLD_CHARGE_TOLERANCE_KW: Final = 0.5

#: A charge per phase: how long after its targets changed it is first judged (s), how far apart its readings are
#: (s), and how much more than its target a phase may take (kW, or a quarter of the target when that is more).
PHASE_GRACE_S: Final = 120.0
PHASE_GAP_S: Final = 30.0
PHASE_OVER_KW: Final = 0.3
#: A phase over its target (the runaway) is judged sooner and more often: from this long after the charge's writes
#: were taken (s; a target settles in under 5 s), a reading every this long (s).
PHASE_OVER_GRACE_S: Final = 20.0
PHASE_OVER_GAP_S: Final = 10.0

#: The least a charge or a discharge must be expected to deliver (kW) for a run of "ok" readings to show the mode
#: obeyed (`proves`): an "ok" needs only `PARTIAL_FRACTION` of it, 0.5 kW here, which is clear of an idle battery
#: (`FLOOR_KW`) and of a meter's noise; it is the size of the verification's own probe (`verify.PROBE_KW`).
PROVE_MIN_KW: Final = 1.0
#: The least state of charge (%) at which a hold's "ok" readings show it obeyed (`proves`): the level the
#: verification asks before it starts (`gate.SUITABLE_SOC_PCT`). Below it a battery may have stopped at a floor of
#: its own that the facts do not read (`Capability.soc_min_pct` is then 0).
PROVE_HOLD_SOC_MIN_PCT: Final = 30.0

PhaseVerdict = Literal["ok", "over", "partial", "no_effect"]

CommandKind = Literal["hold", "charge", "discharge", "curtail"]
ReadingVerdict = Literal["ok", "partial", "no_effect", "import_rose"]
_KINDS: Final = frozenset({"hold", "charge", "discharge", "curtail"})


def grace_s(kind: str) -> float:
    """How long after a command its effect is first judged (s)."""
    return CURTAIL_GRACE_S if kind == "curtail" else GRACE_S


@dataclass(frozen=True, slots=True)
class Command:
    kind: CommandKind
    at: datetime
    #: The rate commanded (kW, ≥ 0); 0 for a hold.
    rate_kw: float = 0.0
    #: The battery's power when the command was given (kW, + charging).
    before_kw: float | None = None
    #: The end of the slot the command is for; `None`: until it is stopped.
    slot_end: datetime | None = None
    #: `False` for a dry run (shadow): nothing was written.
    written: bool = False
    #: A hold that leaves the battery idle (`commands.HoldVariant.idle`): a charge is a hold not taken.
    idle: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at": self.at.isoformat(),
            "rate_kw": self.rate_kw,
            "before_kw": self.before_kw,
            "slot_end": None if self.slot_end is None else self.slot_end.isoformat(),
            "written": self.written,
            "idle": self.idle,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Command | None:
        if not isinstance(data, Mapping):
            return None
        kind, at, rate, before = data.get("kind"), parse_aware_datetime(data.get("at")), data.get("rate_kw"), data.get("before_kw")
        if not isinstance(kind, str) or kind not in _KINDS or at is None or not _finite(rate):
            return None
        return cls(
            kind=kind,
            at=at,
            rate_kw=float(rate),
            before_kw=float(before) if _finite(before) else None,
            slot_end=parse_aware_datetime(data.get("slot_end")),
            written=data.get("written") is True,
            idle=data.get("idle") is True,
        )


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True, slots=True)
class Reading:
    """The battery now: its power (kW, + charging), how old that is (s) and its SoC."""

    power_kw: float | None
    age_s: float | None = None
    soc_pct: float | None = None
    #: The site's grid power (kW, + import), for a hold that stores the sun: `None` when unknown (then the battery's
    #: own power alone is judged). For a charge it is read only by `proves`.
    grid_kw: float | None = None
    #: For a curtail: what the battery charges and the site's chargers draw now (kW), where read: an import up to it
    #: is theirs, not a sign of an inverter held at no output (`import_rose`).
    excused_kw: float | None = None


@dataclass(frozen=True, slots=True)
class Capability:
    """What the battery and the site allow."""

    rated_charge_kw: float | None = None
    rated_discharge_kw: float | None = None
    soc_min_pct: float = 0.0
    soc_max_pct: float = 100.0
    #: The site's limit on the command: the fuse headroom for a charge, the house's draw for a discharge (kW).
    site_cap_kw: float | None = None


def deliverable_kw(
    command: Command, capability: Capability, soc_pct: float | None, *, full_soc_pct: float = CHARGE_TAPER_SOC_PCT
) -> tuple[float | None, str | None]:
    """What the battery can deliver for the command (kW), or why that cannot be judged. `full_soc_pct`: from this
    state of charge on a charge is not judged (`CHARGE_TAPER_SOC_PCT`; the hardware test's short charge passes its
    own, `verify.TEST_CHARGE_SOC_MAX_PCT`)."""
    if command.kind in ("hold", "curtail"):
        return 0.0, None
    if soc_pct is None:
        return None, "soc_unknown"
    if command.kind == "charge":
        rated = capability.rated_charge_kw
        if soc_pct >= min(capability.soc_max_pct, full_soc_pct):
            return None, "soc_near_full"
    else:
        rated = capability.rated_discharge_kw
        if soc_pct <= capability.soc_min_pct + DISCHARGE_MARGIN_PCT:
            return None, "soc_near_empty"
    if rated is None:
        return None, "rating_unknown"
    caps = [command.rate_kw, rated] + ([] if capability.site_cap_kw is None else [max(capability.site_cap_kw, 0.0)])
    expected = min(caps)
    if expected < FLOOR_KW:
        return None, "too_small_to_judge"
    return expected, None


def judge(
    command: Command, expected_kw: float, power_kw: float, grid_kw: float | None = None, soc_pct: float | None = None,
    *, excused_kw: float | None = None,
) -> ReadingVerdict:
    """One reading against what the command expects (a hold that stores the sun also against the site's grid power,
    when known; a curtail's import against `excused_kw`, `Reading.excused_kw`)."""
    if command.kind == "curtail":
        export = max(-power_kw, 0.0)
        before = None if command.before_kw is None else max(-command.before_kw, 0.0)
        if export <= EXPORT_TOLERANCE_KW:
            excused = excused_kw if excused_kw is not None and math.isfinite(excused_kw) else 0.0
            if before is not None and before > EXPORT_TOLERANCE_KW and power_kw > EXPORT_TOLERANCE_KW + max(excused, 0.0):
                return "import_rose"
            return "ok"
        if before is not None and before > EXPORT_TOLERANCE_KW and export >= (1.0 - FAIL_FRACTION) * before:
            return "no_effect"
        return "partial"
    if command.kind == "hold":
        discharge = max(-power_kw, 0.0)
        if discharge <= FLOOR_KW:
            if command.idle:
                return "no_effect" if power_kw > HOLD_CHARGE_TOLERANCE_KW else "ok"
            if grid_kw is None or not math.isfinite(grid_kw):
                return "ok"
            sun_left = -grid_kw > EXPORT_TOLERANCE_KW and power_kw < FLOOR_KW
            if sun_left and soc_pct is not None and soc_pct < CHARGE_TAPER_SOC_PCT:
                return "partial"
            return "ok"
        before = None if command.before_kw is None else max(-command.before_kw, 0.0)
        if before is not None and before > FLOOR_KW and discharge >= (1.0 - FAIL_FRACTION) * before:
            return "no_effect"
        return "partial"
    achieved = max(power_kw if command.kind == "charge" else -power_kw, 0.0)
    if achieved < max(FAIL_FRACTION * expected_kw, FLOOR_KW):
        return "no_effect"
    if achieved < PARTIAL_FRACTION * expected_kw:
        return "partial"
    return "ok"


def proves(command: Command, expected_kw: float, reading: Reading, capability: Capability) -> bool:
    """Whether an "ok" reading of a written command shows the command acting, not a battery that would have read
    the same left alone (see the module)."""
    if not command.written:
        return False
    before = command.before_kw
    soc, grid = reading.soc_pct, reading.grid_kw
    importing = grid is not None and math.isfinite(grid) and grid > FLOOR_KW
    if command.kind == "charge":
        # Charging while the site draws from the grid: the sun alone never does that.
        return expected_kw >= PROVE_MIN_KW and before is not None and before <= FLOOR_KW and importing
    if command.kind == "discharge":
        return expected_kw >= PROVE_MIN_KW
    if command.kind != "hold" or before is None or before >= -FLOOR_KW:
        return False
    if soc is None or soc <= capability.soc_min_pct + DISCHARGE_MARGIN_PCT or soc < PROVE_HOLD_SOC_MIN_PCT:
        return False  # near its floor the battery stops by itself
    return importing


def judge_phases(
    targets_kw: tuple[float, float, float], plant_kw: tuple[float, float, float], *, tapering: bool = False
) -> PhaseVerdict:
    """One reading of a charge per phase: each phase's draw (the plant's own power on it, `plant_kw`, + output)
    against its target (`targets_kw`, the charge commanded on it, 0 for none). `tapering`: the battery is near full,
    so a draw below the target says nothing (a draw above it still does)."""
    draws = [max(-power, 0.0) for power in plant_kw]
    targets = [max(target, 0.0) for target in targets_kw]
    if any(draw > target + max(PHASE_OVER_KW, FAIL_FRACTION * target) for draw, target in zip(draws, targets, strict=True)):
        return "over"
    judged = [(draw, target) for draw, target in zip(draws, targets, strict=True) if target >= FLOOR_KW]
    if tapering or not judged:
        return "ok"
    if all(draw < FAIL_FRACTION * target for draw, target in judged):
        return "no_effect"
    if any(draw < PARTIAL_FRACTION * target for draw, target in judged):
        return "partial"
    return "ok"


@dataclass(frozen=True, slots=True)
class MonitorEvent:
    """A failure counted (a written command), a written command shown to act (`proved`), or what a dry run could
    tell."""

    kind: Literal["failure", "dry_run", "proved"]
    at: datetime
    command: Command
    #: `no_effect`/`partial`/`import_rose` for a failure; for a dry run `baseline_<verdict>` or `not_judgeable:<why>`;
    #: `ok` for a command shown to act (`expected_kw` is then the least of its readings').
    outcome: str
    expected_kw: float | None = None
    readings: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at": self.at.isoformat(),
            "command": self.command.as_dict(),
            "outcome": self.outcome,
            "expected_kw": self.expected_kw,
            "readings": [dict(reading) for reading in self.readings],
        }


@dataclass(frozen=True, slots=True)
class Watch:
    command: Command
    #: The run of consecutive judged readings (`at`, `power_kw`, `verdict`); not kept over a restart.
    run: tuple[dict[str, Any], ...] = ()
    last_reading_at: datetime | None = None
    #: Why the last reading could not be judged.
    why_not: str | None = None
    done: bool = False
    #: The run of consecutive "ok" readings that show the command acting (`proves`), and whether a full run was
    #: told; neither is kept over a restart.
    oks: tuple[dict[str, Any], ...] = ()
    proved: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command.as_dict(),
            "last_reading_at": None if self.last_reading_at is None else self.last_reading_at.isoformat(),
            "why_not": self.why_not,
            "done": self.done,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Watch | None:
        if not isinstance(data, Mapping):
            return None
        command = Command.from_dict(data.get("command"))
        if command is None:
            return None
        why_not = data.get("why_not")
        return cls(
            command,
            last_reading_at=parse_aware_datetime(data.get("last_reading_at")),
            why_not=why_not if isinstance(why_not, str) else None,
            done=data.get("done") is True,
        )


@dataclass(slots=True)
class Monitor:
    """One watch at a time (one battery, one command) and the counters per local day."""

    watch: Watch | None = None
    counters_by_day: dict[str, dict[str, int]] = field(default_factory=dict)

    def start(self, command: Command) -> MonitorEvent | None:
        """Watch a new command; a dry run still unjudged is closed first (its event returned)."""
        event = self.stop(command.at)
        if command.written:
            self._count(command.at, "writes")
        self.watch = Watch(command)
        return event

    def stop(self, now: datetime) -> MonitorEvent | None:
        """The command ends. A dry run that never got to judge says why, once."""
        watch, self.watch = self.watch, None
        if watch is None or watch.done or watch.command.written:
            return None
        command = watch.command
        if (now - command.at).total_seconds() < grace_s(command.kind):
            why = "ended_within_grace"
        else:
            why = watch.why_not or "too_few_readings"
        return MonitorEvent("dry_run", now, command, f"not_judgeable:{why}", readings=watch.run)

    def observe(self, reading: Reading, capability: Capability, now: datetime) -> MonitorEvent | None:
        watch = self.watch
        if watch is None or watch.done:
            return None
        command = watch.command
        if command.slot_end is not None and now >= command.slot_end:
            return self.stop(now)
        if (now - command.at).total_seconds() < grace_s(command.kind):
            return None
        if watch.last_reading_at is not None and (now - watch.last_reading_at).total_seconds() < READING_GAP_S:
            return None
        power = reading.power_kw
        if power is None or not math.isfinite(power) or reading.age_s is None or reading.age_s > READING_MAX_AGE_S:
            self.watch = replace(watch, run=(), oks=(), last_reading_at=now, why_not="reading_stale")
            return None
        expected, why_not = deliverable_kw(command, capability, reading.soc_pct)
        if expected is None:
            self.watch = replace(watch, run=(), oks=(), last_reading_at=now, why_not=why_not)
            return None
        verdict = judge(command, expected, power, reading.grid_kw, reading.soc_pct, excused_kw=reading.excused_kw)
        if verdict == "import_rose" and (now - command.at).total_seconds() > IMPORT_ROSE_WINDOW_S:
            # A load that started long after the limit was set is the house's own.
            verdict = "ok"
        row = {"at": now.isoformat(), "power_kw": round(power, 3), "soc_pct": reading.soc_pct, "verdict": verdict}
        if not command.written:
            # A dry run judges nothing: it shows the first three readings it would have judged, as a baseline.
            run = (*watch.run, row)
            if len(run) < CONFIRM_READINGS:
                self.watch = replace(watch, run=run, last_reading_at=now, why_not=None)
                return None
            self.watch = replace(watch, run=run, last_reading_at=now, why_not=None, done=True)
            verdicts = {item["verdict"] for item in run}
            baseline = verdicts.pop() if len(verdicts) == 1 else "mixed"
            return MonitorEvent("dry_run", now, command, f"baseline_{baseline}", expected, run)
        if verdict == "ok":
            oks: tuple[dict[str, Any], ...] = ()
            if not watch.proved and proves(command, expected, reading, capability):
                oks = (*watch.oks, row | {"expected_kw": round(expected, 3), "grid_kw": reading.grid_kw})
            if len(oks) < CONFIRM_READINGS:
                self.watch = replace(watch, run=(), oks=oks, last_reading_at=now, why_not=None)
                return None
            # Told once per command; the watch goes on, a later failure is still counted.
            self.watch = replace(watch, run=(), oks=(), proved=True, last_reading_at=now, why_not=None)
            return MonitorEvent("proved", now, command, "ok", min(item["expected_kw"] for item in oks), oks)
        run = (*watch.run, row)
        if len(run) < CONFIRM_READINGS:
            self.watch = replace(watch, run=run, oks=(), last_reading_at=now, why_not=None)
            return None
        verdicts = {item["verdict"] for item in run}
        outcome = next(iter(verdicts)) if len(verdicts) == 1 and verdicts <= {"no_effect", "import_rose"} else "partial"
        self.watch = replace(watch, run=run, last_reading_at=now, why_not=None, done=True)
        # Counted with the commands that had no effect: the limit did not do what was asked.
        self._count(now, "no_effect" if outcome == "import_rose" else outcome)
        return MonitorEvent("failure", now, command, outcome, expected, run)

    def _count(self, now: datetime, key: str) -> None:
        day = now.date().isoformat()
        counts = self.counters_by_day.setdefault(day, dict.fromkeys(COUNTERS, 0))
        counts[key] = counts.get(key, 0) + 1
        if len(self.counters_by_day) > COUNTER_DAYS:
            for old in sorted(self.counters_by_day)[:-COUNTER_DAYS]:
                del self.counters_by_day[old]

    def counters(self, day: str) -> dict[str, int]:
        return dict.fromkeys(COUNTERS, 0) | self.counters_by_day.get(day, {})

    def writes_today(self, now: datetime) -> int:
        return self.counters(now.date().isoformat())["writes"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "watch": None if self.watch is None else self.watch.as_dict(),
            "counters": {day: dict(counts) for day, counts in sorted(self.counters_by_day.items())},
        }

    @classmethod
    def from_dict(cls, data: Any) -> Monitor:
        monitor = cls()
        if not isinstance(data, Mapping):
            return monitor
        monitor.watch = Watch.from_dict(data.get("watch"))
        counters = data.get("counters")
        if isinstance(counters, Mapping):
            for day, counts in sorted(counters.items())[-COUNTER_DAYS:]:
                if isinstance(day, str) and isinstance(counts, Mapping):
                    monitor.counters_by_day[day] = {
                        key: value for key, value in counts.items()
                        if key in COUNTERS and isinstance(value, int) and not isinstance(value, bool) and value >= 0
                    }
        return monitor
