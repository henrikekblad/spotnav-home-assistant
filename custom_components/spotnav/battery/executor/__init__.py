"""The battery's write path: turns a slot's planned action into the brand's commands, safely.

The shell (`control.py`) runs it only behind the site's gate (`gate.py`: the person's opt-in, the "shadow only" flag,
off by default, and a passed verification for the plant's firmware). It is the one battery write route: since wave 7 the shadow hold for the car (`hold.py`) is a request for
this executor's action `hold`, and what it logs comes from `preview`, which runs the executor's own start rules and
commands on a throw-away instance and returns the writes and the hand-back without writing anything.

How it acts (`step`, one look every few seconds; pure, the shell does the writes it returns):

* **Edge-triggered.** It writes only when the wanted state changes (a new action, a new rate past the hysteresis,
  a brand's watchdog to renew) and only the roles whose reading differs from it, one at a time in the brand's order
  (`commands.py`). Each write goes through the confirmed-write ledger (`ledger.py`): it counts only when the entity
  reads it back within `READBACK_DEADLINE_S` (a brand whose link stalls names a longer wait for the writes that
  start or change a command, `BrandControl.readback_s`; a restore and a peak cover keep the 60 s, so a dropped one
  is sent again soon, and a hand-back for the fuse never waits for a read-back either way); the next role is
  written only after that. A write that does not read
  back (a read-only integration drops it silently, an entity went away) hands the battery back and blocks the
  control until it is reset: SpotNav never repeats a write the device did not take.
* **The monitor judges the effect** (`monitor.py`): after its grace, three readings that show no effect hand the
  battery back and block the control (a hold that does not hold, a charge that does not charge).
* **Restore first, on every exit path.** Before the first write the value of every role it may write is kept
  (`snapshot`) and the shell persists it before it writes. The battery is handed back (every role to its snapshot,
  in the brand's restore order, each read back) when: the part is stopped or unloaded; the plan wants plain
  self-consumption, has no slot now or names an action the brand cannot do; a reading is stale or the site's
  measurement is lost; the plant leaves the grid; the fuse headroom falls under `RESTORE_HEADROOM_A` under a hold,
  a grid charge or a sale (`_FUSE_HANDED_BACK`; a sale on the phases that import only; or a grid charge cannot keep `MIN_RATE_KW` within it); the battery's management changes (its own optimiser, another planner,
  an app taking Remote EMS back: blocked, the block clears by itself after `MANAGEMENT_BLOCK_S`) or reads unknown
  past `MANAGEMENT_GRACE_S` (handed back as stale data is, not blocked) or the site's battery is no longer that brand
  (a plant in its own optimiser is taken only when the person acknowledged the takeover, `ExecInputs.takeover`); someone else changes a control
  SpotNav owns (that control is given up and not written back over the other system, as the ledger says); a write
  is not taken; the monitor sees no effect; the day's write cap is reached. After a restart a stored snapshot is
  restored before anything else. A role SpotNav wrote is restored by writing its snapshot and reading it back even
  when it reads as it was found (`ExecState.written`): Sigenergy 2.0.1 shows a never-set limit as `unknown`, which
  after SpotNav's own write proves nothing; the snapshot is then written as the rating, never as `unknown`, and an
  `unknown` read-back is never a confirmed write (the ledger waits for a real value until its deadline). A restore write that does not read back is retried with backoff
  (`RESTORE_RETRY_S`), up to `RESTORE_ATTEMPTS` times each; a role still not restored then is left with its
  snapshot kept (the control blocks and tries that role again every `BLOCKED_RESTORE_S`, and a restart tries it
  first), while the other roles are handed back. A role
  that meanwhile needs no write (it reads its restore value, or its select is idle once Remote EMS is off) drops its
  retry, and a write made for the action that is still awaited when the hand-back begins is no restore retry: every
  hand-back ends. Where the read-back is only Home Assistant's own echo, a recipe may watch the hand-back's writes
  a while after it ends (`BrandControl.settle_s`): a role that reads SpotNav's value again kept no restore (a cloud
  refused it) and is written again `SETTLE_ATTEMPTS` times, then the control blocks (`restore_not_held`) until the
  role reads the value found or someone else's; meanwhile the facts still read the value found (`found_states`). A
  restore given up is tried again every `BLOCKED_RESTORE_S`, or the recipe's longer `restore_retry_s` where each
  write is a billed cloud command.
* **Fuse safety.** A grid charge is clamped by the site's live headroom: what the battery already draws from the
  grid (at most its charge, the commanded rate and the site's import) plus the least phase's headroom less
  `HEADROOM_MARGIN_A`, counted on the phases the plant spreads its charge over (`ExecInputs.charge_phases`): a plant
  that charges on one phase gets that one phase's room, not three. A sale (`export_discharge`) starts, also as a
  change of action, only under the hold's start rule less its own share on each phase (`hold_headroom_ok`): in a
  commanded discharge the plant may spread its output evenly and stop covering the loaded phase.
  Unlike evcc, the battery's charge counts against the main fuse. With the site's solar priority "car first" a
  charger held below what it asked makes the battery yield (`YIELD_STEP_KW` below its measured grid charge, one step
  per `RATE_UP_DWELL_S`, so the car's regulator has time to follow); with "battery first" the
  battery keeps its rate and the site's load balancing backs the car off (the battery is house load to it).
  The rate goes down at once and up only by `RATE_STEP_KW` after `RATE_UP_DWELL_S`.
* **A charge that takes more than commanded** (balanced; a charge per phase has its own runaway check below): the
  lesser of the battery's charge and the site's import above the rate by `CHARGE_OVER_KW` (or a quarter of it) for
  `CHARGE_OVER_S` hands the charge back (`not_obeyed:grid_charge:over`, clearing by itself after
  `MANAGEMENT_BLOCK_S`). It catches what the rate could not tell: a share of a rated power whose base moved, a wrong
  sign, a fixed rate above its estimate.
* **Peak cover under a hold.** A hold stops the battery covering the house, so a peak (a kettle, the oven, one
  phase) lands on the fuse. When the least phase headroom falls under `PEAK_COVER_A` the executor first lifts the
  hold: one write, the brand's `peak_first` roles back to the value found or to the brand's normal value
  (Sigenergy's mode-4 hold, `pv_first`: the control mode back to Maximum Self Consumption, where the plant covers
  each phase itself), Remote EMS left on. A
  mode change may take the plant longer than a limit (its Remote EMS entities blink around a mode change, TypQxQ
  issue #427; the plant's output ramps at its active power regulation gradient, register 40049); `PEAK_RECOVER_S`
  bounds either, then the full hand-back follows. If the headroom is not back at or above
  `PEAK_COVER_A` within `PEAK_RECOVER_S` (or later stays under it that long), the battery is handed back in full.
  Under `RESTORE_HEADROOM_A` it is handed back at once, as before, and every hand-back of a hold or a grid charge
  writes the `peak_first` roles first (a grid charge: the mode that stops it), as does a change of action away from a
  grid charge (`ExecState.lead`): no limit is loosened while the plant still charges from the grid. Where the
  recipe names limits that act in its modes (`BrandControl.mode_limits`, Sigenergy), every change of action lowers
  those that fall first and raises those that rise last, around the mode (`_lead`). A hand-back
  for the fuse writes all its roles in one look, without waiting for each read-back (each is still read back, and
  one not confirmed is retried as any restore); other hand-backs keep one write at a time. The cover's write is never held back
  by the day's write cap. A covered hold holds again only after `PEAK_REHOLD_S`, and only when the start rule
  allows it (the headroom keeps `HEADROOM_MARGIN_A` once the battery stops covering); otherwise it stays covered
  until the plan changes the action.
* **An idle hold never charges.** Under a hold variant that leaves the battery idle (`HoldVariant.idle`,
  Sigenergy's mode 4 with the charge limit at 0) a battery that charges more than `HOLD_CHARGE_KW` for
  `HOLD_CHARGE_S` did not take the hold: mode 4 is a charge at the charge limit, from the grid once the sun is short,
  so it is handed back and the control blocked (`not_obeyed:hold:charging`), judged on the battery's own power (no
  grid reading needed), and its verification fails that variant. The block clears by itself after
  `MANAGEMENT_BLOCK_S`, as a management change's does. A hold that stores the sun has that guard only where its
  recipe asks for it (`BrandControl.hold_grid_guard`: a hold that is a floor at the SoC, which the plant may reach
  from the grid), judged on the lesser of the battery's charge and the site's import, so the sun it stores does not
  count. A hold that follows a sun surplus has that guard too.
* **A hold that follows a sun surplus** (`HoldVariant.surplus`, `commands.HoldSurplus`): its charge limit is set
  to the sun's surplus now (`house_net_kw` below zero), so the sun is stored and nothing is taken from the grid
  (`_follow_surplus`). A new limit is written past the deadband or at 0, a raise no sooner than the recipe's gap
  after the last one, a fall at once; only once the hold's own writes are in, never under a peak cover, while
  priming or in the expiry test. A limit left standing grid-charges once the sun goes, so it never exceeds the
  HA-down grid charge cap the shell gives (`ExecInputs.hold_charge_cap_kw`; unbounded only where the plant's import
  limit guards the fuse). Without the house's load the limit goes to 0, and once the day's setting writes reach the
  grid charge's reserve it goes to 0 and stays there. Each write is a counted setting write.
* **Hold variants** (`BrandControl.hold_variants`): a brand may have more than one way to hold; the executor runs
  one (`ExecState.variant`, stored so a restart hands back with that variant's order) and changes it only while it
  holds nothing (`use_variant`). Every variant writes only the brand's roles, all of them in the snapshot, so the
  hand-back is the same whichever ran.
* **A command left in the plant.** A role that goes idle with its switch (`Role.idle_when`, Sigenergy's mode
  select while Remote EMS is off) cannot be read back once the switch is off, so a fuse hand-back, which writes both
  at once, may leave its write unconfirmed (and 40031 at a command mode). Such a role is remembered
  (`ExecState.unsure`, stored), and the next enter writes the brand's `prime` values before the switch (Sigenergy:
  both limits at 0, so a mode left there does nothing), then the switch and the mode, then the action's own values.
* **A grid charge per phase** (`commands.PhaseControl`, a control made with `BrandControl.with_phases`; no brand
  is named here). Where the plant takes a power target per phase and the shell gives each phase's HA-down cap
  (`ExecInputs.phase_caps_kw`, `gate.grid_charge_phase_caps_kw`), a grid charge is written as three targets instead
  of the brand's own balanced charge (`phase_charge_kw`). Each phase gets at most its HA-down cap and at most its
  own live room (what the plant already takes on that phase, by its own power per phase, plus that phase's headroom
  less `HEADROOM_MARGIN_A`, less what the plant puts out on it now, which a target ends); the plan's power and the battery's rating bound the sum, spread as evenly as the caps
  allow (`gate.phase_split_kw`). The targets stay if Home Assistant stops, so every one of them is within the cap
  that keeps its phase under the fuse at the house's learned peak. A phase's charge is a whole number of amps
  (`PHASE_STEP_A`, rounded down), so a reading that wobbles writes nothing. It goes down at once
  (`PHASE_TOLERANCE_KW`), except within `PHASE_RAISE_SETTLE_S` of a raise (the plant's own power, counted back into
  the room, has not shown the raise yet), and up after `RATE_UP_DWELL_S`; in a change the falling phases are
  written first. The targets are roles like any other: written one at a
  time through the ledger and read back, 0 in every other action, and stopped first on a hand-back and on a change
  of action (`PhaseControl.stop_first`). The fuse rules above still act on the least headroom of any phase: under
  `RESTORE_HEADROOM_A` the charge is handed back at once, whichever phase it is on (in its own mode the plant covers
  that phase again). The effect is judged per phase (`monitor.judge_phases`) on the plant's own power, or on
  the site meter's phase for a recipe whose targets are the grid's (`PhaseControl.target_kind`). A phase that takes
  more than its target is the runaway: judged from `PHASE_OVER_GRACE_S` after the charge's first writes are taken,
  every `PHASE_OVER_GAP_S`, against the larger of the target and the one before it while a change settles, and a
  change of the targets does not start that count again; `CONFIRM_READINGS` in a row hand the charge back and block
  the control (`not_obeyed:grid_charge:phase_over`). One that takes nothing does so as any charge without effect. A change between the two kinds of charge while one
  runs hands back first (`per_phase_changed`). `ExecInputs.phase_probe_kw` is the verification's: targets written
  as given (a discharge on one phase, or a charge on one phase where the balanced charge has no room), a charge
  among them in whole amps within its cap and its live room, down at once (except within `PHASE_RAISE_SETTLE_S` of
  a raise), up again only `RATE_UP_DWELL_S` after its room took it down, from a discharge only through 0, and judged for the runaway as any charge per phase (the rest the verification judges itself).
* **Targets that are the grid's** (`PhaseControl.target_kind` `grid`, with sensors at the target's point,
  `PhaseControl.grid_phases`; the shell reads them into `ExecInputs.phase_behind_kw` and `phase_point_kw`). Such a
  target is the power at the plant's grid-side point on that phase, so a charge per phase is written as the load
  behind the point plus the charge (`PhaseControl.value`), and that phase's target written again when its load
  moved by `PHASE_TRACK_KW`, at most every `PHASE_TRACK_S` (a setpoint write each; between two the plant holds the
  point, so a load that changes is the battery's, never the grid's). The room of a phase is then reckoned exactly
  (`_phase_room_kw`: the headroom less `PHASE_GRID_MARGIN_A` less what the plant puts out on the phase now), since
  it may be the only bound: the HA-down cap of a recipe with its own dead-man is the fuse
  (`gate.grid_charge_phase_caps_kw`). The HA-down cap bounds the whole target, the load behind plus the charge
  (`_target_caps`), as that is what stands if Home Assistant stops. A phase taking more than its target is judged at the point against the target
  written; whether the battery takes its charge is judged on the plant's own power (the load behind the point less
  the point's). Without the sensors' readings no charge per phase runs (`phase_load_unknown`).
* **Targets the plant owns outside the charge** (`PhaseControl.park` false): they are not written in any other
  action nor on a hand-back, and a role the running action no longer sets has its claim released
  (`_release_unset`), so the plant's own writes to it are never read as someone else's change.
* **A fast watchdog** (`BrandControl.watchdog_renew_s`): its roles are renewed on that interval while the action
  sets them (not one it leaves at its value found: no command stands there), a whole round in one look (each still read back), never ahead of a write the action owes. A renewal
  writes the value the role holds and is not counted; one whose value changed meanwhile is a change, and is. A
  watchdog on the shared `WATCHDOG_RENEW_S` (SolarEdge, Huawei, SolaX) is renewed as before: every role every
  round, on time whatever is still owed, a role a look, never counted.
* **An action run by the grid's power** (`BrandControl.house_states`): the brand's values are given the house's net
  load now beside the rate (`house_net_kw`: the site's import less the battery's charge) and are written again only
  when the rate changes; the monitor is told what the battery can still take or give of its rate once the house
  moved (`_house_cap`). Without the house's load the action does not run (`house_unknown`).
* **A charge in blocks** (`BrandControl.charge_block_s`, a plant that sets its own charge power and stops at a SoC
  ceiling: Tesla's reserve). The ceiling is one block ahead of the SoC at the recipe's rate (an upper figure) and is
  renewed every block from the SoC then; with no dead-man, a Home Assistant or a cloud that stops leaves at most one
  block's charge from the grid.
* **Writes a day, per class** (`commands.WriteClass`, `BrandControl.write_cap`): a role is a `setting` (a limit, a
  mode, a switch: 96 a day, `ExecState.writes_today`, shared with the export limit's instance) or a `setpoint` made
  for continuous control (the targets per phase: the brand's own figure, `ExecState.setpoint_writes_today`).
  Reaching the setpoints' cap is the runaway guard: a charge per phase is handed back (`setpoint_write_cap`, not
  blocked) and none starts again that day, while every other action still runs. The same holds for an action whose
  own rate is a setpoint (`_rate_is_setpoint`: a battery power target), so a rate that must fall is never left
  unwritten. A grid charge of either kind also
  leaves `CHARGE_WRITE_RESERVE` of the day's setting writes untouched (`charge_write_budget`), so a hold can still
  start after a day of charges that kept starting. Restores, the peak cover and watchdog renewals are never
  counted or held back.
* **Targets left standing** (`BrandControl.watched`): a control that does not own another control's targets
  refuses to start while one of them, where its entity exists and is enabled, reads other than it must
  (`stale_target:<role>`).
* **The export limit** (`commands.EXPORT_CONTROLS`, kind `export`): the same executor, ledger and rules run the
  site's export limit as its own instance, for the one action `curtail_export` (the limit at 0 kW while the sell price
  is below zero, `export_curtail.py`); anything else hands it back to the value found. It needs no battery reading
  and no fuse headroom (a lower export limit never adds import), but every other rule holds: management (a battery in
  its own optimiser is only watched), off-grid, the site's measurement, read-back, a change by someone else given up,
  the restore. A limit never set (`unknown`) has no value to hand back and the plant's maximum is never written in
  its place, so it is not taken (`no_restore_value`); a value found at the maker's "no limit" (`Role.no_limit`,
  Victron's -1) is handed back exactly. A brand whose limit has a switch that enables it writes the value first and
  the switch last, and back the switch first; a switch found off is switched off again. Without the site's signed
  grid power nothing could judge it, so it is not taken (`no_grid_reading`). The day's write cap is the
  site's, shared with the battery's instance (`ExecInputs.other_writes_today`). A plant whose mode reads SpotNav's
  own battery control (Remote EMS that the battery instance runs, `ExecInputs.sibling_active`) is taken; one in
  Remote EMS of someone else's is not, and a curtail is handed back when someone else takes Remote EMS. The two instances never write each other's roles: the battery's control
  does not read the export limit, and Sigenergy's export limit needs no Remote EMS, so the battery's hand-back
  (Remote EMS off) takes nothing from it. **Obeyed?** The monitor judges a curtail on the site's grid power: after
  `monitor.CURTAIL_GRACE_S`, three readings exporting more than `monitor.EXPORT_TOLERANCE_KW` hand the limit back
  and block it (`not_obeyed`), and so do three readings importing where the site exported before
  (`not_obeyed:curtail_export:import_rose`: a limit that caps the inverter's output, not the export).

Pure: no Home Assistant import; times are aware datetimes from the caller. State survives a restart
(`ExecState.as_dict`, `from_dict`); the ledger and the monitor keep their own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from ..commands import BrandControl, Native
from ..ledger import Ledger
from ..monitor import Monitor
from .const import (
    BATTERY_MAX_AGE_S,
    BLOCKED_RESTORE_S,
    CALL_TIMEOUT_BLOCK_S,
    CHARGE_OVER_GRACE_S,
    CHARGE_OVER_KW,
    CHARGE_OVER_S,
    CHARGE_WRITE_RESERVE,
    DAILY_WRITE_CAP,
    FIXED_CHARGE_JUDGE_KW,
    HEADROOM_MARGIN_A,
    HOLD_CHARGE_KW,
    HOLD_CHARGE_S,
    LAPSES_A_DAY,
    MANAGEMENT_BLOCK_S,
    MANAGEMENT_GRACE_S,
    PEAK_COVER_A,
    PEAK_RECOVER_S,
    PEAK_REHOLD_S,
    PHASE_GRID_MARGIN_A,
    PHASE_PROBE_MAX_KW,
    PHASE_RAISE_SETTLE_S,
    PHASE_STEP_A,
    PHASE_STEP_KW,
    PHASE_TOLERANCE_KW,
    PHASE_TRACK_KW,
    PHASE_TRACK_S,
    RATE_STEP_KW,
    RATE_TOLERANCE_KW,
    RATE_UP_DWELL_S,
    REARM_S,
    RENEW_ON_CHANGE_GAP_S,
    RESTORE_ATTEMPTS,
    RESTORE_HEADROOM_A,
    RESTORE_READ_WAIT_S,
    RESTORE_RETRY_S,
    SETPOINT_SELLING,
    SETTLE_ATTEMPTS,
    SILENCE_GRACE_S,
    YIELD_STEP_KW,
    _AMP_SLACK,
    _BLOCKING_PREFIXES,
    _BUDGET_REASONS,
    _FUSE_HANDED_BACK,
    _FUSE_REASONS,
    _MONITORED,
    _NO_VALUE,
    _PHASES,
    _PROBE_UNBLOCKED,
    _RAISES_IMPORT,
    _SELF_CLEARING,
    Phase,
)
from .core import Executor
from .fuse import (
    OWN_IMPORT_UNKNOWN_A,
    _finite3,
    _floored,
    _fresh,
    _import_headroom_a,
    _kw,
    _own_import_a,
    _phase_room_kw,
    _room_after_cover_a,
    _target_caps,
    cover_a,
    cover_phase_a,
    grid_charge_cap_kw,
    hold_headroom_ok,
    house_net_kw,
    phase_charge_kw,
    uncovered_a,
)
from .helpers import _blocking, _event, _without, _writes_today
from .management import management_problem, taken_over
from .reads import role_reads
from .state import ExecInputs, ExecState, Op, RoleRead, StepResult
from .values import _NEVER_SET, _rating, _restore_value, _rises, _tolerance, _unset, restore_order

__all__ = [
    "BATTERY_MAX_AGE_S",
    "BLOCKED_RESTORE_S",
    "CALL_TIMEOUT_BLOCK_S",
    "CHARGE_OVER_GRACE_S",
    "CHARGE_OVER_KW",
    "CHARGE_OVER_S",
    "CHARGE_WRITE_RESERVE",
    "DAILY_WRITE_CAP",
    "FIXED_CHARGE_JUDGE_KW",
    "HEADROOM_MARGIN_A",
    "HOLD_CHARGE_KW",
    "HOLD_CHARGE_S",
    "LAPSES_A_DAY",
    "MANAGEMENT_BLOCK_S",
    "MANAGEMENT_GRACE_S",
    "OWN_IMPORT_UNKNOWN_A",
    "PEAK_COVER_A",
    "PEAK_RECOVER_S",
    "PEAK_REHOLD_S",
    "PHASE_GRID_MARGIN_A",
    "PHASE_PROBE_MAX_KW",
    "PHASE_RAISE_SETTLE_S",
    "PHASE_STEP_A",
    "PHASE_STEP_KW",
    "PHASE_TOLERANCE_KW",
    "PHASE_TRACK_KW",
    "PHASE_TRACK_S",
    "RATE_STEP_KW",
    "RATE_TOLERANCE_KW",
    "RATE_UP_DWELL_S",
    "REARM_S",
    "RENEW_ON_CHANGE_GAP_S",
    "RESTORE_ATTEMPTS",
    "RESTORE_HEADROOM_A",
    "RESTORE_READ_WAIT_S",
    "RESTORE_RETRY_S",
    "SETPOINT_SELLING",
    "SETTLE_ATTEMPTS",
    "SILENCE_GRACE_S",
    "YIELD_STEP_KW",
    "_AMP_SLACK",
    "_BLOCKING_PREFIXES",
    "_BUDGET_REASONS",
    "_FUSE_HANDED_BACK",
    "_FUSE_REASONS",
    "_MONITORED",
    "_NEVER_SET",
    "_NO_VALUE",
    "_PHASES",
    "_PROBE_UNBLOCKED",
    "_RAISES_IMPORT",
    "_SELF_CLEARING",
    "ExecInputs",
    "ExecState",
    "Executor",
    "Ledger",
    "Monitor",
    "Op",
    "Phase",
    "Preview",
    "RoleRead",
    "StepResult",
    "_blocking",
    "_event",
    "_finite3",
    "_floored",
    "_fresh",
    "_import_headroom_a",
    "_kw",
    "_own_import_a",
    "_phase_room_kw",
    "_rating",
    "_restore_value",
    "_rises",
    "_room_after_cover_a",
    "_target_caps",
    "_tolerance",
    "_unset",
    "_without",
    "_writes_today",
    "cover_a",
    "cover_phase_a",
    "grid_charge_cap_kw",
    "hold_headroom_ok",
    "house_net_kw",
    "management_problem",
    "phase_charge_kw",
    "preview",
    "restore_order",
    "role_reads",
    "taken_over",
    "uncovered_a",
]


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
    control: BrandControl, inputs: ExecInputs, *, writes_today: int = 0, watchdog_s: int | None = None
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
