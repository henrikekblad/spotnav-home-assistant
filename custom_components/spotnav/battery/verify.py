"""The battery's verification routine: does this battery, on this firmware, do what each command asks? The gate of
SpotNav's battery control (`control.py`, `gate.py`): control is on only once it passed.

Modelled on evcc's `evcc meter verifybatterymodes` (evcc `docs/features/battery.mdx`, docs commit 6c5bcd7 of
2026-11-03; `plans/research_integrations/evcc_gap_2026-11-13.md` §2.1) and on the self-test of
`battery_control_2026-10-03.md` §3.3: for each mode in turn (hold, charge at a set power, discharge to the house,
back to normal) it commands the mode **through the executor** (so every write is read back by the ledger and every
safety exit applies), waits for the write to be taken and the battery to settle (`SETTLE_S`), reads the battery's
power `READINGS` times `READING_GAP_S` apart, judges them as the control-failure monitor does (`monitor.judge`),
and hands the battery back before the next mode. The last mode, normal, checks that the hand-back left every control
at the value it had before. The result is a report per firmware string, because the behaviour has changed with
firmware before (Sigenergy SPC116/SPC117).

Outcomes per mode: `obeyed` (every reading as asked), `ignored` (every reading as if nothing was written),
`partial`, `not_judgeable:<why>` (the battery could not show it now: nothing to stop, nearly full, nearly empty, no
rating, stale readings), `aborted:<why>` (the executor handed the battery back for a reason of its own: a write not
taken, the fuse, a stale reading, a management change; the routine then stops, but for an abort for the moment's
circumstances normal still judges that hand-back) or, for normal, `restored` (only after this run wrote something;
`not_judgeable:nothing_written` otherwise).

**Hold variants** (`commands.HoldVariant`, Sigenergy's `msc_no_discharge` then `pv_first`): hold is tried with the
brand's first variant; when the plant ignores it (`VARIANT_RETRY`: every reading as if nothing was written) the
battery is handed back, the block that finding left is lifted, and hold is tried again with the next variant, never
two at once, and only once the battery discharges again (`VARIANT_WAIT_S`; otherwise hold is left
`not_judgeable:battery_not_discharging` and a later run starts with that variant). The hold row records the variant judged
(`variant`) and every one tried (`variants`); a later run starts after the variants that failed, and control runs the
one that passed (`gate.hold_variant`). A hold reading is judged as the monitor judges it (`monitor.judge`): no
discharge while the house draws; for an idle hold (`HoldVariant.idle`) no charge either, on the battery's own power;
for one that stores the sun, the sun still stored when the site's grid power is known.

**Per phase** (the mode `per_phase`, only where the brand has targets per phase, `commands.PhaseControl`, and the
shell names the mode: the targets and the plant's power per phase are there). It answers what a charge per phase
rests on: does a target on one phase move that phase, with the sign the recipe says, and is that phase the site
meter's phase of the same name? Through the executor's phased control (`ExecInputs.phase_probe_kw`) it first
commands every target at 0, where the plant puts out nothing on any phase, so that the house per phase can be read
whatever the wiring is (in its own mode the plant covers each phase, and a house reckoned from a plant phase on
another meter phase would be wrong from the start). Then, for each phase in turn, a small **discharge** on that
phase alone (`PROBE_KW`; the other two at 0). A discharge
because it lowers that phase's import whatever the house does, so it needs no room under the fuse and can be tried
on a phase that has none, which is the phase that matters; the recipe's sign makes a charge the same target
negated. After `PHASE_SETTLE_S` (a target settles in seconds) each of `READINGS` readings `PHASE_READING_GAP_S`
apart must show the plant's own power on that phase at the target and about nothing on the other two (the sign and
that the target acts alone), and the house on that phase, reckoned as the meter's import plus the plant's output
there, unchanged from before the probe (the mapping: with the plant's phase on another of the meter's phases the
house would seem to jump by the probe on both). All three phases so: `obeyed`, the capability (`gate.per_phase`
reads `verified`). The plant's power unchanged: `ignored`. The house jumping in every reading: `partial` (the row
of that phase says `wrong_phase`); in some: `not_judgeable:house_changed`, tried again at a later run. The first
real charge is then judged per phase by the executor (`monitor.judge_phases`). A moment that could make a sound
plant look wrong is never a decisive answer (`_probe_unfit`): the sun giving more than the house takes (the site
exports or the battery charges before the probe, so a phase's target is not the only thing moving it), the battery
near full or near empty (it cannot put out, or take, what a target asks), or the export limit held at 0
(`Sample.curtailing`): `not_judgeable:sun_surplus`, `soc_near_end`, `export_curtailed`, tried again at a later run.

The export limit (`commands.EXPORT_CONTROLS`) has its own two modes (`EXPORT_MODES`): `curtail` (the limit at 0 kW
through the executor's export instance) and `normal` (handed back). Its sample's power is the site's grid power (kW,
+ import), judged as a hold judges the battery's: the export must stop. It can be judged only while the site exports
(`not_judgeable:not_exporting` otherwise).

**The hardware test** (`Timing.quick`, `stop_on_fail`; `control.BatteryControl.async_hardware_test`): the same
modes through the same executor, started by a person and bounded to minutes: a shorter settle and fewer, closer
readings, the hold's first variant that has not failed only, and the first mode that does not pass (an outcome
other than `obeyed` or `not_judgeable:*`) skips the rest but normal, the hand-back's check. Its report is the
test's own: it never counts towards the routine's per firmware (`gate.merge_report`).

Pure: no Home Assistant import; times are aware datetimes from the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from .commands import ACTION_CURTAIL_EXPORT, Phases
from .executor import Executor, StepResult
from .gate import MODE_PER_PHASE, PHASE_NAMES, mode_verdict
from .monitor import (
    CHARGE_TAPER_SOC_PCT,
    DISCHARGE_MARGIN_PCT,
    FAIL_FRACTION,
    FLOOR_KW,
    Capability,
    Command,
    deliverable_kw,
    judge,
)
from .plan_model import ACTION_DISCHARGE_HOUSE, ACTION_GRID_CHARGE, ACTION_HOLD, ACTION_SELF_CONSUME

#: How long a mode settles after its writes are taken before readings count (s), as the monitor's grace.
SETTLE_S: Final = 300.0
READINGS: Final = 3
READING_GAP_S: Final = 60.0
#: A reading older than this is not judged (s).
READING_MAX_AGE_S: Final = 120.0
#: The charge power tried (kW), at most the battery's own rating.
TEST_CHARGE_KW: Final = 2.0
#: A mode whose writes are not taken within this long is aborted (s).
COMMAND_TIMEOUT_S: Final = 300.0
#: Reports kept, one per firmware string.
MAX_REPORTS: Final = 10
#: Hold outcomes after which the next hold variant is tried: the plant took the writes and went on as if nothing had
#: been written (the variant's limit does not act on this firmware). Anything else (a partial effect, a plant that
#: did something other than told, such as an idle hold that charges, a write not taken, a timeout) ends the run as
#: before: a plant that misbehaves under one variant is not given another.
VARIANT_RETRY: Final = ("ignored", "aborted:no_effect")
#: The next variant is commanded only once the battery discharges again after the hand-back (a hold is judged against
#: the discharge it stops), waiting at most this long (s); otherwise hold is left pending for a later run.
VARIANT_WAIT_S: Final = 300.0

#: The per-phase probe: the discharge commanded on one phase (kW), how long it settles (s), how far apart its
#: readings are (s), how far the plant's power on the probed phase may be from the target and on the others from 0
#: (kW), and how far the house on the probed phase may seem to move (kW) before the mapping is in doubt.
PROBE_KW: Final = 1.0
PHASE_SETTLE_S: Final = 60.0
PHASE_READING_GAP_S: Final = 20.0
PROBE_POWER_TOLERANCE_KW: Final = 0.3
PROBE_HOUSE_TOLERANCE_KW: Final = 0.4

#: The modes, in order: (name, the plan action commanded). `per_phase` runs only when a run names it.
MODES: Final[tuple[tuple[str, str], ...]] = (
    ("hold", ACTION_HOLD),
    ("charge", ACTION_GRID_CHARGE),
    ("discharge_house", ACTION_DISCHARGE_HOUSE),
    (MODE_PER_PHASE, ACTION_GRID_CHARGE),
    ("normal", ACTION_SELF_CONSUME),
)

#: The export limit's modes: the export stopped, then handed back.
EXPORT_MODES: Final[tuple[tuple[str, str], ...]] = (
    ("curtail", ACTION_CURTAIL_EXPORT),
    ("normal", ACTION_SELF_CONSUME),
)

Stage = Literal["command", "settle", "read", "hand_back", "done"]


@dataclass(frozen=True, slots=True)
class Timing:
    """How long a mode settles, how many readings judge it and how far apart, and how long its writes may take (s)."""

    settle_s: float = SETTLE_S
    readings: int = READINGS
    reading_gap_s: float = READING_GAP_S
    command_timeout_s: float = COMMAND_TIMEOUT_S

    @classmethod
    def quick(cls) -> Timing:
        """The hardware test's: hold, charge, discharge and the hand-back in about nine minutes. A mode after a
        hand-back waits for the executor's re-arm (`executor.REARM_S`, never shortened) before its writes are
        taken, so the time to take them stays generous; the settle and the readings are what is shortened."""
        return cls(settle_s=20.0, readings=2, reading_gap_s=10.0, command_timeout_s=240.0)


@dataclass(frozen=True, slots=True)
class Sample:
    """The battery now: power (kW, + charging), its age (s), SoC (%) and the house's deficit (kW, for a discharge).
    For the export limit: the site's grid power (kW, + import) and its age."""

    power_kw: float | None
    age_s: float | None
    soc_pct: float | None = None
    house_deficit_kw: float | None = None
    #: The site's grid power (kW, + import): a hold is judged against it too (`monitor.judge`).
    grid_kw: float | None = None
    #: Per phase (L1, L2, L3): the site meter's power (kW, + import) and the plant's own (kW, + output), for the
    #: mode `per_phase`; `None` when a phase is not known or stale.
    grid_phase_kw: Phases | None = None
    plant_phase_kw: Phases | None = None
    #: The site's export limit is held at 0 now (`export_curtail.py`), and the battery's own floor (%).
    curtailing: bool = False
    soc_min_pct: float = 0.0


@dataclass(slots=True)
class ModeResult:
    mode: str
    action: str
    rate_kw: float = 0.0
    before_kw: float | None = None
    commanded_at: datetime | None = None
    taken_at: datetime | None = None
    outcome: str | None = None
    readings: list[dict[str, Any]] = field(default_factory=list)
    #: A hold's variant judged in this row, and every variant tried with its outcome (`[{variant, outcome}]`).
    variant: str | None = None
    variants: list[dict[str, Any]] = field(default_factory=list)
    #: The mode `per_phase`: each phase probed, with its outcome and readings (`[{phase, target_kw, outcome}]`).
    phases: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self.variant is not None:
            extra = {"variant": self.variant, "variants": [dict(row) for row in self.variants]}
        if self.mode == MODE_PER_PHASE:
            extra["phases"] = [dict(row) for row in self.phases]
        return extra | {
            "mode": self.mode,
            "action": self.action,
            "rate_kw": self.rate_kw,
            "before_kw": self.before_kw,
            "commanded_at": None if self.commanded_at is None else self.commanded_at.isoformat(),
            "taken_at": None if self.taken_at is None else self.taken_at.isoformat(),
            "outcome": self.outcome,
            "readings": list(self.readings),
        }


class Verification:
    """One run of the routine on one battery; drive it with `command` before and `after` after each executor step."""

    def __init__(
        self, executor: Executor, now: datetime, *, firmware: str | None, charge_kw: float = TEST_CHARGE_KW,
        modes: tuple[str, ...] | None = None, tried: tuple[Mapping[str, Any], ...] = (),
        timing: Timing | None = None, stop_on_fail: bool = False,
    ) -> None:
        self.executor = executor
        self.timing = timing or Timing()
        #: The hardware test's: a mode that does not pass ends the run (normal still hands back and is judged).
        self.stop_on_fail = stop_on_fail
        self.firmware = firmware or "unknown"
        self.brand = executor.control.brand
        self.started_at = now
        self.finished_at: datetime | None = None
        self.charge_kw = charge_kw
        self.index = 0
        self.stage: Stage = "command"
        self.stage_at = now
        self.last_reading_at: datetime | None = None
        #: Whether this run wrote to the plant: normal judges a hand-back only after something was written.
        self.wrote = False
        # `modes` runs only those (a later run retries the modes an earlier one could not judge); normal, the
        # hand-back's check, always ends a run.
        table = EXPORT_MODES if executor.control.kind == "export" else MODES
        self.results: list[ModeResult] = [
            ModeResult(name, action) for name, action in table
            if (modes is None and name != MODE_PER_PHASE) or (modes is not None and name in modes)
            or action == ACTION_SELF_CONSUME
        ]
        #: The mode `per_phase`: the phase probed now, and the house per phase before its probe (kW).
        self.probe_index = 0
        self.probe_house: Phases | None = None
        # The hold variants still to try: those an earlier run (`tried`, from the report's hold row) did not fail.
        failed = {str(row.get("variant")) for row in tried if mode_verdict(row.get("outcome")) == "fail"}
        self.variants: list[str] = [name for name in executor.control.variant_names() if name not in failed]
        for result in self.results:
            if result.action == ACTION_HOLD and executor.control.hold_variants:
                result.variants = [dict(row) for row in tried if str(row.get("variant")) in failed]
                result.variant = self.variants[0] if self.variants else None
        #: After a hand-back, hold runs again with the next variant (not the next mode).
        self.retry_variant = False
        #: Since when the next variant waits for the battery to discharge again (`VARIANT_WAIT_S`).
        self.variant_wait_since: datetime | None = None

    @property
    def done(self) -> bool:
        return self.stage == "done"

    @property
    def current(self) -> ModeResult | None:
        return None if self.done else self.results[self.index]

    def command(self, now: datetime, sample: Sample, rated_charge_kw: float | None) -> tuple[str, float]:
        """The action (and rate) to give the executor in this look."""
        result = self.current
        if result is None or self.stage == "hand_back":
            return ACTION_SELF_CONSUME, 0.0
        if result.commanded_at is None:
            if result.action == ACTION_HOLD and result.variant is not None and not self.executor.use_variant(result.variant):
                # Only while the executor holds nothing; the next look tries again.
                return ACTION_SELF_CONSUME, 0.0
            if result.mode == MODE_PER_PHASE:
                if not self.executor.use_phases(True):
                    if self.executor.control.phase_control is None:
                        result.outcome = "not_judgeable:not_supported"
                        self._next(now)
                    return ACTION_SELF_CONSUME, 0.0
                house = _house_kw(sample)
                unfit = "phase_reading_stale" if house is None else _probe_unfit(sample, before=True)
                if unfit is not None:
                    # Nothing could be judged soundly now: nothing is written.
                    result.outcome = f"not_judgeable:{unfit}"
                    self._next(now)
                    return ACTION_SELF_CONSUME, 0.0
                # Every target at 0 first (`probe_index` -1): the house per phase is read under that.
                self.probe_index, self.probe_house = -1, None
            if self.variant_wait_since is not None:
                if sample.power_kw is None or sample.power_kw > -FLOOR_KW:
                    if (now - self.variant_wait_since).total_seconds() < VARIANT_WAIT_S:
                        return ACTION_SELF_CONSUME, 0.0
                    # Nothing to stop now: this variant is tried at a later run.
                    self.variant_wait_since = None
                    result.outcome = "not_judgeable:battery_not_discharging"
                    self._next(now)
                    return ACTION_SELF_CONSUME, 0.0
                self.variant_wait_since = None
            result.commanded_at = now
            result.before_kw = sample.power_kw
            if result.action == ACTION_GRID_CHARGE:
                result.rate_kw = min(self.charge_kw, rated_charge_kw) if rated_charge_kw else self.charge_kw
        return result.action, result.rate_kw

    def probe(self) -> Phases | None:
        """The charge per phase to command in this look for the mode `per_phase` (kW, negative: the discharge on the
        phase probed now), `None` in every other mode and once the mode is handing back."""
        result = self.current
        if result is None or result.mode != MODE_PER_PHASE or self.stage == "hand_back" or result.commanded_at is None:
            return None
        values = [0.0, 0.0, 0.0]
        if self.probe_index >= 0:
            values[self.probe_index] = -PROBE_KW
        return (values[0], values[1], values[2])

    def not_judgeable(self, now: datetime, why: str) -> None:
        """The running mode cannot be tried now for a reason of the shell's (the grid charge cap): it is left pending
        and the run goes on once the executor has handed back."""
        result = self.current
        if result is None or self.stage == "hand_back" or result.action == ACTION_SELF_CONSUME:
            return
        result.outcome = f"not_judgeable:{why}"
        self._enter(now, "hand_back")

    def after(self, now: datetime, step: StepResult, sample: Sample, capability: Capability) -> None:
        """What the executor did in this look, and the battery's reading."""
        result = self.current
        if result is None:
            return
        state = step.state
        if result.action != ACTION_SELF_CONSUME and (step.ops or state.phase in ("active", "restoring")):
            self.wrote = True
        if result.action != ACTION_SELF_CONSUME and self.stage != "hand_back":
            hand_back = next((event for event in step.events if event["kind"] == "hand_back"), None)
            not_started = next((event for event in step.events if event["kind"] == "not_started"), None)
            if hand_back is not None and str(hand_back["reason"]).startswith("no_effect"):
                # The executor's own monitor saw no effect first: that is this mode's answer, not a reason to stop.
                result.outcome = "ignored"
                self._variant_failed(now, result)
                self._enter(now, "hand_back")
                return
            if hand_back is not None or (not_started is not None and state.phase != "active"):
                why = (hand_back or not_started or {}).get("reason", "unknown")
                self._abort(now, f"aborted:{why}")
                return
        if self.stage == "command":
            if result.action == ACTION_SELF_CONSUME:
                if state.phase in ("idle", "blocked") and not state.holds_snapshot and not step.ops:
                    result.taken_at = now
                    if not self.wrote:
                        # Nothing of this run's was handed back: normal is judged at a later run.
                        result.outcome = "not_judgeable:nothing_written"
                    else:
                        result.outcome = "restored" if state.phase == "idle" else f"aborted:{state.blocked}"
                    self._finish(now)
                return
            taken = state.phase == "active" and state.action == result.action and state.pending is None and not step.ops
            if taken and result.mode == MODE_PER_PHASE and state.phase_kw != self.probe():
                taken = False
            if taken:
                result.taken_at = now
                self._enter(now, "settle")
            elif (
                result.commanded_at is not None
                and (now - result.commanded_at).total_seconds() >= self.timing.command_timeout_s
            ):
                self._abort(now, "aborted:command_timeout")
            return
        if self.stage == "settle":
            settle = PHASE_SETTLE_S if result.mode == MODE_PER_PHASE else self.timing.settle_s
            if (now - self.stage_at).total_seconds() >= settle:
                self._enter(now, "read")
            else:
                return
        if self.stage == "read":
            if result.mode == MODE_PER_PHASE:
                self._read_phase(now, result, sample)
            else:
                self._read(now, result, sample, capability)
            return
        if self.stage == "hand_back" and state.phase in ("idle", "blocked") and not state.holds_snapshot and not step.ops:
            if state.phase == "blocked":
                if not str(state.blocked).startswith("no_effect"):
                    self.retry_variant = False
                    self._abort(now, f"aborted:{state.blocked}")
                    return
                # The block is the routine's own finding; it is lifted to try the next mode (or variant).
                self.executor.reset()
            if self.retry_variant:
                self.retry_variant = False
                self.variant_wait_since = now
                self._enter(now, "command")
                return
            self._next(now)

    def _read(self, now: datetime, result: ModeResult, sample: Sample, capability: Capability) -> None:
        if self.last_reading_at is not None and (now - self.last_reading_at).total_seconds() < self.timing.reading_gap_s:
            return
        self.last_reading_at = now
        # The export limit is judged as a hold, on the grid's power: what left the site must stop.
        kind = {ACTION_HOLD: "hold", ACTION_GRID_CHARGE: "charge", ACTION_DISCHARGE_HOUSE: "discharge",
                ACTION_CURTAIL_EXPORT: "hold"}[result.action]
        rate = result.rate_kw if kind == "charge" else (sample.house_deficit_kw or 0.0)
        idle = result.action == ACTION_HOLD and self.executor.control.hold_idle()
        command = Command(  # type: ignore[arg-type]
            kind, result.commanded_at or now, rate, before_kw=result.before_kw, written=True, idle=idle
        )
        why: str | None = None
        expected: float | None = None
        if sample.power_kw is None or sample.age_s is None or sample.age_s > READING_MAX_AGE_S:
            why = "reading_stale"
        elif result.action == ACTION_CURTAIL_EXPORT:
            if result.before_kw is None or result.before_kw > -FLOOR_KW:
                why = "not_exporting"
            else:
                expected = 0.0
        elif kind == "hold" and (result.before_kw is None or result.before_kw > -FLOOR_KW):
            why = "battery_not_discharging"
        elif kind == "discharge" and sample.house_deficit_kw is None:
            why = "house_deficit_unknown"
        else:
            cap = capability if kind != "discharge" else Capability(
                capability.rated_charge_kw, capability.rated_discharge_kw, capability.soc_min_pct,
                capability.soc_max_pct, sample.house_deficit_kw,
            )
            expected, why = deliverable_kw(command, cap, sample.soc_pct)
        if why is not None:
            result.outcome = f"not_judgeable:{why}"
            self._enter(now, "hand_back")
            return
        assert expected is not None and sample.power_kw is not None
        grid = sample.grid_kw if kind == "hold" and result.action == ACTION_HOLD else None
        verdict = judge(command, expected, sample.power_kw, grid, sample.soc_pct)
        result.readings.append(
            {"at": now.isoformat(), "power_kw": round(sample.power_kw, 3), "soc_pct": sample.soc_pct,
             "expected_kw": round(expected, 3), "verdict": verdict}
        )
        if len(result.readings) < self.timing.readings:
            return
        verdicts = {row["verdict"] for row in result.readings}
        result.outcome = "obeyed" if verdicts == {"ok"} else "ignored" if verdicts == {"no_effect"} else "partial"
        self._variant_failed(now, result)
        self._enter(now, "hand_back")

    def _read_phase(self, now: datetime, result: ModeResult, sample: Sample) -> None:
        """One reading of the probe on one phase (see the module): the plant's power on it at the target and on the
        others at nothing, and the house on it where it was."""
        if self.last_reading_at is not None and (now - self.last_reading_at).total_seconds() < PHASE_READING_GAP_S:
            return
        self.last_reading_at = now
        index = self.probe_index
        plant, grid, before = sample.plant_phase_kw, sample.grid_phase_kw, self.probe_house
        house = _house_kw(sample)
        unfit = _probe_unfit(sample, before=False)
        if plant is None or grid is None or house is None or (before is None and index >= 0):
            unfit = "phase_reading_stale"
        if unfit is not None:
            result.outcome = f"not_judgeable:{unfit}"
            self._enter(now, "hand_back")
            return
        assert plant is not None and grid is not None and house is not None
        if index < 0:
            # Every target at 0: the plant must put out nothing, and the house per phase is what the meter reads.
            if max(abs(value) for value in plant) > PROBE_POWER_TOLERANCE_KW:
                result.phases.append({"phase": None, "target_kw": 0.0, "outcome": "ignored",
                                      "readings": [{"at": now.isoformat(), "plant_kw": [round(v, 3) for v in plant]}]})
                result.outcome = "ignored"
                self._enter(now, "hand_back")
                return
            self.probe_index, self.probe_house = 0, house
            result.commanded_at = now
            self._enter(now, "command")
            return
        assert before is not None
        others = [abs(plant[other]) for other in range(3) if other != index]
        acts = abs(plant[index] - PROBE_KW) <= PROBE_POWER_TOLERANCE_KW and max(others) <= PROBE_POWER_TOLERANCE_KW
        moved = house[index] - before[index]
        if not acts:
            verdict = "no_effect" if plant[index] < FAIL_FRACTION * PROBE_KW else "partial"
        else:
            verdict = "ok" if abs(moved) <= PROBE_HOUSE_TOLERANCE_KW else "wrong_phase"
        result.readings.append({
            "at": now.isoformat(), "phase": PHASE_NAMES[index], "plant_kw": [round(value, 3) for value in plant],
            "grid_kw": [round(value, 3) for value in grid], "house_moved_kw": round(moved, 3), "verdict": verdict,
        })
        if len(result.readings) < READINGS:
            return
        verdicts = {row["verdict"] for row in result.readings}
        if verdicts == {"ok"}:
            outcome = "obeyed"
        elif verdicts == {"no_effect"}:
            outcome = "ignored"
        elif verdicts == {"wrong_phase"}:
            outcome = "wrong_phase"
        elif "wrong_phase" in verdicts and verdicts <= {"ok", "wrong_phase"}:
            outcome = "not_judgeable:house_changed"
        else:
            outcome = "partial"
        result.phases.append({
            "phase": PHASE_NAMES[index], "target_kw": -PROBE_KW, "outcome": outcome, "readings": list(result.readings),
        })
        result.readings = []
        if outcome == "obeyed" and index < 2:
            # The next phase, from the house as it reads under this one's probe (just judged right).
            self.probe_index, self.probe_house = index + 1, house
            result.commanded_at = now  # the next phase's own time to be taken (`COMMAND_TIMEOUT_S`)
            self._enter(now, "command")
            return
        result.outcome = "partial" if outcome == "wrong_phase" else outcome
        self._enter(now, "hand_back")

    def _variant_failed(self, now: datetime, result: ModeResult) -> bool:
        """A hold variant that failed (`VARIANT_RETRY`) is recorded; with another left, hold is tried again with it
        once the battery is handed back. Whether it will be."""
        outcome = result.outcome or ""
        if (
            result.action != ACTION_HOLD or result.variant is None or not outcome.startswith(VARIANT_RETRY)
            or self.stop_on_fail
        ):
            if result.variant is not None and result.outcome is not None:
                result.variants.append({"variant": result.variant, "outcome": result.outcome})
            return False
        result.variants.append({"variant": result.variant, "outcome": outcome})
        if result.variant in self.variants:
            self.variants.remove(result.variant)
        if not self.variants:
            return False
        result.variant = self.variants[0]
        result.commanded_at = result.taken_at = None
        result.before_kw = None
        result.outcome = None
        result.readings = []
        self.retry_variant = True
        return True

    def _enter(self, now: datetime, stage: Stage) -> None:
        self.stage = stage
        self.stage_at = now
        self.last_reading_at = None

    def _abort(self, now: datetime, outcome: str) -> None:
        result = self.current
        if result is not None:
            result.outcome = outcome
            if self._variant_failed(now, result):
                # Handed back (the executor's own hand-back, or the routine's normal), then the next variant.
                self._enter(now, "hand_back")
                return
        # The executor hands the battery back by itself; no further mode is commanded. An abort for the moment's
        # circumstances (`gate.mode_verdict` pending: the fuse, a stale reading) still lets normal judge that
        # hand-back; one that shows the plant does not do as told ends the run.
        moment = result is not None and result.action != ACTION_SELF_CONSUME and mode_verdict(outcome) == "pending"
        normal = len(self.results) - 1
        for later in self.results[self.index + 1:]:
            if not (moment and later is self.results[normal] and later.action == ACTION_SELF_CONSUME):
                later.outcome = "skipped"
        if moment and self.results[normal].action == ACTION_SELF_CONSUME and self.index < normal:
            self.index = normal
            self._enter(now, "command")
            return
        self._finish(now)

    def _next(self, now: datetime) -> None:
        result = self.current
        if self.stop_on_fail and result is not None and result.action != ACTION_SELF_CONSUME and not _passes(result):
            # The hardware test: the first mode that did not pass skips the rest; normal judges the hand-back.
            normal = len(self.results) - 1
            for later in self.results[self.index + 1:normal]:
                later.outcome = "skipped"
            self.index = normal
            self._enter(now, "command")
            return
        self.index += 1
        if self.index >= len(self.results):
            self._finish(now)
            return
        self._enter(now, "command")

    def _finish(self, now: datetime) -> None:
        self.stage = "done"
        self.finished_at = now

    def report(self) -> dict[str, Any]:
        return {
            "brand": self.brand,
            "firmware": self.firmware,
            "started_at": self.started_at.isoformat(),
            "finished_at": None if self.finished_at is None else self.finished_at.isoformat(),
            "modes": {result.mode: result.as_dict() for result in self.results},
        }


def _passes(result: ModeResult) -> bool:
    """Whether a mode's outcome lets the hardware test go on: obeyed, or not judgeable at this moment."""
    outcome = result.outcome or ""
    return outcome == "obeyed" or outcome.startswith("not_judgeable:")


def _probe_unfit(sample: Sample, *, before: bool) -> str | None:
    """Why the per-phase probe could not be judged soundly at this moment, `None` when it can (see the module).
    `before`: the plant is still in its own mode, where a sun surplus shows as export or as the battery charging."""
    if sample.curtailing:
        return "export_curtailed"
    soc = sample.soc_pct
    if soc is None or soc >= CHARGE_TAPER_SOC_PCT or soc <= sample.soc_min_pct + DISCHARGE_MARGIN_PCT:
        return "soc_near_end"
    if before and (
        (sample.grid_kw is not None and sample.grid_kw < -FLOOR_KW)
        or (sample.power_kw is not None and sample.power_kw > FLOOR_KW)
    ):
        return "sun_surplus"
    return None


def _house_kw(sample: Sample) -> Phases | None:
    """The house per phase (kW): what the meter imports on it plus what the plant puts out on it."""
    grid, plant = sample.grid_phase_kw, sample.plant_phase_kw
    if grid is None or plant is None:
        return None
    return (grid[0] + plant[0], grid[1] + plant[1], grid[2] + plant[2])


def keep_report(reports: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    """The reports by firmware string with this one added (the newest replaces an older one of the same firmware),
    at most `MAX_REPORTS`, the oldest dropped first."""
    kept = {key: value for key, value in reports.items() if key != report["firmware"] and isinstance(value, Mapping)}
    kept[str(report["firmware"])] = dict(report)
    ordered = sorted(kept.items(), key=lambda item: str(item[1].get("finished_at") or item[1].get("started_at") or ""))
    return dict(ordered[-MAX_REPORTS:])
