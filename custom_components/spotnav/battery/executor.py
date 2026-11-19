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
  measurement is lost; the plant leaves the grid; the fuse headroom falls under `RESTORE_HEADROOM_A` (or a grid
  charge cannot keep `MIN_RATE_KW` within it); the battery's management changes (its own optimiser, another planner,
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
  that charges on one phase gets that one phase's room, not three.
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
  (Sigenergy: the discharge limit, lifted in Maximum Self Consumption, where the plant covers each phase itself;
  with the mode-4 hold variant `pv_first` the control mode back to Maximum Self Consumption), Remote EMS left on. A
  mode change may take the plant longer than a limit (its Remote EMS entities blink around a mode change, TypQxQ
  issue #427; the plant's output ramps at its active power regulation gradient, register 40049); `PEAK_RECOVER_S`
  bounds either, then the full hand-back follows. If the headroom is not back at or above
  `PEAK_COVER_A` within `PEAK_RECOVER_S` (or later stays under it that long), the battery is handed back in full.
  Under `RESTORE_HEADROOM_A` it is handed back at once, as before, and every hand-back of a hold or a grid charge
  writes the `peak_first` roles first (a grid charge: the mode that stops it), as does a change of action away from a
  grid charge (`ExecState.lead`): no limit is loosened while the plant still charges from the grid. A hand-back
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
  count.
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
  less `HEADROOM_MARGIN_A`); the plan's power and the battery's rating bound the sum, spread as evenly as the caps
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
  as given (a discharge on one phase), a charge among them still within its cap.
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
  sets them, a whole round in one look (each still read back), never ahead of a write the action owes. A renewal
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
  the restore. A never-set limit (`unknown`) is restored as the plant's maximum output. The day's write cap is the
  site's, shared with the battery's instance (`ExecInputs.other_writes_today`). A plant whose mode reads SpotNav's
  own battery control (Remote EMS that the battery instance runs, `ExecInputs.sibling_active`) is taken; one in
  Remote EMS of someone else's is not, and a curtail is handed back when someone else takes Remote EMS. The two instances never write each other's roles: the battery's control
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

from ..core.electric import TN_VOLTS_BETWEEN_PHASES
from .commands import (
    ACTION_CURTAIL_EXPORT,
    NORMAL,
    SNAPSHOT,
    WATCHDOG_RENEW_S,
    WATCHDOG_S,
    BrandControl,
    Native,
    Phases,
    Role,
    answered,
    fixed_rate_kw,
    reading,
)
from .facts import BatteryFacts, EntityFact, find_entity
from .gate import phase_split_kw
from .ledger import READBACK_DEADLINE_S, Ledger, same
from .monitor import (
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
#: An idle hold (`HoldVariant.idle`) whose battery charges more than this (kW) for this long (s) is handed back.
HOLD_CHARGE_KW: Final = 0.5
HOLD_CHARGE_S: Final = 60.0
#: A balanced grid charge that takes more from the grid than commanded (the lesser of its charge and the site's
#: import, over the rate by this much (kW) or a quarter of it, whichever is more) for this long (s), judged from this
#: long after its rate last changed (s; at least the recipe's read-back wait), did not take its rate: a wrong base
#: for a share of the rated power, a wrong sign, a fixed rate above its estimate.
CHARGE_OVER_KW: Final = 0.3
CHARGE_OVER_S: Final = 60.0
CHARGE_OVER_GRACE_S: Final = 120.0
#: A grid charge below this is not worth running (kW).
MIN_RATE_KW: Final = 0.5
#: The rate goes up only by this much (kW), after this long since its last change (s).
RATE_STEP_KW: Final = 0.5
RATE_UP_DWELL_S: Final = 60.0
#: A rate is lowered when the clamp is under it by more than this (kW).
RATE_TOLERANCE_KW: Final = 0.1
#: A phase's target is lowered when its room is under it by more than this (kW), and raised back to what the caps
#: allow when that is more than this above it.
PHASE_TOLERANCE_KW: Final = 0.05
#: A fixed-rate charge (`BrandControl.fixed_charge`) is judged only on whether the battery charges at all: the rate
#: the monitor expects (kW), far under what such a plant does.
FIXED_CHARGE_JUDGE_KW: Final = 1.0
#: The step a phase's charge is rounded down to: whole amps (A), so a wobbling reading writes nothing; the
#: verification's probe is rounded to this instead (kW).
PHASE_STEP_A: Final = 1.0
PHASE_STEP_KW: Final = 0.01
#: A value this near under a whole amp counts as that amp (in amps): what a target lost when it was rounded down to
#: `PHASE_STEP_KW` must not cost a whole amp when it is counted back.
_AMP_SLACK: Final = 0.02
#: A phase's room that falls is not acted on this long after a raise (s): what the plant takes on the phase is
#: counted back from its own power, which shows the raise a reading or two late. The fuse's own hand-back still acts.
PHASE_RAISE_SETTLE_S: Final = 10.0
#: The headroom a charge per phase by `grid` targets is sized to leave on each phase (A): more than
#: `HEADROOM_MARGIN_A`, since with the HA-down cap lifted (`gate.grid_charge_phase_caps_kw`) nothing else stands
#: between the charge and the fuse; a load that starts on a phase then lowers that phase's charge at the next look
#: instead of handing everything back (`RESTORE_HEADROOM_A`).
PHASE_GRID_MARGIN_A: Final = 4.0
#: A `grid` target per phase follows the load behind its point: written again when a target would move by more than
#: this (kW), no sooner than this after the last change (s). Between two the plant holds the point's power, so a
#: load that changes is taken by the battery, never by the grid.
PHASE_TRACK_KW: Final = 0.3
PHASE_TRACK_S: Final = 30.0
#: The day's setting writes a grid charge leaves untouched, so a hold can still start after it.
CHARGE_WRITE_RESERVE: Final = 12
#: The most the verification's probe may discharge on one phase (kW).
PHASE_PROBE_MAX_KW: Final = 1.0
#: With "car first", how much the battery's grid charge gives up per review while a car is held (kW).
YIELD_STEP_KW: Final = 1.0
#: After a hand-back, no new action for this long (s).
REARM_S: Final = 120.0
#: A management reading may be unknown this long (Sigenergy's Remote EMS entities blink, issue #427) (s).
MANAGEMENT_GRACE_S: Final = 60.0
#: A block for a real management change (the app, the plant's own optimiser) clears by itself this long after the
#: hand-back (s): the start rule still refuses a plant that is not free, so nothing is fought.
MANAGEMENT_BLOCK_S: Final = 6 * 3600.0
#: A block for a write whose service call the shell gave up on (`call_timeout`: a slow integration, no finding about
#: the plant) clears by itself this long after the hand-back (s).
CALL_TIMEOUT_BLOCK_S: Final = 900.0
#: Writes a day outside restores and watchdog renewals (flash wear is unknown for most brands). A renewal is bounded
#: by its timer: `WATCHDOG_RENEW_S` per role (SolarEdge renews two roles: 192 a day).
DAILY_WRITE_CAP: Final = 96
#: Hand-back reasons for a day's budget that neither block the control nor are logged at every look after.
_BUDGET_REASONS: Final = ("setpoint_write_cap", "charge_write_budget")
#: A restore write that does not read back is retried after these delays (s), then given up.
RESTORE_RETRY_S: Final = (30.0, 60.0, 120.0, 300.0, 600.0)
RESTORE_ATTEMPTS: Final = len(RESTORE_RETRY_S)
#: A restore given up after every retry is tried again this often while the control is blocked (s): Remote EMS or a
#: limit left set must not wait for a restart.
BLOCKED_RESTORE_S: Final = 900.0
#: A restore read back only by Home Assistant's echo that shows SpotNav's value again within the recipe's
#: `settle_s` is written again this many times; then the control blocks (`restore_not_held:<roles>`) until the role
#: reads the value found again (set back by hand) or someone else's.
SETTLE_ATTEMPTS: Final = 2

Phase = Literal["idle", "active", "restoring", "blocked"]
_PHASES: Final = frozenset({"idle", "active", "restoring", "blocked"})
#: Hand-back reasons that block the control until it is reset (the cap until the next day; `_SELF_CLEARING` after
#: `MANAGEMENT_BLOCK_S`).
_BLOCKING_PREFIXES: Final = (
    "write_not_taken", "changed_outside_spotnav", "no_effect", "management_changed", "not_obeyed", "call_timeout",
)
#: Blocks that clear by themselves `MANAGEMENT_BLOCK_S` after the hand-back: a management change, and an idle hold
#: that charged (the plant may take it another day; a block until reset would stop control for good).
_SELF_CLEARING: Final = (
    "management_changed", f"not_obeyed:{ACTION_HOLD}:charging", f"not_obeyed:{ACTION_GRID_CHARGE}:over",
    "call_timeout",
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
    for role in (*control.roles, *(role for role, _value, _why in control.watched)):
        found = find_entity(role.match, entities, battery)
        if found is None:
            continue
        reads[role.name] = RoleRead(
            entity_id=found.entity_id, state=answered(role, found.state, found.attributes), found=True,
            enabled=not found.disabled, step=found.step,
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
    #: The phases the plant spreads a balanced grid charge over (`BrandControl.grid_charge_phases`, or the plant's
    #: Output Type): the live clamp counts the least headroom on that many phases, as the HA-down cap does.
    charge_phases: int = 3
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
    #: Each phase's headroom now (A), `None` when one is unknown.
    phase_headroom_a: Phases | None = None
    #: The plant's own power on each phase now (kW, + output; the adapter's `plant_phase_power`), `None` when it is
    #: not read or stale.
    plant_phase_kw: Phases | None = None
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
            "phased": self.phased,
            "phase_kw": None if self.phase_kw is None else list(self.phase_kw),
            "setpoint_writes_today": self.setpoint_writes_today,
        } | self._settle_dict()

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
            phased=data.get("phased") is True,
            phase_kw=phase_kw,
            setpoint_writes_today=(
                setpoints if isinstance(setpoints, int) and not isinstance(setpoints, bool) and setpoints >= 0 else 0
            ),
            wrote=natives("wrote"),
            settle=settle,
            settle_until=parse_aware_datetime(data.get("settle_until")),
            settle_misses=misses if isinstance(misses, int) and not isinstance(misses, bool) and misses >= 0 else 0,
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


def grid_charge_cap_kw(inputs: ExecInputs, commanded_kw: float, *, yield_now: bool = True) -> float | None:
    """The most the battery may charge from the grid now (kW), `None` when the headroom is unknown.

    What the battery already takes from the grid is counted back (it is in the measured import): at most its own
    charge, the rate commanded and the site's import, and nothing when the import is unknown, so the figure is never
    above the truth. The headroom keeps `HEADROOM_MARGIN_A` under the fuse, on as many phases as the plant spreads
    the charge over (`ExecInputs.charge_phases`: a charge not known to be balanced may land on the tightest one). With "car first" and a car held, the
    battery yields `YIELD_STEP_KW` below what it measurably takes from the grid now (not below its last command, which
    it may not have reached yet), one step per `RATE_UP_DWELL_S` (`yield_now`); between steps it keeps its rate."""
    if inputs.headroom_a is None or inputs.volts_between_phases <= 0:
        return None
    battery = inputs.facts.power_kw
    own = measured = 0.0
    if inputs.grid_import_kw is not None and battery is not None:
        own = max(min(commanded_kw, battery, inputs.grid_import_kw), 0.0)
        measured = max(min(battery, inputs.grid_import_kw), 0.0)
    phases = min(max(inputs.charge_phases, 1), 3)
    cap = own + _kw(inputs.headroom_a - HEADROOM_MARGIN_A, inputs.volts_between_phases) * phases / 3
    if inputs.car_first and inputs.car_held:
        cap = min(cap, measured - YIELD_STEP_KW if yield_now else commanded_kw)
    return cap


def house_net_kw(inputs: ExecInputs) -> float | None:
    """The house's net load now (kW): the site's import less what the battery charges (negative: the sun gives
    more than the house takes); `None` when either is unknown."""
    grid, battery = inputs.grid_import_kw, inputs.facts.power_kw
    if grid is None or battery is None or not math.isfinite(grid) or not math.isfinite(battery):
        return None
    return grid - battery


def _finite3(values: Any) -> bool:
    return values is not None and len(values) == 3 and all(
        value is not None and math.isfinite(value) for value in values
    )


def _target_caps(inputs: ExecInputs) -> Phases:
    """For `grid` targets: the most each phase may charge by its HA-down cap (kW). The target that stands if Home
    Assistant stops is the load behind the point plus the charge, and the grid's phase is then what is beside the
    point plus that target, whatever the load behind does meanwhile: so the cap bounds the target, and the charge's
    share of it is the cap less the load behind (a negative load, a source behind the point, is not counted as
    room). 0 on every phase without the load behind."""
    caps = inputs.phase_caps_kw if _finite3(inputs.phase_caps_kw) else (0.0, 0.0, 0.0)
    behind = inputs.phase_behind_kw
    if not _finite3(behind) or behind is None:
        return (0.0, 0.0, 0.0)
    out = [max(cap - max(load, 0.0), 0.0) for cap, load in zip(caps, behind, strict=True)]  # type: ignore[arg-type]
    return (out[0], out[1], out[2])


def _phase_room_kw(inputs: ExecInputs, commanded: Phases | None, *, grid: bool = False) -> Phases | None:
    """The most each phase may charge now (kW): its HA-down cap (`ExecInputs.phase_caps_kw`, 0 without one) and its
    live room, what the plant already takes on that phase (at most what was commanded on it and what the plant's own
    power per phase shows, nothing when that is unknown, so never above the truth) plus the phase's headroom less
    `HEADROOM_MARGIN_A`. `None` when a phase's headroom is unknown.

    `grid`: the targets are the grid's (`PhaseControl.target_kind`), where the phase's cap may be the fuse itself
    (`gate.grid_charge_phase_caps_kw`), so the live room is reckoned exactly and is the one bound. A target of the
    load behind the point plus a charge `c` puts the phase at its import now, plus what the plant puts out on it
    now (which it then no longer does), plus `c`: the room is the headroom less `PHASE_GRID_MARGIN_A` less the
    plant's own output now (signed: a plant that charges now has that back). No room without the plant's power per
    phase. The HA-down cap bounds the whole target, which is what stands if Home Assistant stops
    (`_target_caps`)."""
    if not _finite3(inputs.phase_headroom_a) or inputs.volts_between_phases <= 0:
        return None
    assert inputs.phase_headroom_a is not None
    volts = inputs.volts_between_phases / math.sqrt(3.0)
    caps = _target_caps(inputs) if grid else (
        inputs.phase_caps_kw if _finite3(inputs.phase_caps_kw) else (0.0, 0.0, 0.0)
    )
    plant = inputs.plant_phase_kw if _finite3(inputs.plant_phase_kw) else None
    room: list[float] = []
    for index in range(3):
        if grid:
            if plant is None:
                room.append(0.0)
                continue
            exact = (inputs.phase_headroom_a[index] - PHASE_GRID_MARGIN_A) * volts / 1000.0 - plant[index]
            room.append(max(min(caps[index], exact), 0.0))  # type: ignore[index]
            continue
        own = 0.0
        if plant is not None and commanded is not None:
            own = max(min(commanded[index], -plant[index]), 0.0)
        live = own + (inputs.phase_headroom_a[index] - HEADROOM_MARGIN_A) * volts / 1000.0
        room.append(max(min(caps[index], live), 0.0))  # type: ignore[index]
    return (room[0], room[1], room[2])


def _floored(values: Phases | list[float], step_kw: float = PHASE_STEP_KW) -> Phases:
    """Each phase rounded toward zero to `step_kw`, then to `PHASE_STEP_KW` (never above what was allowed)."""
    slack = 1e-6 if step_kw <= PHASE_STEP_KW else _AMP_SLACK
    out = [math.floor(abs(value) / step_kw + slack) * step_kw for value in values]
    out = [math.copysign(math.floor(size / PHASE_STEP_KW + 1e-6) * PHASE_STEP_KW, value)
           for size, value in zip(out, values, strict=True)]
    return (round(out[0], 3) + 0.0, round(out[1], 3) + 0.0, round(out[2], 3) + 0.0)


def phase_charge_kw(
    inputs: ExecInputs, commanded: Phases | None, *, yield_now: bool = True, live: bool = True, grid: bool = False
) -> tuple[Phases | None, str | None]:
    """The charge to command on L1, L2 and L3 now (kW), or why none can run.

    Each phase at most its room (`_phase_room_kw`: the HA-down cap and the live headroom); the sum at most the
    plan's power and the battery's own limit, spread as evenly as the rooms allow (`gate.phase_split_kw`). With "car
    first" and a car held the sum yields as the balanced charge does (`grid_charge_cap_kw`). `live` false: the caps
    alone, for what the charge would be with the house quiet. The verification's probe
    (`ExecInputs.phase_probe_kw`) is taken as it is: a charge on a phase still within that phase's room, a discharge
    at most `PHASE_PROBE_MAX_KW`."""
    room = _phase_room_kw(inputs, commanded, grid=grid)
    if room is None:
        return None, "headroom_unknown"
    if not live:
        caps = _target_caps(inputs) if grid else (
            inputs.phase_caps_kw if _finite3(inputs.phase_caps_kw) else (0.0, 0.0, 0.0)
        )
        room = (max(caps[0], 0.0), max(caps[1], 0.0), max(caps[2], 0.0))  # type: ignore[index]
    probe = inputs.phase_probe_kw
    if probe is not None:
        if not _finite3(probe):
            return None, "probe_unknown"
        return _floored([
            min(value, room[index]) if value > 0 else max(value, -PHASE_PROBE_MAX_KW)
            for index, value in enumerate(probe)
        ]), None
    # Whole amps on each phase: a room that wobbles by a fraction of an amp changes nothing.
    amp_kw = PHASE_STEP_A * inputs.volts_between_phases / math.sqrt(3.0) / 1000.0
    room = (*(math.floor(value / amp_kw + _AMP_SLACK) * amp_kw for value in room),)  # type: ignore[assignment]
    total = min(abs(inputs.rate_kw), sum(room))
    limit = inputs.facts.max_charge_kw
    if limit is not None:
        total = min(total, limit)
    if live and inputs.car_first and inputs.car_held:
        battery, grid = inputs.facts.power_kw, inputs.grid_import_kw
        measured = max(min(battery, grid), 0.0) if battery is not None and grid is not None else 0.0
        held = 0.0 if commanded is None else sum(max(value, 0.0) for value in commanded)
        total = min(total, measured - YIELD_STEP_KW if yield_now else held)
    rates = _floored(list(phase_split_kw(total, room)), amp_kw)
    if sum(rates) < MIN_RATE_KW:
        return None, "fuse_headroom"
    return rates, None


def hold_headroom_ok(inputs: ExecInputs) -> bool:
    """Whether a hold may start (or a lifted one hold again): the headroom keeps `HEADROOM_MARGIN_A` once the
    battery stops covering what it discharges now.

    The discharge is counted as if it were all on one phase (kW·1000 / the phase voltage): a SigenStor covers each
    phase on its own, so what it gives may be one phase's load, and the battery's power per phase is not known."""
    if inputs.headroom_a is None or inputs.volts_between_phases <= 0:
        return False
    extra_a = 0.0
    power = inputs.facts.power_kw
    if power is not None and power < 0:
        extra_a = -power * 1000.0 * math.sqrt(3.0) / inputs.volts_between_phases
    return inputs.headroom_a - extra_a >= HEADROOM_MARGIN_A


# --- helpers -------------------------------------------------------------------------------------------------------


def _fresh(inputs: ExecInputs) -> bool:
    age = inputs.battery_age_s
    return inputs.facts.power_kw is not None and age is not None and age <= BATTERY_MAX_AGE_S


def _tolerance(role: Role, read: RoleRead) -> float | None:
    """How near a read must be to a value to count as it: the role's own figure where it has one
    (`Role.tolerance`), else the entity's step, else the role's."""
    return role.tolerance if role.tolerance is not None else (read.step or role.step)


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


def taken_over(facts: BatteryFacts, control: BrandControl) -> bool:
    """Whether the battery runs only its own optimiser in a mode SpotNav may take it over from
    (`BrandControl.takeover`: Sigenergy's AI mode), with every other mode setting free."""
    management = facts.management
    if management.state != "self_managed" or not control.takeover:
        return False
    seen = False
    for rule in control.adapter.modes:
        if rule.label not in management.settings:
            continue
        state = management.settings[rule.label]
        if state in rule.free:
            continue
        if state is not None and state in control.takeover.get(rule.label, frozenset()):
            seen = True
            continue
        return False
    return seen


def management_problem(
    facts: BatteryFacts, control: BrandControl, active: bool, *, takeover: bool = False
) -> str | None:
    """Why the battery is not SpotNav's to run now; `unknown` when a setting cannot be read. `takeover`: the person
    acknowledged that SpotNav may take the plant over from its own optimiser (`taken_over`), and for a plant whose
    own strategy cannot be read (`BrandControl.strategy_unread`) that SpotNav overrides whatever strategy runs:
    without it such a plant is never SpotNav's to run (`strategy_unread`)."""
    if control.strategy_unread and not takeover:
        return "strategy_unread"
    management = facts.management
    if management.state == "free":
        return None
    if takeover and taken_over(facts, control):
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
        self.ledger = ledger or Ledger()
        self.monitor = monitor or Monitor()
        self.state = state or ExecState()
        # A stored state runs (and hands back) with the variant it was written with, and with the targets per phase
        # when it ran them (their roles are the phased control's).
        control = control.with_hold(self.state.variant) if self.state.variant else control
        self.control = control.with_phases(True) if self.state.phased else control
        self.state = replace(self.state, variant=self.control.variant, phased=self.control.phased)
        self.watchdog_s = watchdog_s
        #: What a state that depends on the plant is filled in with (`BrandControl.wanted`): the SoC, the rating and the
        #: optional roles this plant lacks, as of the last look; not stored.
        self._soc_pct: float | None = None
        self._rated_kw: float | None = None
        self._capacity_kwh: float | None = None
        self._absent: frozenset[str] = frozenset()

    def _sense(self, inputs: ExecInputs) -> None:
        facts = inputs.facts
        self._soc_pct, self._rated_kw, self._capacity_kwh = facts.soc_pct, facts.max_charge_kw, facts.capacity_kwh
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
            # A restore not held gets its attempts again (the record stays: the next look writes it again).
            self.state = replace(self.state, phase="idle", blocked=None, reason=None, given_up=(), settle_misses=0)

    # --- one look ---

    def step(self, inputs: ExecInputs) -> StepResult:
        now = inputs.now
        self._sense(inputs)
        events: list[dict[str, Any]] = []
        today = now.date().isoformat()
        if self.state.day != today:
            self.state = replace(self.state, day=today, writes_today=0, setpoint_writes_today=0)
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
                events += self._not_taken(now, role_name, why)
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
        elif self.state.phase == "active" and self.state.phase_kw is not None:
            events += self._judge_phases(inputs)
        elif self.state.phase == "active":
            facts = inputs.facts
            capability = Capability(
                rated_charge_kw=facts.max_charge_kw,
                rated_discharge_kw=facts.max_discharge_kw,
                soc_min_pct=facts.reserve_soc_pct or 0.0,
                site_cap_kw=(
                    inputs.house_deficit_kw if self.state.action == ACTION_DISCHARGE_HOUSE else self._house_cap(inputs)
                ),
            )
            grid = inputs.grid_import_kw if self.state.action == ACTION_HOLD and inputs.measurement_ok else None
            judged = self.monitor.observe(
                Reading(facts.power_kw, inputs.battery_age_s, facts.soc_pct, grid), capability, now
            )
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
        full. `CONFIRM_READINGS` in a row hand the charge back, blocked. Nothing is judged while a write is awaited,
        without the reading, or for the verification's probe (it judges that itself)."""
        state = self.state
        now = inputs.now
        measured = self._phase_measured(inputs)
        changed = state.rate_changed_at
        phases = state.phase_kw
        entered = state.entered_at
        if (
            phases is None or inputs.phase_probe_kw is not None or state.priming or state.pending is not None
            or self._next_role(state, inputs) is not None or measured is None or changed is None or entered is None
        ):
            if state.phase_bad:
                self.state = replace(state, phase_bad=())
            return []
        settling = (now - changed).total_seconds() < PHASE_GRACE_S
        if (now - entered).total_seconds() >= PHASE_OVER_GRACE_S and (
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
        if settling:
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
            if same(expected, now_read, _tolerance(role, read)) is not False:
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
            # A call given up on is its own reason: its block clears by itself (`CALL_TIMEOUT_BLOCK_S`) and a
            # verification counts it as the moment's (`gate.FAILING_ABORTS` does not name it).
            reason = f"call_timeout:{role}" if why == "call_timeout" else f"write_not_taken:{role}:{why}"
            events += self._begin_restore(now, reason)
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
        if (
            action == ACTION_GRID_CHARGE and self.state.action == ACTION_GRID_CHARGE
            and (self.state.phase_kw is not None) != self._per_phase(inputs)
        ):
            # Between a charge per phase and the brand's own: handed back first, never changed while it runs.
            return "per_phase_changed"
        if facts.on_grid is False:
            return "off_grid"
        if not inputs.measurement_ok or (not self._export and not _fresh(inputs)):
            return "stale_data"
        if inputs.headroom_a is None and not self._export:
            return "headroom_unknown"
        if (action in _RAISES_IMPORT or self.state.action in _RAISES_IMPORT) and inputs.headroom_a < RESTORE_HEADROOM_A:
            return "fuse_headroom"
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
            if (inputs.now - since).total_seconds() >= MANAGEMENT_GRACE_S:
                # No evidence of another owner: handed back as stale data is, the next start re-arms.
                return "management_unknown"
        elif management is not None:
            return f"management_changed:{management}"
        else:
            self.state = replace(self.state, management_unknown_since=None)
        if _writes_today(self.state, inputs) >= self._daily_cap:
            return "daily_write_cap"
        return self._charge_budget(inputs)

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
            self.control.hold_grid_guard and power is not None and grid is not None and math.isfinite(grid)
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
            charging_since=None, over_since=None, priming=False, confirmed=(), phase_bad=(), house_kw=None,
        )
        self._release_unset()
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
                sent=(), renew=(), released_at=now, phase_kw=None,
            )
            return [_event(now, "restore_failed", ",".join(state.unrestored))]
        blocked = state.blocked
        if blocked is not None and blocked.startswith("restore_failed"):
            blocked = None
        if state.reason == "daily_write_cap":
            blocked = "daily_write_cap"
        settle: dict[str, tuple[str, Native, Native, str]] = {}
        if self.control.settle_s > 0:
            # Watched a while: a restore that only Home Assistant echoed may show SpotNav's value again.
            settle = {
                name: (state.snapshot[name], state.restore_values[name], state.wrote[name], state.entities[name])
                for name in state.written
                if name in state.snapshot and name in state.restore_values and name in state.wrote
                and name in state.entities and name not in state.given_up
            }
        # A role that goes idle with the switch, written by the action, and handed back without a read-back.
        unsure = tuple(
            role.name for role in self.control.roles
            if role.idle_when and role.name in state.written and role.name in state.target
            and role.name not in state.confirmed
        )
        self.state = replace(
            state, phase="blocked" if blocked else "idle", blocked=blocked, action=None, rate_kw=0.0, target={},
            snapshot={}, restore_values={}, entities={}, device_id=None, snapshot_at=None, sent=(), renew=(),
            released_at=now, given_up=state.given_up if blocked else (), written=(), confirmed=(),
            unsure=tuple(dict.fromkeys((*state.unsure, *unsure))), phase_kw=None, wrote={},
            settle=settle, settle_until=now + timedelta(seconds=self.control.settle_s) if settle else None,
            settle_misses=state.settle_misses if settle else 0,
        )
        return [_event(now, "handed_back", state.reason or "")]

    def _settle(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """After a hand-back (`ExecState.settle`): a role that reads SpotNav's value again did not keep its restore
        (a cloud refused it, or only Home Assistant echoed it). It is written again, `SETTLE_ATTEMPTS` times; then the
        control blocks (`restore_not_held:<roles>`, the value found is kept so the facts still read it) until the role
        reads the value found or someone else's. A role that reads someone else's value is no longer watched; the
        watch ends `settle_s` after the hand-back."""
        state = self.state
        now = inputs.now
        if not state.settle or state.phase not in ("idle", "blocked") or state.holds_snapshot:
            return []
        keep: dict[str, tuple[str, Native, Native, str]] = {}
        not_held: list[str] = []
        for name, row in state.settle.items():
            _found, restore, wrote, _entity = row
            try:
                role = self.control.role(name)
            except KeyError:
                continue
            read = inputs.reads.get(name)
            now_read = reading(role, None if read is None else read.state)
            step = None if read is None else (read.step or role.step)
            if now_read is None or same(restore, now_read, step):
                keep[name] = row
            elif same(wrote, now_read, step):
                keep[name] = row
                not_held.append(name)
        stuck = state.phase == "blocked" and (state.blocked or "").startswith("restore_not_held")
        if not not_held:
            if stuck:
                # Set back (by hand) or taken by someone else: nothing of SpotNav's is left standing.
                self.state = replace(state, phase="idle", blocked=None, reason=None, settle={}, settle_until=None,
                                     settle_misses=0)
                return [_event(now, "unblocked", state.blocked or "")]
            if not keep or state.settle_until is None or now >= state.settle_until:
                self.state = replace(state, settle={}, settle_until=None, settle_misses=0)
            elif keep != state.settle:
                self.state = replace(state, settle=keep)
            return []
        if stuck:
            if keep != state.settle:
                self.state = replace(state, settle=keep)
            return []
        misses = state.settle_misses + 1
        events = [_event(now, "restore_not_held", ",".join(not_held), attempt=misses)]
        if misses > SETTLE_ATTEMPTS:
            self.state = replace(
                state, phase="blocked", blocked=f"restore_not_held:{','.join(not_held)}", settle=keep,
                settle_until=None, settle_misses=misses,
            )
            return events
        rows = {name: keep[name] for name in not_held}
        self.state = replace(
            state, action=None, snapshot={name: row[0] for name, row in rows.items()},
            restore_values={name: row[1] for name, row in rows.items()},
            entities={name: row[3] for name, row in rows.items()},
            wrote={name: row[2] for name, row in rows.items()}, written=tuple(not_held),
            settle={}, settle_until=None, settle_misses=misses, released_at=now,
        )
        return events + self._begin_restore(now, "restore_not_held")

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
            rate, why = self._rate(inputs, 0.0 if starting else state.rate_kw)
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
        if action == ACTION_GRID_CHARGE and self.control.fixed_charge:
            # The plant charges at its own rate: it must fit under the fuse cap as it is, never lowered.
            fixed = fixed_rate_kw(self.control, facts)
            if fixed is None or not math.isfinite(fixed):
                return 0.0, "size_unknown"
            cap = grid_charge_cap_kw(inputs, commanded_kw, yield_now=False)
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
            cap = grid_charge_cap_kw(inputs, commanded_kw, yield_now=yield_now)
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
        if inputs.headroom_a is None and not self._export:
            return "headroom_unknown"
        if action == ACTION_HOLD and not hold_headroom_ok(inputs):
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
        return True, [_event(now, "enter", action, rate_kw=rate, snapshot=snapshot, **detail)]

    def _grid_terms(
        self, inputs: ExecInputs, action: str, phases: Phases | None
    ) -> tuple[float | None, Phases | None]:
        """What a brand that sets the grid's power is given beside the rate: the house's net load now for an action
        of `BrandControl.house_states` (not for a charge per phase), and the load behind each phase's point for a
        `grid` target per phase. `None` each for a brand that sets the battery's."""
        if phases is not None:
            return None, self._behind(inputs)
        return (house_net_kw(inputs) if action in self.control.house_states else None), None

    def _release_unset(self) -> None:
        """A role the running action no longer sets (and no hand-back writes) is the plant's again: its claim is
        released, also for a write still awaited, so the plant's own writes to it are never read as someone else's
        change and nothing waits for its read-back. A brand whose roles are set in every action and handed back
        (every one but a recipe with unparked targets per phase) has no such role."""
        state = self.state
        for name, entity_id in state.entities.items():
            if name in state.target or name in state.given_up:
                continue
            if self.ledger.claim(entity_id) is not None:
                self.ledger.release(entity_id)
            if state.pending == name or name in state.in_flight:
                self.state = state = replace(
                    state, pending=None if state.pending == name else state.pending,
                    in_flight=_without(state.in_flight, name),
                )

    def _lead(self, previous: str | None) -> tuple[str, ...]:
        """The roles a change of action away from `previous` writes first: a grid charge's stop (its `peak_first`
        roles; Sigenergy: the control mode), so a limit the next action loosens never acts in the grid charge's mode
        (the charge limit back at the rating in Command Charging would charge from the grid at full power). A button
        is never moved ahead of the values it sends."""
        if previous != ACTION_GRID_CHARGE:
            return ()
        first = self.control.peak_first.get(previous, ())
        return tuple(name for name in first if self.control.role(name).method != "button")

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

    def _retarget(self, inputs: ExecInputs) -> list[dict[str, Any]]:
        """A new action, a new rate past the hysteresis, or a watchdog to renew."""
        now = inputs.now
        state = self.state
        action = inputs.action
        assert action is not None
        rate, phases, why = self._aim(inputs, False)
        if why is not None:
            return self._begin_restore(now, why)
        events: list[dict[str, Any]] = []
        if state.priming and state.pending is None and self._next_role(state, inputs) is None:
            # Primed: the switch is on and the role that was unsure reads what this action wrote; now its own values.
            self.state = state = replace(state, priming=False, unsure=())
            self.state = state = replace(
                state, sent=(),
                target=self._wanted(
                    state.action or action, state.rate_kw, state.restore_values, state.phase_kw,
                    house_kw=state.house_kw, behind=state.phase_behind_kw,
                ),
            )
            events.append(_event(now, "primed", state.action or action))
        if action != state.action and self.control.slow and state.rate_changed_at is not None and (
            (now - state.rate_changed_at).total_seconds() < self.control.min_change_s
        ):
            # A slow control (a cloud) changes its action no sooner than its own minimum: the plan's next look asks again.
            return events
        if action != state.action:
            house, behind = self._grid_terms(inputs, action, phases)
            self.state = replace(
                state, action=action, rate_kw=rate,
                target=self._wanted(action, rate, state.restore_values, phases, house_kw=house, behind=behind),
                house_kw=house, phase_behind_kw=behind, phase_was_behind_kw=None,
                sent=(), renew=(), rate_changed_at=now, watchdog_at=now, covered_at=None, peak_low_since=None,
                lead=self._lead(state.action), phase_kw=phases, phase_bad=(), phase_judged_at=None,
                phase_was_kw=None, phase_raised_at=None, phase_over=0, phase_over_at=None,
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
            if down or up:
                house, _behind = self._grid_terms(inputs, action, None)
                self.state = replace(
                    state, rate_kw=rate, target=self._wanted(action, rate, state.restore_values, house_kw=house),
                    sent=(), rate_changed_at=now, house_kw=house,
                )
                self._watch(inputs, action, rate)
                events.append(_event(now, "rate", action, rate_kw=rate, was_kw=state.rate_kw))
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
        renewed = tuple(name for name in self.control.watchdog_roles if name in state.target)
        if (
            renewed and state.watchdog_at is not None and state.pending is None
            and (now - state.watchdog_at).total_seconds() >= (self.control.watchdog_renew_s or WATCHDOG_RENEW_S)
            # One round at a time, and never ahead of a write the action still owes: a round begun before the last
            # one ended would never reach its last role.
            and not state.renew and not state.in_flight and self._next_role(state, inputs) is None
        ):
            self.state = replace(state, renew=renewed, watchdog_at=now)
            events.append(_event(now, "renew", ",".join(renewed)))
        return events

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
        written as it changes."""
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
        headroom = inputs.headroom_a
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
        if restoring:
            order = restore_order(self.control, state.action)
        else:
            order = (*state.lead, *(name for name in self.control.enter_order if name not in state.lead))
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
        # A restore and a watchdog renewal are not counted: the renewal is the brand's dead-man, on a fixed timer
        # (`WATCHDOG_RENEW_S`, at most a few roles), never a change of the wanted state.
        # On a fast watchdog a role in a renewal round whose value changed meanwhile is a change, and is counted;
        # the brands on `WATCHDOG_RENEW_S` count no renewal, as before (their round is a few roles a quarter-hour).
        renewing = name in state.renew and (
            not self._fast_renewal() or self._satisfied(role, value, read, restoring=False)
        )
        counted = 0 if restoring or renewing else 1
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

    def _readback_s(self, state: ExecState, name: str) -> float:
        """How long this write may take to read back: the brand's longer wait (`BrandControl.readback_s`) only for a
        write that starts or changes a command. A restore and a peak cover keep the ledger's own, so one the link
        dropped is sent again soon (both are idempotent, and the house stands on the grid without the battery
        meanwhile)."""
        if self.control.readback_s is None or state.phase == "restoring" or self._covering(state, name):
            return READBACK_DEADLINE_S
        return self.control.readback_s

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
