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
brand's first variant; when that fails (`VARIANT_RETRY`: ignored, partial, or an abort that shows the plant did not do
as told, such as a hold that charges from the grid) the battery is handed back, the block that finding left is lifted,
and hold is tried again with the next variant, never two at once. The hold row records the variant judged
(`variant`) and every one tried (`variants`); a later run starts after the variants that failed, and control runs the
one that passed (`gate.hold_variant`). A hold reading is judged with the site's grid power too (`monitor.judge`): no
discharge while the house draws, no grid charge, and the sun still stored.

The export limit (`commands.EXPORT_CONTROLS`) has its own two modes (`EXPORT_MODES`): `curtail` (the limit at 0 kW
through the executor's export instance) and `normal` (handed back). Its sample's power is the site's grid power (kW,
+ import), judged as a hold judges the battery's: the export must stop. It can be judged only while the site exports
(`not_judgeable:not_exporting` otherwise).

Pure: no Home Assistant import; times are aware datetimes from the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from .commands import ACTION_CURTAIL_EXPORT
from .executor import Executor, StepResult
from .gate import mode_verdict
from .monitor import FLOOR_KW, Capability, Command, deliverable_kw, judge
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
#: Hold outcomes after which the next hold variant is tried: the plant did not do as this variant told it. Not a
#: change by someone else, a restore that failed or a control that is missing: those end the run as before.
VARIANT_RETRY: Final = (
    "ignored", "partial", "aborted:no_effect", "aborted:not_obeyed", "aborted:write_not_taken",
    "aborted:command_timeout",
)
#: Blocks the executor is left with after such a finding, lifted to try the next variant.
_VARIANT_BLOCKS: Final = ("no_effect", "not_obeyed", "write_not_taken")

#: The modes, in order: (name, the plan action commanded).
MODES: Final[tuple[tuple[str, str], ...]] = (
    ("hold", ACTION_HOLD),
    ("charge", ACTION_GRID_CHARGE),
    ("discharge_house", ACTION_DISCHARGE_HOUSE),
    ("normal", ACTION_SELF_CONSUME),
)

#: The export limit's modes: the export stopped, then handed back.
EXPORT_MODES: Final[tuple[tuple[str, str], ...]] = (
    ("curtail", ACTION_CURTAIL_EXPORT),
    ("normal", ACTION_SELF_CONSUME),
)

Stage = Literal["command", "settle", "read", "hand_back", "done"]


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

    def as_dict(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self.variant is not None:
            extra = {"variant": self.variant, "variants": [dict(row) for row in self.variants]}
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
    ) -> None:
        self.executor = executor
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
            if modes is None or name in modes or action == ACTION_SELF_CONSUME
        ]
        # The hold variants still to try: those an earlier run (`tried`, from the report's hold row) did not fail.
        failed = {str(row.get("variant")) for row in tried if mode_verdict(row.get("outcome")) == "fail"}
        self.variants: list[str] = [name for name in executor.control.variant_names() if name not in failed]
        for result in self.results:
            if result.action == ACTION_HOLD and executor.control.hold_variants:
                result.variants = [dict(row) for row in tried if str(row.get("variant")) in failed]
                result.variant = self.variants[0] if self.variants else None
        #: After a hand-back, hold runs again with the next variant (not the next mode).
        self.retry_variant = False

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
            result.commanded_at = now
            result.before_kw = sample.power_kw
            if result.action == ACTION_GRID_CHARGE:
                result.rate_kw = min(self.charge_kw, rated_charge_kw) if rated_charge_kw else self.charge_kw
        return result.action, result.rate_kw

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
            if state.phase == "active" and state.action == result.action and state.pending is None and not step.ops:
                result.taken_at = now
                self._enter(now, "settle")
            elif result.commanded_at is not None and (now - result.commanded_at).total_seconds() >= COMMAND_TIMEOUT_S:
                self._abort(now, "aborted:command_timeout")
            return
        if self.stage == "settle":
            if (now - self.stage_at).total_seconds() >= SETTLE_S:
                self._enter(now, "read")
            else:
                return
        if self.stage == "read":
            self._read(now, result, sample, capability)
            return
        if self.stage == "hand_back" and state.phase in ("idle", "blocked") and not state.holds_snapshot and not step.ops:
            if state.phase == "blocked":
                lifted = _VARIANT_BLOCKS if self.retry_variant else ("no_effect",)
                if not str(state.blocked).startswith(lifted):
                    self.retry_variant = False
                    self._abort(now, f"aborted:{state.blocked}")
                    return
                # The block is the routine's own finding; it is lifted to try the next mode (or variant).
                self.executor.reset()
            if self.retry_variant:
                self.retry_variant = False
                self._enter(now, "command")
                return
            self._next(now)

    def _read(self, now: datetime, result: ModeResult, sample: Sample, capability: Capability) -> None:
        if self.last_reading_at is not None and (now - self.last_reading_at).total_seconds() < READING_GAP_S:
            return
        self.last_reading_at = now
        # The export limit is judged as a hold, on the grid's power: what left the site must stop.
        kind = {ACTION_HOLD: "hold", ACTION_GRID_CHARGE: "charge", ACTION_DISCHARGE_HOUSE: "discharge",
                ACTION_CURTAIL_EXPORT: "hold"}[result.action]
        rate = result.rate_kw if kind == "charge" else (sample.house_deficit_kw or 0.0)
        command = Command(kind, result.commanded_at or now, rate, before_kw=result.before_kw, written=True)  # type: ignore[arg-type]
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
        if len(result.readings) < READINGS:
            return
        verdicts = {row["verdict"] for row in result.readings}
        result.outcome = "obeyed" if verdicts == {"ok"} else "ignored" if verdicts == {"no_effect"} else "partial"
        self._variant_failed(now, result)
        self._enter(now, "hand_back")

    def _variant_failed(self, now: datetime, result: ModeResult) -> bool:
        """A hold variant that failed (`VARIANT_RETRY`) is recorded; with another left, hold is tried again with it
        once the battery is handed back. Whether it will be."""
        outcome = result.outcome or ""
        if result.action != ACTION_HOLD or result.variant is None or not outcome.startswith(VARIANT_RETRY):
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


def keep_report(reports: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    """The reports by firmware string with this one added (the newest replaces an older one of the same firmware),
    at most `MAX_REPORTS`, the oldest dropped first."""
    kept = {key: value for key, value in reports.items() if key != report["firmware"] and isinstance(value, Mapping)}
    kept[str(report["firmware"])] = dict(report)
    ordered = sorted(kept.items(), key=lambda item: str(item[1].get("finished_at") or item[1].get("started_at") or ""))
    return dict(ordered[-MAX_REPORTS:])
