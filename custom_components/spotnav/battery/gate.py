"""Whether SpotNav's battery and export-limit control may write on a site, and what it may do (pure).

The site's options (`Options`, kept in the site entry's data beside its other settings, all `battery_control_*`):

* `battery_control_enabled` (default off): the person's opt-in. Turning it on does not turn control on: it starts the
  verification routine (`verify.py`), and control is on only once that passed for the plant's current firmware and
  integration version (`firmware_key`).
* `battery_control_shadow_only` (default **on**): while on, nothing is written whatever else is set; the shell only
  logs what it would do. It stays on until the owner's on-site tests are done (plan `v3_battery_control_optin.md`).
* `battery_control_ai_takeover_ack`: the person read that SpotNav takes the plant over from Sigenergy's AI mode and
  that turning control off leaves it in Maximum Self Consumption. Without it a plant in AI mode is only watched.
* `battery_control_allow_sell` (default off): the plan may discharge the battery to the grid (`export_discharge`).
* `battery_control_import_limit_acts` (default off): the owner's on-site test (T3) showed that the plant's own grid
  import limit acts under Remote EMS. Until then, and whenever that limit does not read at most the fuse, a grid
  charge is capped so that it, the chargers' last currents and a house peak on one phase keep every phase under the
  fuse while Home Assistant is down (`grid_charge_cap_kw`; Sigenergy has no dead-man: a command stays until
  something writes it).

The verification report, per firmware key (`merge_report`): each mode's last decisive outcome. `obeyed` (`restored`
for normal) passes; `ignored`, `partial` and an abort that shows the plant does not do as told (`FAILING_ABORTS`)
fail; anything else (`not_judgeable:*`, an abort for the moment's circumstances: the fuse, a stale reading, a stop)
is pending and tried again at a later suitable moment (`modes_to_run`, `suitable`), at most `MAX_ATTEMPTS` runs.
**The report decides which actions are allowed** (`allowed_actions`): hold and normal must pass for any control; a
grid charge only if `charge` passed, a discharge for the house only if `discharge_house` passed, a discharge to the
grid only with `battery_control_allow_sell` on top of that. The export limit needs no routine of its own: it starts
on its live check (the executor's monitor hands it back and blocks it when the export does not stop).

The fast shedding order on the fuse (`battery_yields`): heat loads first (when the site has them, later), then the
battery's grid charge, then the car; the car earlier when its departure has room. The site's solar priority still
orders car against battery: with "battery first" the car backs off before the battery does.

Pure: no Home Assistant import; times are aware datetimes from the caller.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from .commands import ACTION_CURTAIL_EXPORT
from .plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
)
from .shadow import parse_aware_datetime

CONF_ENABLED: Final = "battery_control_enabled"
CONF_SHADOW_ONLY: Final = "battery_control_shadow_only"
CONF_AI_TAKEOVER_ACK: Final = "battery_control_ai_takeover_ack"
CONF_ALLOW_SELL: Final = "battery_control_allow_sell"
CONF_IMPORT_LIMIT_ACTS: Final = "battery_control_import_limit_acts"
#: Every option and its default.
DEFAULTS: Final[Mapping[str, bool]] = {
    CONF_ENABLED: False,
    CONF_SHADOW_ONLY: True,
    CONF_AI_TAKEOVER_ACK: False,
    CONF_ALLOW_SELL: False,
    CONF_IMPORT_LIMIT_ACTS: False,
}

#: The battery's modes the routine judges, and normal (the hand-back's check).
BATTERY_MODES: Final = ("hold", "charge", "discharge_house")
MODE_NORMAL: Final = "normal"
#: Outcomes that pass.
PASSING: Final = frozenset({"obeyed", "restored"})
#: Outcomes that fail outright.
FAILING: Final = frozenset({"ignored", "partial"})
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

#: The house's normal peak when its forecast has no p90 for the hour (kW), for the grid charge cap.
HOUSE_PEAK_DEFAULT_KW: Final = 4.0
#: A grid charge below this is not worth running (kW), as the executor's `MIN_RATE_KW`.
MIN_RATE_KW: Final = 0.5
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


@dataclass(frozen=True, slots=True)
class Options:
    """The site's battery control options (see the module)."""

    enabled: bool = False
    shadow_only: bool = True
    ai_takeover_ack: bool = False
    allow_sell: bool = False
    import_limit_acts: bool = False

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> Options:
        """From the site entry's data: anything but a stored boolean is its default (shadow only stays on)."""
        return cls(
            enabled=_flag(config.get(CONF_ENABLED), False),
            shadow_only=_flag(config.get(CONF_SHADOW_ONLY), True),
            ai_takeover_ack=_flag(config.get(CONF_AI_TAKEOVER_ACK), False),
            allow_sell=_flag(config.get(CONF_ALLOW_SELL), False),
            import_limit_acts=_flag(config.get(CONF_IMPORT_LIMIT_ACTS), False),
        )

    @property
    def writes(self) -> bool:
        """Whether anything may be written at all (the routine, control, the export limit)."""
        return self.enabled and not self.shadow_only

    def as_dict(self) -> dict[str, bool]:
        return {
            CONF_ENABLED: self.enabled, CONF_SHADOW_ONLY: self.shadow_only, CONF_AI_TAKEOVER_ACK: self.ai_takeover_ack,
            CONF_ALLOW_SELL: self.allow_sell, CONF_IMPORT_LIMIT_ACTS: self.import_limit_acts,
        }


def parse_time(value: Any) -> datetime | None:
    """A stored ISO time (a report's `last_run_at`), `None` for anything else."""
    return parse_aware_datetime(value)


def firmware_key(firmware: str | None, integration: str | None, version: str | None) -> str:
    """What a verification report is kept under: the plant's firmware and its integration's version (a change of
    either can change what the plant does with a command)."""
    return f"{firmware or 'unknown'} | {integration or 'unknown'} {version or 'unknown'}"


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
        if kept is not None and mode_verdict(kept.get("outcome")) != "pending":
            continue
        modes[name] = dict(row)
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


def modes_to_run(report: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The battery modes a next run tries: those still pending (normal ends every run by itself)."""
    modes = _modes(report)
    return tuple(name for name in BATTERY_MODES if mode_verdict((modes.get(name) or {}).get("outcome")) == "pending")


def needs_run(report: Mapping[str, Any] | None) -> bool:
    """Whether the routine has anything left to try for this key: never after `MAX_ATTEMPTS` runs, also once hold
    and normal passed (each run writes to the plant; a mode still pending then stays off)."""
    status = report_status(report)
    if status == "failed":
        return False
    if status == "none":
        return True
    attempts = (report or {}).get("attempts")
    if isinstance(attempts, int) and attempts >= MAX_ATTEMPTS:
        return False
    return bool(modes_to_run(report)) or mode_verdict((_modes(report).get(MODE_NORMAL) or {}).get("outcome")) == "pending"


def allowed_actions(report: Mapping[str, Any] | None, options: Options) -> frozenset[str]:
    """The plan actions control may run on this plant (see the module); empty until hold and normal passed."""
    if report_status(report) != "passed":
        return frozenset()
    modes = _modes(report)
    passed = {name for name, row in modes.items() if isinstance(row, Mapping) and mode_verdict(row.get("outcome")) == "pass"}
    allowed = {ACTION_HOLD}
    if "charge" in passed:
        allowed.add(ACTION_GRID_CHARGE)
    if "discharge_house" in passed:
        allowed.add(ACTION_DISCHARGE_HOUSE)
        if options.allow_sell:
            allowed.add(ACTION_EXPORT_DISCHARGE)
    return frozenset(allowed)


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


def fuse_kw(main_fuse_a: float | None, volts_between_phases: float, margin_a: float) -> float | None:
    """The fuse as three-phase power (kW), less `margin_a` per phase; `None` when the fuse is unknown."""
    if main_fuse_a is None or not math.isfinite(main_fuse_a) or main_fuse_a <= 0 or volts_between_phases <= 0:
        return None
    return max(main_fuse_a - margin_a, 0.0) * math.sqrt(3.0) * volts_between_phases / 1000.0


def phase_volts(volts_between_phases: float) -> float:
    """One phase's voltage to neutral."""
    return volts_between_phases / math.sqrt(3.0)


def import_limit_holds(import_limit_kw: float | None, main_fuse_a: float | None, volts_between_phases: float) -> bool:
    """Whether the plant's own grid import limit reads a value at or below the fuse (as three-phase kW)."""
    fuse = fuse_kw(main_fuse_a, volts_between_phases, 0.0)
    return (
        fuse is not None and import_limit_kw is not None and math.isfinite(import_limit_kw)
        and 0.0 <= import_limit_kw <= fuse + 1e-6
    )


def grid_charge_cap_kw(
    main_fuse_a: float | None, volts_between_phases: float, *, margin_a: float, cars_a: float,
    house_peak_kw: float | None, plant_phases: int, import_limit_kw: float | None = None,
    import_limit_acts: bool = False,
) -> float | None:
    """The most a grid charge may be commanded at (kW), so that no phase passes the fuse even if Home Assistant
    stops while it runs (Sigenergy has no dead-man: the command stays, and so does each charger's last current).

    Reckoned in amps on the worst phase: the fuse, less `margin_a`, less what the site's chargers may draw now
    (`cars_a`, each at its last current, all counted on that phase), less the house's peak (the larger of the house
    forecast's p90 for the hour and `HOUSE_PEAK_DEFAULT_KW`) all on that one phase; the battery's charge takes
    `1 / plant_phases` of its power on each phase (Sigenergy's three-phase plants balance it). `None` (no cap of this
    kind) only while the person's on-site test showed the plant's own import limit acts (`import_limit_acts`) and that
    limit reads at most the fuse (`import_limit_holds`). With the fuse unknown no grid charge may run (0)."""
    if import_limit_acts and import_limit_holds(import_limit_kw, main_fuse_a, volts_between_phases):
        return None
    if (
        main_fuse_a is None or not math.isfinite(main_fuse_a) or main_fuse_a <= 0 or volts_between_phases <= 0
        or not math.isfinite(cars_a)
    ):
        return 0.0
    volts = phase_volts(volts_between_phases)
    peak = HOUSE_PEAK_DEFAULT_KW
    if house_peak_kw is not None and math.isfinite(house_peak_kw):
        peak = max(peak, house_peak_kw)
    room_a = main_fuse_a - margin_a - max(cars_a, 0.0) - peak * 1000.0 / volts
    phases = min(max(int(plant_phases), 1), 3)
    return max(room_a, 0.0) * phases * volts / 1000.0


# --- what control runs ---------------------------------------------------------------------------------------------

#: `(action, battery kW magnitude, slot end)`.
Slot = tuple[str, float, datetime | None]


def merged_action(plan: Slot | None, hold_requested: bool) -> Slot | None:
    """The plan's running slot with the hold for the car: a hold where the plan self-consumes (or has no slot) and a
    SpotNav charger draws from the grid while the battery discharges into it."""
    if hold_requested and (plan is None or plan[0] == ACTION_SELF_CONSUME):
        return (ACTION_HOLD, 0.0, None if plan is None else plan[2])
    return plan


def restricted(slot: Slot | None, allowed: Iterable[str], cap_kw: float | None) -> tuple[Slot | None, str | None]:
    """The slot control may run (an action the report does not allow becomes plain self-consumption; a grid charge
    is capped, `restricted`'s second value says why it changed)."""
    if slot is None:
        return None, None
    action, rate, end = slot
    if action == ACTION_SELF_CONSUME:
        return slot, None
    if action not in allowed:
        return (ACTION_SELF_CONSUME, 0.0, end), f"not_verified:{action}"
    if action == ACTION_GRID_CHARGE and cap_kw is not None and abs(rate) > cap_kw:
        if cap_kw < MIN_RATE_KW:
            return (ACTION_SELF_CONSUME, 0.0, end), "grid_charge_cap"
        return (action, cap_kw, end), "grid_charge_cap"
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
    first", never (the site's load balancing backs the car off). Heat loads, when a site has them, go before both."""
    return car_first and not held_cars_have_room
