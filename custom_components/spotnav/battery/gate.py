"""Whether SpotNav's battery and export-limit control may write on a site, and what it may do (pure).

The site's options (`Options`, kept in the site entry's data beside its other settings, all `battery_control_*`):

* `battery_control_enabled` (default off): the person's opt-in. Turning it on does not turn control on: it starts the
  verification routine (`verify.py`), and control is on only once that passed for the plant's current firmware and
  integration version (`firmware_key`).
* `battery_control_shadow_only`: while on, nothing is written whatever else is set; the shell only logs what it would
  do. Its default depends on the plant (`shadow_default`): **off** for a recipe tested on a real plant
  (`commands.BrandControl.field_tested`; none yet: Sigenergy's flips once the owner's on-site tests are rerun) and
  for a beta tester's site (`beta.learning_gate_open`), **on** for every other recipe. A value the site stored is kept
  whatever the default (a site that set it keeps it). Off only lets the verification routine start: control still
  runs only once that passed, under every fuse rule. A recipe marked `hardware_test_first` (every brand but
  Sigenergy; a plant whose control already ran keeps it, `commands.GATED_AFTER_RUNNING`) counts as shadow only, whatever is stored and on a beta site too, until a person's hardware test passed
  on the plant's firmware key (`hardware_test_passed`, kept by the shell per key) and the developer's control code for
  the installation and brand was entered (`codes.py`, `tester.py`: the code approves the test's result, so control
  needs both); the checklist says so (`tester_signup`, `hardware_test_first`, `control_code`) and the test itself
  runs once the person signed up as a tester.
* `battery_control_ai_takeover_ack`: the person read that SpotNav takes the plant over from Sigenergy's AI mode and
  that turning control off leaves it in Maximum Self Consumption. Without it a plant in AI mode is only watched.
* `battery_mode` (`CONF_MODE`: self_consumption | balanced | max_profit, default balanced): the person's choice of
  what the battery is for. Balanced and Max profit let the plan discharge the battery to the grid
  (`export_discharge`; Balanced only through its spread gate, which the plan weighs: surplus beyond the house's need,
  at a high margin), capped so that it and the sun's export keep every phase under the fuse
  (`export_discharge_cap_kw`); Self-consumption never does. It replaced
  `battery_control_allow_sell`, which an early 3.0 build stored: read once at load, as the source of a one-time
  migration (`legacy_mode`, `BatteryControl._migrate_mode`), then removed from the entry; never written or sent.
* `battery_wear_minor_per_kwh` (`CONF_WEAR`: a number in the area's minor unit per kWh each way, or absent / `None`
  = the currency's default, `plan_model.WEAR_MINOR_PER_KWH`): what the plan weighs as the battery's wear. A write
  takes a finite number from 0 to `WEAR_MAX_FACTOR` times the currency's default (`valid_change`), or `None` to go
  back to the default. Balanced still doubles it.
* `battery_control_import_limit_acts` (default off): the owner's on-site test (T3) showed that the plant's own grid
  import limit acts under Remote EMS. Until then, and whenever that limit reads more than keeps the worst phase
  under the fuse (`import_limit_max_kw`: the plant limits the **total** import, not each phase), a grid
  charge is capped so that it, the chargers' last currents (the shell adds a hot-water boost SpotNav or the button
  started and a heat raise held, `BatteryControl._heat_standing_a`) and the house's peak keep every phase under the fuse
  while Home Assistant is down (`grid_charge_cap_kw`; Sigenergy has no dead-man: a command stays until something
  writes it). The house's peak is each phase's own, learned from the site's meter (`site/phase_peaks.py`, the days
  before it from the recorder's history), once a week of it is kept, with a further margin that falls to
  nothing at two weeks; until then a peak of at least `HOUSE_PEAK_DEFAULT_KW` all on one phase. Where the plant
  takes a target per phase (`commands.PhaseControl`, verified as the mode `per_phase`) the cap is each phase's own
  (`grid_charge_phase_caps_kw`), so a house that loads one phase leaves the others' room to the charge. Where the
  plant's own dead-man or lease, renewed by SpotNav, was **measured** on this firmware (`expiry_lifts`: the recipe's
  `commands.Expiry`, recorded from a field test or the verification's mode `expiry`; a maker's word never lifts, nor
  an integration's own renewal loop) the command does not stay, and the cap is the fuse less the margin: the live
  clamp bounds the charge. A plant that holds its import limit **per phase**
  (`commands.ImportLimitPhases`, Sigenergy with independent phase power control on) needs no option: once the
  owner's on-site test is recorded in the recipe, and while the switch reads on, the grid sensor online and the
  Output Type L1/L2/L3/N, and the site's own option says its limit acts, a limit at most three times the fuse less the
  site's safety margin (`import_limit_phase_max_kw`) lifts the cap (`grid_charge_cap_kw`); the live clamp per phase
  stays. Recorded for Sigenergy after the owner's on-site test of 2026-11-21. The residual risk is accepted (owner
  decision 2026-11-21): a second fault while Home Assistant is down (the grid sensor dropping, 40040 raised or 40030
  turned off in the app) goes unchecked until Home Assistant reads again, as it does under Sigenergy's own AI mode.
* `battery_control_accept_ha_down` (default off; administrators only, like every option): the person accepts that if
  Home Assistant stops during a grid charge of the home battery and the house draws a lot, a phase may pass the main
  fuse and the fuse may open (owner decision 2026-11-21, for brands without the per-phase hold above). With it the
  HA-down cap above is not reckoned with the house at its peak: a grid charge is capped at what the fuse allows with
  an **empty** house, each phase's fuse less the site's own safety margin (`ha_down_accepted_cap_kw`,
  `grid_charge_phase_caps_kw`), so a command can never ask for more than an empty house could take. The live clamp
  per phase while Home Assistant runs (`executor._phase_room_kw`, `HEADROOM_MARGIN_A`, every look), the write caps,
  the read-back, the hand-back and the chargers' load balancing are unchanged. It changes nothing where the plant's
  own import limit guards the fuse (either rule above: no cap of this kind) or a command's own measured expiry does
  (`expiry_lifts`): those come first, and the card does not offer it there. It is given for one setup, the
  site's main fuse and the plant's brand (`CONF_ACCEPT_HA_DOWN_SETUP`, `ha_down_setup_state`): once either changes it
  counts as off and is cleared.

The verification report, per firmware key (`merge_report`): each mode's last decisive outcome. `obeyed` (`restored`
for normal) passes; `ignored`, `partial` and an abort that shows the plant does not do as told (`FAILING_ABORTS`)
fail; anything else (`not_judgeable:*`, an abort for the moment's circumstances: the fuse, a stale reading, a stop)
is pending and tried again at a later suitable moment (`modes_to_run`, `suitable`), at most `MAX_ATTEMPTS` runs.
**The report decides which actions are allowed** (`allowed_actions`): hold and normal must pass for any control; a
grid charge only if `charge` passed (where it could be tried only as a charge on one phase, a charge per phase
only, once `per_phase` is verified too: `charge_by_phases`), a discharge for the house only if `discharge_house` passed, a discharge to the
grid only in a mode that sells (Balanced, Max profit) on top of that (and `charge` passed too where the discharge writes a signed
power target: the house step, the plant's own mode, says nothing about its sign). The export limit needs no routine of its own: it starts
on its live check (the executor's monitor hands it back and blocks it when the export does not stop).

The order in which loads give way when a phase nears the fuse, as it runs: the car first (the site's load balancing
lowers it within seconds), the battery next (the executor's 10 s look; a full hand-back under `RESTORE_HEADROOM_A`),
SpotNav's own hot-water boost last (shed after two hot-water looks over the fuse, a minute apart:
`heat/dhw_runtime.py`), and a heat raise never (it only drives the compressor; the car and the battery give way).
Who gets the room when it is short (`battery_yields`): with the solar priority "car first" the battery's grid
charge yields to a held car, unless every held car's departure has room; with "battery first" the battery keeps
its rate and the car backs off.

Pure: no Home Assistant import; times are aware datetimes from the caller.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from ..core.electric import phase_volts
from .commands import ACTION_CURTAIL_EXPORT, Expiry
from .plan_model import (
    DEFAULT_CYCLE_COST,
    DEFAULT_MODE,
    EXPORT_OFF,
    MODE_MAX_PROFIT,
    MODES,
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
    mode_named,
    wear_minor_per_kwh,
)
from .shadow import parse_aware_datetime

CONF_ENABLED: Final = "battery_control_enabled"
CONF_SHADOW_ONLY: Final = "battery_control_shadow_only"
CONF_AI_TAKEOVER_ACK: Final = "battery_control_ai_takeover_ack"
#: The legacy option (read once at load as the migration's source, then removed; never written).
CONF_ALLOW_SELL: Final = "battery_control_allow_sell"
#: The site's battery mode (the plan's `plan_runtime.MODE_KEY`).
CONF_MODE: Final = "battery_mode"
CONF_IMPORT_LIMIT_ACTS: Final = "battery_control_import_limit_acts"
#: The site's own wear per kWh each way in the area's minor unit (`None`/absent: the currency's default).
CONF_WEAR: Final = "battery_wear_minor_per_kwh"
#: The most a write may set the wear to, as a multiple of the currency's default.
WEAR_MAX_FACTOR: Final = 10.0
CONF_ACCEPT_HA_DOWN: Final = "battery_control_accept_ha_down"
#: The setup the acceptance was given for (`ha_down_setup`): stored beside it, never written by a request.
CONF_ACCEPT_HA_DOWN_SETUP: Final = "battery_control_accept_ha_down_setup"
#: Every option and its default.
DEFAULTS: Final[Mapping[str, bool]] = {
    CONF_ENABLED: False,
    CONF_SHADOW_ONLY: True,
    CONF_AI_TAKEOVER_ACK: False,
    CONF_IMPORT_LIMIT_ACTS: False,
    CONF_ACCEPT_HA_DOWN: False,
}
#: The keys a write may set: the booleans above, the mode and the wear.
WRITABLE: Final = (*DEFAULTS, CONF_MODE, CONF_WEAR)

#: The battery's modes the routine judges, and normal (the hand-back's check).
BATTERY_MODES: Final = ("hold", "charge", "discharge_house")
MODE_NORMAL: Final = "normal"
#: The capability a plant with targets per phase is checked for (`verify.py`); never needed for control to run.
MODE_PER_PHASE: Final = "per_phase"
#: Whether a command's own expiry (`commands.Expiry`) acts on this firmware (`verify.py`): never needed for control to
#: run, and never failing the report; only a pass changes anything (`expiry_measured`: the HA-down cap may lift).
MODE_EXPIRY: Final = "expiry"
#: Outcomes that pass.
PASSING: Final = frozenset({"obeyed", "restored"})
#: Outcomes that fail outright.
FAILING: Final = frozenset({"ignored", "partial"})
#: The hardware test's charge step on a recipe whose charge power is the plant's own (`BrandControl.fixed_charge`,
#: Tesla) when that power does not fit under the fuse cap: the step cannot be run at a safe size on this plant, so the
#: test goes on without it and may pass (`hardware_test_passed`). It is no pass of the charge: control runs no grid
#: charge until the routine's own charge step passed (`allowed_actions`), which it tries again later as pending.
NOT_APPLICABLE_FIXED_CHARGE: Final = "not_applicable:fixed_charge_over_cap"

#: Abort reasons (`aborted:<reason>`) that show the plant does not do as told; every other abort is the moment's.
FAILING_ABORTS: Final = (
    "write_not_taken", "no_effect", "changed_outside_spotnav", "restore_failed", "command_timeout", "not_obeyed",
    "control_not_found", "control_disabled", "action_not_supported", "no_restore_value",
)
#: Runs of the routine without a decision before it gives up (each run writes to the plant).
MAX_ATTEMPTS: Final = 5
#: The least time between two runs (s).
RETRY_GAP_S: Final = 3600.0
#: When a run may start: the battery's level (%), its discharge (kW, so a hold can be judged), the fuse's headroom (A).
SUITABLE_SOC_PCT: Final = (30.0, 80.0)
SUITABLE_DISCHARGE_KW: Final = 0.3
SUITABLE_HEADROOM_A: Final = 4.0
#: A re-verification after a firmware or integration change waits for an evening (local hours, end excluded).
EVENING_HOURS: Final = (17, 22)

#: The phases a learned house peak is kept for (`site/phase_peaks.py`).
PHASE_NAMES: Final = ("L1", "L2", "L3")
_PHASE_NAMES: Final = PHASE_NAMES
#: The house's normal peak when its forecast has no p90 for the hour (kW), for the grid charge cap.
HOUSE_PEAK_DEFAULT_KW: Final = 4.0
#: A grid charge below this is not worth running (kW); the executor imports it.
MIN_RATE_KW: Final = 0.5
#: The longest a command's own measured expiry (`commands.Expiry`, `expiry_lifts`) may take to stop a charge per
#: phase for the HA-down cap to be lifted on its account (s): about what a fuse and a breaker carry a phase's
#: overload for without opening (see `grid_charge_phase_caps_kw`).
SILENCE_FALLBACK_MAX_S: Final = 60.0
#: How far apart the looks are that see a fallback (s, `runtime.TICK_S`): a fallback measured by the verification's
#: mode `expiry` counts once it came within `SILENCE_FALLBACK_MAX_S` and one look of the last write.
EXPIRY_LOOK_S: Final = 10.0
#: A held car's departure has room when this many hours at its planned power are left over (h).
DEPARTURE_ROOM_H: Final = 2.0

#: The plan's action each mode proves.
_ACTION_MODE: Final = {
    ACTION_HOLD: "hold", ACTION_GRID_CHARGE: "charge", ACTION_DISCHARGE_HOUSE: "discharge_house",
    ACTION_EXPORT_DISCHARGE: "discharge_house",
}

Verdict = Literal["pass", "fail", "pending"]
ReportStatus = Literal["none", "pending", "passed", "failed"]


def _flag(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def valid_change(key: str, value: Any, *, wear_default: float = DEFAULT_CYCLE_COST) -> bool:
    """Whether a write may set this key to this value: a boolean for an option, one of `MODES` for the mode, and for
    the wear `None` (the default) or a finite number from 0 to `WEAR_MAX_FACTOR` times [wear_default] (the area
    currency's own figure)."""
    if key == CONF_MODE:
        return isinstance(value, str) and value in MODES
    if key == CONF_WEAR:
        if value is None:
            return True
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return False
        return 0 <= value <= WEAR_MAX_FACTOR * wear_default
    return key in DEFAULTS and isinstance(value, bool)


def stored_wear(config: Mapping[str, Any]) -> float | None:
    """The site's own wear (`CONF_WEAR`) when it holds a finite number of at least 0, else `None` (the default)."""
    value = config.get(CONF_WEAR)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def wear_default(currency: str | None) -> tuple[float, bool]:
    """The currency's own wear per kWh each way, and whether the table lacks it (then the SEK figure)."""
    wear = wear_minor_per_kwh(currency)
    return (DEFAULT_CYCLE_COST, True) if wear is None else (wear, False)


def site_wear(config: Mapping[str, Any], currency: str | None) -> tuple[float, bool]:
    """The wear the plan weighs on a site: its own (`CONF_WEAR`) when set, else the currency's default; and whether
    that default stood in for a currency the table lacks."""
    own = stored_wear(config)
    return (own, False) if own is not None else wear_default(currency)


def legacy_mode(config: Mapping[str, Any]) -> str | None:
    """The mode to store once for a site that never chose one but had selling allowed, else `None`."""
    if CONF_MODE not in config and config.get(CONF_ALLOW_SELL) is True:
        return MODE_MAX_PROFIT
    return None


@dataclass(frozen=True, slots=True)
class Options:
    """The site's battery control options (see the module)."""

    enabled: bool = False
    shadow_only: bool = True
    ai_takeover_ack: bool = False
    mode: str = DEFAULT_MODE
    import_limit_acts: bool = False
    accept_ha_down: bool = False
    wear_minor_per_kwh: float | None = None

    @classmethod
    def from_config(cls, config: Mapping[str, Any], *, shadow_default: bool = True) -> Options:
        """From the site entry's data: anything but a stored boolean is its default (`shadow_default` for shadow
        only: on unless the caller knows the plant's recipe was field tested or the site is a beta tester)."""
        return cls(
            enabled=_flag(config.get(CONF_ENABLED), False),
            shadow_only=_flag(config.get(CONF_SHADOW_ONLY), shadow_default),
            ai_takeover_ack=_flag(config.get(CONF_AI_TAKEOVER_ACK), False),
            mode=_mode(config),
            import_limit_acts=_flag(config.get(CONF_IMPORT_LIMIT_ACTS), False),
            accept_ha_down=_flag(config.get(CONF_ACCEPT_HA_DOWN), False),
            wear_minor_per_kwh=stored_wear(config),
        )

    @property
    def allow_sell(self) -> bool:
        """Whether the mode sells from the battery: Max profit wherever it pays, Balanced through its spread gate
        (the plan weighs it), Self-consumption never."""
        mode = mode_named(self.mode)
        return mode is not None and mode.export != EXPORT_OFF

    @property
    def writes(self) -> bool:
        """Whether anything may be written at all (the routine, control, the export limit)."""
        return self.enabled and not self.shadow_only

    def as_dict(self) -> dict[str, bool | str | float | None]:
        """The payload's options."""
        return {
            CONF_ENABLED: self.enabled, CONF_SHADOW_ONLY: self.shadow_only, CONF_AI_TAKEOVER_ACK: self.ai_takeover_ack,
            CONF_IMPORT_LIMIT_ACTS: self.import_limit_acts, CONF_ACCEPT_HA_DOWN: self.accept_ha_down,
            CONF_MODE: self.mode, CONF_WEAR: self.wear_minor_per_kwh,
        }


def shadow_default(*, field_tested: bool, beta: bool) -> bool:
    """Shadow only's default for a site that never stored it: off for a field-tested recipe or a beta tester."""
    return not (field_tested or beta)


def _step_status(outcome: Any) -> str:
    """One mode's outcome as the hardware test's step shows it: `passed`, `failed`, `not_judgeable`, `skipped` (an
    action the recipe does not have, `verify.Verification.unsupported`) or `open` (not run, or stopped)."""
    if outcome == "skipped" or (isinstance(outcome, str) and outcome.startswith("not_applicable:")):
        return "skipped"
    if isinstance(outcome, str) and outcome.startswith("not_judgeable:"):
        return "not_judgeable"
    return {"pass": "passed", "fail": "failed"}.get(mode_verdict(outcome), "open")


def hardware_test_passed(
    report: Mapping[str, Any] | None, supported: Iterable[str] | None = None, *, deadman_required: bool = False,
) -> bool:
    """Whether a person's finished hardware test (its verification's report) shows the battery doing as told: what a
    recipe with `BrandControl.hardware_test_first` waits for before anything is written outside a test. The hold and
    the hand-back (`normal`) passed; the grid charge passed where the recipe has one (`supported`, the modes it has
    an action for, `verify.supported_modes`; `None`: all), else skipped, or not applicable where the plant's own
    charge power does not fit under the cap (`NOT_APPLICABLE_FIXED_CHARGE`); the dead-man step (`expiry`, or
    `expiry:<phase>` per phase, where the test times one) passed or could not be judged (`deadman_required`: passed, and
    there); every other step passed, could not be judged or was skipped. A step stopped or still open is no pass."""
    if not isinstance(report, Mapping):
        return False
    modes = _modes(report)
    status = {name: _step_status((row or {}).get("outcome")) for name, row in modes.items()}
    must = {"hold", MODE_NORMAL} | ({"charge"} if supported is None or "charge" in set(supported) else set())
    if (modes.get("charge") or {}).get("outcome") == NOT_APPLICABLE_FIXED_CHARGE:
        # A fixed charge that does not fit under the fuse cap here: the one step that may stay unrun.
        must.discard("charge")
    if any(status.get(name) != "passed" for name in must):
        return False
    deadman = [value for name, value in status.items() if name.split(":", 1)[0] == MODE_EXPIRY]
    if any(value not in ("passed", "not_judgeable") for value in deadman):
        return False
    if deadman_required and (not deadman or any(value != "passed" for value in deadman)):
        # A recipe whose every action is a command that stands until it expires (`BrandControl.deadman_must_pass`):
        # its dead-man must have been timed, not merely tried; a step not judgeable is run again later.
        return False
    return all(value in ("passed", "not_judgeable", "skipped") for value in status.values())


def _mode(config: Mapping[str, Any]) -> str:
    """The stored mode; none stored, or anything unknown, is Balanced (a legacy sell option is migrated at load)."""
    value = config.get(CONF_MODE)
    return value if isinstance(value, str) and value in MODES else DEFAULT_MODE


def parse_time(value: Any) -> datetime | None:
    """A stored ISO time (a report's `last_run_at`), `None` for anything else."""
    return parse_aware_datetime(value)


def firmware_key(firmware: str | None, integration: str | None, version: str | None) -> str:
    """What a verification report is kept under: the plant's firmware and its integration's version (a change of
    either can change what the plant does with a command)."""
    return f"{firmware or 'unknown'} | {integration or 'unknown'} {version or 'unknown'}"


def firmware_known(key: str) -> bool:
    """Whether a report key (`firmware_key`) names a firmware that was read."""
    return not key.startswith("unknown |")


# --- the report ------------------------------------------------------------------------------------------------------


def mode_verdict(outcome: Any) -> Verdict:
    """One mode's outcome as pass, fail or pending (tried again later)."""
    if not isinstance(outcome, str):
        return "pending"
    if outcome in PASSING:
        return "pass"
    if outcome in FAILING:
        return "fail"
    if outcome.startswith("aborted:") and outcome.removeprefix("aborted:").startswith(FAILING_ABORTS):
        return "fail"
    return "pending"


def _modes(report: Mapping[str, Any] | None) -> Mapping[str, Any]:
    modes = None if report is None else report.get("modes")
    return modes if isinstance(modes, Mapping) else {}


def merge_report(
    previous: Mapping[str, Any] | None, run: Mapping[str, Any], *, key: str, attempts: int, now: datetime
) -> dict[str, Any]:
    """The report for `key` with a run's outcomes added: a mode decided (passed or failed) keeps its row; a mode
    still pending takes the run's row when the run has one."""
    modes: dict[str, Any] = {name: dict(row) for name, row in _modes(previous).items() if isinstance(row, Mapping)}
    for name, row in _modes(run).items():
        if not isinstance(row, Mapping):
            continue
        kept = modes.get(name)
        if (
            kept is not None and mode_verdict(kept.get("outcome")) != "pending"
            # An expiry decided for another expiry than the one this run tried (the recipe changed) is not kept.
            and not (name.startswith(MODE_EXPIRY) and kept.get("expiry_id") != row.get("expiry_id"))
        ):
            continue
        modes[name] = _expiry_row(row, kept) if name.startswith(MODE_EXPIRY) else dict(row)
    return {
        "key": key,
        "brand": run.get("brand"),
        "firmware": run.get("firmware"),
        "first_run_at": (previous or {}).get("first_run_at") or run.get("started_at"),
        "last_run_at": run.get("started_at"),
        "updated_at": now.isoformat(),
        "attempts": attempts,
        "modes": modes,
    }


#: Separate runs on one firmware key that must each give the mode `expiry` `obeyed` before it counts as measured
#: (owner decision 4: "measured = two passing runs").
EXPIRY_PASSES: Final = 2
#: A run's `obeyed` before the passes are complete: pending, so the next run tries it again.
EXPIRY_PASSED_ONCE: Final = "obeyed_once"


def _expiry_row(row: Mapping[str, Any], kept: Mapping[str, Any] | None) -> dict[str, Any]:
    """A run's expiry row merged with the one kept (still pending, for the same expiry): `obeyed` counts a pass
    (`passes`), and is `obeyed` in the report only once `EXPIRY_PASSES` runs passed, `EXPIRY_PASSED_ONCE` (pending)
    before; a pending outcome keeps the passes so far; any other (`ignored`, a failing abort) starts them again."""
    same = kept is not None and kept.get("expiry_id") == row.get("expiry_id")
    passes = kept.get("passes", 0) if same and kept is not None else 0
    passes = passes if isinstance(passes, int) and not isinstance(passes, bool) else 0
    out = dict(row)
    outcome = row.get("outcome")
    if outcome == "obeyed":
        passes += 1
        out["outcome"] = "obeyed" if passes >= EXPIRY_PASSES else EXPIRY_PASSED_ONCE
    elif mode_verdict(outcome) != "pending":
        passes = 0
    out["passes"] = passes
    return out


def report_status(report: Mapping[str, Any] | None) -> ReportStatus:
    """`passed` once hold and normal passed; `failed` once either failed, or the routine gave up; else `pending`."""
    if report is None:
        return "none"
    modes = _modes(report)
    needed = [mode_verdict((modes.get(name) or {}).get("outcome")) for name in ("hold", MODE_NORMAL)]
    if "fail" in needed:
        return "failed"
    if needed == ["pass", "pass"]:
        return "passed"
    attempts = report.get("attempts")
    if isinstance(attempts, int) and attempts >= MAX_ATTEMPTS:
        return "failed"
    return "pending"


def failed_mode(report: Mapping[str, Any] | None) -> str | None:
    """The first mode that failed (hold, normal, then the others), for the one line the person sees."""
    modes = _modes(report)
    for name in ("hold", MODE_NORMAL, *BATTERY_MODES):
        if mode_verdict((modes.get(name) or {}).get("outcome")) == "fail":
            return name
    return None


def modes_to_run(report: Mapping[str, Any] | None, supported: Iterable[str] | None = None) -> tuple[str, ...]:
    """The battery modes a next run tries: those still pending (normal ends every run by itself). When only normal is
    left, hold runs again, so that the hand-back normal judges is one of something written. `supported`: the modes the
    plant's recipe has an action for (`verify.supported_modes`; a limits-only recipe has no grid charge), `None` all."""
    modes = _modes(report)
    names = BATTERY_MODES if supported is None else tuple(name for name in BATTERY_MODES if name in set(supported))
    pending = tuple(name for name in names if mode_verdict((modes.get(name) or {}).get("outcome")) == "pending")
    if not pending and mode_verdict((modes.get(MODE_NORMAL) or {}).get("outcome")) == "pending":
        return ("hold",)
    return pending


def needs_run(report: Mapping[str, Any] | None, supported: Iterable[str] | None = None) -> bool:
    """Whether the routine has anything left to try for this key: a mode still pending (one that could not be judged
    is tried again at a later suitable moment by itself, also once hold and normal passed), never after `MAX_ATTEMPTS`
    runs (each run writes to the plant; a mode still pending then stays off). `supported` as for `modes_to_run`."""
    status = report_status(report)
    if status == "failed":
        return False
    if status == "none":
        return True
    attempts = (report or {}).get("attempts")
    if isinstance(attempts, int) and attempts >= MAX_ATTEMPTS:
        return False
    return bool(modes_to_run(report, supported)) or (
        mode_verdict((_modes(report).get(MODE_NORMAL) or {}).get("outcome")) == "pending"
    )


def allowed_actions(
    report: Mapping[str, Any] | None, options: Options, *, export_needs_charge: bool = False
) -> frozenset[str]:
    """The plan actions control may run on this plant (see the module); empty until hold and normal passed.
    `export_needs_charge`: the discharge to the grid writes a signed target whose sign only the charge step showed
    (`commands.BrandControl.export_signed_by_charge`), so it needs `charge` passed as well."""
    if report_status(report) != "passed":
        return frozenset()
    modes = _modes(report)
    passed = {name for name, row in modes.items() if isinstance(row, Mapping) and mode_verdict(row.get("outcome")) == "pass"}
    # `charge` shown only per phase (`verify.py`) proves a charge per phase, once the capability is verified too;
    # it says nothing of the sign of the brand's own balanced target.
    by_phases = charge_by_phases(report)
    allowed = {ACTION_HOLD}
    if "charge" in passed and (not by_phases or per_phase(report) == "verified"):
        allowed.add(ACTION_GRID_CHARGE)
    if "discharge_house" in passed:
        allowed.add(ACTION_DISCHARGE_HOUSE)
        if options.allow_sell and (not export_needs_charge or ("charge" in passed and not by_phases)):
            allowed.add(ACTION_EXPORT_DISCHARGE)
    return frozenset(allowed)


def charge_by_phases(report: Mapping[str, Any] | None) -> bool:
    """Whether the mode `charge` was tried as a charge on one phase (`verify.VIA_PER_PHASE`: the balanced charge had
    no room): a grid charge then runs only per phase."""
    row = _modes(report).get("charge")
    return isinstance(row, Mapping) and row.get("via") == "per_phase"


def per_phase(report: Mapping[str, Any] | None) -> str:
    """The capability "a target per phase acts on that phase, on the meter's phase of that name" on this firmware:
    `verified`, `failed` or `pending` (not tried, or not judgeable yet). A grid charge runs per phase only on
    `verified`; anything else is the balanced charge. `charge` tried per phase that failed fails it too."""
    row = _modes(report).get(MODE_PER_PHASE)
    verdict = mode_verdict(row.get("outcome")) if isinstance(row, Mapping) else "pending"
    if charge_by_phases(report) and mode_verdict((_modes(report).get("charge") or {}).get("outcome")) == "fail":
        # `charge` tried per phase found the targets not followed, of the wrong sign or on another of the meter's
        # phases: that is this capability's answer too.
        verdict = "fail"
    return {"pass": "verified", "fail": "failed"}.get(verdict, "pending")


def per_phase_failed(report: Mapping[str, Any], outcome: str, now: datetime) -> dict[str, Any]:
    """The report with the capability withdrawn: a running charge per phase showed the plant not following its
    targets (`executor._judge_phases`). The row is decided, so no later run tries it on this firmware."""
    modes = {name: dict(row) for name, row in _modes(report).items() if isinstance(row, Mapping)}
    modes[MODE_PER_PHASE] = {**modes.get(MODE_PER_PHASE, {}), "mode": MODE_PER_PHASE, "outcome": outcome,
                             "withdrawn_at": now.isoformat()}
    return {**report, "modes": modes, "updated_at": now.isoformat()}


def hold_variant(report: Mapping[str, Any] | None) -> str | None:
    """The hold variant that passed (`verify.py`), `None` when hold has not passed or the brand has one hold."""
    row = _modes(report).get("hold")
    if not isinstance(row, Mapping) or mode_verdict(row.get("outcome")) != "pass":
        return None
    variant = row.get("variant")
    return variant if isinstance(variant, str) else None


def variants_tried(report: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    """The hold variants an earlier run tried, with their outcomes (a next run starts after those that failed)."""
    row = _modes(report).get("hold")
    rows = row.get("variants") if isinstance(row, Mapping) else None
    return tuple(item for item in rows if isinstance(item, Mapping)) if isinstance(rows, list) else ()


def without_removed_hold(report: Mapping[str, Any] | None, names: tuple[str, ...]) -> Mapping[str, Any] | None:
    """The report with a hold that passed in a variant the recipe no longer has (`names`; Sigenergy's Maximum Self
    Consumption hold before 2026-11-23) taken back to pending, its variants tried kept: the routine checks the
    recipe's hold on that plant again before control relies on it. The same report when nothing is stale."""
    passed = hold_variant(report)
    if report is None or passed is None or not names or passed in names:
        return report
    row = dict(_modes(report).get("hold") or {})
    for name in ("outcome", "variant", "commanded_at", "decided_at"):
        row.pop(name, None)
    # A check to run again, not one that ran out of attempts: its count starts over, and no gap is owed to its last
    # run (`report_status`, `needs_run`, `RETRY_GAP_S`).
    migrated = {key: value for key, value in report.items() if key != "last_run_at"}
    return {**migrated, "attempts": 0, "modes": {**_modes(report), "hold": row}}


def hold_variant_to_run(report: Mapping[str, Any] | None, names: tuple[str, ...]) -> str | None:
    """The hold variant control and a test action run (`names`: the brand's, in order): the one that passed, else the
    first not recorded as failed; `None` when every one failed or the brand has one hold."""
    passed = hold_variant(report)
    if passed in names:
        return passed
    failed = {str(row.get("variant")) for row in variants_tried(report) if mode_verdict(row.get("outcome")) == "fail"}
    return next((name for name in names if name not in failed), None)


def keep_reports(reports: Mapping[str, Any], key: str, report: Mapping[str, Any], *, most: int) -> dict[str, Any]:
    """The reports by key with this one put in (the newest last), at most `most`, the oldest dropped first."""
    kept = {name: value for name, value in reports.items() if name != key and isinstance(value, Mapping)}
    kept[key] = dict(report)
    ordered = sorted(kept.items(), key=lambda item: str(item[1].get("updated_at") or ""))
    return dict(ordered[-most:])


# --- when the routine may run ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Moment:
    """The site now, for whether the routine may start."""

    local_hour: int
    soc_pct: float | None
    power_kw: float | None
    fresh: bool
    measurement_ok: bool
    headroom_a: float | None
    car_charging: bool
    #: Why the executor could not take the plant now (management, controls), `None` when it could.
    blocker: str | None
    #: The executor is idle and holds nothing.
    executor_idle: bool


def suitable(moment: Moment, *, evening: bool) -> str | None:
    """Why the routine may not start now, `None` when it may: a fresh reading, the battery between
    `SUITABLE_SOC_PCT` and discharging (so a hold can be judged), no car charging, the fuse with room, the plant
    SpotNav's to take and the executor idle; after a firmware change also an evening."""
    if evening and not EVENING_HOURS[0] <= moment.local_hour < EVENING_HOURS[1]:
        return "not_evening"
    if not moment.executor_idle:
        return "executor_busy"
    if moment.blocker is not None:
        return moment.blocker
    if not moment.fresh or not moment.measurement_ok or moment.soc_pct is None or moment.power_kw is None:
        return "stale_data"
    if not SUITABLE_SOC_PCT[0] <= moment.soc_pct <= SUITABLE_SOC_PCT[1]:
        return "soc_out_of_range"
    if moment.power_kw > -SUITABLE_DISCHARGE_KW:
        return "battery_not_discharging"
    if moment.car_charging:
        return "car_charging"
    if moment.headroom_a is None or moment.headroom_a < SUITABLE_HEADROOM_A:
        return "headroom_too_small"
    return None


# --- the grid charge cap -----------------------------------------------------------------------------------------------




def _usable(value: float | None) -> bool:
    return value is not None and not isinstance(value, bool) and math.isfinite(value)


def _learned_peaks(house_phase_peaks_a: Mapping[str, float] | None) -> list[float] | None:
    """The learned per-phase peaks, in the order of `PHASE_NAMES`, when each of the three phases has a usable one,
    else `None` (the conservative rule): a phase missing is never taken as a phase without load."""
    if not house_phase_peaks_a or set(house_phase_peaks_a) != set(_PHASE_NAMES):
        return None
    peaks: list[float] = []
    for name in _PHASE_NAMES:
        value = house_phase_peaks_a[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        peaks.append(max(float(value), 0.0))
    return peaks


def house_peaks_a(
    volts_between_phases: float, house_peak_kw: float | None, house_phase_peaks_a: Mapping[str, float] | None = None,
) -> tuple[float, float, float]:
    """The house's peak on L1, L2 and L3 (A) that every HA-down rule reckons with.

    With the learned peaks (`house_phase_peaks_a`, with their margins, `site/phase_peaks.py` `Learned.phase_peaks`)
    each phase's own, never below the house forecast's p90 for the hour spread evenly over the three phases (what
    the house is expected to reach at least, on a day colder than the days learned). Without them the conservative
    rule: the larger of that p90 and `HOUSE_PEAK_DEFAULT_KW`, all on one phase, and since any phase can be that
    one, on each."""
    volts = phase_volts(volts_between_phases)
    learned = _learned_peaks(house_phase_peaks_a)
    known = house_peak_kw is not None and math.isfinite(house_peak_kw)
    if learned is not None:
        floor_a = max(house_peak_kw, 0.0) * 1000.0 / (3.0 * volts) if known else 0.0  # type: ignore[arg-type]
        return (max(learned[0], floor_a), max(learned[1], floor_a), max(learned[2], floor_a))
    peak = max(HOUSE_PEAK_DEFAULT_KW, house_peak_kw) if known else HOUSE_PEAK_DEFAULT_KW  # type: ignore[type-var]
    amps = peak * 1000.0 / volts
    return (amps, amps, amps)


def _fuse_known(main_fuse_a: float | None, volts_between_phases: float, cars_a: float) -> bool:
    return (
        main_fuse_a is not None and math.isfinite(main_fuse_a) and main_fuse_a > 0 and volts_between_phases > 0
        and math.isfinite(cars_a)
    )


def import_limit_max_kw(
    main_fuse_a: float | None, volts_between_phases: float, *, margin_a: float, cars_a: float,
    house_peak_kw: float | None, house_phase_peaks_a: Mapping[str, float] | None = None,
) -> float | None:
    """The largest value of the plant's own grid import limit (kW) that keeps every phase under the fuse with Home
    Assistant down; `None` when the fuse or the chargers cannot be read (no value guards then).

    The rule for a plant that limits only the **total** import over its phases (seen on site 2026-11-18: 40040 at
    6 kW held the sum at 6.0 kW by cutting the battery's charge). Its charge is spread evenly while the limit does not
    bind (seen before the cut that evening). Once it binds, Sigenergy with independent phase power control on shares
    the cut out unevenly and holds each phase at `L / 3` (`import_limit_phase_max_kw`), but just below that point
    the charge is still even, so this rule takes the even spread. With the limit at `L` amps (the sum over the
    phases), a house drawing `h1, h2, h3` and a charge cut until the sum is `L`, a phase carries

        h_i + (L - h1 - h2 - h3) / 3  =  L / 3 + h_i - mean(h)

    which is largest when that phase is at its peak `p` while the other two draw nothing: `L / 3 + 2 p / 3`. The
    worst phase stays at or under the fuse less the margin exactly when

        L  <=  3 (fuse - margin) - 2 (p_max + cars)

    with `p_max` the largest phase's house peak (`house_peaks_a`: learned, else the conservative 4 kW on one
    phase) and the chargers' last currents (`cars_a`) counted as one phase's load, as the cap counts them (a
    three-phase car would count for nothing here: the plant's limit sees its whole draw). The form that takes every
    phase at its peak at once, `3 (fuse - margin) - sum(p_max - p_i)`, is **not** safe: it leaves the charge the room
    of the quiet phases, and a third of that lands on the loaded one (18 A on L2 alone on a 20 A fuse: 27 A)."""
    if not _fuse_known(main_fuse_a, volts_between_phases, cars_a):
        return None
    assert main_fuse_a is not None
    worst_a = max(house_peaks_a(volts_between_phases, house_peak_kw, house_phase_peaks_a))
    amps = 3.0 * (main_fuse_a - margin_a) - 2.0 * (worst_a + max(cars_a, 0.0))
    return max(amps, 0.0) * phase_volts(volts_between_phases) / 1000.0


def import_limit_holds(
    import_limit_kw: float | None, main_fuse_a: float | None, volts_between_phases: float, *, margin_a: float = 0.0,
    cars_a: float = 0.0, house_peak_kw: float | None = None, house_phase_peaks_a: Mapping[str, float] | None = None,
) -> bool:
    """Whether the plant's own grid import limit reads a value that keeps the worst phase under the fuse
    (`import_limit_max_kw`). A limit that reads nothing, or one too small to leave a grid charge worth running
    beside it (`MIN_RATE_KW`), guards nothing."""
    most = import_limit_max_kw(
        main_fuse_a, volts_between_phases, margin_a=margin_a, cars_a=cars_a, house_peak_kw=house_peak_kw,
        house_phase_peaks_a=house_phase_peaks_a,
    )
    return (
        most is not None and most >= MIN_RATE_KW and import_limit_kw is not None and math.isfinite(import_limit_kw)
        and 0.0 <= import_limit_kw <= most + 1e-6
    )


def import_limit_phase_max_kw(
    main_fuse_a: float | None, volts_between_phases: float, *, margin_a: float, cars_a: float,
) -> float | None:
    """The largest value of the plant's own grid import limit (kW) that keeps every phase under the fuse with Home
    Assistant down, for a plant that holds **each phase** at a third of it (`commands.ImportLimitPhases`); `None` when
    the fuse or the chargers cannot be read, or when no value guards.

    Such a plant holds a phase at `L / 3` by moving its own charge: when a phase's house and cars draw more than
    `L / 3` it stops charging on that phase and, where it can, puts out on it, while it charges on the others (seen
    in Remote EMS, Command Charging (Grid First), with independent phase power control on: on 2026-11-18, 40040 at
    6 kW, a 2.4 kW load on L1 left L1 at 2.00 kW with the plant putting out 0.33 kW there; on 2026-11-21, 40040 at 6
    and at 8 kW, a 3.3 kW load on L2 left L2 at 2.0 and 2.65 kW with the plant putting out 1.3 and 0.65 kW there and
    charging on L1 and L3, also with the total under the limit). So the battery's charge never raises a phase above

        max(L / 3, h_i + cars)

    the house and the cars on that phase alone: on a phase they keep under `L / 3` the charge fills it up to
    `L / 3` and no further, and on a phase where they draw more the plant charges nothing there, so that phase
    carries what the house and the cars put on it, less whatever the plant puts out, exactly as without a battery.
    The limit guards the fuse when

        L / 3  <=  fuse - margin

    and that is the whole rule. A phase that the house and the cars alone take past the fuse is the load
    balancer's matter while Home Assistant runs and the fuse's when it does not, as on a site without a battery:
    with Home Assistant down the chargers stay at their last currents whatever the battery does, and the battery
    takes nothing from such a phase. The chargers' currents (`cars_a`) must still read, as for every rule here,
    but they and the house's peaks do not lower `L`; a limit at `3 (fuse - margin)` is allowed whatever they are.

    The case where the house and the cars draw more on a phase than `L / 3` plus what the plant can put out there
    rests on inference, not yet on a measurement: the plant charges nothing on a phase it cannot hold.

    **Settling** after a charge start or a load step is not at once: up to about 0.65 kW above `L / 3` on a phase
    for one or two 5 s readings, then up to about 0.15 kW for up to a minute (seen in the same tests: at a charge
    start 2.47 and 2.43 kW on L3 against 2.0, then 0.14 to 0.16 kW on L1 for 15 s and about 0.07 kW on L2 for 35 s;
    the loaded phase 0.11 kW above now and then). The plant's loop takes about 5 to 10 s to move its charge off a
    phase, the same order as the time today's live clamp per phase takes to act on a running Home Assistant.

    **The margin** the shell passes is the site's own safety margin as the person set it for load balancing
    (`core.electric.stored_safety_margin_a`, 1 A by default for a new site; 0 A on the owner's, where 13.8 kW, the
    fuse itself, is allowed on 20 A), not the executor's `HEADROOM_MARGIN_A`: the plant holds the phase at `L / 3` in
    **kW**, so a voltage dip turns the same power into more current (4.6 kW is 20 A at 230 V, 20.9 A at 220 V), and
    the plant's own loop overshoots for a reading or two after a step. Both are small and short against what a fuse
    carries: a gG or diazed fuse carries 1.13 to 1.25 times its rating for long periods (IEC 60269: the conventional
    non-fusing current, 1.25 times for an hour for gG from 16 A; a breaker 1.13 times), which is why the person's own
    margin is trusted here.

    **Residual risk accepted** (owner decision 2026-11-21): with Home Assistant down nothing re-checks the readings,
    so a second fault in that time leaves the charge standing without this guard: the grid sensor dropping (the
    limit has nothing to measure), 40040 raised or 40030 turned off in the app. That is the exposure Sigenergy's own
    AI mode runs with every day; while Home Assistant runs, each look re-reads all of them and falls back to the cap."""
    if not _fuse_known(main_fuse_a, volts_between_phases, cars_a):
        return None
    assert main_fuse_a is not None
    volts = phase_volts(volts_between_phases)
    room_a = main_fuse_a - margin_a
    if room_a <= 0:
        return None
    return 3.0 * room_a * volts / 1000.0


def import_limit_phase_holds(
    import_limit_kw: float | None, main_fuse_a: float | None, volts_between_phases: float, *, margin_a: float = 0.0,
    cars_a: float = 0.0,
) -> bool:
    """Whether the plant's own grid import limit, held per phase, reads a value that keeps every phase under the fuse
    (`import_limit_phase_max_kw`). A limit that reads nothing, below 0 or not a number guards nothing; one that reads
    the register's "no limit" (about 4.3 GW) is far above any fuse."""
    most = import_limit_phase_max_kw(main_fuse_a, volts_between_phases, margin_a=margin_a, cars_a=cars_a)
    return (
        most is not None and _usable(import_limit_kw) and 0.0 <= import_limit_kw <= most + 1e-6  # type: ignore[operator]
    )


def expiry_mode(phase: str | None = None) -> str:
    """The report's row for an expiry measured on the whole command (`expiry`), or on one phase of a phase spec's
    (`expiry:L1`): each phase is proven on its own (owner decision 2026-11-23)."""
    return MODE_EXPIRY if phase is None else f"{MODE_EXPIRY}:{phase}"


def expiry_measured(
    expiry: Expiry, key: str | None, report: Mapping[str, Any] | None = None, phase: str | None = None
) -> bool:
    """Whether the expiry's fallback was seen to act on this firmware (`firmware_key`): the recipe records a field
    test on it (`Expiry.evidence` `measured`, `measured_on`), or the routine's report for this key has the mode
    `expiry` obeyed for this very expiry (`Expiry.expiry_id`: a measured phase expiry never counts for a balanced
    one) and, for a phase spec's, on this very phase (`phase`, its row `expiry:<phase>`; in the recipe, one of
    `Expiry.measured_phases`: a recipe measurement naming no phase lifts no phase). A key not read is never
    measured; a report kept under another key never counts."""
    if key is None or not firmware_known(key):
        return False
    if expiry.evidence == "measured" and key in expiry.measured_on and (
        phase is None or phase in expiry.measured_phases
    ):
        return True
    return expiry_row_passed(report, expiry, phase) and report is not None and report.get("key") == key


def expiry_phases_measured(expiry: Expiry, key: str | None, report: Mapping[str, Any] | None) -> tuple[bool, bool, bool]:
    """A phase spec's expiry measured on L1, L2 and L3 (`expiry_measured` per phase)."""
    found = [expiry_measured(expiry, key, report, phase) for phase in PHASE_NAMES]
    return (found[0], found[1], found[2])


def expiry_row_passed(report: Mapping[str, Any] | None, expiry: Expiry, phase: str | None = None) -> bool:
    """Whether the report's row for this expiry (`expiry_mode`) is `obeyed` for this very expiry
    (`Expiry.expiry_id`), its fallback within the minute and a look."""
    row = _modes(report).get(expiry_mode(phase))
    if not isinstance(row, Mapping) or row.get("outcome") != "obeyed" or row.get("expiry_id") != expiry.expiry_id:
        return False
    # Two separate passing runs on this firmware (`merge_report`, `EXPIRY_PASSES`); a run's own row never counts.
    passes = row.get("passes")
    if not isinstance(passes, int) or isinstance(passes, bool) or passes < EXPIRY_PASSES:
        return False
    # The fallback counted from the last write, within the minute the lift rests on (and one look).
    fallback = row.get("fallback_s")
    return (
        isinstance(fallback, (int, float)) and not isinstance(fallback, bool) and math.isfinite(fallback)
        and 0 <= fallback <= SILENCE_FALLBACK_MAX_S + EXPIRY_LOOK_S
    )


def expiry_due(report: Mapping[str, Any] | None, expiry: Expiry, phase: str | None = None) -> bool:
    """Whether the report has the mode `expiry` still undecided for this very expiry (and phase): no row, a row for
    another expiry, or a row still pending."""
    row = _modes(report).get(expiry_mode(phase))
    if not isinstance(row, Mapping) or row.get("expiry_id") != expiry.expiry_id:
        return True
    return mode_verdict(row.get("outcome")) == "pending"


def expiry_testable(expiry: Expiry, *, slow: bool, fixed_charge: bool) -> bool:
    """Whether the verification's mode `expiry` may try this expiry: one whose pass would lift the HA-down cap
    (`expiry_lifts` once measured): a dead-man or lease SpotNav renews, within `SILENCE_FALLBACK_MAX_S`, on a control
    the live clamp can lower. An integration's own renewal or nobody's is never tried (a field test recorded in the
    recipe is its only route, and it still never lifts)."""
    return expiry_lifts(expiry, measured=True, slow=slow, fixed_charge=fixed_charge)


def deadman_testable(expiry: Expiry, *, slow: bool, fixed_charge: bool) -> bool:
    """Whether a person's hardware test times this expiry (the verification's mode `expiry`, after its hold): a
    dead-man an integration renews that SpotNav can silence with a write of its own (`Expiry.silence`, EcoFlow's
    Modbus Control off), within `SILENCE_FALLBACK_MAX_S`. Its result is the tester's evidence for the recipe
    (`Expiry.measured_on`); like every hardware test row it never counts for the routine, and an integration's
    renewal never lifts a cap (`expiry_lifts`)."""
    window = expiry.window_s
    return (
        expiry.kind == "device_deadman" and expiry.renewer == "integration" and bool(expiry.silence)
        and not slow and not fixed_charge and window is not None and 0 < window <= SILENCE_FALLBACK_MAX_S
    )


def expiry_lifts(expiry: Expiry, *, measured: bool, slow: bool, fixed_charge: bool) -> bool:
    """Whether a command's own expiry (`commands.Expiry`) stands in for the HA-down cap: a dead-man or a lease that
    **SpotNav** renews (an integration's own loop keeps running while SpotNav stalls, so it never counts), whose
    fallback was **measured** on this firmware (`measured`, `expiry_measured`; a maker's word alone never lifts),
    on a control the live clamp can lower within seconds (not `slow`, not `fixed_charge`), ending within
    `SILENCE_FALLBACK_MAX_S` of the last write. A SoC bound or a clock window never lifts a fuse cap."""
    window = expiry.window_s
    return (
        expiry.kind in ("device_deadman", "device_lease") and expiry.renewer == "spotnav"
        and measured is True and not slow and not fixed_charge
        and window is not None and not isinstance(window, bool) and math.isfinite(window)
        and 0 < window <= SILENCE_FALLBACK_MAX_S
    )


def expiry_energy_kwh(expiry: Expiry, rate_kw: float, *, slot_left_s: float | None = None) -> float | None:
    """The kWh a grid charge at `rate_kw` may still take from the grid after Home Assistant dies, by the command's
    own expiry: the rate over the window for a dead-man, a lease and a SoC block; for a clock window the rate to the
    slot's end (`slot_left_s`, at most the window). `None` (unbounded): no expiry, a clock window whose end is not
    known, and an integration's own renewal (a stalled SpotNav does not stop it). For the diagnostics and the control
    log only: never a cap."""
    if not _usable(rate_kw):
        return None
    rate = max(rate_kw, 0.0)
    window = expiry.window_s
    if expiry.renewer == "integration" or window is None:
        return None
    if expiry.kind in ("device_deadman", "device_lease", "soc_bound"):
        return rate * window / 3600.0
    if expiry.kind == "clock_window" and _usable(slot_left_s):
        return rate * min(max(slot_left_s, 0.0), window) / 3600.0  # type: ignore[type-var]
    return None


def expiry_note(expiry: Expiry, *, measured: bool) -> str | None:
    """The diagnostics line for an expiry, where it has one: an integration's own renewal (`continues while HA
    runs`), a SoC block (`charges at most one block`), a dead-man SpotNav renews that no field test has seen act on
    this firmware (`dead-man stated, not measured`)."""
    if expiry.kind != "none" and expiry.renewer == "integration":
        return "continues while HA runs"
    if expiry.kind == "soc_bound":
        return "charges at most one block"
    if expiry.kind == "device_deadman" and expiry.renewer == "spotnav" and not measured:
        return "dead-man stated, not measured"
    return None


def grid_charge_phase_caps_kw(
    main_fuse_a: float | None, volts_between_phases: float, *, margin_a: float, cars_a: float,
    house_peak_kw: float | None, house_phase_peaks_a: Mapping[str, float] | None = None,
    expiry_lifts: bool | tuple[bool, bool, bool] = False, accept_ha_down: bool = False,
    site_margin_a: float | None = None, own_import_a: float = 0.0,
) -> tuple[float, float, float]:
    """The most a grid charge may be commanded at on L1, L2 and L3 (kW) where the plant takes a target per phase,
    so that no phase passes the fuse even if Home Assistant stops while it runs (the targets stay, as each charger's
    last current does):

        cap_i = V * max(0, fuse - margin - house_peak_i - cars) / 1000

    with each phase's own house peak (`house_peaks_a`) and the chargers' last currents (`cars_a`) counted on
    every phase. With the fuse or the chargers unknown no phase may charge (0).

    **A plant with its own dead-man** (`expiry_lifts`, the phase spec's expiry by `gate.expiry_lifts`: measured on
    this firmware, by a field test or the verification's mode `expiry`. Victron's page promises it for Mode 3, but
    until it is seen its recipe states it only, `commands.py`): the targets do not stay, so the cap is
    not reckoned for a house at its peak with nobody watching. It is the fuse less the margin on each phase,

        cap_i = V * max(0, fuse - margin) / 1000

    and what bounds the charge is the live clamp per phase (`executor._phase_room_kw`: the phase's headroom now,
    the house and the cars as they are, less `HEADROOM_MARGIN_A`), which runs at every look while Home Assistant
    does. What is left unguarded is the minute after Home Assistant stops: a load that starts on a phase in that
    minute rides on top of the charge until the plant falls back. That is the same exposure a running Home Assistant
    has between two looks and the plant's own settling, stretched from seconds to at most
    `SILENCE_FALLBACK_MAX_S`; it ends by itself, and a breaker or a fuse carries a moderate overload far longer than
    that (IEC 60898: a breaker holds 1.13 times its rating for an hour and may take an hour to open at 1.45 times;
    a gG fuse 1.25 and 1.6 times). A large load starting on a full phase in that very minute can still open it: the
    cost is a tripped fuse, not a danger, and it is the price of using the phase's live room. A recipe whose
    fallback takes longer, or is not stated by the maker, keeps the cap above.

    **The person accepted the HA-down risk** (`accept_ha_down`, the site's `battery_control_accept_ha_down`; after a
    recipe's own dead-man, which needs nothing): each phase's cap is what the fuse allows with an empty house, the
    fuse less the site's own safety margin (`site_margin_a`; `None`: `margin_a`), and the live clamp per phase is
    again what bounds the charge while Home Assistant runs. With it down, the house's load rides on top of the last
    targets and may open the fuse: what the person accepted.

    `own_import_a`: what the plant may import by itself on each phase (A, `commands.AutonomousImport`), taken off
    every phase in each of the rules above (infinite: no grid charge).

    `expiry_lifts` per phase (L1, L2, L3; one bool: all three): the dead-man lifts only the phases it was measured on
    (owner decision 2026-11-23); every other phase keeps its own cap by the rules after it."""
    if not _fuse_known(main_fuse_a, volts_between_phases, cars_a):
        return (0.0, 0.0, 0.0)
    assert main_fuse_a is not None
    volts = phase_volts(volts_between_phases)
    own_a = _own_a(own_import_a)
    lifts = (expiry_lifts,) * 3 if isinstance(expiry_lifts, bool) else tuple(item is True for item in expiry_lifts)
    lifted = max(main_fuse_a - margin_a - own_a, 0.0) * volts / 1000.0
    if accept_ha_down:
        most = max(
            main_fuse_a - (margin_a if site_margin_a is None else site_margin_a) - own_a, 0.0
        ) * volts / 1000.0
        room = [most, most, most]
    else:
        house = house_peaks_a(volts_between_phases, house_peak_kw, house_phase_peaks_a)
        room = [max(main_fuse_a - margin_a - max(cars_a, 0.0) - own_a - peak, 0.0) * volts / 1000.0 for peak in house]
    out = [lifted if lifts[index] else room[index] for index in range(3)]
    return (out[0], out[1], out[2])


def _own_a(own_import_a: float) -> float:
    """A plant's own import per phase (A) as counted: never below 0; anything not a number counts as without end."""
    if isinstance(own_import_a, bool) or not isinstance(own_import_a, (int, float)) or math.isnan(own_import_a):
        return math.inf
    return max(float(own_import_a), 0.0)


def phase_split_kw(total_kw: float, caps_kw: tuple[float, float, float]) -> tuple[float, float, float]:
    """`total_kw` spread over the phases as evenly as their caps allow: every phase the same, a phase with less
    room at its cap and the rest shared by the others. Never above a phase's cap, never above the total."""
    caps = [max(cap, 0.0) if math.isfinite(cap) else 0.0 for cap in caps_kw]
    left = min(max(total_kw, 0.0), sum(caps)) if math.isfinite(total_kw) else 0.0
    out = [0.0, 0.0, 0.0]
    for position, index in enumerate(sorted(range(3), key=lambda i: caps[i])):
        out[index] = min(caps[index], left / (3 - position))
        left -= out[index]
    return (out[0], out[1], out[2])


def grid_charge_cap_kw(
    main_fuse_a: float | None, volts_between_phases: float, *, margin_a: float, cars_a: float,
    house_peak_kw: float | None, plant_phases: int, import_limit_kw: float | None = None,
    import_limit_acts: bool = False, house_phase_peaks_a: Mapping[str, float] | None = None,
    import_limit_per_phase: bool = False, phase_margin_a: float | None = None, accept_ha_down: bool = False,
    expiry_lifts: bool = False, own_import_a: float = 0.0,
) -> float | None:
    """The most a grid charge may be commanded at (kW), so that no phase passes the fuse even if Home Assistant
    stops while it runs (Sigenergy has no dead-man: the command stays, and so does each charger's last current).

    Reckoned in amps on the worst phase: the fuse, less `margin_a`, less what the site's chargers may draw now
    (`cars_a`, each at its last current, all counted on that phase), less the house's peak: with
    `house_phase_peaks_a` (the learned peaks with their margins, `site/phase_peaks.py` `Learned.phase_peaks`) the
    largest phase's, taken as the house on every phase; without them the larger of the house forecast's p90 for the
    hour and `HOUSE_PEAK_DEFAULT_KW`, all on that one phase. The learned peaks replace that rule, so the cap may be
    larger or smaller than it gave; they are never taken below the forecast's p90 for the hour spread evenly over
    three phases (what the house is expected to reach at least, on a day colder than the days learned); the battery's charge takes
    `1 / plant_phases` of its power on each phase (Sigenergy's three-phase plants balance it). `None` (no cap of this
    kind) only while the person's on-site test showed the plant's own import limit acts (`import_limit_acts`) and that
    limit reads at most what keeps the worst phase under the fuse (`import_limit_holds`, `import_limit_max_kw`: the
    plant's limit is a total, so that is less than the fuse as three-phase power), or while the plant holds that
    limit **per phase** (`import_limit_per_phase`: the shell found the recipe's `commands.ImportLimitPhases` recorded
    as field-tested and every reading it needs as it must be, now) and it reads at most what keeps every phase under
    the fuse that way (`import_limit_phase_holds`), with `phase_margin_a` (the site's own safety margin, see
    `import_limit_phase_max_kw`; `None`: `margin_a`). With the fuse unknown no grid charge may run (0).

    `expiry_lifts` (the grid charge's own expiry stands in for the cap, `gate.expiry_lifts`: a dead-man or lease
    SpotNav renews, measured on this firmware, ending within `SILENCE_FALLBACK_MAX_S`; after both import-limit rules
    and the fuse known, before the acceptance): the command does not stay, so the cap is the fuse less `margin_a` on
    each of the plant's phases, and the live clamp bounds the charge, as for a charge per phase with its own
    dead-man (`grid_charge_phase_caps_kw`). No recipe's balanced grid charge has one measured yet.

    `accept_ha_down` (the site's `battery_control_accept_ha_down`, only where neither import-limit rule holds: those
    come first): the cap is what the fuse allows with an empty house (`ha_down_accepted_cap_kw`, with `phase_margin_a`
    as the site's own safety margin), not the house at its peak and the chargers' last currents with nobody
    watching.

    `own_import_a` (what the plant may import by itself on each phase it spreads that over, A,
    `commands.AutonomousImport`; infinite: no grid charge) is taken off the fuse like the chargers' currents, in the
    expiry's, the accepted and the house's rule alike; the import-limit rules are unchanged."""
    if import_limit_per_phase and import_limit_phase_holds(
        import_limit_kw, main_fuse_a, volts_between_phases,
        margin_a=margin_a if phase_margin_a is None else phase_margin_a, cars_a=cars_a,
    ):
        return None
    if import_limit_acts and import_limit_holds(
        import_limit_kw, main_fuse_a, volts_between_phases, margin_a=margin_a, cars_a=cars_a,
        house_peak_kw=house_peak_kw, house_phase_peaks_a=house_phase_peaks_a,
    ):
        return None
    if not _fuse_known(main_fuse_a, volts_between_phases, cars_a):
        return 0.0
    own_a = _own_a(own_import_a)
    if expiry_lifts:
        assert main_fuse_a is not None
        phases = min(max(int(plant_phases), 1), 3)
        return max(main_fuse_a - margin_a - own_a, 0.0) * phases * phase_volts(volts_between_phases) / 1000.0
    if accept_ha_down:
        return ha_down_accepted_cap_kw(
            main_fuse_a, volts_between_phases, margin_a=margin_a if phase_margin_a is None else phase_margin_a,
            plant_phases=plant_phases, own_import_a=own_a,
        )
    assert main_fuse_a is not None
    volts = phase_volts(volts_between_phases)
    house_a = max(house_peaks_a(volts_between_phases, house_peak_kw, house_phase_peaks_a))
    room_a = main_fuse_a - margin_a - max(cars_a, 0.0) - own_a - house_a
    phases = min(max(int(plant_phases), 1), 3)
    return max(room_a, 0.0) * phases * volts / 1000.0


def ha_down_setup(main_fuse_a: float | None, brand: str | None) -> dict[str, Any]:
    """The setup an acceptance of the HA-down risk is given for, as stored beside it."""
    return {"main_fuse_a": main_fuse_a, "brand": brand}


def ha_down_setup_state(stored: Any, main_fuse_a: float | None, brand: str | None) -> Literal["same", "changed", "unknown"]:
    """Whether the setup now is the one the acceptance was given for: `same`, `changed` (another main fuse, another
    brand read, or no setup stored: the acceptance is cleared) or `unknown` (the brand not read yet, or not read when
    it was given: the acceptance counts as off meanwhile, and is kept)."""
    if not isinstance(stored, Mapping) or stored.get("main_fuse_a") != main_fuse_a:
        return "changed"
    if brand is None or stored.get("brand") is None:
        return "unknown"
    return "same" if stored.get("brand") == brand else "changed"


def ha_down_accepted_cap_kw(
    main_fuse_a: float | None, volts_between_phases: float, *, margin_a: float, plant_phases: int,
    own_import_a: float = 0.0,
) -> float:
    """The grid charge cap once the person accepted the HA-down risk (`battery_control_accept_ha_down`): what the
    fuse allows with an empty house, the fuse less the site's own safety margin (`margin_a`) on each of the phases
    the plant spreads its charge over (`plant_phases`, as `grid_charge_cap_kw`; 1 where the brand does not say, so
    the whole charge may be on one phase), less what the plant may import by itself on each of them (`own_import_a`,
    A; infinite: 0). 0 with the fuse unknown."""
    if main_fuse_a is None or not math.isfinite(main_fuse_a) or main_fuse_a <= 0 or volts_between_phases <= 0:
        return 0.0
    phases = min(max(int(plant_phases), 1), 3)
    room_a = main_fuse_a - max(margin_a, 0.0) - _own_a(own_import_a)
    return max(room_a, 0.0) * phases * phase_volts(volts_between_phases) / 1000.0


def export_discharge_cap_kw(
    main_fuse_a: float | None, volts_between_phases: float, *, margin_a: float,
    phase_export_a: Iterable[float] | None, battery_share_a: float, plant_phases: int,
) -> float:
    """The most a discharge to the grid may be commanded at (kW), so that it and the sun's export keep every phase
    under the fuse: the fuse, less `margin_a`, less the worst phase's export now (`phase_export_a`, A, each phase's
    export, 0 where it imports) without the battery's own part of it (`battery_share_a`, A per phase, what SpotNav's
    running discharge to the grid carries now), spread over the plant's phases. 0 with the fuse or a phase unknown."""
    if (
        main_fuse_a is None or not math.isfinite(main_fuse_a) or main_fuse_a <= 0 or volts_between_phases <= 0
        or phase_export_a is None
    ):
        return 0.0
    exports = list(phase_export_a)
    if not exports or not all(math.isfinite(value) for value in exports):
        return 0.0
    volts = phase_volts(volts_between_phases)
    sun_a = max(max(value - max(battery_share_a, 0.0), 0.0) for value in exports)
    phases = min(max(int(plant_phases), 1), 3)
    return max(main_fuse_a - margin_a - sun_a, 0.0) * phases * volts / 1000.0


# --- what control runs ---------------------------------------------------------------------------------------------

#: `(action, battery kW magnitude, slot end)`.
Slot = tuple[str, float, datetime | None]


def merged_action(plan: Slot | None, hold_requested: bool) -> Slot | None:
    """The plan's running slot with the hold for the car: a hold where the plan self-consumes (or has no slot) and a
    SpotNav charger draws from the grid while the battery discharges into it."""
    if hold_requested and (plan is None or plan[0] == ACTION_SELF_CONSUME):
        return (ACTION_HOLD, 0.0, None if plan is None else plan[2])
    return plan


def restricted(
    slot: Slot | None, allowed: Iterable[str], cap_kw: float | None, export_cap_kw: float | None = None,
    fixed_charge_kw: float | None = None, *, hold_charges: bool = False,
) -> tuple[Slot | None, str | None]:
    """The slot control may run (an action the report does not allow becomes plain self-consumption; a grid charge
    is capped by `cap_kw`, a discharge to the grid by `export_cap_kw`; `restricted`'s second value says why it
    changed). `fixed_charge_kw`: the plant charges at its own rate (Tesla): the charge runs as planned or, when that
    rate is over the cap, not at all (`grid_charge_cap_fixed`): it is never lowered. An infinite rate (the battery's
    size, so its number of units, is not read) is never run (`grid_charge_size_unknown`). `hold_charges`: a hold on
    this plant may itself charge from the grid at that rate (Tesla without the grid-charging switch,
    `BrandControl.hold_charges_without`), so it runs only under the same rule (`hold_cap_fixed`, `hold_size_unknown`)."""
    if slot is None:
        return None, None
    action, rate, end = slot
    if action == ACTION_SELF_CONSUME:
        return slot, None
    if action not in allowed:
        return (ACTION_SELF_CONSUME, 0.0, end), f"not_verified:{action}"
    if fixed_charge_kw is not None and (action == ACTION_GRID_CHARGE or (action == ACTION_HOLD and hold_charges)):
        name = "grid_charge" if action == ACTION_GRID_CHARGE else "hold"
        if not math.isfinite(fixed_charge_kw):
            return (ACTION_SELF_CONSUME, 0.0, end), f"{name}_size_unknown"
        if cap_kw is not None and fixed_charge_kw > cap_kw:
            return (ACTION_SELF_CONSUME, 0.0, end), f"{name}_cap_fixed"
        return slot, None
    for capped, cap, why in ((ACTION_GRID_CHARGE, cap_kw, "grid_charge_cap"),
                             (ACTION_EXPORT_DISCHARGE, export_cap_kw, "export_fuse_cap")):
        if action == capped and cap is not None and abs(rate) > cap:
            if cap < MIN_RATE_KW:
                return (ACTION_SELF_CONSUME, 0.0, end), why
            return (action, cap, end), why
    return slot, None


def trial_refusal(action: str, options: Options) -> str | None:
    """Why the owner's test action may not run this action, `None` when it may."""
    if action == ACTION_EXPORT_DISCHARGE and not options.allow_sell:
        return "sell_not_allowed"
    if action not in (*_ACTION_MODE, ACTION_CURTAIL_EXPORT):
        return "unknown_action"
    return None


# --- the fast shedding order ----------------------------------------------------------------------------------------


def departure_has_room(
    departure: datetime | None, now: datetime, need_kwh: float | None, power_kw: float | None
) -> bool:
    """Whether a held car can wait: at its planned power, `DEPARTURE_ROOM_H` hours are left over before its
    departure once its need is met. No departure, no need or no power known: no room (the car is not shed first)."""
    if departure is None or need_kwh is None or power_kw is None or power_kw <= 0 or departure <= now:
        return False
    hours = (departure - now).total_seconds() / 3600.0
    return hours * power_kw - max(need_kwh, 0.0) >= DEPARTURE_ROOM_H * power_kw


def battery_yields(car_first: bool, held_cars_have_room: bool) -> bool:
    """Whether the battery's grid charge gives way to a held car (the executor's `car_first`): with the solar
    priority "car first", unless every held car's departure has room (then the car backs off first); with "battery
    first", never (the site's load balancing backs the car off). SpotNav's own hot-water boost is shed after both
    (see the module)."""
    return car_first and not held_cars_have_room
