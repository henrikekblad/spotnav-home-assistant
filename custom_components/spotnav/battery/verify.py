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

**Hold variants** (`commands.HoldVariant`; no shipping brand has more than one since Sigenergy's hold became `pv_first`
only, 2026-11-23): hold is tried with the
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
negated. The house per phase with every target at 0 is read after the zeros were read back and `ZERO_SETTLE_S` at
least (`Timing.zero_settle_s`). After `PHASE_SETTLE_S` (a target settles in seconds; the hardware test's `Timing.phase_settle_s`)
each of `READINGS` readings `PHASE_READING_GAP_S` apart (the hardware test's fewer and closer ones) must show the plant's own power on that phase at the target and about nothing on the other two (the sign and
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
A plant whose power on the probed phase goes the other way than the target says, in every reading, has its sign
wrong: the phase's row says `wrong_sign` and the mode is `partial`.

**The charge per phase** (`Verification.charge_per_phase`). Where the balanced grid charge has no room under its
HA-down cap (an unbalanced house: the worst phase is full, `gate.grid_charge_cap_kw`) and the plant takes targets
per phase, the mode `charge` is tried as a charge on one phase instead (`phase_charge_step`): on the phase whose own
HA-down cap (`gate.grid_charge_phase_caps_kw`) is the largest, at most `PROBE_KW` and at most that cap, in whole
amps, the other two at 0. Unless the mode `per_phase` has passed on this firmware, a discharge on that phase
comes first, judged as above: a charge on a plant phase that is another meter phase could land on the full one and
stay there if Home Assistant stops, so the charge is commanded only once that phase's sign and mapping are shown,
by a step that cannot charge. It runs as the probe above does (every target at 0 first, then the charge; the same
readings, the plant's power on that phase at the target's charge and the house on it unchanged) through the same
executor, where every fuse rule acts on it as on any charge per phase: the phase's live room, down at once, up only
after the dwell, the runaway. It is judged on what the executor commands on that phase as it reads (a target it
lowered to the phase's room is the one judged; one it keeps at nothing once it could raise it is
`not_judgeable:fuse_headroom`, as is one it lowers under `PHASE_CHARGE_MIN_KW`, where a plant doing nothing would
read as one at the target). A runaway on a phased mode's target (the executor's `phase_over`) is that mode's
answer: `partial`, the executor's block lifted after the hand-back. Its row says so (`via: per_phase`, its `phases` row with `sign` and `mapping`), and
what it shows of the sign and the mapping is the capability's too (`gate.per_phase`): a charge passed this way
allows a grid charge only as a charge per phase, once the mode `per_phase` passed as well (`gate.allowed_actions`,
`gate.charge_by_phases`). No phase with room either: `not_judgeable:no_phase_room`.

**The expiry** (the mode `expiry`, only where a run names it: `control.BatteryControl._expiry_due`). Does a
command's own expiry (`commands.Expiry`) end it when nothing renews it? Tried only for an expiry whose pass would lift
the HA-down cap (`gate.expiry_testable`: a dead-man or lease SpotNav renews, within `gate.SILENCE_FALLBACK_MAX_S`),
not yet measured on this firmware, after a hold that passed in the same run. Its command is that hold for the
recipe's own expiry, or for the phase spec's (Victron) the per-phase probe's **discharge** on one phase (`PROBE_KW`,
the others at 0), once `per_phase` passed: never a charge. Its fallback does add import, the probe's own discharge
back on that phase, so the silence starts and goes on only with the headroom at least `HEADROOM_MARGIN_A` plus the
probe's amperes (unknown headroom: none). Once the command is taken, settled (`Timing.phase_settle_s`), seen obeyed
and nothing more is owed to it, the executor stops renewing it (`ExecInputs.expiry_probe`: no renewal, no change; a
renewed role that reads the maker's documented fallback is the device's own, any other change of it is another
writer and ends the test unjudged). Every look then samples the plant. A fallback is the probed phase's plant power
at about 0 **and**, for a target that is the grid's power at a point, that power off the value last written, for
`READINGS` looks in a row (one look is a glitch). Each phase of a phase spec's is proven on its own (its row
`expiry:L1`..`expiry:L3`, one phase a run, `control.BatteryControl._expiry_to_test`): the HA-down cap is lifted only on
the phases proven (owner decision 2026-11-23), with the house on that phase (the whole site's for a hold) where
it was at the silence's start: a load that moves on it reads exactly like a fallback, and is
`not_judgeable:house_changed`, never a pass. The fallback is timed from the last write of a renewed role: `obeyed`
after one look before the window up to one look after it (the gate takes it only within
`gate.SILENCE_FALLBACK_MAX_S` and one look, and only once two separate runs on the firmware passed,
`gate.EXPIRY_PASSES`, `gate.expiry_measured`; the fallback reads so on each of the looks up to the hand-back), `partial:early` sooner than the renewal (the recipe is wrong),
`not_judgeable:early_fallback` sooner than that window and `not_judgeable:late_look` one look past it (both tried
again at a later run, `Verification._expiry_decide`), `ignored` later or not by
`expiry_deadline_s` (the window plus `max(15 s, half the window)` from the last write; the silence never runs past
`EXPIRY_TEST_MAX_S`), `not_judgeable:*` (tried again at a later run) or `aborted:*` (near the fuse or a peak cover
due: `fuse_headroom`; the battery taking from the grid: `charge_in_silence`; any hand-back of the executor's own). The battery is then handed back at once. Its row never decides the report (`gate.allowed_actions`
and `gate.report_status` ignore it); only a pass changes anything. The routine never tries a dead-man an
integration renews; a person's hardware test does where SpotNav can silence it with a write
(`gate.deadman_testable`, `Expiry.silence`: EcoFlow's Modbus Control off, which stops the integration's heartbeat
and writes nothing else). The silence is then that write (`executor.Executor.silenced`), the fallback is timed from
it (the integration's last renewal came up to one of its intervals before, so any fallback within the window and a
look is `obeyed`, later `ignored`), and after the outcome the plant is taken back as the inverter's own
self-consumption (`_retake`: the discharge to the house, the integration renewing again) before the hand-back, so a
dead-man that did not act leaves no command standing. That row is the tester's evidence; it never lifts a cap.
A person's hardware test also times a dead-man SpotNav renews whose window is longer than any the gate takes
(`expiry_long`, Solis's 5-minute RC Timeout; `BatteryControl._hardware_test_deadman`): the silence starts only once one
renewal followed the command's own write, the fallback is timed from that renewal (`obeyed` from one look before the
window up to `EXPIRY_LONG_SLACK_S` and a look after it, a timer in whole minutes; sooner than the window but after the
renewal interval `partial:not_renewed`: the renewal did not restart it), and the silence runs at most
`expiry_max_s`. It too never lifts a cap.

The export limit (`commands.EXPORT_CONTROLS`) has its own two modes (`EXPORT_MODES`): `curtail` (the limit at 0 kW
through the executor's export instance) and `normal` (handed back). Its sample's power is the site's grid power (kW,
+ import), judged as a hold judges the battery's: the export must stop. It can be judged only while the site exports
(`not_judgeable:not_exporting` otherwise).

**The hardware test** (`Timing.quick`, `stop_on_fail`; `control.BatteryControl.async_hardware_test`): the same
modes through the same executor, started by a person and bounded to minutes: a shorter settle and fewer, closer
readings, the hold's first variant that has not failed only, and the first mode that does not pass (an outcome
other than `obeyed` or `not_judgeable:*`) skips the rest but normal, the hand-back's check. Its report is the
test's own: it never counts towards the routine's per firmware (`gate.merge_report`).

Its charge step is tried up to `TEST_CHARGE_SOC_MAX_PCT` (98 %) of charge (`Verification.charge_soc_max_pct`), not
only up to `CHARGE_TAPER_SOC_PCT` (90 %) as everywhere else: a battery that sits near full most days would otherwise
never show the step. This is about judging, not safety: the step is about 1 kW for minutes, under the same HA-down
caps and the executor's live fuse rules at any charge level, and a full battery only takes less. So from
`CHARGE_TAPER_SOC_PCT` on, a charge step the plant takes less of than commanded (`ignored` or `partial` by its
readings) is `not_judgeable:battery_full`, never a failure; one it takes in full is `obeyed`. Normal control, the
monitor and the routine keep 90 %.

Pure: no Home Assistant import; times are aware datetimes from the caller.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from .commands import ACTION_CURTAIL_EXPORT, WATCHDOG_RENEW_S, BrandControl, Expiry, Phases
from .executor import HEADROOM_MARGIN_A, RATE_UP_DWELL_S, REARM_S, Executor, StepResult
from .ledger import READBACK_DEADLINE_S
from ..core.electric import TN_VOLTS_BETWEEN_PHASES, phase_volts
from .gate import (
    EXPIRY_LOOK_S,
    MODE_EXPIRY,
    MODE_PER_PHASE,
    PHASE_NAMES,
    SILENCE_FALLBACK_MAX_S,
    expiry_mode,
    mode_verdict,
)
from .monitor import (
    CHARGE_TAPER_SOC_PCT,
    DISCHARGE_MARGIN_PCT,
    FAIL_FRACTION,
    FLOOR_KW,
    READING_GAP_S,
    READING_MAX_AGE_S,
    Capability,
    Command,
    deliverable_kw,
    judge,
)
from .plan_model import ACTION_DISCHARGE_HOUSE, ACTION_GRID_CHARGE, ACTION_HOLD, ACTION_SELF_CONSUME

#: How long a mode settles after its writes are taken before readings count (s), as the monitor's grace.
SETTLE_S: Final = 300.0
READINGS: Final = 3
#: The charge power tried (kW), at most the battery's own rating.
TEST_CHARGE_KW: Final = 2.0
#: The state of charge up to which the hardware test's charge step is tried (%), where everything else stops judging
#: a charge at `CHARGE_TAPER_SOC_PCT` (see the module, "The hardware test").
TEST_CHARGE_SOC_MAX_PCT: Final = 98.0
#: A mode whose writes are not taken within this long is aborted (s).
COMMAND_TIMEOUT_S: Final = 300.0
#: Reports kept, one per firmware string (the control imports it for its own, one per firmware key).
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
#: The least settle of the step with every target at 0, also in the hardware test (s): the house per phase the
#: mapping is judged against is read only once a battery that was charging or covering just before has stopped and
#: the meter's phase readings have caught up with the plant's.
ZERO_SETTLE_S: Final = 10.0
PROBE_POWER_TOLERANCE_KW: Final = 0.3
PROBE_HOUSE_TOLERANCE_KW: Final = 0.4
#: The least charge on one phase the mode `charge` tries per phase (kW): below it a reading at the target cannot be
#: told from one at nothing or one of the wrong sign (`PROBE_POWER_TOLERANCE_KW`).
PHASE_CHARGE_MIN_KW: Final = 0.5
#: What the mode `charge` tried per phase records in its row (`ModeResult.via`).
VIA_PER_PHASE: Final = "per_phase"
#: The mode `expiry`: the longest its silence may run (s), whatever the window and the margin.
EXPIRY_TEST_MAX_S: Final = 120.0
#: The least margin past the window a fallback may take (s); half the window where that is more.
EXPIRY_MARGIN_MIN_S: Final = 15.0


def expiry_margin_s(window_s: float) -> float:
    """How long past its window a fallback still counts as the expiry acting (s): `max(15 s, half the window)`."""
    return max(EXPIRY_MARGIN_MIN_S, 0.5 * window_s)


#: A dead-man longer than `SILENCE_FALLBACK_MAX_S` (only a hardware test times one: it lifts no cap, the test is the
#: tester's evidence; Solis's RC Timeout in whole minutes): how late past its window a fallback still counts (s), one
#: minute tick of a timer in whole minutes and a look.
EXPIRY_LONG_SLACK_S: Final = 60.0


def expiry_long(window_s: float) -> bool:
    """Whether a window is longer than any the gate takes (`SILENCE_FALLBACK_MAX_S`): timed as such (see the module)."""
    return window_s > SILENCE_FALLBACK_MAX_S


def expiry_max_s(window_s: float) -> float:
    """The longest the mode `expiry`'s silence runs (s): `EXPIRY_TEST_MAX_S`, or for a long window that window with its
    slack and two looks."""
    if not expiry_long(window_s):
        return EXPIRY_TEST_MAX_S
    return window_s + EXPIRY_LONG_SLACK_S + 3 * EXPIRY_LOOK_S


def expiry_deadline_s(window_s: float) -> float:
    """How long the mode `expiry` stays silent at most (s): the window and its margin, at most `expiry_max_s`."""
    if expiry_long(window_s):
        return window_s + EXPIRY_LONG_SLACK_S + 2 * EXPIRY_LOOK_S
    return min(window_s + expiry_margin_s(window_s), EXPIRY_TEST_MAX_S)

#: The modes, in order: (name, the plan action commanded). `per_phase` and `expiry` run only when a run names them
#: (`expiry` commands a hold, or on a phased control the probe's discharge on one phase).
MODES: Final[tuple[tuple[str, str], ...]] = (
    ("hold", ACTION_HOLD),
    ("charge", ACTION_GRID_CHARGE),
    ("discharge_house", ACTION_DISCHARGE_HOUSE),
    (MODE_PER_PHASE, ACTION_GRID_CHARGE),
    (MODE_EXPIRY, ACTION_HOLD),
    ("normal", ACTION_SELF_CONSUME),
)
#: Modes a run without a list of its own (the hardware test) leaves out.
_NAMED_ONLY: Final = frozenset({MODE_PER_PHASE, MODE_EXPIRY})

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
    #: A phased mode's step (a target per phase settles in seconds).
    phase_settle_s: float = PHASE_SETTLE_S
    #: The step with every target at 0, before the house per phase is read (never under `ZERO_SETTLE_S`).
    zero_settle_s: float = PHASE_SETTLE_S

    @classmethod
    def quick(cls) -> Timing:
        """The hardware test's: hold, charge, discharge and the hand-back in about nine minutes. A mode after a
        hand-back waits for the executor's re-arm (`executor.REARM_S`, never shortened) before its writes are
        taken, so the time to take them stays generous; the settle and the readings are what is shortened (the
        settle to 10 s, a look, which pays for the zeros' settle within the ten minutes). A target
        per phase is taken only once read back, a look after it was written, and settles in seconds: its readings
        start at once, except the house per phase with every target at 0, read after `ZERO_SETTLE_S`."""
        return cls(
            settle_s=10.0, readings=2, reading_gap_s=10.0, command_timeout_s=240.0, phase_settle_s=0.0,
            zero_settle_s=ZERO_SETTLE_S,
        )


#: A slow recipe's hardware test (`quick_for`): the settle once a command is taken (s, one cloud poll of the battery's
#: power), and the room past its re-arm and read-backs for the looks themselves (s).
SLOW_TEST_SETTLE_S: Final = 60.0
SLOW_TEST_SLACK_S: Final = 120.0


def quick_for(control: BrandControl) -> Timing:
    """The hardware test's timing for a recipe: `Timing.quick`, and for a slow one (`BrandControl.slow`, Tesla's cloud)
    a command's wait long enough for what the executor itself waits before it is taken: the re-arm after the previous
    mode's hand-back (`BrandControl.rearm_s`), then each role written and read back in turn at its longest wait
    (`BrandControl.readback_s`), and a cloud poll of settle. Every other recipe's timing is `Timing.quick`'s."""
    quick = Timing.quick()
    if not control.slow:
        return quick
    rearm = REARM_S if control.rearm_s is None else max(REARM_S, control.rearm_s)
    readback = control.readback_s or READBACK_DEADLINE_S
    command_s = rearm + len(control.roles) * readback + SLOW_TEST_SLACK_S
    return Timing(
        settle_s=SLOW_TEST_SETTLE_S, readings=quick.readings, reading_gap_s=quick.reading_gap_s,
        command_timeout_s=max(quick.command_timeout_s, command_s), phase_settle_s=quick.phase_settle_s,
        zero_settle_s=quick.zero_settle_s,
    )


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
    #: The least headroom on any phase now (A), `None` when unknown (the mode `expiry` aborts near the fuse).
    headroom_a: float | None = None
    #: For a recipe whose targets are the grid's power at a point: that power on L1, L2, L3 now (kW, + from the
    #: grid), `None` when not read; and the site's voltage between phases.
    phase_point_kw: Phases | None = None
    volts_between_phases: float = TN_VOLTS_BETWEEN_PHASES


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
    #: The mode `per_phase`, and `charge` tried per phase: each phase probed, with its outcome, what it showed of
    #: the sign and the mapping, and its readings (`[{phase, target_kw, outcome, sign, mapping, readings}]`).
    phases: list[dict[str, Any]] = field(default_factory=list)
    #: `charge` tried as a charge on one phase (`VIA_PER_PHASE`), `None` for the brand's own balanced charge.
    via: str | None = None
    #: The mode `expiry`: the expiry tried (`Expiry.expiry_id`), its window (s), `hold` or `phase` (`scope`), when the
    #: plant left the command after the last write (s; `None`: it did not) and the renewed roles read as expired.
    expiry_id: str | None = None
    window_s: float | None = None
    scope: str | None = None
    fallback_s: float | None = None
    expired: list[str] = field(default_factory=list)
    #: The fallback came, but later than the window and one look (`ignored`).
    late: bool = False
    #: For a phase spec's expiry: the phase probed (`L1`..`L3`); its row is that phase's own (`report_name`).
    phase: str | None = None

    @property
    def report_name(self) -> str:
        """The report's row: the mode, or for a phase spec's expiry `expiry:<phase>` (each phase proven alone)."""
        return expiry_mode(self.phase) if self.mode == MODE_EXPIRY else self.mode

    @property
    def phased(self) -> bool:
        """Whether this mode commands targets per phase (`Verification.probe`)."""
        return self.mode == MODE_PER_PHASE or self.via == VIA_PER_PHASE or (
            self.mode == MODE_EXPIRY and self.scope == "phase"
        )

    def as_dict(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self.variant is not None:
            extra = {"variant": self.variant, "variants": [dict(row) for row in self.variants]}
        if self.via is not None:
            extra["via"] = self.via
        if self.mode == MODE_EXPIRY:
            extra |= {"expiry_id": self.expiry_id, "window_s": self.window_s, "scope": self.scope,
                      "fallback_s": self.fallback_s, "expired": list(self.expired), "late": self.late,
                      "phase": self.phase}
        if self.phased:
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
        timing: Timing | None = None, stop_on_fail: bool = False, charge_soc_max_pct: float = CHARGE_TAPER_SOC_PCT,
        expiry: Expiry | None = None, expiry_phased: bool = False, phase_mapped: bool = False,
        expiry_phase: int = 0,
    ) -> None:
        self.executor = executor
        #: The mode `expiry`: the expiry it tries, whether it is the phase spec's (tried on the per-phase probe's
        #: discharge on one phase) or the hold's own, and whether `per_phase` passed on this firmware before the run.
        self.expiry = expiry
        self.expiry_phased = expiry_phased
        self.phase_mapped = phase_mapped
        #: For a phase spec's expiry: the phase this run proves (0-2, the meter's L1-L3); one a run.
        self.expiry_phase = min(max(int(expiry_phase), 0), 2)
        #: When the mode `expiry`'s silence began (`ExecInputs.expiry_probe` from then on), `None` before and after.
        self.expiry_since: datetime | None = None
        #: The mode `expiry`'s house at the silence's start (kW), the last look a renewed role was written in, and
        #: the first look of a fallback and how many looks in a row it has read so.
        self.expiry_house: float | None = None
        self.expiry_last_write: datetime | None = None
        self.expiry_fallback_at: datetime | None = None
        self.expiry_fallback_looks = 0
        #: The looks in the mode `expiry` that wrote a renewed role: a long dead-man (`expiry_long`) goes silent only
        #: once one renewal followed the command's own write, so the timing also shows a renewal restarts it.
        self.expiry_writes = 0
        #: For a silence the test wrote (`Expiry.silence`, a dead-man the integration renews): since when the plant is
        #: being taken back after the outcome (the discharge to the house, the integration's own state again) before
        #: the hand-back; `None` otherwise.
        self.expiry_retake_since: datetime | None = None
        self.timing = timing or Timing()
        #: Up to which state of charge the mode `charge` is tried (`TEST_CHARGE_SOC_MAX_PCT` for the hardware test; see
        #: the module). From `CHARGE_TAPER_SOC_PCT` on, a step taken short is `not_judgeable:battery_full`.
        self.charge_soc_max_pct = charge_soc_max_pct
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
        wanted = [
            (name, action) for name, action in table
            if (modes is None and name not in _NAMED_ONLY) or (modes is not None and name in modes)
            or action == ACTION_SELF_CONSUME
        ]
        #: Modes whose action the recipe does not have (a limits-only recipe has no grid charge): never commanded,
        #: reported `skipped` (`report`), and left out of the routine's next runs (`gate.modes_to_run`'s `supported`).
        self.unsupported: tuple[tuple[str, str], ...] = tuple(
            (name, action) for name, action in wanted
            if action != ACTION_SELF_CONSUME and not executor.control.supports(action)
        )
        self.results: list[ModeResult] = [
            ModeResult(name, action) for name, action in wanted if (name, action) not in self.unsupported
        ]
        for result in self.results:
            if result.mode == MODE_EXPIRY:
                result.scope = "phase" if expiry_phased else "hold"
                result.phase = PHASE_NAMES[self.expiry_phase] if expiry_phased else None
        #: The phased modes: the steps (phase index, charge kW, negative a discharge), the step now (-1: every
        #: target at 0), and the house per phase before its probe (kW).
        self.steps: list[tuple[int, float]] = [(index, -PROBE_KW) for index in range(3)]
        self.probe_index = 0
        self.probe_house: Phases | None = None
        #: Whether the executor ran targets per phase before `charge` was tried per phase (given back after it).
        self.phased_before: bool | None = None
        #: Settled looks in a row in which the executor could raise the charge step and kept it too small.
        self.unraised_looks = 0
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
        if result is not None and self.stage == "hand_back" and self.expiry_retake_since is not None:
            # After a silence: the plant taken back as the inverter's own self-consumption (`_retake`), then handed
            # back as ever. A dead-man that did not act leaves the inverter on its last command until it is.
            return ACTION_DISCHARGE_HOUSE, 0.0
        if result is None or self.stage == "hand_back":
            return ACTION_SELF_CONSUME, 0.0
        if result.commanded_at is None and result.mode == MODE_EXPIRY:
            refusal = self._expiry_refusal(sample)
            if refusal is not None:
                if refusal != "wait":
                    result.outcome = f"not_judgeable:{refusal}"
                    self._next(now)
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
                self.steps = [(index, -PROBE_KW) for index in range(3)]
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

    def _expiry_refusal(self, sample: Sample) -> str | None:
        """Before the mode `expiry` commands anything (see the module): why it cannot be tried now (a `not_judgeable`
        reason, or `wait` while the executor still holds something), `None` once it is set up. Its hold is the one
        that passed in this run, with that run's variant; on a phased control the probe's discharge on L1 once
        `per_phase` passed (before, or in this run)."""
        result = self.current
        assert result is not None
        expiry = self.expiry
        if expiry is None:
            return "not_supported"
        hold = next((row for row in self.results if row.mode == "hold"), None)
        if hold is None or hold.outcome != "obeyed":
            return "hold_not_passed"
        result.expiry_id, result.window_s = expiry.expiry_id, expiry.window_s
        result.scope = "phase" if self.expiry_phased else "hold"
        if not self.expiry_phased:
            if hold.variant is not None and not self.executor.use_variant(hold.variant):
                return "wait"
            # A hold shows its expiry only against a discharge it stops.
            if sample.power_kw is None or sample.power_kw > -FLOOR_KW:
                return "battery_not_discharging"
            return None
        per_phase = next((row for row in self.results if row.mode == MODE_PER_PHASE), None)
        if not (self.phase_mapped or (per_phase is not None and per_phase.outcome == "obeyed")):
            return "per_phase_not_verified"
        if not self.executor.use_phases(True):
            return "wait" if self.executor.control.phase_control is not None else "not_supported"
        if _house_kw(sample) is None:
            return "readings_stale"
        unfit = _probe_unfit(sample, before=True)
        if unfit is not None:
            return unfit
        # A discharge, never a charge (its fallback puts the probe's power back on the phase: `_expiry_unsafe`).
        result.action = ACTION_GRID_CHARGE
        self.steps = [(self.expiry_phase, -PROBE_KW)]
        self.probe_index, self.probe_house = 0, _house_kw(sample)
        return None

    def expiry_probe(self) -> bool:
        """Whether the executor is to stop renewing the command now (`ExecInputs.expiry_probe`): the mode `expiry`'s
        silence, from the battery seen obeying to an outcome."""
        result = self.current
        return (
            result is not None and result.mode == MODE_EXPIRY and self.stage == "read" and self.expiry_since is not None
        )

    def charge_per_phase(
        self, now: datetime, sample: Sample, step: tuple[int, float] | None, *, mapped: bool = False
    ) -> None:
        """The mode `charge`, which has no room as the balanced charge, tried as a charge on one phase (see the
        module): `step` (`phase_charge_step`) on the phase with the most room, `None` when no phase has any
        (`not_judgeable:no_phase_room`). Unless the mode `per_phase` has shown the sign and the mapping on this
        firmware (`mapped`), a discharge on that phase comes first, and the charge only once it showed both. Left
        for a later run, with nothing written, when the executor cannot run its targets per phase now or the moment
        could not judge it soundly (`_probe_unfit`)."""
        result = self.current
        if result is None or result.action != ACTION_GRID_CHARGE or result.phased or self.stage != "command":
            return
        if step is None:
            self.not_judgeable(now, "no_phase_room")
            return
        before = self.executor.control.phased
        if not self.executor.use_phases(True):
            self.not_judgeable(now, "grid_charge_cap")
            return
        self.phased_before = before
        house = _house_kw(sample)
        unfit = "phase_reading_stale" if house is None else _probe_unfit(
            sample, before=True, soc_max_pct=self.charge_soc_max_pct
        )
        if unfit is not None:
            self.not_judgeable(now, unfit)
            return
        result.via = VIA_PER_PHASE
        result.rate_kw = step[1]
        # A charge on a phase that is not the meter's phase of that name could land on a full one, and stay there
        # if Home Assistant stops: the discharge, which needs no room, shows the phase first.
        self.steps = [step] if mapped else [(step[0], -PROBE_KW), step]
        self.probe_index, self.probe_house = -1, None

    def probe(self) -> Phases | None:
        """The charge per phase to command in this look for a phased mode (kW, negative: a discharge; the mode
        `per_phase`, and `charge` tried per phase), `None` in every other mode and once the mode is handing back."""
        result = self.current
        if result is None or not result.phased or self.stage == "hand_back" or result.commanded_at is None:
            return None
        values = [0.0, 0.0, 0.0]
        if self.probe_index >= 0:
            phase, target = self.steps[self.probe_index]
            values[phase] = target
        return (values[0], values[1], values[2])

    def _probe_taken(self, commanded: Phases | None) -> bool:
        """Whether the executor commands the probe now: every target as asked, except that a charge may be lowered
        to its phase's room (the executor's own fuse rule), not under `PHASE_CHARGE_MIN_KW`, where a plant that
        does nothing would read as one at the target."""
        wanted = self.probe()
        if commanded is None or wanted is None:
            return False
        for value, asked in zip(commanded, wanted, strict=True):
            if asked > 0:
                if not PHASE_CHARGE_MIN_KW - 1e-9 <= value <= asked + 1e-9:
                    return False
            elif abs(value - asked) > 1e-9:
                return False
        return True

    def _unraised(self, now: datetime, state: Any, step: StepResult) -> bool:
        """Whether the executor, settled, keeps the charge step under `PHASE_CHARGE_MIN_KW` in two looks in a row in
        which its own rules let it raise it (not from a discharge, which goes through 0 first, nor within the dwell
        after its room took the charge down), or once the dwell has passed whatever the last change was."""
        commanded = state.phase_kw
        if not (state.phase == "active" and state.pending is None and not step.ops
                and self._probe_not_raised(commanded)):
            self.unraised_looks = 0
            return False
        phase = self.steps[self.probe_index][0]
        since = None if state.rate_changed_at is None else (now - state.rate_changed_at).total_seconds()
        if since is not None and since >= RATE_UP_DWELL_S:
            return True
        lowered = state.phase_was_kw
        held = commanded[phase] < 0 or (lowered is not None and lowered[phase] > max(commanded[phase], 0.0) + 1e-9)
        self.unraised_looks = 0 if held else self.unraised_looks + 1
        return self.unraised_looks >= 2

    def _probe_not_raised(self, commanded: Phases | None) -> bool:
        """Whether the step now is a charge the executor holds under `PHASE_CHARGE_MIN_KW` on its phase (too little
        live room to judge it)."""
        if self.probe_index < 0 or commanded is None:
            return False
        phase, target = self.steps[self.probe_index]
        return target > 0 and commanded[phase] < PHASE_CHARGE_MIN_KW - 1e-9

    def not_judgeable(self, now: datetime, why: str) -> None:
        """The running mode cannot be tried now for a reason of the shell's (the grid charge cap): it is left pending
        and the run goes on once the executor has handed back."""
        result = self.current
        if result is None or self.stage == "hand_back" or result.action == ACTION_SELF_CONSUME:
            return
        result.outcome = f"not_judgeable:{why}"
        self._enter(now, "hand_back")

    def not_applicable(self, now: datetime, outcome: str) -> None:
        """The running mode cannot be run at a safe size on this plant (`gate.NOT_APPLICABLE_FIXED_CHARGE`): recorded so,
        and the hardware test goes on with the next mode once the executor has handed back (nothing was written for
        it)."""
        result = self.current
        if result is None or self.stage == "hand_back" or result.action == ACTION_SELF_CONSUME:
            return
        result.outcome = outcome
        self._enter(now, "hand_back")

    def after(self, now: datetime, step: StepResult, sample: Sample, capability: Capability) -> None:
        """What the executor did in this look, and the battery's reading."""
        result = self.current
        if result is None:
            return
        state = step.state
        if result.action != ACTION_SELF_CONSUME and (step.ops or state.phase in ("active", "restoring")):
            self.wrote = True
        if result.mode == MODE_EXPIRY and any(op.role in self._expiry_roles() for op in step.ops):
            # The dead-man counts from the last write of a renewed role (or of the silence), not from the look the
            # test began.
            self.expiry_last_write = now
            self.expiry_writes += 1
        if result.action != ACTION_SELF_CONSUME and self.stage != "hand_back":
            hand_back = next((event for event in step.events if event["kind"] == "hand_back"), None)
            not_started = next((event for event in step.events if event["kind"] == "not_started"), None)
            if hand_back is not None and result.mode == MODE_EXPIRY:
                # The executor's own hand-back ends the test: a charge seen in the silence is said as such.
                reason = str(hand_back["reason"])
                charging = reason.endswith(":phase_over") or reason.endswith(":charging")
                self.expiry_since = None
                self._abort(now, "aborted:charge_in_silence" if charging else f"aborted:{reason}")
                return
            if hand_back is not None and result.phased and str(hand_back["reason"]).endswith(":phase_over"):
                # The executor's runaway on a target of this mode's: its answer, and the block is its own finding.
                self._runaway(now, result, sample)
                self._enter(now, "hand_back")
                return
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
            if taken and result.phased and not self._probe_taken(state.phase_kw):
                taken = False
            if taken:
                result.taken_at = now
                self._enter(now, "settle")
                if not (result.phased and self._settle_s(result) <= 0):
                    return
            elif result.phased and self._unraised(now, state, step):
                # The executor could raise the charge and kept it too small: not enough live room on its phase.
                result.outcome = "not_judgeable:fuse_headroom"
                self._enter(now, "hand_back")
            elif (
                result.commanded_at is not None
                and (now - result.commanded_at).total_seconds() >= self.timing.command_timeout_s
            ):
                self._abort(now, "aborted:command_timeout")
            if self.stage != "settle":
                return
        if self.stage == "settle":
            if (now - self.stage_at).total_seconds() >= self._settle_s(result):
                self._enter(now, "read")
            else:
                return
        if self.stage == "read":
            if result.mode == MODE_EXPIRY:
                self._read_expiry(now, result, sample, step)
            elif result.phased:
                self._read_phase(now, result, sample, state.phase_kw)
            else:
                self._read(now, result, sample, capability)
            return
        if self.stage == "hand_back" and self.expiry_retake_since is not None:
            self._retake(now, result, step)
            return
        if self.stage == "hand_back" and state.phase in ("idle", "blocked") and not state.holds_snapshot and not step.ops:
            if state.phase == "blocked":
                # A runaway on a phased mode's own target (`_runaway`, or one seen while it was handed back after
                # its readings) is that mode's finding too.
                own = result.phased and str(state.blocked).endswith(":phase_over")
                if not str(state.blocked).startswith("no_effect") and not own:
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

    def _settle_s(self, result: ModeResult) -> float:
        """How long the step just taken settles before its readings (s): the step with every target at 0 at least
        `ZERO_SETTLE_S`, so a battery that was charging just before cannot show in the house per phase."""
        if result.mode == MODE_EXPIRY:
            # Its command already passed in this run: it settles as a target does.
            return self.timing.phase_settle_s
        if not result.phased:
            return self.timing.settle_s
        if self.probe_index < 0:
            return max(self.timing.zero_settle_s, ZERO_SETTLE_S)
        return self.timing.phase_settle_s

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
            full = self.charge_soc_max_pct if kind == "charge" else CHARGE_TAPER_SOC_PCT
            expected, why = deliverable_kw(command, cap, sample.soc_pct, full_soc_pct=full)
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
        if kind == "charge" and result.outcome != "obeyed" and _near_full(sample):
            # A battery near full takes less than a charge asks: nothing to judge (see the module).
            result.outcome = "not_judgeable:battery_full"
        self._variant_failed(now, result)
        self._enter(now, "hand_back")

    def _read_phase(self, now: datetime, result: ModeResult, sample: Sample, commanded: Phases | None) -> None:
        """One reading of a phased mode's step (see the module): the plant's power on the probed phase at what the
        executor commands there (`commanded`: a charge it lowered to the phase's room is judged at that), the others
        at nothing, and the house on it where it was."""
        if self.last_reading_at is not None and (now - self.last_reading_at).total_seconds() < min(
            PHASE_READING_GAP_S, self.timing.reading_gap_s
        ):
            return
        self.last_reading_at = now
        index = self.probe_index
        plant, grid, before = sample.plant_phase_kw, sample.grid_phase_kw, self.probe_house
        house = _house_kw(sample)
        # The mode `charge` tried per phase (not `per_phase`, whose steps are discharges).
        charging = result.mode == "charge"
        unfit = _probe_unfit(sample, before=False, soc_max_pct=self.charge_soc_max_pct if charging else None)
        if plant is None or grid is None or house is None or (before is None and index >= 0):
            unfit = "phase_reading_stale"
        elif index >= 0 and self._probe_not_raised(commanded):
            # The phase's live room fell to nothing and the executor took the charge down: nothing to judge.
            unfit = "fuse_headroom"
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
            self.unraised_looks = 0
            self._enter(now, "command")
            return
        assert before is not None
        phase, asked = self.steps[index]
        target = asked if commanded is None or asked <= 0 else min(asked, commanded[phase])
        # The plant's own power is + output: a charge of `target` on the phase reads as -target there.
        expected = -target
        others = [abs(plant[other]) for other in range(3) if other != phase]
        acts = abs(plant[phase] - expected) <= PROBE_POWER_TOLERANCE_KW and max(others) <= PROBE_POWER_TOLERANCE_KW
        moved = house[phase] - before[phase]
        toward = plant[phase] if expected > 0 else -plant[phase]  # how far the plant went the way it was told
        if acts:
            verdict = "ok" if abs(moved) <= PROBE_HOUSE_TOLERANCE_KW else "wrong_phase"
        elif -toward >= FAIL_FRACTION * abs(expected) and -toward > PROBE_POWER_TOLERANCE_KW:
            verdict = "wrong_sign"
        elif toward < FAIL_FRACTION * abs(expected):
            verdict = "no_effect"
        else:
            verdict = "partial"
        result.readings.append({
            "at": now.isoformat(), "phase": PHASE_NAMES[phase], "target_kw": round(target, 3),
            "plant_kw": [round(value, 3) for value in plant], "grid_kw": [round(value, 3) for value in grid],
            "house_moved_kw": round(moved, 3), "verdict": verdict,
        })
        if len(result.readings) < min(READINGS, self.timing.readings):
            return
        verdicts = {row["verdict"] for row in result.readings}
        if len(verdicts) == 1 and verdicts <= {"no_effect", "wrong_phase", "wrong_sign"}:
            outcome = "ignored" if verdicts == {"no_effect"} else next(iter(verdicts))
        elif verdicts == {"ok"}:
            outcome = "obeyed"
        elif "wrong_phase" in verdicts and verdicts <= {"ok", "wrong_phase"}:
            outcome = "not_judgeable:house_changed"
        else:
            outcome = "partial"
        result.phases.append({
            "phase": PHASE_NAMES[phase], "target_kw": round(asked, 3), "outcome": outcome,
            "sign": "wrong" if outcome == "wrong_sign" else "ok" if outcome in (
                "obeyed", "wrong_phase", "not_judgeable:house_changed") else None,
            "mapping": "wrong" if outcome == "wrong_phase" else "ok" if outcome == "obeyed" else None,
            "readings": list(result.readings),
        })
        result.readings = []
        if charging and asked > 0 and outcome in ("ignored", "partial") and _near_full(sample):
            # A battery near full takes less than a charge asks: nothing to judge (see the module).
            result.phases[-1]["outcome"] = outcome = "not_judgeable:battery_full"
        if outcome == "obeyed" and index < len(self.steps) - 1:
            # The next phase, from the house as it reads under this one's probe (just judged right).
            self.probe_index, self.probe_house = index + 1, house
            result.commanded_at = now  # the next phase's own time to be taken (`COMMAND_TIMEOUT_S`)
            self.unraised_looks = 0
            self._enter(now, "command")
            return
        result.outcome = "partial" if outcome in ("wrong_phase", "wrong_sign") else outcome
        self._enter(now, "hand_back")

    def _read_expiry(self, now: datetime, result: ModeResult, sample: Sample, step: StepResult) -> None:
        """One look of the mode `expiry` (see the module): first the battery seen obeying (with nothing owed to the
        command), then the silence, sampled every look until the plant has read as fallen back for `READINGS` looks in
        a row, or the deadline. The house on the probed phase (on the whole site for a hold) must stay where it was:
        a load that moves can look exactly like a fallback, and is never a pass."""
        expiry = self.expiry
        assert expiry is not None and expiry.window_s is not None
        phase = self.steps[0][0] if result.scope == "phase" else None
        result.expired += [str(event["reason"]) for event in step.events if event["kind"] == "expired"]
        if any(event["kind"] == "diverged" for event in step.events):
            # A renewed role changed to anything but the device's documented fallback: another writer, which would
            # also keep a command alive that the plant itself never ends. Never a pass.
            self._expiry_outcome(now, result, "not_judgeable:other_writer")
            return
        stale = sample.power_kw is None or sample.age_s is None or sample.age_s > READING_MAX_AGE_S
        plant, house = sample.plant_phase_kw, self._expiry_house_kw(sample, phase)
        point = None if phase is None else self._written_point_kw(phase)
        grid_target = phase is not None and self._grid_target()
        if stale or house is None or (phase is not None and (plant is None or (
            grid_target and (point is None or sample.phase_point_kw is None)
        ))):
            # For a target that is the grid's power at a point, the value written and the point's reading are both
            # needed to tell a fallback (`_written_point_kw`).
            self._expiry_outcome(now, result, "not_judgeable:readings_stale")
            return
        assert sample.power_kw is not None
        if self.expiry_since is None:
            self._expiry_start(now, result, sample, step, phase, house)
            return
        elapsed = (now - self.expiry_since).total_seconds()
        if self.expiry_last_write is None:
            # No write of a renewed role seen in this mode: the dead-man could not be timed.
            self._expiry_outcome(now, result, "not_judgeable:no_write_seen")
            return
        last_write = self.expiry_last_write
        result.readings.append({
            "at": now.isoformat(), "since_s": round(elapsed, 1),
            "since_write_s": round((now - last_write).total_seconds(), 1), "power_kw": round(sample.power_kw, 3),
            "grid_kw": None if sample.grid_kw is None else round(sample.grid_kw, 3),
            "plant_kw": None if plant is None else [round(value, 3) for value in plant],
            "house_kw": round(house, 3),
        })
        if self._expiry_unsafe(now, result, sample, step, phase):
            return
        assert self.expiry_house is not None
        if self._by_register(expiry, phase):
            # A long dead-man with a documented fallback: judged by the command reading it (a register fact, seen as
            # the executor's `expired`), whatever the house does; the battery's power is the cross-check kept.
            if result.expired:
                if self.expiry_fallback_at is None:
                    self.expiry_fallback_at = now
                    result.readings.append({
                        "at": now.isoformat(), "fallback": list(result.expired),
                        "cross_check": "covering" if sample.power_kw < -FLOOR_KW else "not_covering",
                    })
                self._expiry_decide(now, result, expiry, (self.expiry_fallback_at - last_write).total_seconds())
                return
            if (now - last_write).total_seconds() >= expiry_deadline_s(expiry.window_s) or (
                elapsed >= expiry_max_s(expiry.window_s)
            ):
                self._expiry_outcome(now, result, "ignored")
            return
        if abs(house - self.expiry_house) > PROBE_HOUSE_TOLERANCE_KW:
            # The house moved: a load that stops on the phase reads as the plant leaving its target.
            self._expiry_outcome(now, result, "not_judgeable:house_changed")
            return
        if sample.grid_kw is not None and sample.grid_kw < -FLOOR_KW:
            self._expiry_outcome(now, result, "not_judgeable:sun_surplus")
            return
        if phase is not None:
            assert plant is not None
            fell_back = abs(plant[phase]) <= PROBE_POWER_TOLERANCE_KW and (
                point is None or abs(sample.phase_point_kw[phase] - point) > PROBE_POWER_TOLERANCE_KW  # type: ignore[index]
            )
        else:
            fell_back = sample.power_kw < -FLOOR_KW
        if fell_back:
            if self.expiry_fallback_at is None:
                self.expiry_fallback_at = now
            self.expiry_fallback_looks += 1
            if self.expiry_fallback_looks >= READINGS:
                self._expiry_decide(now, result, expiry, (self.expiry_fallback_at - last_write).total_seconds())
                return
        else:
            # A fallback must hold: one look of it is a glitch.
            self.expiry_fallback_at, self.expiry_fallback_looks = None, 0
        window_over = (now - last_write).total_seconds() >= expiry_deadline_s(expiry.window_s)
        if (window_over and self.expiry_fallback_at is None) or elapsed >= expiry_max_s(expiry.window_s):
            self._expiry_outcome(now, result, "ignored")

    def _expiry_start(
        self, now: datetime, result: ModeResult, sample: Sample, step: StepResult, phase: int | None, house: float
    ) -> None:
        """The mode `expiry` before its silence: the command as written, obeyed, and nothing owed to it (a write
        still to go out in the silence would renew it)."""
        state = step.state
        if step.ops or state.pending is not None or state.in_flight or state.renew:
            if (now - self.stage_at).total_seconds() >= self.timing.command_timeout_s:
                self._expiry_outcome(now, result, "not_judgeable:writes_owed")
            return
        expiry = self.expiry
        if (
            expiry is not None and expiry.window_s is not None and expiry_long(expiry.window_s) and not expiry.silence
            and self.expiry_writes < 2
        ):
            # A long dead-man: one renewal after the command's own write first, so the fallback timed from it shows the
            # renewal restarted the timer (`_expiry_decide`).
            renew_s = expiry.renew_s or WATCHDOG_RENEW_S
            if (now - self.stage_at).total_seconds() >= renew_s + self.timing.command_timeout_s:
                self._expiry_outcome(now, result, "not_judgeable:no_renewal_seen")
            return
        if phase is not None:
            plant = sample.plant_phase_kw
            assert plant is not None
            unfit = _probe_unfit(sample, before=False)
            others = max(abs(plant[other]) for other in range(3) if other != phase)
            if unfit is not None:
                self._expiry_outcome(now, result, f"not_judgeable:{unfit}")
                return
            if abs(plant[phase] - PROBE_KW) > PROBE_POWER_TOLERANCE_KW or others > PROBE_POWER_TOLERANCE_KW:
                self._expiry_outcome(now, result, "not_judgeable:not_obeying")
                return
        else:
            assert sample.grid_kw is not None and sample.power_kw is not None
            if sample.power_kw < -FLOOR_KW:
                self._expiry_outcome(now, result, "not_judgeable:not_obeying")
                return
            if sample.grid_kw <= FLOOR_KW and not self._by_register(self.expiry, None):
                # Nothing for the battery to cover: a fallback could not show.
                self._expiry_outcome(now, result, "not_judgeable:battery_not_discharging")
                return
        if self._expiry_unsafe(now, result, sample, step, phase):
            return
        self.expiry_since, self.expiry_house = now, house
        self.expiry_fallback_at, self.expiry_fallback_looks = None, 0

    @staticmethod
    def _by_register(expiry: Expiry | None, phase: int | None) -> bool:
        """Whether the mode `expiry` judges the fallback by the command role reading its documented fallback
        (`Expiry.fallback_values`) instead of the house's power: a long dead-man (`expiry_long`) SpotNav renews, on
        the whole command (a five-minute silence outlasts any steady house)."""
        return (
            expiry is not None and phase is None and expiry.window_s is not None and expiry_long(expiry.window_s)
            and bool(expiry.fallback_values) and not expiry.silence
        )

    def _expiry_unsafe(
        self, now: datetime, result: ModeResult, sample: Sample, step: StepResult, phase: int | None
    ) -> bool:
        """Whether the test gives way now (and it is ended): the headroom unknown, a peak cover due, the headroom
        under the margin plus what the fallback itself puts back on the probed phase (the probe's discharge, while it
        has not fallen back yet), or the battery taking from the grid in the silence."""
        needed = HEADROOM_MARGIN_A
        if phase is not None and self.expiry_fallback_at is None:
            needed += PROBE_KW * 1000.0 / phase_volts(sample.volts_between_phases)
        if (
            sample.headroom_a is None or any(event["kind"] == "peak_cover" for event in step.events)
            or sample.headroom_a < needed
        ):
            # A fallback that ends the probe's discharge adds that much import on its phase; a peak cover would
            # write (and renew the command).
            self._expiry_outcome(now, result, "aborted:fuse_headroom")
            return True
        plant = sample.plant_phase_kw
        assert sample.power_kw is not None
        # On any phase: the executor counts no runaway in the silence, so this is the one check for a charge.
        charging = (
            sample.grid_kw is not None and min(sample.power_kw, sample.grid_kw) > FLOOR_KW
        ) or (phase is not None and plant is not None and min(plant) < -PROBE_POWER_TOLERANCE_KW)
        if charging:
            # The battery took from the grid: never a fallback, handed back at once.
            self._expiry_outcome(now, result, "aborted:charge_in_silence")
            return True
        return False

    def _expiry_roles(self) -> tuple[str, ...]:
        """The roles whose write the dead-man counts from: those SpotNav renews, and a silence's (`Expiry.silence`)."""
        silence = () if self.expiry is None else tuple(name for name, _value in self.expiry.silence)
        return (*self.executor.control.watchdog_roles, *silence)

    def _retake(self, now: datetime, result: ModeResult, step: StepResult) -> None:
        """After a silence the test wrote: the discharge to the house is commanded until the executor has it in place
        (the integration renewing again, the inverter in its own self-consumption whatever the dead-man did), then
        the hand-back. Ended without it when the executor handed back by itself or after `command_timeout_s`."""
        state = step.state
        since = self.expiry_retake_since
        assert since is not None
        done = (
            state.phase == "active" and state.action == ACTION_DISCHARGE_HOUSE and state.pending is None
            and not state.in_flight and not step.ops
        )
        if done or state.phase != "active" or (now - since).total_seconds() >= self.timing.command_timeout_s:
            why = "done" if done else "handed_back" if state.phase != "active" else "timeout"
            result.readings.append({"at": now.isoformat(), "retake": why})
            self.expiry_retake_since = None

    def _expiry_decide(self, now: datetime, result: ModeResult, expiry: Expiry, fallback_s: float) -> None:
        """The plant read as fallen back, first seen `fallback_s` after the last write of a renewed role (a look's
        resolution: the fallback itself came up to one look sooner). With `W` the window (at most
        `gate.SILENCE_FALLBACK_MAX_S`) and `L` one look (`gate.EXPIRY_LOOK_S`):

        * sooner than the renewal: `partial:early` (the renewals would not keep it alive: the recipe is wrong);
        * else at `W - L` or sooner: `not_judgeable:early_fallback` (something else stopped the plant following its
          target; tried again at a later run, never a pass);
        * after `W - L` up to `W + L`: `obeyed` (a pass; measured only after `gate.EXPIRY_PASSES` runs,
          `gate.merge_report`);
        * up to one more look later: `not_judgeable:late_look` (the look and the write's timing can put a true `W`
          there; tried again, never a pass: the gate takes `W + L` at most, owner decision 5);
        * later: `ignored` (late, decided)."""
        assert expiry.window_s is not None
        result.fallback_s = round(fallback_s, 1)
        if expiry.silence:
            # Timed from the silence's write: the integration's last renewal came up to one of its own intervals
            # before it, so a fallback sooner than the window is still the dead-man; one past it by more than a look
            # is not.
            if fallback_s <= expiry.window_s + EXPIRY_LOOK_S:
                outcome = "obeyed"
            elif fallback_s <= expiry.window_s + 2 * EXPIRY_LOOK_S:
                outcome = "not_judgeable:late_look"
            else:
                result.late = True
                outcome = "ignored"
            self._expiry_outcome(now, result, outcome)
            return
        renew_s = expiry.renew_s or WATCHDOG_RENEW_S
        if expiry_long(expiry.window_s):
            # Timed from the renewal: a fallback a window after the command's first write but sooner than a window
            # after the renewal shows the renewal did not restart the timer (the recipe's renewal is wrong).
            window = expiry.window_s
            if fallback_s < renew_s:
                outcome = "partial:early"
            elif fallback_s <= window - EXPIRY_LOOK_S:
                outcome = "partial:not_renewed"
            elif fallback_s <= window + EXPIRY_LONG_SLACK_S + EXPIRY_LOOK_S:
                outcome = "obeyed"
            elif fallback_s <= window + EXPIRY_LONG_SLACK_S + 2 * EXPIRY_LOOK_S:
                outcome = "not_judgeable:late_look"
            else:
                result.late = True
                outcome = "ignored"
            self._expiry_outcome(now, result, outcome)
            return
        window = min(expiry.window_s, SILENCE_FALLBACK_MAX_S)
        if fallback_s < renew_s:
            outcome = "partial:early"
        elif fallback_s <= window - EXPIRY_LOOK_S:
            # The edge itself too: three looks of passthru seen first a look before the window are not enough.
            outcome = "not_judgeable:early_fallback"
        elif fallback_s <= window + EXPIRY_LOOK_S:
            outcome = "obeyed"
        elif fallback_s <= window + 2 * EXPIRY_LOOK_S:
            outcome = "not_judgeable:late_look"
        else:
            result.late = True
            outcome = "ignored"
        self._expiry_outcome(now, result, outcome)

    def _expiry_house_kw(self, sample: Sample, phase: int | None) -> float | None:
        """The house the mode `expiry` holds steady: on the probed phase (the meter's import plus the plant's output
        there), or for a hold the whole site's (the grid's import plus the battery's discharge)."""
        if phase is not None:
            house = _house_kw(sample)
            return None if house is None else house[phase]
        if sample.grid_kw is None or sample.power_kw is None:
            return None
        return sample.grid_kw - sample.power_kw

    def _grid_target(self) -> bool:
        """Whether the phase spec's targets are the grid's power at a point (`PhaseControl.target_kind` `grid`)."""
        spec = self.executor.control.phase_control
        return spec is not None and spec.target_kind == "grid"

    def _written_point_kw(self, phase: int) -> float | None:
        """For a recipe whose targets are the grid's power at a point (`PhaseControl.target_kind` `grid`, Victron):
        the power at the point the probed phase's target was last written to (kW, + from the grid); `None` for a
        target that is the plant's own power, or one not written."""
        spec = self.executor.control.phase_control
        if spec is None or spec.target_kind != "grid":
            return None
        role = spec.targets[phase]
        value = self.executor.state.target.get(role.name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        return float(value) / role.scale * spec.charge_sign

    def _expiry_outcome(self, now: datetime, result: ModeResult, outcome: str) -> None:
        """The mode `expiry` decided (or left for a later run): the silence ends and the battery is handed back."""
        result.outcome = outcome
        self.expiry_since = None
        if self.executor.silenced:
            self.expiry_retake_since = now
        self._enter(now, "hand_back")

    def _runaway(self, now: datetime, result: ModeResult, sample: Sample) -> None:
        """The executor handed a phased mode back for a phase taking more than its target (`phase_over`): the step
        now is recorded as such, `wrong_sign` where the probed phase took from the grid when told to discharge, and
        the mode is `partial`. The executor's block is lifted once it handed back, so control goes on by its own
        rules (the capability is what failed)."""
        index = self.probe_index
        phase, target = self.steps[index] if index >= 0 else (None, 0.0)
        plant = sample.plant_phase_kw
        outcome = "phase_over"
        if phase is not None and target < 0 and plant is not None and plant[phase] < -PROBE_POWER_TOLERANCE_KW:
            outcome = "wrong_sign"
        result.phases.append({
            "phase": None if phase is None else PHASE_NAMES[phase], "target_kw": round(target, 3), "outcome": outcome,
            "sign": "wrong" if outcome == "wrong_sign" else None, "mapping": None,
            "readings": [*result.readings, {"at": now.isoformat(), "plant_kw": None if plant is None else [
                round(value, 3) for value in plant], "verdict": "over"}],
        })
        result.readings = []
        result.outcome = "partial"

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
        if result is not None and result.mode == "charge" and self.phased_before is not None:
            # `charge` tried per phase is done and handed back: the executor's control as it was before it.
            if self.executor.use_phases(self.phased_before):
                self.phased_before = None
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
            "modes": {
                **{name: {"mode": name, "action": action, "outcome": "skipped"} for name, action in self.unsupported},
                **{result.report_name: result.as_dict() for result in self.results},
            },
        }


def supported_modes(control: BrandControl) -> frozenset[str]:
    """The battery modes whose action the recipe has (`gate.modes_to_run`'s `supported`): a limits-only recipe has
    no grid charge, so its routine never waits for a charge step."""
    return frozenset(name for name, action in MODES if action == ACTION_SELF_CONSUME or control.supports(action))


def phase_charge_step(caps_kw: Phases, volts_between_phases: float) -> tuple[int, float] | None:
    """The charge on one phase the mode `charge` tries where the balanced charge has no room (see the module): the
    phase with the largest HA-down cap (`gate.grid_charge_phase_caps_kw`, the first of equals), at most `PROBE_KW`
    and at most that cap, in whole amps at the phase voltage; `None` when that is under `PHASE_CHARGE_MIN_KW`."""
    if volts_between_phases <= 0 or len(caps_kw) != 3 or not all(math.isfinite(cap) for cap in caps_kw):
        return None
    phase = max(range(3), key=lambda index: (caps_kw[index], -index))
    amp_kw = volts_between_phases / math.sqrt(3.0) / 1000.0
    target = math.floor(min(PROBE_KW, max(caps_kw[phase], 0.0)) / amp_kw + 1e-6) * amp_kw
    if target < PHASE_CHARGE_MIN_KW:
        return None
    return phase, math.floor(target * 1000.0 + 1e-6) / 1000.0


def _passes(result: ModeResult) -> bool:
    """Whether a mode's outcome lets the hardware test go on: obeyed, not judgeable at this moment, or not applicable
    on this plant (`Verification.not_applicable`)."""
    outcome = result.outcome or ""
    return outcome == "obeyed" or outcome.startswith(("not_judgeable:", "not_applicable:"))


def _near_full(sample: Sample) -> bool:
    """Whether the battery is where a charge tapers (`CHARGE_TAPER_SOC_PCT`): it may take less than asked."""
    return sample.soc_pct is not None and sample.soc_pct >= CHARGE_TAPER_SOC_PCT


def _probe_unfit(sample: Sample, *, before: bool, soc_max_pct: float | None = None) -> str | None:
    """Why the per-phase probe could not be judged soundly at this moment, `None` when it can (see the module).
    `before`: the plant is still in its own mode, where a sun surplus shows as export or as the battery charging.
    `soc_max_pct`: up to which state of charge a charge step is tried (`Verification.charge_soc_max_pct`; by default
    `CHARGE_TAPER_SOC_PCT`)."""
    if sample.curtailing:
        return "export_curtailed"
    soc = sample.soc_pct
    full = CHARGE_TAPER_SOC_PCT if soc_max_pct is None else soc_max_pct
    if soc is None or soc >= full or soc <= sample.soc_min_pct + DISCHARGE_MARGIN_PCT:
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
