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

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from ..commands import (
    SNAPSHOT,
    TOLD_ACTION_REASONS,
    WATCHDOG_RENEW_S,
    WATCHDOG_S,
    BrandControl,
    Native,
    Phases,
    Role,
    fixed_rate_kw,
    forbidden_read,
    native,
    reading,
)
from ..gate import MIN_RATE_KW
from ..learned import SOURCE_LEARNED
from ..ledger import READBACK_DEADLINE_S, Ledger, same
from ..monitor import (
    CHARGE_TAPER_SOC_PCT,
    CONFIRM_READINGS,
    PHASE_GAP_S,
    PHASE_GRACE_S,
    PHASE_OVER_GAP_S,
    PHASE_OVER_GRACE_S,
    Capability,
    Command,
    Monitor,
    Reading,
    judge_phases,
)
from ..plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
)
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
from .expiry_probe import ExpiryProbe
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
from .handback import HandBack
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


# --- helpers -------------------------------------------------------------------------------------------------------


class Executor(ExpiryProbe, HandBack):
    """One battery's write path: the brand's control, the ledger and the monitor, and the state."""

    def __init__(
        self, control: BrandControl, ledger: Ledger | None = None, monitor: Monitor | None = None,
        state: ExecState | None = None, *, watchdog_s: int | None = None,
    ) -> None:
        self.ledger = ledger or Ledger()
        self.monitor = monitor or Monitor()
        self.state = state or ExecState()
        # A stored state runs (and hands back) with the variant it was written with, and with the targets per phase
        # when it ran them (their roles are the phased control's).
        control = control.with_hold(self.state.variant) if self.state.variant else control
        self.control = control.with_phases(True) if self.state.phased else control
        self.state = replace(self.state, variant=self.control.variant, phased=self.control.phased)
        #: The window written into a command that carries one (a timeout, an autorepeat, a lease's duration): the
        #: recipe's expiry window (`Expiry.window_s`), `WATCHDOG_S` where it states none; a caller's own wins.
        self.watchdog_s = watchdog_s if watchdog_s is not None else int(self.control.expiry.window_s or WATCHDOG_S)
        #: What a state that depends on the plant is filled in with (`BrandControl.wanted`): the SoC, the rating and the
        #: optional roles this plant lacks, as of the last look; not stored.
        self._soc_pct: float | None = None
        self._rated_kw: float | None = None
        self._capacity_kwh: float | None = None
        #: The renewed roles read as expired in the running expiry test (`ExecInputs.expiry_probe`), and those that
        #: changed there to anything but the device's documented fallback (`diverged`: another writer); not stored:
        #: a restart ends the test.
        self.expired: tuple[str, ...] = ()
        self.diverged: tuple[str, ...] = ()
        #: The value the command role was owned at when SpotNav's next write to it went out (a renewal, a change of
        #: power or of action; `BrandControl.command_role`): kept across that write's read-back, so a reading that is
        #: none of SpotNav's is someone else's at once, also while the write is awaited. Not stored.
        self.renewal_owned: dict[str, str] = {}
        #: Since when a hand-back has waited for a first reading of the command role (`BrandControl.command_role`):
        #: nothing is written blind (after a restart the integration may not have polled yet); `_command_unread`.
        self.command_wait_since: datetime | None = None
        #: Since when the running hand-back has waited for a first reading of each role it would write back
        #: (`BrandControl.restore_own_only`, `_restore_unread`), every unread role from the look it was first seen
        #: unread: they wait together, not one after the other; not stored.
        self.restore_wait_since: dict[str, datetime] = {}
        #: In the running expiry test, the control's silence was put in the target (`Expiry.silence`: a dead-man the
        #: integration renews, stopped by a write); not stored: a restart ends the test.
        self.silenced = False
        self._absent: frozenset[str] = frozenset()
        #: When a hold's surplus follow last raised its limit (`_follow_surplus`); not stored.
        self._surplus_raised_at: datetime | None = None

    def _sense(self, inputs: ExecInputs) -> None:
        facts = inputs.facts
        self._soc_pct, self._rated_kw, self._capacity_kwh = facts.soc_pct, facts.max_charge_kw, facts.capacity_kwh
        if self.control.rating_read:
            # Shares of the rating as the plant reads it (Fronius's WChaMax), never of a learnt one or a lower plant
            # maximum: a learnt rating below the real base would ask for more than the rate (`BrandControl.rating_read`).
            learnt = facts.sources.get("rated_charge") == SOURCE_LEARNED
            self._rated_kw = None if learnt else facts.rated_charge_kw
        optional = self.control.optional_names()
        self._absent = frozenset(
            name for name in optional if (read := inputs.reads.get(name)) is None or not read.found
        )

    def found_states(self) -> dict[str, str]:
        """The state found before SpotNav first wrote each role it keeps a snapshot of, by entity id: what the
        battery's facts read in place of SpotNav's own value (`facts.battery_facts`), so a floor set for a hold is
        never taken for the battery's own and the plan does not see an empty battery."""
        state = self.state
        found = {
            entity_id: row[0] for row in state.settle.values()
            if (entity_id := row[3]) and row[0].strip().lower() not in _NO_VALUE
        }
        return found | {
            state.entities[name]: value for name, value in state.snapshot.items()
            if name in state.entities and value.strip().lower() not in _NO_VALUE
        }

    @property
    def _daily_cap(self) -> int:
        """The day's setting writes (`BrandControl.write_cap`; 96 unless the recipe says less)."""
        return min(DAILY_WRITE_CAP, self.control.write_cap("setting"))

    @property
    def _charge_reserve(self) -> int:
        return min(CHARGE_WRITE_RESERVE, self._daily_cap // 4)

    def use_phases(self, on: bool) -> bool:
        """Run the brand's targets per phase from now on, or not (`BrandControl.with_phases`); only while nothing is
        held, since the two controls have different roles. Whether the control now is as asked."""
        control = self.control.with_phases(on)
        if control is self.control:
            return self.control.phased == on
        if self.state.phase in ("active", "restoring") or self.state.holds_snapshot:
            return False
        self.control = control
        self.state = replace(self.state, phased=control.phased)
        return True

    def _per_phase(self, inputs: ExecInputs) -> bool:
        """Whether a grid charge asked for now runs as targets per phase."""
        return (
            self.control.phased and inputs.action == ACTION_GRID_CHARGE
            and (inputs.phase_caps_kw is not None or inputs.phase_probe_kw is not None)
        )

    def use_variant(self, name: str | None) -> bool:
        """Run the hold variant `name` from now on; only while nothing is held (never two variants' writes at once).
        Whether the control now runs it."""
        control = self.control.with_hold(name)
        if control is self.control:
            return self.control.variant == name
        if self.state.phase in ("active", "restoring") or self.state.holds_snapshot:
            return False
        self.control = control
        self.state = replace(self.state, variant=control.variant)
        return True

    @property
    def _export(self) -> bool:
        """Whether this instance runs the export limit (`commands.EXPORT_CONTROLS`)."""
        return self.control.kind == "export"

    # --- one look ---

    def step(self, inputs: ExecInputs) -> StepResult:
        now = inputs.now
        self._sense(inputs)
        probing = self._probing(inputs)
        if not probing:
            self.expired = self.diverged = ()
            if self.silenced or (self.state.retake and self._silence_in_target()):
                # Also after a restart in the silence (`ExecState.retake` is stored, `silenced` is not).
                self._unsilence()
        events: list[dict[str, Any]] = []
        today = now.date().isoformat()
        if self.state.day != today:
            self.state = replace(
                self.state, day=today, writes_today=0, setpoint_writes_today=0, renewals_today=0, lapses_today=0,
            )
            if self.state.phase == "blocked" and self.state.blocked == "daily_write_cap" and not self.state.holds_snapshot:
                self.state = replace(self.state, phase="idle", blocked=None)
        state = self.state
        if (
            state.phase == "blocked" and (state.blocked or "").startswith(_SELF_CLEARING)
            and not state.holds_snapshot and state.released_at is not None
            and (now - state.released_at).total_seconds() >= (
                CALL_TIMEOUT_BLOCK_S if (state.blocked or "").startswith("call_timeout") else MANAGEMENT_BLOCK_S
            )
        ):
            self.state = replace(state, phase="idle", blocked=None, reason=None, given_up=())
            events.append(_event(now, "unblocked", state.blocked or ""))
        if (
            state.phase == "blocked" and state.holds_snapshot and (state.blocked or "").startswith("restore_failed")
            and (
                state.released_at is None
                or (now - state.released_at).total_seconds() >= max(BLOCKED_RESTORE_S, self.control.restore_retry_s or 0.0)
            )
        ):
            events += self._begin_restore(now, "restore_again")
        events += self._settle(inputs)
        self._retaken(inputs)

        events += self._observe(inputs)
        if self.state.phase == "active":
            reason = self._exit_reason(inputs)
            if reason is not None:
                events += self._begin_restore(now, reason)
        if probing and self.state.phase == "restoring" and (self.state.blocked or "").startswith(_PROBE_UNBLOCKED):
            # Handed back in the expiry test's silence for what the plant did after nothing more was written: that is
            # the test's to judge, never a lasting block of the control (the hand-back itself goes on).
            self.state = replace(self.state, blocked=None)
        persist_first = False
        if self.state.phase == "idle" and inputs.stopping is None:
            started, start_events = self._try_start(inputs)
            events += start_events
            persist_first = started
        elif self.state.phase == "active" and not self._riding_out():
            events += self._cover(inputs)
            if self.state.phase == "active":
                events += self._retarget(inputs)
        self._settle_retries(inputs)
        ops: list[Op] = []
        while not self._riding_out():
            events += self._precheck(inputs)
            batch = self._progress(inputs)
            ops += batch
            # A fuse hand-back writes all its roles in one look; so does a round of a fast watchdog's renewals.
            if not batch or not (self._fuse_hand_back() or (self._fast_renewal() and self.state.renew)):
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
                why = "call_timeout" if role_name in inputs.timed_out else "service_failed"
                if why == "service_failed" and self._silent_since(inputs, None):
                    # A call that failed while the plant says nothing at all: no finding about the control.
                    events += self._silent_not_taken(now, role_name, why)
                else:
                    events += self._not_taken(now, role_name, why)
        for role in self.control.roles:
            entity_id = self.state.entities.get(role.name)
            claim = None if entity_id is None else self.ledger.claim(entity_id)
            if entity_id is None or claim is None:
                continue
            read = inputs.reads.get(role.name)
            now_read = reading(role, None if read is None else read.state)
            expiring = False
            command = role.name == self.control.command_role
            foreign = command and self._foreign(role, claim, now_read)
            if foreign:
                # Another system's command on the role that holds the whole command: given up at once, and from now
                # on nothing is written to it or to the limits that act in it (`_gave_up`).
                events += self._gave_up(now, role.name, entity_id, now_read, foreign=True)
                self.ledger.give_up(entity_id, now_read, now, claim.intended)
                continue
            if (
                command and self.state.altered == role.name and self.state.phase == "active"
                and now_read is not None and now_read == self._fallback_values().get(role.name)
            ):
                # SpotNav's own command, its value set from outside, ended by the device's own expiry: handed back.
                events += self._begin_restore(now, f"altered_ended:{role.name}")
                continue
            owned = claim.owned if claim.owned is not None else self.renewal_owned.get(role.name)
            if (
                command and not self._probing(inputs) and owned is not None and now_read is not None
                and self.state.phase == "active" and role.name not in self.state.given_up
            ):
                fallback = self._fallback_values().get(role.name)
                step = None if read is None else _tolerance(role, read)
                if fallback is not None and same(fallback, now_read, step) and same(owned, now_read, step) is False:
                    # The command lapsed by itself (a renewal that never landed): the device's own fallback, not
                    # someone else's change. Handed back and started again; blocked only on repeats.
                    events += self._lapsed(now, role.name, entity_id, now_read)
                    self.ledger.release(entity_id)
                    self.renewal_owned.pop(role.name, None)
                    continue
            if self._probing(inputs) and role.name in self.control.watchdog_roles and claim.owned is not None:
                step = None if read is None else _tolerance(role, read)
                if now_read is not None and same(claim.owned, now_read, step) is False:
                    fallback = self._fallback_values().get(role.name)
                    if fallback is not None and same(fallback, now_read, step):
                        # The device's own documented fallback.
                        expiring = True
                    elif role.name not in self.diverged:
                        # Changed to something else in the silence: another writer, never the expiry. Left to the
                        # ledger's dwell as any outside change; the test ends on it.
                        self.diverged = (*self.diverged, role.name)
                        events.append(_event(now, "diverged", role.name, entity_id=entity_id, read=now_read))
            if (
                self.control.command_role is not None and role.name in self.control.mode_limits
                and role.name not in self.state.given_up and claim.owned is not None and now_read is not None
                and role.name in self.state.target and same(claim.owned, now_read, claim.step) is False
                and same(self.state.target[role.name], now_read, claim.step)
            ):
                # The command's own call set this limit to the value the action wants (Solis's force carries its
                # power and timeout): SpotNav's own write, owned again.
                self.ledger.intend(entity_id, self.state.target[role.name], now, step=claim.step)
            event = self.ledger.observe(entity_id, now_read, now, expiring=expiring)
            if event is None:
                continue
            if event.kind == "expired":
                # The device's own fallback while the test renews nothing: not written again for the test's span.
                self.expired = (*self.expired, role.name)
                events.append(_event(now, "expired", role.name, entity_id=entity_id, read=event.read))
            elif event.kind == "confirmed":
                if self.state.pending == role.name or role.name in self.state.in_flight:
                    retries = {name: row for name, row in self.state.retries.items() if name != role.name}
                    self.state = replace(
                        self.state, pending=None if self.state.pending == role.name else self.state.pending,
                        in_flight=_without(self.state.in_flight, role.name), retries=retries,
                    )
                if self.state.phase == "restoring" and role.name not in self.state.confirmed:
                    self.state = replace(self.state, confirmed=(*self.state.confirmed, role.name))
                events.append(_event(now, "confirmed", role.name, entity_id=entity_id, read=event.read))
            elif event.kind == "unconfirmed":
                if self.state.pending == role.name:
                    self.state = replace(self.state, pending=None)
                self.state = replace(self.state, in_flight=_without(self.state.in_flight, role.name))
                if role.name not in self.state.target or (
                    self.state.phase == "restoring" and event.intended != self.state.target.get(role.name)
                ):
                    # A write made for the action, still awaited when the hand-back began: no restore to retry,
                    # the role is judged afresh against its restore value. Likewise one for a role the action no
                    # longer sets.
                    events.append(_event(now, "not_taken", role.name, why="superseded", read=event.read))
                elif self._silent_since(inputs, claim.at):
                    events += self._silent_not_taken(now, role.name, "no_read_back", read=event.read)
                else:
                    events += self._not_taken(now, role.name, "no_read_back", read=event.read)
            else:
                events += self._gave_up(now, role.name, entity_id, event.read)
        if self.state.phase == "active" and self._export:
            # The export limit is judged on the site's grid power: the export must stop.
            grid = inputs.grid_import_kw
            fresh = inputs.measurement_ok and grid is not None and math.isfinite(grid)
            judged = self.monitor.observe(
                Reading(grid, 0.0 if fresh else None, excused_kw=inputs.excused_import_kw), Capability(), now
            )
            if judged is not None and judged.kind == "failure":
                events.append(_event(now, "monitor", judged.outcome, detail=judged.as_dict()))
                events += self._begin_restore(now, f"not_obeyed:{self.state.action}:{judged.outcome}")
        elif self.state.phase == "active" and self.state.phase_kw is not None:
            events += self._judge_phases(inputs)
        elif self.state.phase == "active" and not self._probing(inputs):
            # The expiry test judges the effect itself: a fallback is what it looks for, not a command not obeyed.
            facts = inputs.facts
            capability = Capability(
                rated_charge_kw=facts.max_charge_kw,
                rated_discharge_kw=facts.max_discharge_kw,
                soc_min_pct=facts.reserve_soc_pct or 0.0,
                site_cap_kw=(
                    inputs.house_deficit_kw if self.state.action == ACTION_DISCHARGE_HOUSE else self._house_cap(inputs)
                ),
            )
            # A hold is judged on the grid's power too; a grid charge only shown to act with it (`monitor.proves`).
            grid = (
                inputs.grid_import_kw
                if self.state.action in (ACTION_HOLD, ACTION_GRID_CHARGE) and inputs.measurement_ok else None
            )
            judged = self.monitor.observe(
                Reading(facts.power_kw, inputs.battery_age_s, facts.soc_pct, grid), capability, now
            )
            if judged is not None and judged.kind == "proved":
                # The command was seen to act (`monitor.proves`): the shell may count it for the plant's report.
                events.append(_event(
                    now, "proved", self.state.action or "", variant=self.control.variant, detail=judged.as_dict()
                ))
            if judged is not None and judged.kind == "failure":
                events.append(_event(now, "monitor", judged.outcome, detail=judged.as_dict()))
                if judged.outcome == "no_effect":
                    events += self._begin_restore(now, f"no_effect:{self.state.action}")
        return events

    def _grid_kind(self) -> bool:
        """Whether the control's targets per phase are the grid's (`PhaseControl.target_kind`)."""
        spec = self.control.phase_control
        return spec is not None and spec.target_kind == "grid"

    def _phase_measured(self, inputs: ExecInputs) -> Phases | None:
        """What a phase taking more than its target is judged on, as power put out on each phase (kW): the plant's
        own power, or for a recipe whose targets are the grid's the power at the target's point (negated: + from
        the grid is a draw), the site meter's phase where the recipe has no sensor at the point."""
        if self._grid_kind():
            point = inputs.phase_point_kw if _finite3(inputs.phase_point_kw) else inputs.grid_phase_kw
            return (-point[0], -point[1], -point[2]) if _finite3(point) and point is not None else None
        return inputs.plant_phase_kw if _finite3(inputs.plant_phase_kw) else None

    def _phase_aims(self, phases: Phases, behind: Phases | None) -> Phases:
        """What each phase may take where `_phase_measured` reads it (kW): its charge, or for a `grid` target the
        power at the point, the load behind it plus the charge."""
        if not self._grid_kind() or behind is None:
            return phases
        return (phases[0] + behind[0], phases[1] + behind[1], phases[2] + behind[2])

    def _behind(self, inputs: ExecInputs) -> Phases | None:
        """The load behind each phase's point now (kW), for a `grid` target; `None` for a plant target."""
        if not self._grid_kind():
            return None
        behind = inputs.phase_behind_kw
        assert behind is not None and _finite3(behind)  # `_aim` refuses a charge per phase without it
        return (round(behind[0], 2), round(behind[1], 2), round(behind[2], 2))

    def _house_cap(self, inputs: ExecInputs) -> float | None:
        """For an action run by the grid's power (`BrandControl.house_states`): what the battery can take or give of
        its rate now that the house moved since the values were written (the setpoint is not moved with it), for the
        monitor; `None` for any other action or when the house is not known."""
        state = self.state
        if state.action not in self.control.house_states or state.house_kw is None or state.phase_kw is not None:
            return None
        now_kw = house_net_kw(inputs)
        if now_kw is None:
            return None
        moved = now_kw - state.house_kw
        return state.rate_kw - moved if state.action == ACTION_GRID_CHARGE else state.rate_kw + moved

    def _judge_phases(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A running charge per phase against what each phase takes (`monitor.judge_phases`, `_phase_measured`).

        **Over its target** (the runaway): from `PHASE_OVER_GRACE_S` after the charge's first writes were taken,
        every `PHASE_OVER_GAP_S`, each phase against the larger of its target and the one before the last change
        while that change settles (`PHASE_GRACE_S`); a change of the targets does not start the count again.
        **Taking nothing or too little**: after `PHASE_GRACE_S` since the last change, every `PHASE_GAP_S`, not near
        full. `CONFIRM_READINGS` in a row hand the charge back, blocked. Nothing is judged while a write is awaited
        or without the reading; for the verification's probe only the runaway (the rest it judges itself), and in the
        expiry test's silence nothing (`_probing`: a passthru is no charge; the test ends on a real one itself)."""
        state = self.state
        now = inputs.now
        measured = self._phase_measured(inputs)
        changed = state.rate_changed_at
        phases = state.phase_kw
        entered = state.entered_at
        if (
            phases is None or state.priming or state.pending is not None
            or self._next_role(state, inputs) is not None or measured is None or changed is None or entered is None
        ):
            if state.phase_bad:
                self.state = replace(state, phase_bad=())
            return []
        settling = (now - changed).total_seconds() < PHASE_GRACE_S
        if not self._probing(inputs) and (now - entered).total_seconds() >= PHASE_OVER_GRACE_S and (
            state.phase_over_at is None or (now - state.phase_over_at).total_seconds() >= PHASE_OVER_GAP_S
        ):
            aims = self._phase_aims(phases, state.phase_behind_kw)
            was = aims
            if settling and state.phase_was_kw is not None:
                was = self._phase_aims(state.phase_was_kw, state.phase_was_behind_kw or state.phase_behind_kw)
            most = (max(aims[0], was[0]), max(aims[1], was[1]), max(aims[2], was[2]))
            over = judge_phases(most, measured, tapering=True) == "over"
            count = state.phase_over + 1 if over else 0
            self.state = state = replace(state, phase_over=count, phase_over_at=now)
            if count >= CONFIRM_READINGS:
                self.state = replace(state, phase_over=0)
                return [
                    _event(now, "monitor", "phase_over", targets_kw=list(most),
                           plant_kw=[round(value, 3) for value in measured]),
                    *self._begin_restore(now, f"not_obeyed:{ACTION_GRID_CHARGE}:phase_over"),
                ]
        if settling or inputs.phase_probe_kw is not None:
            if state.phase_bad:
                self.state = replace(state, phase_bad=())
            return []
        if state.phase_judged_at is not None and (now - state.phase_judged_at).total_seconds() < PHASE_GAP_S:
            return []
        soc = inputs.facts.soc_pct
        if self._grid_kind():
            # Whether the battery takes its charge is the plant's own power (the load behind the point less the
            # point's), not the point's: a point that only feeds its load reads its target without charging.
            if not _finite3(inputs.plant_phase_kw):
                return []
            assert inputs.plant_phase_kw is not None
            measured = inputs.plant_phase_kw
        verdict = judge_phases(phases, measured, tapering=soc is None or soc >= CHARGE_TAPER_SOC_PCT)
        if verdict in ("ok", "over"):
            # Over is the count above's.
            self.state = replace(state, phase_bad=(), phase_judged_at=now)
            return []
        bad = (*state.phase_bad, verdict)
        if len(bad) < CONFIRM_READINGS:
            self.state = replace(state, phase_bad=bad, phase_judged_at=now)
            return []
        self.state = replace(state, phase_bad=(), phase_judged_at=now)
        outcome = bad[0] if len(set(bad)) == 1 else "partial"
        events = [_event(now, "monitor", f"phase_{outcome}", targets_kw=list(phases),
                         plant_kw=[round(value, 3) for value in measured])]
        if outcome == "no_effect":
            events += self._begin_restore(now, f"no_effect:{ACTION_GRID_CHARGE}")
        return events

    def _gave_up(self, now: datetime, role: str, entity_id: str, read: str | None, **detail: Any) -> list[dict[str, Any]]:
        """A role someone else changed: given up (never fought); an active part hands the rest back. The role that
        holds the whole command (`BrandControl.command_role`) takes the limits that act in it along: another system's
        command runs on them, so the hand-back writes none of them (`left_alone`, logged)."""
        given = (*self.state.given_up, role) if role not in self.state.given_up else self.state.given_up
        events: list[dict[str, Any]] = []
        if role == self.control.command_role:
            left = tuple(name for name in self.control.mode_limits if name not in given)
            given = (*given, *left)
            events.append(_event(now, "left_alone", role, roles=[role, *left]))
        self.state = replace(
            self.state, given_up=given,
            pending=None if self.state.pending in given else self.state.pending,
            in_flight=tuple(name for name in self.state.in_flight if name not in given),
        )
        events.insert(0, _event(now, "external", role, entity_id=entity_id, read=read, **detail))
        if role == self.control.command_role and self.state.phase == "active" and self._own_command_altered(role, read):
            # SpotNav's own command with a value set from outside (the power under its force): never fought. It stays
            # SpotNav's to end only for the fuse (`_begin_restore`); otherwise the device's own expiry ends it. The
            # action still runs, writing nothing, until the plan or a rule hands back.
            self.state = replace(self.state, altered=role)
            events.append(_event(now, "altered", role, read=read))
            return events
        if self.state.phase == "active":
            events += self._begin_restore(now, f"changed_outside_spotnav:{role}")
        return events

    def _foreign(self, role: Role, claim: Any, now_read: str | None) -> bool:
        """Whether the command role reads another system's command (`BrandControl.command_role`): a value that is none
        of SpotNav's for it: the one owned (also the one owned before the write awaited now), the one written or
        awaited, the one the action wants now, the one found, or the device's documented fallback. Every write to
        the command's limits goes before or with the command (`mode_limits`, the command's own call), so SpotNav's
        own change of power reads as the value it wants, and its own call taken only in part as the value written
        before it."""
        if now_read is None or role.name in self.state.given_up:
            return False
        state = self.state
        known = {
            claim.intended, claim.owned, self.renewal_owned.get(role.name), state.target.get(role.name),
            state.wrote.get(role.name), state.restore_values.get(role.name), self._fallback_values().get(role.name),
        }
        return now_read not in known

    def _command_unread(self, inputs: ExecInputs) -> bool:
        """Whether a hand-back waits for a first reading of the command role (`BrandControl.command_role`) before it
        writes anything: after a restart the integration may not have read the plant yet, and a command found then
        may be another system's (`_foreign` judges it once it reads). Never for the fuse. Bounded by the command's
        own expiry window (SpotNav's own command has ended by then): past it, the command and its limits are left as
        they are (`left_alone`), nothing written blind."""
        state = self.state
        name = self.control.command_role
        if (
            name is None or name in state.given_up or name not in state.target or state.reason in _FUSE_REASONS
        ):
            self.command_wait_since = None
            return False
        read = inputs.reads.get(name)
        if reading(self.control.role(name), None if read is None else read.state) is not None:
            self.command_wait_since = None
            return False
        since = self.command_wait_since or inputs.now
        self.command_wait_since = since
        if (inputs.now - since).total_seconds() < (self.control.expiry.window_s or 0.0):
            return True
        left = tuple(item for item in (name, *self.control.mode_limits) if item not in state.given_up)
        self.state = replace(state, given_up=(*state.given_up, *left))
        self.command_wait_since = None
        return False

    def _own_command_altered(self, name: str, now_read: str | None) -> bool:
        """Whether a foreign reading of the command role is SpotNav's own command with another value (its kind, the
        text before `:`, is the one SpotNav owns: Solis's 43135 still SpotNav's, only the power moved from outside)."""
        claim_owned = self.renewal_owned.get(name) or self.state.wrote.get(name)
        return (
            now_read is not None and claim_owned is not None
            and str(now_read).partition(":")[0] == str(claim_owned).partition(":")[0]
            and str(now_read).partition(":")[0] != str(self._fallback_values().get(name)).partition(":")[0]
        )

    def _lapsed(self, now: datetime, role: str, entity_id: str, read: str | None) -> list[dict[str, Any]]:
        """The command lapsed by itself (`BrandControl.command_role` read at its documented fallback while it runs):
        handed back and started again after the re-arm, unless it lapsed `LAPSES_A_DAY` times today."""
        lapses = self.state.lapses_today + 1
        self.state = replace(
            self.state, lapses_today=lapses, pending=None if self.state.pending == role else self.state.pending,
            in_flight=_without(self.state.in_flight, role),
        )
        events = [_event(now, "lapsed", role, entity_id=entity_id, read=read, lapses_today=lapses)]
        why = "lapsed_repeatedly" if lapses >= LAPSES_A_DAY else "lapsed"
        return events + self._begin_restore(now, f"{why}:{role}")

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
            elif self._restores_own_only(state, name):
                # No claim (the ledger lost or released it): SpotNav's own value or the one found; anything else
                # someone else set meanwhile, never written back over (`BrandControl.restore_own_only`).
                now_read = reading(role, read.state)
                ours = (state.wrote.get(name), state.restore_values.get(name))
                if now_read is None or any(
                    value is not None and same(value, now_read, _tolerance(role, read)) for value in ours
                ):
                    break
                events += self._gave_up(inputs.now, name, entity_id, now_read, why="changed_while_not_owned")
                continue
            else:
                break
            if expected is None:
                break
            now_read = reading(role, read.state)
            if same(expected, now_read, _tolerance(role, read)) is not False:
                break
            if name == self.control.command_role and claim is not None and not self._foreign(role, claim, now_read):
                # The command reads one of SpotNav's own values (its own change of power, written before the command's
                # call): no one else's.
                break
            if claim is not None:
                self.ledger.give_up(entity_id, now_read, inputs.now, state.target.get(name, expected))
            events += self._gave_up(
                inputs.now, name, entity_id, now_read, expected=expected, why="changed_before_write"
            )
        return events

    # --- leaving ---

    def _action_problem(self, inputs: ExecInputs, action: str) -> str | None:
        """Why the brand cannot run this action now (an AC charge policy that forbids grid charging)."""
        if not self.control.supports(action):
            return f"action_not_supported:{action}"
        for act, name, forbidden, why in self.control.action_requires:
            if act == action and forbidden_read(self.person_state(name, inputs.reads.get(name)), forbidden):
                return why
        return None

    def person_state(self, name: str, read: RoleRead | None) -> str | None:
        """A role's state as the person set it: the value found at enter while SpotNav's own write stands in it (a
        Victron hold writes the discharge power 0, which must not read as the person's "never discharge"), else
        the state now."""
        state = self.state
        if state.phase != "idle" and name in state.written and name in state.snapshot:
            return state.snapshot[name]
        return None if read is None else read.state

    def told_problems(self, reads: Mapping[str, RoleRead]) -> tuple[str, ...]:
        """The person's own settings that keep an action from ever running (`commands.TOLD_ACTION_REASONS`), and a
        sale left standing in a setpoint SpotNav holds no snapshot of (`SETPOINT_SELLING`), for the status."""
        found: list[str] = []
        for _act, name, forbidden, why in self.control.action_requires:
            read = reads.get(name)
            if why not in TOLD_ACTION_REASONS or why in found or read is None or not read.found:
                continue
            state = self.person_state(name, read)
            if state is not None and reading(self.control.role(name), state) is not None and forbidden_read(
                state, forbidden
            ):
                found.append(why)
        for role in self.control.roles:
            # A sale left standing (`Role.restored_below`: Victron's grid setpoint far below 0) that SpotNav holds no
            # snapshot of: a hold or a discharge for the house does not write it, so it sells on. Told, never written.
            read = reads.get(role.name)
            if role.restored_below is None or read is None or not read.found or role.name in self.state.snapshot:
                continue
            text = reading(role, read.state)
            try:
                value = None if text is None else float(text)
            except ValueError:
                value = None
            if value is not None and math.isfinite(value) and value < role.restored_below[0]:
                found.append(SETPOINT_SELLING)
        return tuple(found)

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
        if (
            action == ACTION_GRID_CHARGE and self.state.action == ACTION_GRID_CHARGE
            and (self.state.phase_kw is not None) != self._per_phase(inputs)
        ):
            # Between a charge per phase and the brand's own: handed back first, never changed while it runs.
            return "per_phase_changed"
        if facts.on_grid is False:
            return "off_grid"
        if not inputs.measurement_ok:
            return "stale_data"
        # The fuse before the battery's own reading: the headroom is the site meter's, so a plant that is silent or
        # stale is still handed back for the fuse, every role at once (`_fuse_hand_back`), never a role a look.
        if action in _FUSE_HANDED_BACK or self.state.action in _FUSE_HANDED_BACK:
            headroom = (
                inputs.headroom_a if action in _RAISES_IMPORT or self.state.action in _RAISES_IMPORT
                else _import_headroom_a(inputs)
            )
            if headroom is not None and headroom < RESTORE_HEADROOM_A:
                return "fuse_headroom"
        if self._export or _fresh(inputs):
            self.state = replace(self.state, silent_since=None)
        elif not self._ride_out(inputs):
            return "stale_data"
        if inputs.headroom_a is None and not self._export:
            return "headroom_unknown"
        if self._hold_charges(inputs):
            return f"not_obeyed:{ACTION_HOLD}:charging"
        if self._charge_over(inputs):
            return f"not_obeyed:{ACTION_GRID_CHARGE}:over"
        # The battery's own instance runs the Remote EMS it turned on; the export instance only while the battery's
        # runs it (`sibling_active`): one taken by another controller (the app, a VPP) ends the curtail.
        management = management_problem(
            facts, self.control, active=inputs.sibling_active if self._export else True, takeover=inputs.takeover
        )
        if management == "unknown":
            since = self.state.management_unknown_since or inputs.now
            self.state = replace(self.state, management_unknown_since=since)
            grace = MANAGEMENT_GRACE_S if self.state.silent_since is None else SILENCE_GRACE_S
            if (inputs.now - since).total_seconds() >= grace:
                # No evidence of another owner: handed back as stale data is, the next start re-arms.
                return "management_unknown"
        elif management is not None:
            return f"management_changed:{management}"
        else:
            self.state = replace(self.state, management_unknown_since=None)
        if _writes_today(self.state, inputs) >= self._daily_cap:
            return "daily_write_cap"
        return self._charge_budget(inputs)

    def _ride_out(self, inputs: ExecInputs) -> bool:
        """Whether a running action may ride out a battery reading that is missing or stale (kept in the state):
        within `SILENCE_GRACE_S`, the plan's action unchanged, and the fuse safe on what is known
        without the battery's reading. A hold keeps `PEAK_COVER_A` and is not lifted for a peak; a balanced grid
        charge fits under the cap counting none of its own charge back (the least it can be); a sale keeps the rate
        the plan allows now (its export cap); a charge per phase is not ridden out. Judged at every look on the
        site meter's headroom now. Meanwhile nothing is written (`step`)."""
        state = self.state
        now = inputs.now
        since = state.silent_since or now
        self.state = replace(state, silent_since=since)
        if (now - since).total_seconds() >= SILENCE_GRACE_S or inputs.action != state.action:
            return False
        if state.phase_kw is not None or inputs.headroom_a is None:
            return False
        if state.action == ACTION_HOLD:
            return state.covered_at is None and inputs.headroom_a >= PEAK_COVER_A
        if state.action == ACTION_GRID_CHARGE:
            blind = replace(inputs, facts=replace(inputs.facts, power_kw=None))
            cap = grid_charge_cap_kw(blind, state.rate_kw, yield_now=False)
            return cap is not None and cap >= state.rate_kw - RATE_TOLERANCE_KW
        if state.action == ACTION_EXPORT_DISCHARGE:
            # The export side's fuse rule is the rate the plan gives each look (`gate.export_discharge_cap_kw`), and
            # no rate is written meanwhile: a sale asked to go lower is handed back instead.
            return abs(inputs.rate_kw) >= state.rate_kw - RATE_TOLERANCE_KW
        return True

    def _silent_since(self, inputs: ExecInputs, written_at: datetime | None) -> bool:
        """Whether a running action's plant has said nothing since `written_at` (or is silent now, `None`): its
        battery reading is missing, or (for a write) not reported since the write was made, or stale."""
        if self.state.phase != "active" or self._export:
            return False
        age = inputs.battery_age_s
        if inputs.facts.power_kw is None or age is None or not _fresh(inputs):
            return True
        return written_at is not None and age >= (inputs.now - written_at).total_seconds()

    def _silent_not_taken(self, now: datetime, role: str, why: str, **detail: Any) -> list[dict[str, Any]]:
        """A write not taken while the plant was silent: handed back as stale data is (no block, the next start
        re-arms), never repeated, and no finding against the control or its verification."""
        return [_event(now, "not_taken", role, why=why, silent=True, **detail), *self._begin_restore(now, "stale_data")]

    def _riding_out(self) -> bool:
        return self.state.phase == "active" and self.state.silent_since is not None

    def _setpoints_spent(self) -> bool:
        return self.state.setpoint_writes_today >= self.control.write_cap("setpoint")

    def _rate_is_setpoint(self, action: str | None) -> bool:
        """Whether the brand's own command for the action carries its rate in a setpoint (a battery power target)."""
        build = self.control.states.get(action or "")
        return build is not None and any(
            want.value != 0.0 and self.control.role(want.role).write_class == "setpoint"
            for want in build(1.0, self.watchdog_s)
        )

    def _charge_budget(self, inputs: ExecInputs) -> str | None:
        """Why an action may not run (on) for the day's writes: a charge per phase, or an action whose rate is a
        setpoint, once the setpoints' cap is reached (the runaway guard), and a grid charge of either kind once only
        `CHARGE_WRITE_RESERVE` of the settings' writes are left (a hold must still be able to start)."""
        if self._export:
            return None
        if inputs.phase_probe_kw is None and self._setpoints_spent() and (
            self._per_phase(inputs) or self._rate_is_setpoint(inputs.action)
        ):
            return "setpoint_write_cap"
        if inputs.action != ACTION_GRID_CHARGE:
            return None
        if _writes_today(self.state, inputs) >= self._daily_cap - self._charge_reserve:
            return "charge_write_budget"
        return None

    def _hold_charges(self, inputs: ExecInputs) -> bool:
        """Whether a running idle hold's battery has charged for `HOLD_CHARGE_S` (kept in the state), or one under a
        recipe's grid guard (`BrandControl.hold_grid_guard`) has charged from the grid that long: the lesser of its
        charge and the site's import, so a sun it stores does not count. Not while a peak cover has the plant back in
        its own mode."""
        state = self.state
        power = inputs.facts.power_kw
        grid = inputs.grid_import_kw if inputs.measurement_ok else None
        from_grid = (
            (self.control.hold_grid_guard or self.control.hold_surplus is not None)
            and power is not None and grid is not None and math.isfinite(grid)
            and min(power, grid) > HOLD_CHARGE_KW
        )
        charging = (
            not self._export and state.action == ACTION_HOLD and state.covered_at is None
            and power is not None and math.isfinite(power) and power > HOLD_CHARGE_KW
            and (self.control.hold_idle() or from_grid)
        )
        if not charging:
            if state.charging_since is not None:
                self.state = replace(state, charging_since=None)
            return False
        since = state.charging_since or inputs.now
        if state.charging_since is None:
            self.state = replace(state, charging_since=since)
        return (inputs.now - since).total_seconds() >= HOLD_CHARGE_S

    def _charge_over(self, inputs: ExecInputs) -> bool:
        """Whether a running balanced grid charge has taken more from the grid than commanded for `CHARGE_OVER_S`
        (kept in the state): the lesser of the battery's charge and the site's import (so a sun it stores does not
        count) above the rate by `CHARGE_OVER_KW` or a quarter of it. Judged only once its writes are read back and
        `CHARGE_OVER_GRACE_S` (or the recipe's read-back wait) after the rate last changed. A charge per phase is
        judged per phase."""
        state = self.state
        power = inputs.facts.power_kw
        grid = inputs.grid_import_kw if inputs.measurement_ok else None
        changed = state.rate_changed_at
        grace = max(CHARGE_OVER_GRACE_S, self.control.readback_s or 0.0)
        over = (
            not self._export and state.action == ACTION_GRID_CHARGE and state.phase_kw is None
            and changed is not None and (inputs.now - changed).total_seconds() >= grace
            and state.pending is None and self._next_role(state, inputs) is None
            and power is not None and grid is not None and math.isfinite(power) and math.isfinite(grid)
            and min(power, grid) > state.rate_kw + max(CHARGE_OVER_KW, 0.25 * state.rate_kw)
        )
        if not over:
            if state.over_since is not None:
                self.state = replace(state, over_since=None)
            return False
        since = state.over_since or inputs.now
        if state.over_since is None:
            self.state = replace(state, over_since=since)
        return (inputs.now - since).total_seconds() >= CHARGE_OVER_S

    # --- entering ---

    def _wanted(
        self, action: str, rate_kw: float, restore_values: Mapping[str, Native], phases: Phases | None = None, *,
        house_kw: float | None = None, behind: Phases | None = None,
    ) -> dict[str, Native]:
        wanted = self.control.wanted(
            action, rate_kw, self.watchdog_s, phases, house_kw=house_kw, behind_kw=behind,
            soc_pct=self._soc_pct, rated_kw=self._rated_kw, absent=self._absent, capacity_kwh=self._capacity_kwh,
        )
        out = {
            name: (restore_values[name] if value == SNAPSHOT else value)
            for name, value in wanted.items()
            if not (value == SNAPSHOT and name not in restore_values)
        }
        if self.state.priming:
            out |= {name: value for name, value in self.control.prime.items() if name in out}
        return out

    def _aim(self, inputs: ExecInputs, starting: bool) -> tuple[float, Phases | None, str | None]:
        """The rate to run the planned action at, the charge per phase when it runs so (the rate is then their sum),
        and why it cannot run."""
        state = self.state
        if not self._per_phase(inputs):
            if inputs.action in self.control.house_states and house_net_kw(inputs) is None:
                # The brand sets the grid's power: without the house's load now there is nothing to write.
                return 0.0, None, "house_unknown"
            # The rate commanded counts only while the same action runs: another action's rate (a sale's) is no
            # charge the battery runs, and a change into the charge is judged as its start.
            rate, why = self._rate(inputs, 0.0 if starting or state.action != inputs.action else state.rate_kw)
            return rate, None, why
        if self._grid_kind() and not _finite3(inputs.phase_behind_kw):
            return 0.0, None, "phase_load_unknown"
        changed = state.rate_changed_at
        yield_now = (
            starting or state.phase != "active" or changed is None
            or (inputs.now - changed).total_seconds() >= RATE_UP_DWELL_S
        )
        phases, why = phase_charge_kw(
            inputs, None if starting else state.phase_kw, yield_now=yield_now, grid=self._grid_kind()
        )
        if phases is None:
            return 0.0, None, why
        return round(sum(max(value, 0.0) for value in phases), 3), phases, None

    def _rate(self, inputs: ExecInputs, commanded_kw: float) -> tuple[float, str | None]:
        """The rate to run the planned action at, and why it cannot run."""
        facts = inputs.facts
        action = inputs.action
        rate = abs(inputs.rate_kw)
        covered_a = self._change_cover_a(inputs) if action == ACTION_GRID_CHARGE else 0.0
        if action == ACTION_GRID_CHARGE and self.control.fixed_charge:
            # The plant charges at its own rate: it must fit under the fuse cap as it is, never lowered.
            fixed = fixed_rate_kw(self.control, facts)
            if fixed is None or not math.isfinite(fixed):
                return 0.0, "size_unknown"
            cap = grid_charge_cap_kw(inputs, commanded_kw, yield_now=False, covered_a=covered_a)
            if cap is None:
                return 0.0, "headroom_unknown"
            if fixed > cap + RATE_TOLERANCE_KW:
                return 0.0, "fuse_headroom"
            return round(fixed, 3), None
        if action == ACTION_GRID_CHARGE:
            limit = facts.max_charge_kw
            if limit is not None:
                rate = min(rate, limit)
            changed = self.state.rate_changed_at
            yield_now = (
                self.state.phase != "active" or changed is None
                or (inputs.now - changed).total_seconds() >= RATE_UP_DWELL_S
            )
            cap = grid_charge_cap_kw(inputs, commanded_kw, yield_now=yield_now, covered_a=covered_a)
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

    def _change_cover_a(self, inputs: ExecInputs) -> float | Phases:
        """On a start of a grid charge, or a change into one from a running action, what the battery's discharge now
        gives the most loaded phase (`cover_a`; a sale's spread as the sale is): the headroom is measured under that
        cover, which ends with the charge. From idle the plant covers the house in its own mode, which follows the
        house's load on each phase: with the plant's power per phase read, each phase's own cover (`cover_phase_a`),
        else all of it on one phase, as for a hold from idle (`hold_headroom_ok`). With the battery idle or
        charging, the sun the plant puts on a phase above its average over the three is that phase's cover (read
        per phase only; without the reading the sun is taken to stay where it is). The first rate is the lower for it
        and the charge goes up once the cover has ended (`RATE_UP_DWELL_S`). 0 within a running charge (its own charge
        is counted back instead)."""
        state = self.state
        running = state.phase == "active"
        if running and state.action == ACTION_GRID_CHARGE:
            return 0.0
        if running and state.action == ACTION_EXPORT_DISCHARGE:
            return cover_a(inputs, sale=True)
        per_phase = cover_phase_a(inputs)
        return per_phase if per_phase is not None else cover_a(inputs)

    def _hold_charge_problem(self, inputs: ExecInputs) -> str | None:
        """Why a hold that may itself charge from the grid at the plant's fixed rate
        (`BrandControl.hold_charges_without`, a role this plant lacks) cannot start: that rate does not fit in the
        headroom left once the battery stops covering what it discharges now (`size_unknown`, `fuse_headroom`)."""
        if not any(name in self._absent for name in self.control.hold_charges_without):
            return None
        fixed = fixed_rate_kw(self.control, inputs.facts)
        if fixed is None or not math.isfinite(fixed):
            return "size_unknown"
        if inputs.headroom_a is None or inputs.volts_between_phases <= 0:
            return "headroom_unknown"
        power = inputs.facts.power_kw
        covered_a = -power * 1000.0 * math.sqrt(3.0) / inputs.volts_between_phases if power is not None and power < 0 else 0.0
        phases = min(max(inputs.charge_phases, 1), 3)
        room = _kw(inputs.headroom_a - covered_a - HEADROOM_MARGIN_A, inputs.volts_between_phases) * phases / 3
        return "fuse_headroom" if fixed > room + RATE_TOLERANCE_KW else None

    def _sale_headroom_ok(self, inputs: ExecInputs) -> bool:
        """A sale's start rule (from idle or as a change): the hold's, less the sale's own share on each phase at the
        rate it would run (`hold_headroom_ok`)."""
        rate, _why = self._rate(inputs, 0.0)
        return hold_headroom_ok(inputs, rate)

    @staticmethod
    def _sale_step_too_large(inputs: ExecInputs, rate: float) -> bool:
        """Whether a sale stepped down to `rate` fails the sale's start rule on a phase: the step takes back the cover
        the sale gives now beyond its new share (`uncovered_a`, from the running sale), as an end into a hold would,
        and never more off one phase than the step itself (what the battery discharges now less `rate`, all of it on
        one phase): the sun in the plant's power per phase, and a plant not known to spread its sale over three
        phases, are then judged by the step and not by the whole sale.

        Each phase is judged by the site meter's sign. One that exports at least what is taken back only exports
        less. Any other must keep `HEADROOM_MARGIN_A`: its headroom, plus what it exports now (the fuse is at least
        the two together), less what is taken back. With the meter's signs or a phase's headroom not known, the
        least headroom of any phase less what is taken back. Judged only for a step that may take at least
        `HEADROOM_MARGIN_A` off a phase: a smaller one is the fuse hand-back's (`RESTORE_HEADROOM_A`, the next
        look)."""
        power = inputs.facts.power_kw
        if power is None or not math.isfinite(power) or inputs.volts_between_phases <= 0:
            return False
        phase_v = inputs.volts_between_phases / math.sqrt(3.0)
        step_a = max(-power - rate, 0.0) * 1000.0 / phase_v
        taken_a = min(uncovered_a(inputs, rate, from_sale=True), step_a)
        if taken_a < HEADROOM_MARGIN_A:
            return False
        signed, rooms = inputs.grid_phase_kw, inputs.phase_headroom_a
        if not _finite3(signed) or not _finite3(rooms):
            return inputs.headroom_a is None or inputs.headroom_a - taken_a < HEADROOM_MARGIN_A
        assert signed is not None and rooms is not None
        for grid_kw, room in zip(signed, rooms, strict=True):
            export_a = max(-grid_kw, 0.0) * 1000.0 / phase_v
            if export_a < taken_a and room + export_a - taken_a < HEADROOM_MARGIN_A:
                return True
        return False

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
        management = management_problem(facts, self.control, active=inputs.sibling_active, takeover=inputs.takeover)
        if management is not None:
            return management
        if facts.on_grid is False:
            return "off_grid"
        if not inputs.measurement_ok or (not self._export and not _fresh(inputs)):
            return "stale_data"
        if self._export and (inputs.grid_import_kw is None or not math.isfinite(inputs.grid_import_kw)):
            # Nothing could judge whether the limit is obeyed: the export limit is not taken without the site's
            # signed grid power.
            return "no_grid_reading"
        if inputs.headroom_a is None and not self._export:
            return "headroom_unknown"
        if action == ACTION_HOLD and not hold_headroom_ok(inputs):
            return "headroom_too_small"
        if action == ACTION_EXPORT_DISCHARGE and not self._sale_headroom_ok(inputs):
            return "headroom_too_small"
        if action == ACTION_HOLD:
            self._sense(inputs)
            problem = self._hold_charge_problem(inputs)
            if problem is not None:
                return problem
        if action == ACTION_GRID_CHARGE and inputs.headroom_a < HEADROOM_MARGIN_A:
            return "headroom_too_small"
        if action == ACTION_GRID_CHARGE and inputs.phase_probe_kw is not None and not hold_headroom_ok(inputs):
            # The verification's probe leaves two phases' house on the grid, as a hold does: the hold's start rule.
            return "headroom_too_small"
        if _writes_today(state, inputs) >= self._daily_cap:
            return "daily_write_cap"
        if self.control.renewal_cap is not None and state.renewals_today >= self.control.renewal_cap:
            return "renewal_cap"
        budget = self._charge_budget(inputs)
        if budget is not None:
            return budget
        rearm = REARM_S if self.control.rearm_s is None else max(REARM_S, self.control.rearm_s)
        if state.released_at is not None and (inputs.now - state.released_at).total_seconds() < rearm:
            return "rearm"
        for role, value, why in self.control.watched:
            read = inputs.reads.get(role.name)
            if read is not None and read.found and read.enabled and same(
                value, reading(role, read.state), _tolerance(role, read)
            ) is False:
                # Another control's target left standing would act under this one.
                return f"{why}:{role.name}"
        self._sense(inputs)
        wanted = self.control.wanted(
            action, 1.0, self.watchdog_s, soc_pct=self._soc_pct, rated_kw=self._rated_kw, absent=self._absent,
            capacity_kwh=self._capacity_kwh,
        )
        unresolved = self.control.unresolved(wanted)
        if unresolved is not None:
            return unresolved
        needed = set(wanted) | {name for name, _ok, _why in self.control.requires}
        for name in sorted(needed):
            read = inputs.reads.get(name)
            role = self.control.role(name)
            if read is None or not read.found or read.entity_id is None:
                return "needs_hacs_integration" if name in self.control.hacs_roles else f"control_not_found:{name}"
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
            quiet = blocker in ("nothing_to_do", "rearm", *_BUDGET_REASONS)
            return False, ([] if quiet else [_event(now, "not_started", blocker)])
        rate, phases, why = self._aim(inputs, True)
        if why is not None:
            return False, [_event(now, "not_started", why)]
        snapshot: dict[str, str] = {}
        restore_values: dict[str, Native] = {}
        replaced: dict[str, dict[str, Any]] = {}
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
            if role.restored_below is not None and value == role.restored_below[1] and _restore_value(
                replace(role, restored_below=None), read.state, inputs.facts
            ) != value:
                replaced[role.name] = {"found": read.state, "restored_as": value}
        action = inputs.action
        assert action is not None
        house, behind = self._grid_terms(inputs, action, phases)
        self.state = replace(
            self.state, priming=bool(self.state.unsure and self.control.prime), wrote={}, settle={},
            settle_until=None, settle_misses=0,
        )
        self.state = replace(
            self.state, phase="active", brand=self.control.brand, action=action, rate_kw=rate,
            target=self._wanted(action, rate, restore_values, phases, house_kw=house, behind=behind),
            snapshot=snapshot, house_kw=house, phase_behind_kw=behind, phase_was_behind_kw=None,
            restore_values=restore_values,
            entities=entities, device_id=device_id, snapshot_at=now, entered_at=now, rate_changed_at=now,
            watchdog_at=now, sent=(), renew=(), reason=None, given_up=(), management_unknown_since=None, written=(),
            covered_at=None, peak_low_since=None, lead=(), phase_kw=phases, phase_bad=(), phase_judged_at=None,
            phase_was_kw=None, phase_raised_at=None, phase_over=0, phase_over_at=None,
        )
        self._watch(inputs, action, rate)
        detail: dict[str, Any] = {} if phases is None else {"phase_kw": list(phases)}
        events = [_event(now, "enter", action, rate_kw=rate, snapshot=snapshot, **detail)]
        # A value found that is handed back as something else (`Role.restored_below`): said in the change log.
        events += [_event(now, "restore_replaced", name, **row) for name, row in replaced.items()]
        return True, events

    def _grid_terms(
        self, inputs: ExecInputs, action: str, phases: Phases | None
    ) -> tuple[float | None, Phases | None]:
        """What a brand that sets the grid's power is given beside the rate: the house's net load now for an action
        of `BrandControl.house_states` (not for a charge per phase), and the load behind each phase's point for a
        `grid` target per phase. `None` each for a brand that sets the battery's."""
        if phases is not None:
            return None, self._behind(inputs)
        return (house_net_kw(inputs) if action in self.control.house_states else None), None

    def _lead(
        self, previous: str | None, before: Mapping[str, Native] | None = None,
        after: Mapping[str, Native] | None = None,
    ) -> tuple[str, ...]:
        """The roles a change of action away from `previous` writes first: a grid charge's stop (its `peak_first`
        roles; Sigenergy: the control mode), so a limit the next action loosens never acts in the grid charge's mode
        (the charge limit back at the rating in Command Charging would charge from the grid at full power). A button
        is never moved ahead of the values it sends. Where the recipe names its mode limits (`mode_limits`), the
        order is the whole of it: those that fall from `before` to `after` first, then the stop and the brand's
        order, then those that rise."""
        stop: tuple[str, ...] = ()
        if previous == ACTION_GRID_CHARGE:
            first = self.control.peak_first.get(previous, ())
            stop = tuple(name for name in first if self.control.role(name).method != "button")
        if not self.control.mode_limits or before is None or after is None:
            return stop

        def moved(name: str) -> float:
            old, new = before.get(name), after.get(name)
            if any(isinstance(value, bool) or not isinstance(value, int | float) for value in (old, new)):
                return 0.0
            return float(new) - float(old)  # type: ignore[arg-type]

        limits = [name for name in self.control.mode_limits if name in after]
        falling = tuple(name for name in limits if moved(name) < 0)
        rising = tuple(name for name in limits if moved(name) > 0)
        middle = tuple(dict.fromkeys(
            name for name in (*stop, *self.control.enter_order) if name not in falling and name not in rising
        ))
        return (*falling, *middle, *rising)

    def _watch(self, inputs: ExecInputs, action: str, rate: float) -> None:
        """The monitor watches the new command; a discharge for the house only when the house's deficit is known."""
        kind = _MONITORED.get(action)
        if (
            kind is None or (action == ACTION_DISCHARGE_HOUSE and inputs.house_deficit_kw is None)
            # A charge per phase is judged per phase, on the plant's own power (`_judge_phases`).
            or self.state.phase_kw is not None
        ):
            self.monitor.stop(inputs.now)
            return
        if action == ACTION_DISCHARGE_HOUSE:
            rate = max(inputs.house_deficit_kw or 0.0, 0.0)
        elif action == ACTION_GRID_CHARGE and self.control.fixed_charge:
            rate = FIXED_CHARGE_JUDGE_KW
        before = inputs.grid_import_kw if kind == "curtail" else inputs.facts.power_kw
        idle = kind == "hold" and self.control.hold_idle()
        self.monitor.start(Command(kind, inputs.now, rate, before_kw=before, written=True, idle=idle))

    def _end_priming(self, inputs: ExecInputs, action: str) -> list[dict[str, Any]]:
        """A priming action (`BrandControl.prime`) whose writes are all read back takes its own values."""
        state = self.state
        if not (state.priming and state.pending is None and self._next_role(state, inputs) is None):
            return []
        # Primed: the switch is on and the role that was unsure reads what this action wrote; now its own values.
        self.state = state = replace(state, priming=False, unsure=())
        self.state = state = replace(
            state, sent=(),
            target=self._wanted(
                state.action or action, state.rate_kw, state.restore_values, state.phase_kw,
                house_kw=state.house_kw, behind=state.phase_behind_kw,
            ),
        )
        return [_event(inputs.now, "primed", state.action or action)]

    def _retarget(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A new action, a new rate past the hysteresis, or a watchdog to renew."""
        now = inputs.now
        state = self.state
        action = inputs.action
        assert action is not None
        rate, phases, why = self._aim(inputs, False)
        if why is not None:
            return self._begin_restore(now, why)
        if self._probing(inputs):
            # The expiry test: the command stands as written, nothing renews it and nothing changes it.
            if state.renew:
                self.state = state = replace(state, renew=())
            return self._silence(now)
        events = self._end_priming(inputs, action)
        state = self.state
        if action != state.action and self.control.slow and state.rate_changed_at is not None and (
            (now - state.rate_changed_at).total_seconds() < self.control.min_change_s
        ):
            # A slow control (a cloud) changes its action no sooner than its own minimum: the plan's next look asks again.
            return events
        sale = state.action == ACTION_EXPORT_DISCHARGE
        if action != state.action and state.action != ACTION_GRID_CHARGE and (
            (action == ACTION_HOLD and not hold_headroom_ok(inputs, from_sale=sale))
            or (action == ACTION_EXPORT_DISCHARGE and not self._sale_headroom_ok(inputs))
        ):
            # A change into a hold or a sale from an action in which the battery may cover the house is held to that
            # action's start rule: the cover the battery gives now must not come off a phase that cannot carry it.
            # Handed back (plain self-consumption) instead. A sale covers the house and the cars behind the meter
            # before it exports, so a sale into a hold is judged too (on a plant known to spread a sale evenly over
            # three phases, a third of it on each: `cover_a`). Only from a grid charge is the change made directly:
            # ending a charge only frees room.
            return events + self._begin_restore(now, "headroom_too_small")
        if action != state.action:
            house, behind = self._grid_terms(inputs, action, phases)
            target = self._wanted(action, rate, state.restore_values, phases, house_kw=house, behind=behind)
            unresolved = self.control.unresolved(target)
            if unresolved is not None:
                # A value that depends on a reading gone missing (the rating, the SoC) is never written as it stands.
                return events + self._begin_restore(now, unresolved)
            # The renewal counts from this change, so a watchdog role the new action leaves as it was (SolarEdge's
            # timeout) is written again with the new command: with an action change every quarter hour it would
            # otherwise never be renewed after the start. The roles it changes are written anyway, and counted; so is
            # a button, pressed once per target (SolaX's trigger sends the whole new command).
            # At most once a `RENEW_ON_CHANGE_GAP_S` (an action that flips at every look would otherwise renew at
            # every look, uncounted): a change sooner keeps the last renewal's time, so the shared timer still
            # renews `WATCHDOG_RENEW_S` after it.
            unchanged = () if self._fast_renewal() else tuple(
                name for name in self.control.watchdog_roles
                if name in target and state.target.get(name) == target[name]
                and self.control.role(name).method != "button"
            )
            due = state.watchdog_at is None or (now - state.watchdog_at).total_seconds() >= RENEW_ON_CHANGE_GAP_S
            # Within the gap, a renewal the timer already owed (and `watchdog_at` records as done) is kept.
            renew = unchanged if due else tuple(name for name in state.renew if name in unchanged)
            watchdog_at = state.watchdog_at if unchanged and not due else now
            self.state = replace(
                state, action=action, rate_kw=rate,
                target=target,
                house_kw=house, phase_behind_kw=behind, phase_was_behind_kw=None,
                sent=(), renew=renew, rate_changed_at=now, watchdog_at=watchdog_at, covered_at=None,
                peak_low_since=None,
                lead=self._lead(state.action, state.target, target), phase_kw=phases, phase_bad=(),
                phase_judged_at=None, phase_was_kw=None, phase_raised_at=None, phase_over=0, phase_over_at=None,
                # The runaway's grace counts from the charge's own start.
                entered_at=now if phases is not None else state.entered_at,
            )
            self._release_unset()
            self._watch(inputs, action, rate)
            detail: dict[str, Any] = {} if phases is None else {"phase_kw": list(phases)}
            return [_event(now, "change", action, rate_kw=rate, **detail)]
        if phases is not None and state.phase_kw is not None:
            events += self._retarget_phases(inputs, rate, phases)
        elif action in (ACTION_GRID_CHARGE, ACTION_EXPORT_DISCHARGE) and not (
            action == ACTION_GRID_CHARGE and self.control.fixed_charge
        ):
            since = (now - (state.rate_changed_at or now)).total_seconds()
            down = rate < state.rate_kw - RATE_TOLERANCE_KW
            up = rate >= state.rate_kw + RATE_STEP_KW and since >= RATE_UP_DWELL_S
            if down and action == ACTION_EXPORT_DISCHARGE and self._sale_step_too_large(inputs, rate):
                return events + self._begin_restore(now, "headroom_too_small")
            if down or up:
                house, _behind = self._grid_terms(inputs, action, None)
                target = self._wanted(action, rate, state.restore_values, house_kw=house)
                unresolved = self.control.unresolved(target)
                if unresolved is not None:
                    return events + self._begin_restore(now, unresolved)
                self.state = replace(
                    state, rate_kw=rate, target=target, sent=(), rate_changed_at=now, house_kw=house,
                )
                self._watch(inputs, action, rate)
                events.append(_event(now, "rate", action, rate_kw=rate, was_kw=state.rate_kw))
        events += self._follow_surplus(inputs)
        events += self._renew_block(inputs)
        state = self.state
        if not self._fast_renewal():
            # A watchdog on the shared `WATCHDOG_RENEW_S` (SolarEdge, Huawei, SolaX): renewed as it always was, every
            # role whatever is still owed, a round a look per role.
            if (
                self.control.watchdog_roles and state.watchdog_at is not None and state.pending is None
                and (now - state.watchdog_at).total_seconds() >= WATCHDOG_RENEW_S
            ):
                self.state = replace(state, renew=self.control.watchdog_roles, watchdog_at=now)
                events.append(_event(now, "renew", ",".join(self.control.watchdog_roles)))
            return events
        # A fast watchdog (`BrandControl.watchdog_renew_s`): only what the running action sets is renewed (a
        # watchdog of the charge per phase has nothing to keep alive under a hold).
        # A renewed role the action leaves at its value found holds no command (Solis's force select back at "None",
        # in the inverter's own Self-Use or under a hold's peak cover): nothing to keep alive, so not written again.
        renewed = tuple(
            name for name in self.control.watchdog_roles
            if name in state.target and name not in state.given_up and not (
                name in state.restore_values and state.target[name] == state.restore_values[name]
            )
        )
        if (
            renewed and state.watchdog_at is not None and state.pending is None
            and (now - state.watchdog_at).total_seconds() >= (self.control.watchdog_renew_s or WATCHDOG_RENEW_S)
            # One round at a time, and never ahead of a write the action still owes: a round begun before the last
            # one ended would never reach its last role.
            and not state.renew and not state.in_flight and self._next_role(state, inputs) is None
        ):
            cap = self.control.renewal_cap
            if cap is not None and state.renewals_today >= cap:
                # The day's renewals are spent: handed back (the command then ends as found), none started again today.
                return events + self._begin_restore(now, "renewal_cap")
            self.state = replace(state, renew=renewed, watchdog_at=now, renewals_today=state.renewals_today + 1)
            events.append(_event(now, "renew", ",".join(renewed)))
        return events

    def _follow_surplus(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A running hold that follows a sun surplus (`BrandControl.hold_surplus`): its charge limit set to the
        surplus now, past the deadband (see the module)."""
        follow = self.control.hold_surplus
        state = self.state
        if (
            follow is None or state.action != ACTION_HOLD or inputs.action != ACTION_HOLD
            or state.covered_at is not None or state.priming
            # Only once the hold's own writes are in: its limit at 0 first, then Remote EMS and the mode.
            or state.pending is not None or self._next_role(state, inputs) is not None
        ):
            return []
        standing = state.target.get(follow.role)
        if not isinstance(standing, int | float) or isinstance(standing, bool):
            return []
        now = inputs.now
        net = house_net_kw(inputs) if inputs.measurement_ok else None
        surplus = 0.0 if net is None else max(-net, 0.0)
        rated = inputs.facts.max_charge_kw
        if rated is not None and math.isfinite(rated):
            surplus = min(surplus, rated)
        surplus = min(surplus, max(inputs.hold_charge_cap_kw, 0.0))
        new = math.floor(surplus / follow.step_kw + 1e-9) * follow.step_kw
        if new < follow.deadband_kw or _writes_today(state, inputs) >= self._daily_cap - self._charge_reserve:
            # Too little to follow, or the day's writes are nearly spent: the limit back at 0 and kept there.
            new = 0.0
        new = native(self.control.role(follow.role), new)
        if new == standing or (new != 0.0 and abs(new - standing) < follow.deadband_kw):
            return []
        raised = self._surplus_raised_at
        if new > standing and raised is not None and (now - raised).total_seconds() < follow.raise_gap_s:
            return []
        if new > standing:
            self._surplus_raised_at = now
        self.state = replace(
            state, target={**state.target, follow.role: new},
            sent=tuple(name for name in state.sent if name != follow.role),
        )
        return [_event(now, "surplus", ACTION_HOLD, role=follow.role, kw=new, was_kw=standing)]

    def _renew_block(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A grid charge commanded in blocks (`BrandControl.charge_block_s`): once a block has run, its ceiling is
        set one block ahead of the SoC now (written only where it changed). The block's start is `watchdog_at`."""
        state = self.state
        now = inputs.now
        block = self.control.charge_block_s
        if (
            not block or state.action != ACTION_GRID_CHARGE or inputs.action != ACTION_GRID_CHARGE
            or state.pending is not None or state.watchdog_at is None
            or (now - state.watchdog_at).total_seconds() < block
        ):
            return []
        target = self._wanted(ACTION_GRID_CHARGE, state.rate_kw, state.restore_values, state.phase_kw)
        if self.control.unresolved(target) is not None:
            return []
        self.state = replace(state, target=target, watchdog_at=now)
        changed = {name: value for name, value in target.items() if state.target.get(name) != value}
        return [_event(now, "block", ACTION_GRID_CHARGE, **({"target": changed} if changed else {}))]

    def _retarget_phases(self, inputs: ExecInputs, rate: float, phases: Phases) -> list[dict[str, Any]]:
        """A running charge per phase: a phase whose room fell under its target goes down at once (every phase then
        takes its new value, each within its own room); the charge goes up after `RATE_UP_DWELL_S`, by at least
        `RATE_STEP_KW` in all or back to what the caps allow with the house quiet. The verification's probe is
        written as it changes, a charge in it raised only after the dwell too."""
        state = self.state
        now = inputs.now
        old = state.phase_kw
        assert old is not None
        since = (now - (state.rate_changed_at or now)).total_seconds()
        behind, old_behind = self._behind(inputs), state.phase_behind_kw
        # A `grid` target follows the load behind its point: written again once a target would move enough.
        moved = (
            behind is not None and old_behind is not None and since >= PHASE_TRACK_S
            and any(abs(new - was) > PHASE_TRACK_KW for new, was in zip(behind, old_behind, strict=True))
        )
        if inputs.phase_probe_kw is not None:
            # A charge in it goes up again, as any charge per phase, only `RATE_UP_DWELL_S` after its room took it
            # down (the last change lowered a charge on that phase); and from a discharge only through 0, so that
            # its room is read without the discharge that eased the phase.
            lowered = state.phase_was_kw
            held = []
            for index, (new, was) in enumerate(zip(phases, old, strict=True)):
                if new > 0 and was < 0:
                    new = 0.0
                elif (
                    new > max(was, 0.0) and since < RATE_UP_DWELL_S and lowered is not None
                    and lowered[index] > max(was, 0.0) + 1e-9
                ):
                    new = max(was, 0.0)
                held.append(new)
            phases = (held[0], held[1], held[2])
            raised = state.phase_raised_at
            if raised is not None and (now - raised).total_seconds() < PHASE_RAISE_SETTLE_S:
                # Just raised: the plant's own power does not show it yet, so the room reads too small. A charge
                # the probe itself lowers (the verification's next step) still goes down at once.
                asked = inputs.phase_probe_kw
                kept = [was if 0 <= new < was and new < want - 1e-9 else new
                        for new, was, want in zip(phases, old, asked, strict=True)]
                phases = (kept[0], kept[1], kept[2])
            rate = round(sum(max(value, 0.0) for value in phases), 3)
            change = any(abs(new - was) > 1e-9 for new, was in zip(phases, old, strict=True))
        else:
            gain = sum(phases) - sum(old)
            ceiling, _why = phase_charge_kw(inputs, None, live=False, grid=self._grid_kind())
            full = ceiling is not None and all(
                new >= most - PHASE_STEP_KW - 1e-9 for new, most in zip(phases, ceiling, strict=True)
            )
            down = any(new < was - PHASE_TOLERANCE_KW for new, was in zip(phases, old, strict=True))
            raised = state.phase_raised_at
            if down and raised is not None and (now - raised).total_seconds() < PHASE_RAISE_SETTLE_S:
                # Just raised: the plant's own power does not show it yet, so the room reads too small.
                down = False
            up = since >= RATE_UP_DWELL_S and (gain >= RATE_STEP_KW or (full and gain > PHASE_TOLERANCE_KW))
            change = down or up
        if not change and not moved:
            return []
        if not change:
            # Only the load moved: the charge per phase stays as commanded (never above what the phase allows with
            # the load as it is now), and only the targets whose load moved follow it, so a busy house spends the
            # day's setpoint writes on the phases that need them.
            phases = (min(old[0], phases[0]), min(old[1], phases[1]), min(old[2], phases[2]))
            rate = round(sum(max(value, 0.0) for value in phases), 3)
            assert behind is not None and old_behind is not None
            kept = [new if abs(new - was) > PHASE_TRACK_KW else was for new, was in zip(behind, old_behind, strict=True)]
            behind = (kept[0], kept[1], kept[2])
        spec = self.control.phase_control
        names = () if spec is None else spec.names()
        # The falling phases are written first: no phase is raised while another still draws what it must give up.
        if inputs.phase_probe_kw is not None:
            # The verification's probe is a discharge: the phase that ends is returned to 0 and read back before
            # the next one's is written, so never two phases discharge at once.
            falling = tuple(name for name, new, was in zip(names, phases, old, strict=False) if abs(new) < abs(was))
        else:
            aims, was_aims = self._phase_aims(phases, behind), self._phase_aims(old, old_behind)
            falling = tuple(name for name, new, was in zip(names, aims, was_aims, strict=False) if new < was)
        rising = any(new > was for new, was in zip(phases, old, strict=True))
        self.state = replace(
            state, rate_kw=rate, phase_kw=phases, sent=(), rate_changed_at=now, phase_bad=(), phase_judged_at=None,
            target=self._wanted(ACTION_GRID_CHARGE, rate, state.restore_values, phases, behind=behind), lead=falling,
            phase_was_kw=old, phase_raised_at=now if rising else state.phase_raised_at,
            phase_behind_kw=behind, phase_was_behind_kw=old_behind,
        )
        return [_event(now, "rate", ACTION_GRID_CHARGE, rate_kw=rate, was_kw=state.rate_kw, phase_kw=list(phases),
                       was_phase_kw=list(old))]

    # --- the peak cover ---

    def _covering(self, state: ExecState, name: str) -> bool:
        """Whether `name` is a write of a lifted hold (never held back: the battery must cover the peak)."""
        return state.covered_at is not None and name in self.control.peak_first.get(state.action or "", ())

    def _capped(self, state: ExecState, inputs: ExecInputs, name: str) -> bool:
        """Whether the day's write cap holds this write back (a restore, a peak cover and a watchdog renewal never
        are)."""
        if state.phase == "restoring" or self._covering(state, name) or name in state.renew:
            return False
        if self.control.role(name).write_class == "setpoint":
            # A setpoint written to 0 stops something: never held back.
            return self._setpoints_spent() and state.target.get(name) != 0.0
        return _writes_today(state, inputs) >= self._daily_cap

    def _cover(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """Under a hold: lift it when the headroom falls under `PEAK_COVER_A`, hand back in full when it does not
        recover within `PEAK_RECOVER_S`, hold again after `PEAK_REHOLD_S` when the start rule allows it."""
        state = self.state
        now = inputs.now
        # What the plant may import by itself now comes off the room the peak cover reads.
        headroom = None if inputs.headroom_a is None else inputs.headroom_a - _own_import_a(inputs)
        first = self.control.peak_first.get(ACTION_HOLD, ())
        if self.control.slow or state.action != ACTION_HOLD or inputs.action != ACTION_HOLD or headroom is None or not first:
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
        never_written = role.name not in self.state.written
        if never_written and reading(role, state) is None and (
            restoring or wanted == self.state.restore_values.get(role.name)
        ):
            # Never written and no reading now (an entity that blinks unknown or unavailable): nothing SpotNav did to
            # undo, and no evidence it needs its snapshot written.
            return True
        if (
            snapshot is not None and state == snapshot and wanted == self.state.restore_values.get(role.name)
            and never_written
        ):
            # Never written and still as found (a never-set limit reads "unknown"): nothing to write back. A role
            # SpotNav wrote is restored by writing its snapshot and reading it back, whatever it reads now.
            return True
        return bool(same(wanted, reading(role, state), None if read is None else _tolerance(role, read)))

    def _next_role(self, state: ExecState, inputs: ExecInputs) -> str | None:
        """The first role, in the brand's order, that does not read what it should (or must be renewed)."""
        restoring = state.phase == "restoring"
        retaking = restoring and state.retake
        if retaking:
            # After a silence: the plant taken back in the enter order (the switch before the roles it makes
            # available), no idle role counted as done (`ExecState.retake`).
            order = self.control.enter_order
        elif restoring:
            order = restore_order(self.control, state.action)
        else:
            order = (*state.lead, *(name for name in self.control.enter_order if name not in state.lead))
            if self.control.rising_first:
                # A limit that rises goes ahead of one that falls (after a change's stop): an enabled pair never
                # sums below 0 on the way (`BrandControl.rising_first`).
                rising = tuple(
                    name for name in self.control.rising_first
                    if name not in state.lead and _rises(state.target.get(name), inputs.reads.get(name))
                )
                order = (*state.lead, *rising, *(name for name in order if name not in state.lead and name not in rising))
        # A role whose value must change goes before a role that is only renewed: renewals never hold back a write
        # the action owes (an action that flips at every look must still write its command, and be counted).
        renewals: list[str] = []
        for name in order:
            if name not in state.target or name in state.given_up or name in state.unrestored or name in state.in_flight:
                continue
            if not restoring and name in self.expired and not self._covering(state, name):
                # Expired in the running expiry test: writing it would renew the command the test watches end.
                continue
            retry = state.retries.get(name)
            if retry is not None and inputs.now < retry[1]:
                continue
            entity_id = state.entities.get(name)
            claim = None if entity_id is None else self.ledger.claim(entity_id)
            if claim is not None and claim.owned is not None and claim.intended == state.target[name]:
                # Written and read back: a later difference is the ledger's to judge (another system, or a
                # gap), never a reason to write it again.
                if name in state.renew:
                    renewals.append(name)
                continue
            if not self._satisfied(
                self.control.role(name), state.target[name], inputs.reads.get(name), restoring=restoring and not retaking
            ):
                return name
            if name in state.renew:
                renewals.append(name)
        return renewals[0] if renewals else None

    def _progress(self, inputs: ExecInputs) -> list[Op]:
        state = self.state
        if state.phase not in ("active", "restoring") or state.pending is not None:
            return []
        restoring = state.phase == "restoring"
        if restoring and self._command_unread(inputs):
            return []
        state = self.state
        name = self._next_role(state, inputs)
        if restoring and name is not None and self._restore_unread(state, inputs, name):
            return []
        if name is None or self._capped(state, inputs, name) or (restoring and self._limit_waits(state, inputs, name)):
            return []
        role = self.control.role(name)
        value = state.target[name]
        entity_id = state.entities.get(name)
        read = inputs.reads.get(name)
        renew = tuple(item for item in state.renew if item != name)
        sent = state.sent if name in state.sent else (*state.sent, name)
        # A restore and a watchdog renewal are not counted: the renewal is the brand's dead-man, on a fixed timer
        # (`WATCHDOG_RENEW_S`, at most a few roles), never a change of the wanted state.
        # On a fast watchdog a role in a renewal round whose value changed meanwhile is a change, and is counted;
        # the brands on `WATCHDOG_RENEW_S` count no renewal, as before (their round is a few roles a quarter-hour).
        renewing = name in state.renew and (
            not self._fast_renewal() or self._satisfied(role, value, read, restoring=False)
        )
        counted = 0 if renewing or (restoring and not self.control.count_restores) else 1
        setpoint = role.write_class == "setpoint"
        writes = state.writes_today + (0 if setpoint else counted)
        setpoints = state.setpoint_writes_today + (counted if setpoint else 0)
        written = state.written if name in state.written else (*state.written, name)
        if not restoring and not renewing:
            state = replace(state, wrote=state.wrote | {name: value})
        if role.method == "button":
            # A button has no read-back: pressed once per target; the monitor judges what it did.
            self.state = replace(state, sent=sent, renew=renew, writes_today=writes, written=written,
                                 setpoint_writes_today=setpoints)
        else:
            if entity_id is not None:
                claim = self.ledger.claim(entity_id)
                if name == self.control.command_role and claim is not None and claim.owned is not None:
                    self.renewal_owned[name] = claim.owned
                elif name == self.control.command_role and claim is not None and claim.owned is None:
                    pass  # a write over one still awaited keeps the value owned before it
                else:
                    self.renewal_owned.pop(name, None)
                self.ledger.intend(
                    entity_id, value, inputs.now, step=None if read is None else _tolerance(role, read),
                    deadline_s=self._readback_s(state, name),
                )
            if self._fuse_hand_back() or (renewing and self._fast_renewal()):
                self.state = replace(state, in_flight=(*state.in_flight, name), sent=sent, renew=renew,
                                     writes_today=writes, written=written, setpoint_writes_today=setpoints)
            else:
                self.state = replace(state, pending=name, sent=sent, renew=renew, writes_today=writes, written=written,
                                     setpoint_writes_today=setpoints)
        return [Op(name, role.method, entity_id, value, device_id=state.device_id, restore=restoring)]

    def _limit_waits(self, state: ExecState, inputs: ExecInputs, name: str) -> bool:
        """In a hand-back, whether a limit that acts in the plant's modes (`BrandControl.mode_limits`) must wait before
        it is raised: until every role the hand-back writes before it (the mode, the switch) reads back what it
        should (a mode idle once the switch is off counts: the plant is out of its Command modes). A fuse hand-back
        writes its roles in one look; were the mode write lost or given up while the
        raised limit landed, Sigenergy would sit in Command Charging (PV First) at its full charge limit just as the
        fuse is closest. Waiting, the limit stays where the hold left it (0) and the next look tries again."""
        if name not in self.control.mode_limits:
            return False
        role = self.control.role(name)
        wanted = state.target[name]
        read = inputs.reads.get(name)
        now_read = reading(role, None if read is None else read.state)
        if (
            isinstance(wanted, bool) or not isinstance(wanted, (int, float))
            or (isinstance(now_read, (int, float)) and not isinstance(now_read, bool) and wanted <= now_read)
        ):
            return False  # not raised: lowering a limit never waits
        for before in restore_order(self.control, state.action):
            if before == name:
                return False
            if before in self.control.mode_limits or before not in state.target:
                continue
            if not self._satisfied(
                self.control.role(before), state.target[before], inputs.reads.get(before), restoring=True
            ):
                if (before in state.unrestored or before in state.given_up) and name not in state.unrestored:
                    # The role it waits for will not be written again here: the limit is left where it is (low) and
                    # counted as not handed back, so the hand-back ends and the person is told what to set by hand.
                    self.state = replace(self.state, unrestored=(*self.state.unrestored, name))
                return True
        return False

    def _readback_s(self, state: ExecState, name: str) -> float:
        """How long this write may take to read back: the role's longer wait (`Role.readback_s`) only for a write
        that starts or changes a command. A restore and a peak cover keep the ledger's own, so one the link dropped
        is sent again soon (both are idempotent, and the house stands on the grid without the battery meanwhile)."""
        wait = self.control.role(name).readback_s
        if wait is None or state.phase == "restoring" or self._covering(state, name):
            return READBACK_DEADLINE_S
        return wait

    def _fast_renewal(self) -> bool:
        """Whether the control's watchdog is renewed on the recipe's own short interval
        (`BrandControl.watchdog_renew_s`): its round is then written in one look, each role read back after, as the
        values are the ones the roles already hold. A round a look apart per role would leave no look without a
        write, and nothing is judged while a write is awaited."""
        return self.control.watchdog_renew_s is not None

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
