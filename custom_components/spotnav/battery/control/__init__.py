"""The shell of the battery's write path (`executor/`): reads the brand's control entities, makes the writes the
executor returns, persists its state. One per site, owned by its `SiteCapacityController`.

**The gate** (`gate.py`): nothing is written unless the site's options say so: `battery_control_enabled` (the
person's opt-in, off by default) and `battery_control_shadow_only` off (on by default until the owner's on-site
tests are done). With both, enabling starts the verification routine (`verify.py`); control runs the plan only once
the routine passed for the plant's current firmware and integration version (`gate.firmware_key`), and only the
actions the report allows (`gate.allowed_actions`). An update never stops it by itself (owner rule 2026-11-24): after
a change of firmware, integration version or recipe revision control runs on with what last passed for the brand on
this plant (`control_report`) until the routine has checked the new key. While shadow only, the shell logs what it would do
(`would_do`, `executor.preview`) and writes nothing. One kind of write is never refused: handing back what SpotNav
itself wrote (a restore), on every path, so turning anything off can always put the plant back.

The order on every path:

* **Start** (`async_launch`, a background task, never awaited in the site's setup: the restore may wait up to
  `STARTUP_WAIT_S` for the controls' integration): load the stored state; a stored snapshot (SpotNav wrote and HA
  stopped before handing back) is restored before anything else, after waiting for its controls' entities and
  services to load. Only then does the look timer (`TICK_S`) start. A start that raises is tried again
  (`START_RETRY_S`, doubling); an unload while it has not finished still hands back what its executors hold.
* **Each look** (`async_look`, never raises): the owner's test action (`async_trial`) if one runs; else the
  verification routine if one runs; else the plan's running slot merged with the hold for the car
  (`gate.merged_action`), restricted to the allowed actions and the grid charge cap (`gate.restricted`), when
  control is on; else a hand-back of anything SpotNav holds. The export limit beside it: on its live check whenever
  the options allow writing (no routine of its own, owner 2026-11-16), handed back otherwise. Each executor step:
  read the battery's facts and the brand's control entities, the site's measurement, headroom, import and load
  balancing; step the executor; **persist the state before the writes when it took a new snapshot**; make the writes
  (a raised service call is reported back as not taken); store the state.
* **Stop / unload** (`async_stop`, `async_shutdown`): the executor hands back with that reason; the shell keeps
  stepping until it is done or `HAND_BACK_DEADLINE_S` passes (the state is stored either way, so a restart finishes
  the restore). The site shuts this down first, before its other parts.
* **Verification**: started at the first suitable moment after enabling (`gate.suitable`: a discharging battery
  between 30 and 80 %, no car charging, the plant SpotNav's to take), a mode that could not be judged is tried again
  at a later one. The report is kept per firmware key. After a firmware, integration or recipe-revision change
  control keeps running each step on the latest answer it has on the brand's earlier keys on this plant
  (`control_report`, `gate.control_report`: each step by itself, the hold with the variant of the key it comes
  from, the capability per phase like a step, a measured expiry never) and the routine checks the new key at the first suitable moment of any hour, which is one where the plan
  leaves the battery to itself (the executor idle); control yields to the run as to the first one. A mode decided on
  the new key counts from then on; a check that fails or gives up there stops control as on any key, and turning
  control on again after that runs the check without the old report, as a person's failed hardware test leaves no
  older pass to run on (`carry_after`). Never carried: another brand's
  report, one passed on a key this release holds back (`commands.blocked`), a hold passed in a variant the recipe
  dropped, a report older than a failed one, a step a later key failed.
* **The plan's own commands show steps on the key** (`_credit_plan`, `gate.plan_proved`): while control runs the
  plan, a command it wrote on the key now (`key_since`, and read without a break since: `_credit_since`) that the
  executor's monitor saw act (the event `proved`, `monitor.proves`) counts as that mode passed in the key's own report, and a hand-back of what the plan wrote on the
  key that left every control as found (the event `handed_back` with `clean`) as the hand-back passed. The rows say
  `via: plan`. Only the plan's own: nothing a test action, the routine or a hardware test wrote, nothing in shadow,
  never a failure, never the export limit, a charge per phase or an expiry; a hold only in the variant control runs
  on, a grid charge not where it is allowed only per phase. What the routine still has to check a week
  (`gate.PLAN_PROOF_DEADLINE_S`) after the key was first read is checked ahead of the plan (`_plan_aside`): at a moment that
  suits the routine by every rule but the idle executor (`gate.suitable_ahead`) the plan stands aside for at most
  `ASIDE_S`, the battery is handed back, and the routine starts as it does when the plan is idle; at most once per
  `ASIDE_GAP_S`, and ended at once when a car begins to charge or the fuse or the charge level no longer suits.
  A moment that does not come leaves the plan running; control never stops for the deadline.
* **Sigenergy's AI mode** (owner 2026-11-16): taken over only with `battery_control_ai_takeover_ack`; whenever
  control stops running after such a takeover (turned off, back to shadow only, a failed check, a firmware change,
  the acknowledgement withdrawn) a note asks the person to set AI mode again in the app (the hand-back leaves the
  plant in Maximum Self Consumption); seeing AI mode again ends the note and the takeover. Without the
  acknowledgement a plant in AI mode is only watched.
* **A grid charge per phase** (`commands.PhaseControl`, no brand named here): where the brand has targets per
  phase, they and what they require are found and enabled, the plant's own power per phase is read
  (`_phase_missing`) and the verification's mode `per_phase` passed on this firmware (`gate.per_phase`), the
  executor runs the brand's phased control (`_sync_phases`, changed only while it holds nothing) and a grid charge
  is given each phase's HA-down cap (`gate.grid_charge_phase_caps_kw`) when those allow at least
  `PER_PHASE_GAIN_KW` more in all than the balanced cap (`_charge_limits`); the slot is then capped at their sum.
  Anything else is the balanced charge under the balanced cap, as before: the targets not there, not verified, a
  reading per phase missing, the plant's own import limit guarding the fuse, a balanced house. A running charge
  per phase that the plant does not follow withdraws the capability (`gate.per_phase_failed`). Where the
  verification's `charge` could only be tried per phase (the balanced cap leaves no room: `_async_verify`,
  `verify.phase_charge_step`), a grid charge runs only per phase (`gate.charge_by_phases`, `_control_slot`).
* **The export limit** (`export_curtail.py`): a second executor on the brand's export control
  (`commands.EXPORT_CONTROLS`): `curtail_export` while the sell price now is below zero (`sell_source`, by default
  the plan shadow's `sell_at`), plain hand-back otherwise. Its plant is found from the battery or the grid meter
  (`runtime.read_export_facts`), so a site without a battery has it too. It has its own switch ("No export at
  negative prices", `gate.CONF_EXPORT_CURTAIL`) and gates (`export_shell.ExportShell`): the battery's opt-in and
  shadow only do not arm it. Its state is stored beside the battery's
  (`export`), restored after the battery's after a restart and handed back after it on stop; the two share the day's write cap. While the
  battery's executor runs the same plant (its Remote EMS on, also while it hands back) the export instance is told so
  (`sibling_active`) and still takes and keeps the limit; Remote EMS on otherwise is another controller's and hands
  the limit back; it is judged on the site's grid power (`grid_import_kw`).

The package's parts (every name is imported from the package itself, as when it was one module):

* `core.py`: `BatteryControl` and its attributes. What it does is in mixins on that one state, methods moved as they
  were: `life.py` (the start on what was stored, the timer, the stop and the unload), `look.py` (one look and its
  order, the slot control runs), `write_path.py` (a step with its state stored before its writes, each write behind
  the gate, the hand-back loops), `exec_inputs.py` (the reads and an executor's inputs), `report.py` (the options,
  the firmware key, the report control runs on, the state, the settings), `gate_record.py` (hardware test passes, the
  tester's sign-up, control codes, a grandfathered plant), `verify_driver.py` (the routine), `hardware_run.py` (a
  person's hardware test), `plan_proof.py` (the plan's own commands as the check of a new key), `trial_run.py` (the
  owner's test action), `per_phase.py` (the hold variant and the charge per phase), `expiry.py` (the plant's own
  expiry and its test), `import_limit.py` (the plant's own import limit), `caps.py` (the HA-down caps) and
  `status.py` (the store, the checklist, the status and the snapshot).
* Without a site: `io.py` (a stored control's read, one write as its service call), `stored.py` (the hand-back of a
  stored snapshot), `trial.py` (`Trial` and the refusals), `helpers.py`, `const.py`.

Each module reads its globals from itself: a test replaces one through `tests/battery_patch.py`, which sets it in
every module whose code reads it. A change to one part is checked against the recorded shell traces
(`tests/test_battery_control_golden.py`, `tests/test_battery_control_golden_brands.py`).
"""

from __future__ import annotations

from ..executor import Op
from .const import (
    ACTING_GRACE_S,
    ASIDE_GAP_S,
    ASIDE_S,
    CALL_TIMEOUT_S,
    EXCLUSIVE_BOUND_MARGIN,
    FAILED_LOOKS_HAND_BACK,
    FIRMWARE_STEPS,
    HAND_BACK_DEADLINE_S,
    HAND_BACK_LOOK_S,
    HA_DOWN_OFFER_GAIN_KW,
    HEAT_RAISE_KW,
    IMPORT_LIMIT_READ_BACK_S,
    IMPORT_LIMIT_READ_BACK_STEP_S,
    LOG_MAX,
    LOOK_WAIT_S,
    NEED_CONTROL_CODE,
    NEED_HARDWARE_TEST_AGAIN,
    NEED_HARDWARE_TEST_FIRST,
    NEED_RECIPE_BLOCKED,
    NEED_RESTORE_BY_HAND,
    NEED_TESTER_SIGNUP,
    NO_ROOM_FOR_CHARGE,
    NOTE_AI_MODE_BACK,
    ORPHANED,
    PER_PHASE_GAIN_KW,
    PHASE_PATH_STEPS,
    RULE_CHARGE_POWER_ENTITY,
    RULE_NO_GRID_CHARGE,
    SOURCES_REFRESH_S,
    STARTUP_LOOK_S,
    STARTUP_WAIT_S,
    START_RETRY_MAX_S,
    START_RETRY_S,
    STORE_VERSION,
    TRIAL_MAX_MIN,
    _ASIDE_ENDS,
    _HARDWARE_TEST_NEEDS,
    _METHOD_SERVICES,
    _PLAN_ACTIONS,
    _PLAN_KINDS,
    _PLAN_PROVES,
    _TEST_IGNORES,
    _UNSET,
    PlanSource,
    SellSource,
)
from .core import BatteryControl
from .helpers import _accept_applies, _card_refusal, _float, _grid_charge_expiry, _least, _state_of
from .io import _async_call, _async_service, _error_text, _failure, resolve_follows_max, stored_reads
from .stored import _orphans, async_hand_back_stored, store_key
from .trial import ControlNotReachable, Trial, TrialRefused

__all__ = [
    "ACTING_GRACE_S",
    "ASIDE_GAP_S",
    "ASIDE_S",
    "CALL_TIMEOUT_S",
    "EXCLUSIVE_BOUND_MARGIN",
    "FAILED_LOOKS_HAND_BACK",
    "FIRMWARE_STEPS",
    "HAND_BACK_DEADLINE_S",
    "HAND_BACK_LOOK_S",
    "HA_DOWN_OFFER_GAIN_KW",
    "HEAT_RAISE_KW",
    "IMPORT_LIMIT_READ_BACK_S",
    "IMPORT_LIMIT_READ_BACK_STEP_S",
    "LOG_MAX",
    "LOOK_WAIT_S",
    "NEED_CONTROL_CODE",
    "NEED_HARDWARE_TEST_AGAIN",
    "NEED_HARDWARE_TEST_FIRST",
    "NEED_RECIPE_BLOCKED",
    "NEED_RESTORE_BY_HAND",
    "NEED_TESTER_SIGNUP",
    "NO_ROOM_FOR_CHARGE",
    "NOTE_AI_MODE_BACK",
    "ORPHANED",
    "PER_PHASE_GAIN_KW",
    "PHASE_PATH_STEPS",
    "RULE_CHARGE_POWER_ENTITY",
    "RULE_NO_GRID_CHARGE",
    "SOURCES_REFRESH_S",
    "STARTUP_LOOK_S",
    "STARTUP_WAIT_S",
    "START_RETRY_MAX_S",
    "START_RETRY_S",
    "STORE_VERSION",
    "TRIAL_MAX_MIN",
    "_ASIDE_ENDS",
    "_HARDWARE_TEST_NEEDS",
    "_METHOD_SERVICES",
    "_PLAN_ACTIONS",
    "_PLAN_KINDS",
    "_PLAN_PROVES",
    "_TEST_IGNORES",
    "_UNSET",
    "BatteryControl",
    "ControlNotReachable",
    "Op",
    "PlanSource",
    "SellSource",
    "Trial",
    "TrialRefused",
    "_accept_applies",
    "_async_call",
    "_async_service",
    "_card_refusal",
    "_error_text",
    "_failure",
    "_float",
    "_grid_charge_expiry",
    "_least",
    "_orphans",
    "_state_of",
    "async_hand_back_stored",
    "resolve_follows_max",
    "store_key",
    "stored_reads",
]
