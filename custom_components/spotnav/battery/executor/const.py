"""The executor's constants: the fuse margins, dwell times, write caps, the restore's retries and the reasons that
block (see the package's docstring for the rules they belong to)."""

from __future__ import annotations

from typing import Final, Literal

from ..commands import ACTION_CURTAIL_EXPORT
from ..plan_model import ACTION_DISCHARGE_HOUSE, ACTION_EXPORT_DISCHARGE, ACTION_GRID_CHARGE, ACTION_HOLD

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
#: A running action whose battery reading is missing or stale (a plant whose link goes silent a while: one Sigenergy
#: plant does not answer Modbus for about 90 s each night) is ridden out this long (s) before it is handed back as
#: stale data: nothing is written meanwhile, and only while the fuse allows it without the battery's own reading
#: (`Executor._ride_out`). The site's measurement lost is still handed back at once.
SILENCE_GRACE_S: Final = 180.0
#: A block for a real management change (the app, the plant's own optimiser) clears by itself this long after the
#: hand-back (s): the start rule still refuses a plant that is not free, so nothing is fought.
MANAGEMENT_BLOCK_S: Final = 6 * 3600.0
#: A block for a write whose service call the shell gave up on (`call_timeout`: a slow integration, no finding about
#: the plant) clears by itself this long after the hand-back (s).
CALL_TIMEOUT_BLOCK_S: Final = 900.0
#: Writes a day outside restores and watchdog renewals (flash wear is unknown for most brands). A renewal is bounded
#: by its timer: `WATCHDOG_RENEW_S` per role (SolarEdge renews two roles: 192 a day).
DAILY_WRITE_CAP: Final = 96
#: The least time since the last renewal (or change) before a change of action renews the watchdog roles it leaves
#: as they were (s).
RENEW_ON_CHANGE_GAP_S: Final = 60.0
#: The checklist's code for a sale left standing in a setpoint SpotNav holds no snapshot of (`told_problems`).
SETPOINT_SELLING: Final = "grid_setpoint_selling"
#: Hand-back reasons for a day's budget that neither block the control nor are logged at every look after.
_BUDGET_REASONS: Final = ("setpoint_write_cap", "charge_write_budget", "renewal_cap")
#: A command that lapses by itself (its documented fallback read while it runs, `BrandControl.command_role`) is handed
#: back and may start again; this many lapses in a day block the control (`lapsed_repeatedly`, clearing by itself).
LAPSES_A_DAY: Final = 3
#: How long a hand-back of a recipe that restores only its own values waits for a first reading of a role after a
#: restart before writing it back as before (s; `BrandControl.restore_own_only`): a few slow polls.
RESTORE_READ_WAIT_S: Final = 180.0
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
    "lapsed_repeatedly",
)
#: Blocks that clear by themselves `MANAGEMENT_BLOCK_S` after the hand-back: a management change, and an idle hold
#: that charged (the plant may take it another day; a block until reset would stop control for good).
_SELF_CLEARING: Final = (
    "management_changed", f"not_obeyed:{ACTION_HOLD}:charging", f"not_obeyed:{ACTION_GRID_CHARGE}:over",
    "call_timeout", "lapsed_repeatedly",
)
#: Blocks the expiry test's silence never leaves behind (`ExecInputs.expiry_probe`): nothing new was asked of the plant,
#: and a fallback reads as a command not obeyed.
_PROBE_UNBLOCKED: Final = ("not_obeyed", "no_effect")
#: States that are no value: never a snapshot to write back.
_NO_VALUE: Final = frozenset({"", "unavailable", "unknown", "none"})
_RAISES_IMPORT: Final = frozenset({ACTION_HOLD, ACTION_GRID_CHARGE})
#: Handed back at once under `RESTORE_HEADROOM_A`: what adds import, and a sale (in a commanded discharge the plant
#: may spread its output evenly and stop covering the loaded phase).
_FUSE_HANDED_BACK: Final = _RAISES_IMPORT | {ACTION_EXPORT_DISCHARGE}
#: Hand-backs for the fuse: every role is written at once, without waiting for the last one's read-back.
_FUSE_REASONS: Final = frozenset({"fuse_headroom"})
_MONITORED: Final = {ACTION_HOLD: "hold", ACTION_GRID_CHARGE: "charge", ACTION_DISCHARGE_HOUSE: "discharge",
                     ACTION_EXPORT_DISCHARGE: "discharge", ACTION_CURTAIL_EXPORT: "curtail"}
