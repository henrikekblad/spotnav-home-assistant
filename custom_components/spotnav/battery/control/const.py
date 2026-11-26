"""The battery control shell's constants: its waits and deadlines, the checklist's codes (`NEED_*`), the cap rules and
what the plan's own commands show."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Final


from ..plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
)


STORE_VERSION: Final = 1
#: The share below a charge entity's maximum a recipe with `charge_power_bound_exclusive` stays at least (Kostal: the
#: maximum moves with the battery's voltage between the read and the write), where the entity's step is less.
EXCLUSIVE_BOUND_MARGIN: Final = 0.05
#: How long a stop or unload keeps stepping the hand-back before it leaves the rest to the next start (s).
HAND_BACK_DEADLINE_S: Final = 30.0
#: How long the shell waits for an entity to change after a write, between looks during a hand-back (s).
HAND_BACK_LOOK_S: Final = 2.0
#: How long an unload waits for a look in progress before it cancels it (s).
LOOK_WAIT_S: Final = 10.0
LOG_MAX: Final = 200
#: After a restart, how long the restore of a stored snapshot waits for its controls' integration to load (s), and
#: how often it looks.
STARTUP_WAIT_S: Final = 120.0
STARTUP_LOOK_S: Final = 5.0
#: A start that raised is tried again after this long, doubled each time up to the most (s).
START_RETRY_S: Final = 30.0
START_RETRY_MAX_S: Final = 600.0
#: How long after a hand-back the plant's changes still count as SpotNav's own (s): the outside-control watch
#: (`acting`) must not take a sensor that shows the hand-back late for someone else's change.
ACTING_GRACE_S: Final = 300.0
#: What a heat raise held drives the compressor above anything the learned house peak saw (kW, one phase): taken
#: off each phase's HA-down cap while the raise stands (`_heat_standing_a`).
HEAT_RAISE_KW: Final = 1.0
#: The longest test action (min).
TRIAL_MAX_MIN: Final = 120
#: Looks in a row that may fail while SpotNav holds the plant before it hands back on the fewest reads.
FAILED_LOOKS_HAND_BACK: Final = 3
#: How long one write's service call may take before it is given up on as a write not taken (s): Home Assistant
#: has no timeout of its own, and an integration that never answers must not hold a look, the lock or a hand-back.
#: As long as a charger command's (`execution.chargers.base.DEFAULT_CALL_TIMEOUT_S`), so a slow cloud still answers.
CALL_TIMEOUT_S: Final = 30.0
#: The service each role's method writes with (a service role names its own, `Role.services`).
_METHOD_SERVICES: Final = {
    "number": ("set_value",), "select": ("select_option",), "switch": ("turn_on", "turn_off"), "button": ("press",),
}
#: Every action control may run when the report allows it; selling only in a mode that sells (`gate.Options.allow_sell`).
_PLAN_ACTIONS: Final = frozenset({ACTION_HOLD, ACTION_GRID_CHARGE, ACTION_DISCHARGE_HOUSE})
#: How often the site's energy sources are read again for the import-limit rule (s).
SOURCES_REFRESH_S: Final = 3600.0
#: A grid charge runs per phase only when the phases' caps allow at least this much more in all than the balanced
#: cap (kW): a balanced house keeps the brand's own charge.
PER_PHASE_GAIN_KW: Final = 0.5
#: The person is offered to accept the HA-down risk (`battery_control_accept_ha_down`) only where that would let a
#: grid charge run at least this much faster (kW): the HA-down cap with an empty house of cars, against what the fuse
#: allows with an empty house (and at most the battery's rated charge).
HA_DOWN_OFFER_GAIN_KW: Final = 1.0
#: The note shown after control is turned off on a plant taken over from AI mode.
NOTE_AI_MODE_BACK: Final = "ai_mode_back"

#: The mode of the report each action of the plan's shows when its command was seen to act (`_credit_plan`). A sale
#: is left out: the routine's step is the discharge to the house, and a sale may write another control.
_PLAN_PROVES: Final = {ACTION_HOLD: "hold", ACTION_GRID_CHARGE: "charge", ACTION_DISCHARGE_HOUSE: "discharge_house"}
#: The kind of command (`monitor.Command.kind`) that shows each of those modes: a command of another kind still
#: watched under the action (one kept from before a restart) shows nothing of it.
_PLAN_KINDS: Final = {"hold": "hold", "charge": "charge", "discharge_house": "discharge"}
#: How long the plan stands aside for a check past its deadline to start (s, `_plan_aside`): the hand-back and the
#: battery taking up the house again take well under a minute; the routine then runs its own length.
ASIDE_S: Final = 300.0
#: The least time between two such stand-asides (s): each is a hand-back and a new start of the plan's command
#: (writes to the plant, against the day's cap), so a plant where the routine keeps not starting after one (the
#: battery does not take up the house when left alone) is not handed back every hour for good.
ASIDE_GAP_S: Final = 86400.0
#: What the routine waits for (`waiting_for`) that ends a stand-aside at once: the moment it began at is gone, and
#: the plan's own rules (the hold for a car) are wanted back. Anything else may pass within `ASIDE_S` (the hand-back
#: still running, the battery not yet covering the house, the plant's own state read a poll late).
_ASIDE_ENDS: Final = frozenset({"car_charging", "headroom_too_small", "soc_out_of_range"})

#: `(action, battery kW magnitude, slot end)` of the running slot, or `None`.
PlanSource = Callable[[datetime], tuple[str, float, datetime | None] | None]
#: What an exported kWh earns now (minor per kWh), `None` when unknown.
SellSource = Callable[[datetime], float | None]

_UNSET: Final = object()


#: The store's key for snapshots whose brand had no recipe at a start: each the stored `state`, `ledger` and `monitor`
#: with a `key` ("battery" or "export"), kept until a release with the recipe hands them back.
ORPHANED: Final = "orphaned"

#: The checklist's step where the plant's recipe writes only after a person's hardware test passed on its firmware
#: (`commands.BrandControl.hardware_test_first`): shadow only until then.
NEED_HARDWARE_TEST_FIRST: Final = "hardware_test_first"
#: The checklist's step where such a recipe passed a person's hardware test once on this plant, and the automatic
#: check after a change of firmware, integration or recipe revision failed (or a later hardware test did): shadow
#: only until a hardware test passes again on the key now.
NEED_HARDWARE_TEST_AGAIN: Final = "hardware_test_again"
#: The checklist's step where such a recipe passed a person's hardware test on this plant and waits for the control
#: code SpotNav's developer sends once they read the test's result (`battery/codes.py`): shadow only until it is
#: entered. The code is the developer's approval of the test result: control needs both.
NEED_CONTROL_CODE: Final = "control_code"
#: The checklist's step (and the hardware test's refusal) where such a recipe was never tested on this plant and the
#: person has not signed up as a tester for the brand (`battery/tester.py`): signing up unlocks the hardware test.
NEED_TESTER_SIGNUP: Final = "tester_signup"
#: The checklist's step where this SpotNav release holds the brand's recipe back on the plant's firmware or the
#: recipe's revision (`commands.blocked`): shadow only, whatever was passed.
NEED_RECIPE_BLOCKED: Final = "recipe_blocked"
#: The checklist's step where a hand-back could not write roles back: what to set by hand (`_restore_by_hand`).
NEED_RESTORE_BY_HAND: Final = "restore_by_hand"
#: The needs a hardware test is what answers (never a reason to refuse one).
_HARDWARE_TEST_NEEDS: Final = frozenset({NEED_HARDWARE_TEST_FIRST, NEED_HARDWARE_TEST_AGAIN})
#: The needs a hardware test never waits for: what it answers, the tester sign-up (its own refusal,
#: `hardware_test_refusal`) and the control code (which comes after the test).
_TEST_IGNORES: Final = _HARDWARE_TEST_NEEDS | {NEED_CONTROL_CODE, NEED_TESTER_SIGNUP}
#: The plan's cap rule where control runs no grid charge at all (`BatteryControl.plan_grid_charge_cap`): a cap of 0.
RULE_NO_GRID_CHARGE: Final = "no_grid_charge"
#: The plan's cap rule where the battery's own charge power entity holds the charge below the fuse's cap
#: (`BrandControl.charge_power_bound`).
RULE_CHARGE_POWER_ENTITY: Final = "charge_power_entity"

#: Refusals of the per-phase import-limit rule that leave it a way for the person (`BatteryControl._phase_path_open`):
#: none, a step they can take (enable the Output Type or the grid sensor status, turn the phase switch on), or the
#: site's sources not read yet (a moment after a start: the HA-down row must not flash up meanwhile).
PHASE_PATH_STEPS: Final = frozenset(
    {None, "sources_unknown", "output_type_unread", "phase_control_off", "grid_sensor_offline", *(
        # The import limit not tested on this firmware, or the firmware not read (owner decision 7): the step is the
        # field test on it, never the HA-down acceptance.
        "firmware_not_tested", "firmware_unread",
    )}
)
#: Path steps that leave nothing for the tap to write (`import_limit_write_kw`, `async_set_import_limit`): the limit
#: is not trusted on this firmware until the field test ran on it.
FIRMWARE_STEPS: Final = frozenset({"firmware_not_tested", "firmware_unread"})
#: How long the person's import-limit write is read back for, and how often (s).
IMPORT_LIMIT_READ_BACK_S: Final = 15.0
IMPORT_LIMIT_READ_BACK_STEP_S: Final = 0.5
