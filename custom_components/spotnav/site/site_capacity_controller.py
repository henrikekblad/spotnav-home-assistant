"""Home Assistant-facing site-capacity controller.

Reads the configured measurement entities and each associated charger's commanded state, normalizes
them through `site/site_capacity.py` and exposes the result to entities. The only write path is
`_async_apply_active_control`, which runs only when both `ACTIVE_CONTROL_READY` and this site's
`CONF_ACTIVE_CONTROL_ENABLED` option are set, and re-checks live measurements before each write.
With the option unset (the default) no charger service is ever called.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, cast, Literal

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback, Event, EventStateChangedData, EventStateReportedData, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from ..const import (
    DOMAIN,
    CONF_CHARGER_PRIORITY,
    DEFAULT_CHARGER_PRIORITY,
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_BATTERY_DISCHARGE_POWER_ENTITY,
    CONF_BATTERY_PER_PHASE_SOURCE,
    CONF_BATTERY_POWER_INVERTED,
    CONF_CHARGER_ENTRY_IDS,
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_GRID_POWER_INVERTED,
    CONF_GRID_POWER_SOURCE,
    CONF_MAIN_FUSE_A,
    CONF_MAX_AGE_S,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_MEASUREMENT_MODE,
    CONF_PHASE_WIRING,
    CONF_SAFETY_MARGIN_A,
    CONF_SITE_CURRENT_SIGNED,
    CONF_SITE_CURRENT_SOURCE,
    CONF_SOLAR_PRIORITY,
    CONF_YIELD_STEPPING_ENABLED,
    DEFAULT_MAX_AGE_S,
    DEFAULT_MIN_CURRENT_A,
    REGULATOR_DEADBAND_A,
    REGULATOR_DWELL_S,
    DEFAULT_SOLAR_PRIORITY,
    yield_ceiling_a,
    DEFAULT_YIELD_STEPPING_ENABLED,
    MEASUREMENT_MODE_DERIVED,
    MEASUREMENT_MODE_DIRECT,
    SITE_RECOMPUTE_INTERVAL_S,
    SOLAR_PRIORITY_BATTERY_FIRST,
    SOLAR_PRIORITY_CAR_FIRST,
)
from .site_history import SAMPLE_INTERVAL_S as HISTORY_SAMPLE_INTERVAL_S, SiteHistory
from .demand_tracker import HOUR_S, SiteDemand, TargetMark
from .import_target import (
    ACTION_HELD,
    ACTION_PAUSED,
    ACTION_UNHELD,
    battery_discharge_shares_w,
    departure_at_risk,
    departure_shortfall_kwh,
    effective_target,
    EffectiveTarget,
    held_level_kw,
    HOLD_VOLTAGE_V,
    hour_budget,
    hour_end_lock_a,
    HourBudget,
    lowering_floor_a,
    member_w,
    plan_remaining_kwh,
    share_budget,
    TargetMember,
    TargetShare,
)
from ..execution.charger_connection import (
    CHARGING as CONNECTION_CHARGING,
    UNKNOWN as CONNECTION_UNKNOWN,
)
from ..execution.controller import (
    ChargingController,
    CurrentRestore,
    REGULATED_FLOORED,
    REGULATED_STOPPED,
    RegulatedWrite,
    RESTORE_FAILED,
    RESTORE_NOT_NEEDED,
    RESTORE_RESTORED,
)
from ..execution.chargers.base import WRITE_SESSION_START, WRITE_SOLAR
from ..execution.min_soc_floor import ORIGIN_MIN_SOC
from ..execution.switched_current import measured_amps
from ..execution.yield_stepping import YieldConfig, YieldObservation, YieldStepper, YieldVerdict
from ..planning.grid_voltage import stored_voltage_between_phases_v
from ..planning.auto_settings import (
    IF_NOT_READY_ASK,
    IF_NOT_READY_RELEASE,
    MANUAL_START,
    MANUAL_STOP,
    STRATEGY_HYBRID,
    STRATEGY_SOLAR,
)
from ..runtime import charger_data, controller_for, domain_data, preview_for
from ..vehicles.capability import build_capability_snapshot, SiteCapabilitySnapshot
from .measurement_problem import measurement_problem, MeasurementProblem, UNHEALTHY_STATES
from .meter_cadence import ReportCadence, solar_liveness
from .site_detection import UPDATE_BEHAVIOUR
from .solar_capability import solar_capability
from .measurement_source import (
    combine_power_pair,
    grid_power_source_from_dict,
    negative_phases,
    PhaseMeasurementSource,
    read_phase_measurement,
    source_from_dict,
)
from .battery_probe import (
    BatteryProbe,
    car_minimum_power_w,
    PROBE_IDLE_BELOW_A,
    PROBE_MAX_FUSE_FACTOR,
    within_held_band,
)
from .regulator_damping import DampingDecision, RegulatorDamper
from .step_guard import CATCH_UP_S, charger_above_written, StepGuard
from .regulator import (
    allocate_regulator_decisions,
    applyability_failure,
    ApplyabilityFailure,
    classify_direction,
    confirmed_direction,
    DEFAULT_HELP_MARGIN_FACTOR,
    DEFAULT_MIN_CONSECUTIVE_CONFIRMATIONS,
    DEFAULT_ZERO_MARGIN_W,
    direction_phases,
    DirectionClass,
    magnitude_decision,
    magnitude_overload_lowering,
    MAGNITUDE_RAISE_REASON,
    is_import_overload,
    measurement_lost,
    MEASUREMENT_LOST_GRACE_S,
    MEASUREMENT_LOST_REASON,
    RegulatorDecision,
)
from .current_sign import CurrentSignCheck
from .grid_sign import (
    Change as GridSignChange,
    GridSignCheck,
    KIND_CURRENT as GRID_SIGN_CURRENT,
    KIND_POWER as GRID_SIGN_POWER,
    Reading as GridSignReading,
    Sample as GridSignSample,
)
from .auto_phase import AutoPhaseTracker, drawn_phase
from .site_capacity import (
    ACTIVE_CONTROL_READY,
    allocation_order,
    BatteryYieldEstimate,
    calculate_site_capacity,
    ChargerRequest,
    classify_apparent_power,
    classify_current,
    classify_phase_liveness,
    classify_power,
    classify_signed_current,
    classify_voltage,
    DIRECTION_NOMINAL_PHASE_VOLTAGE_V,
    DerivedPhaseInput,
    DerivedPhaseMeasurement,
    DirectPhaseMeasurement,
    implausible_power,
    PhaseBasis,
    PhaseName,
    PHASES,
    PhaseValue,
    resolve_worst_case_phases,
    SiteCalculationConfig,
    SiteCapacityResult,
)
from .site_membership import find_site_membership_conflicts, SiteMembershipConflict


# Rolling window per phase for consecutive-direction confirmation; longer than
# DEFAULT_MIN_CONSECUTIVE_CONFIRMATIONS so one bad reading ages out.
_DIRECTION_HISTORY_LENGTH = 10

# Decisions kept per site for diagnostics and the debug bundle (oldest dropped first).
DECISION_LOG_LENGTH = 200

#: How long (seconds) a start on its way holds its share of the site's margin for other starts, when the
#: charger's own current has not shown it drawing by then (`SiteCapacityController.reserve_start`): longer
#: than a slow charger takes to answer a Start and a meter to report the draw.
START_RESERVATION_S = 120.0

#: How long (seconds) a charger just started (a start of ours sent, or the charger seen charging) may be credited
#: with the draw the site meter shows, while its own current still reads nothing (`_credited_measurements`). A
#: charger's own reading lags (OCPP meter values come every so often) while the site meter shows the car within
#: seconds; without the credit the car reads as house load and the regulator pauses its own start. Longer than the
#: charger has to acknowledge the start (`START_ACK_TIMEOUT_S`, 30 s) and a usual meter-value interval after it;
#: shorter than the start's reservation (`START_RESERVATION_S`). After it the charger's reading counts alone again.
START_CREDIT_S = 90.0
# A switch-only charger's site step (`_learn_switched_currents`) is learned only from an edge seen within this long
# of the start or stop SpotNav sent.
SWITCH_EDGE_COMMAND_S = 60.0
# The most a site step may show where neither a current setting nor the main fuse bounds it.
SWITCHED_FALLBACK_MAXIMUM_A = 32.0


#: How far (amps) a decision on a credited reading may go above what is credited on a phase and still stand: the
#: credit is the site meter's rise, which carries the house's own flicker. A decision that would raise the charger
#: further rests on the credit for an increase, which it never may (`allocate_regulator_decisions`): the decision on
#: the charger's own reading is taken instead.
START_CREDIT_RAISE_TOLERANCE_A = 1.0


@dataclass(frozen=True, slots=True)
class StartCredit:
    """A charger just started: the amps it was given, when (`_yield_now` seconds), the site's current per phase
    just before, so only what the site rose by since is credited, and what every other charger drew per phase
    then (its own reading or its credit), so a rise that is theirs is not credited to this one too. `seq` orders
    starts of the same moment. `battery_w` is the home battery's power (W, positive while it charges) with that
    baseline, on a fresh reading, else `None`, and `battery_phase_a` its current per phase (A, positive while it
    charges) where the site reads it per phase, else `None`: what the battery stopped drawing since hides as much of
    the car's draw from the site's rise."""

    amps: float
    since: float
    baseline_a: Mapping[PhaseName, float | None]
    others_a: Mapping[str, Mapping[PhaseName, float]]
    seq: int
    battery_w: float | None = None
    battery_phase_a: Mapping[PhaseName, float] | None = None


@dataclass(frozen=True, slots=True)
class RaiseCredit:
    """A raise the regulator wrote to a charger already drawing: when (epoch seconds, as `_written_at`), every current
    written to the charger since, by any path, with when (`writes`, oldest first, the raise itself first), the site's
    current and the charger's own reading per phase just before the raise, and what every other charger drew per phase
    then, so only what the site rose by since, and not on their account, may be the car's
    (`_raise_credited_measurements`)."""

    at: float
    writes: tuple[tuple[float, float], ...]
    baseline_a: Mapping[PhaseName, float | None]
    delivered_a: Mapping[PhaseName, float]
    others_a: Mapping[str, Mapping[PhaseName, float]]


#: How far (amps) the site's rise on a raised charger's phases may be from the step written and still be the car's
#: (`_raise_credited_measurements`): a car draws a little either side of what it is set to, and the site meter carries
#: the house's flicker. A rise further from the step is something else (a load that came on with it, a car that did not
#: take it) and is never credited.
RAISE_CREDIT_STEP_TOLERANCE_A = 1.0
#: How long (s) after a lowering (or a pause) written to a charger with a raise credit the car is given to come down,
#: about one site reading: a car takes a few seconds to ramp, so a site reading taken within this of the write may still
#: show it at the higher current. Only a site reading taken more than this after the write counts the lower current as
#: on the charger (`_raise_credit_in_force`), and only such a reading that is still over the limit ends the credit
#: (`_raise_credited_measurements`).
RAISE_CREDIT_LOWERING_SETTLE_S = 5.0


#: How many resumes of a charge balancing paused (`_async_maybe_resume_paused_charge`) may go out in any
#: `RESUME_WINDOW_S`, and how long (seconds) no resume goes out once one more would be due within it. A load that
#: comes and goes would otherwise cycle the relays as often as the dwell allows; with two resumes in ten minutes the
#: charger is paused at most three times in them, as Peblar allows (`PAUSE_WINDOW_S`, three pauses), like the battery
#: probe's own back-off. The charge's next window, a person's Start or solar still start it as before.
RESUME_LIMIT = 2
RESUME_WINDOW_S = 600.0
RESUME_BACKOFF_S = 900.0

#: How long (seconds) shutdown and turning active control off wait for the apply passes and the chargers'
#: operations they cancel to end.
RETIRE_TIMEOUT_S = 5.0

# Battery charge power (W) from which the battery counts as charging for the limit explanation, and
# the remaining fuse margin (A) at or below which the site counts as sitting at its limit.
LIMIT_BATTERY_CHARGING_W = 300.0
LIMIT_AT_FUSE_MARGIN_A = 2.0

#: Why load balancing holds a car below what it asked for, where known.
LIMIT_CAUSE_BATTERY_SHARES_FUSE = "battery_shares_fuse"
LIMIT_CAUSE_HOUSE_CONSUMPTION = "house_consumption"
#: The site's measurement is lost and the car is held at its minimum until it is back (`_async_step_to_minimum`).
LIMIT_CAUSE_MEASUREMENT_LOST = "measurement_lost"
#: The site's import target holds the car below what the fuse allows (`_hold_to_import_target`), or paused it until
#: the next clock hour (`balancing_pause`).
LIMIT_CAUSE_IMPORT_TARGET = "import_target"
#: The import target's two decisions (`site/regulator.py`): a lowering, never an overload's.
REASON_IMPORT_TARGET_HELD = "import_target_held"
REASON_IMPORT_TARGET_PAUSED = "import_target_paused"
IMPORT_TARGET_REASONS = frozenset({REASON_IMPORT_TARGET_HELD, REASON_IMPORT_TARGET_PAUSED})
#: Why the import target is not applied besides the hour itself (`site/import_target.py` names the rest).
TARGET_NOT_APPLIED_LOAD_BALANCING_OFF = "load_balancing_off"
TARGET_NOT_APPLIED_BATTERY_UNKNOWN = "battery_unknown"
#: Why a person's set-aside was refused (`async_release_import_target`).
ERROR_NO_IMPORT_TARGET = "spotnav_no_import_target"
#: What a set-aside with no departure ahead (a person's Start, a plan without one) is kept as, followed by "/" and the
#: moment it was given (`_unplug_release_since`): until the car is unplugged, the car ends the charge by itself, or the
#: next local noon, whichever comes first, so a release always ends by then, even on a charger that never reports a plug state.
RELEASED_UNTIL_UNPLUG = "until_unplug"
#: The local hour an until-unplug set-aside ends at the latest, at least `RELEASED_UNTIL_UNPLUG_LEAST` after it was
#: given: one given at 11:59 lasts to the next day's noon.
RELEASED_UNTIL_UNPLUG_LATEST_HOUR = 12
RELEASED_UNTIL_UNPLUG_LEAST = timedelta(hours=6)


def _aware_instant(value: str) -> datetime | None:
    """An instant kept as text, with its offset; one without (a store edited by hand) is taken as UTC, and one that
    cannot be read is `None`: never a naive datetime to compare with an aware one."""
    try:
        moment = dt_util.parse_datetime(value)
    except ValueError:
        return None
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt_util.UTC)


def _unplug_release_since(value: str) -> datetime | None:
    """When an until-unplug set-aside (`RELEASED_UNTIL_UNPLUG/<instant>`) was given, else `None`."""
    head, _, since = value.partition("/")
    return _aware_instant(since) if head == RELEASED_UNTIL_UNPLUG and since else None


def unplug_release_value(now: datetime) -> str:
    """The value an until-unplug set-aside given at `now` is kept as."""
    return f"{RELEASED_UNTIL_UNPLUG}/{now.isoformat()}"


def unplug_release_latest(since: datetime) -> datetime:
    """The first local noon at least `RELEASED_UNTIL_UNPLUG_LEAST` after `since`: an until-unplug set-aside ends
    there at the latest, so a release given late in the morning still lasts to the next day's noon."""
    earliest = since + RELEASED_UNTIL_UNPLUG_LEAST
    local = dt_util.as_local(earliest)
    noon = local.replace(hour=RELEASED_UNTIL_UNPLUG_LATEST_HOUR, minute=0, second=0, microsecond=0)
    # Wall-clock arithmetic on the local zone: the next day's noon also across a DST change.
    return noon if noon >= earliest else noon + timedelta(days=1)
ERROR_STALE_DEPARTURE = "spotnav_stale_departure"
#: The target's own code failed (`_compute_regulator_decisions`): the fuse's decisions went as they were.
TARGET_NOT_APPLIED_ERROR = "error"
#: Why the target could not hold a charger it would hold (`import_target_snapshot`'s `inert`): a phase it draws on has
#: no fresh signed reading or none of the charger's own, or a lowering would turn a phase from import to export.
TARGET_INERT_NOT_JUDGED = "phase_cannot_be_judged"
TARGET_INERT_EXPORTING = "phase_exporting"
#: How many of the house's last measured hours a plan under the import target takes its typical load from
#: (`import_target_plan_kw`).
TARGET_HOUSE_HOURS = 24
#: What a charger's minute row says the import target does to it (`import_target_charger_row`).
TARGET_ROW_ACTIONS = {ACTION_UNHELD: "exempt"}
#: The regulator kept a charger where it was rather than raise it on a phase whose active power sits still
#: (`regulator._decide_phase`'s `held_still`); no yield step, probe or resume raises it either.
REASON_HELD_STILL_NO_RAISE = "held_still_no_raise"
#: Where a release of the import target came from (`async_release_import_target`'s `source`, in the change log).
RELEASE_SOURCE_PHONE = "phone"
RELEASE_SOURCE_CARD = "card"
RELEASE_SOURCE_APP = "app"
RELEASE_SOURCE_PLAN = "plan"
#: Why a release ended (`_release_end`, in the change log).
RELEASE_END_DEPARTURE_PASSED = "departure_passed"
RELEASE_END_UNPLUGGED = "unplugged"
RELEASE_END_CHARGE_ENDED = "charge_ended"
RELEASE_END_NOON = "noon"
RELEASE_END_DEPARTURE_CHANGED = "departure_changed"
RELEASE_END_TARGET_REMOVED = "target_removed"
#: The import target's state as the change log tells its transitions: off (no target), applied, or why not.
TARGET_STATE_OFF = "off"
TARGET_STATE_APPLIED = "applied"

#: How far (amps) above its minimum a charger's own fresh reading must be for a charger whose current the site does
#: not know to count as drawing above it, once the site's measurement is lost: a car at its minimum reads a little
#: either side of it.
MEASUREMENT_LOST_ABOVE_MINIMUM_A = 1.0

#: How far back (seconds) a rise of the home battery's power counts (`_battery_takes_the_fuse`): a battery that
#: begins its own grid charge at a quarter-hour's boundary reads a few hundred watts at first and its full power
#: seconds later.
BATTERY_RISE_WINDOW_S = 60.0

#: The regulator decision a charger whose own reading is too old to decide on gets (`site/regulator.py`). An OCPP
#: charger sends no meter values while no transaction runs, so a charger load balancing stopped reads its last value,
#: unchanged and unreported, for as long as it is stopped.
REASON_CHARGER_MEASUREMENT_UNUSABLE = "charger_measurement_unusable"
REASON_MAGNITUDE_LOWERING = "lowering_overload_on_current_magnitude_direction_unknown"
# The regulator's raise of a charge on a phase whose direction is not known, within the room the measured |I| leaves
# (`regulator.magnitude_decision`): written on its own path (`_async_apply_magnitude_raise`), which needs no signed
# power, with the step guard's margin and the damper's deadband and dwell as any raise.
REASON_MAGNITUDE_RAISE = MAGNITUDE_RAISE_REASON
# The regulator's reasons for deciding nothing on a charger because a phase's direction is not known: an overload by
# |I| still lowers it (`magnitude_overload_lowering`), a charge below its request is raised within the room |I| leaves
# (`magnitude_decision`), and a charge that lowering paused is still resumed.
DIRECTION_UNKNOWN_REASONS = frozenset({"phase_direction_unknown", "phase_direction_unknown_at_charge_start"})

def _current_write_lock(charger_controller: Any) -> tuple[float, float, float | None]:
    """A charger's current writes as the import target counts them (`TargetMember`): the seconds until its current
    may be written again, the least time between two writes, and what it is set to (`None` when unknown). `(0, 0,
    None)` for a charger whose current SpotNav does not write. Never raises."""
    try:
        adapter = getattr(charger_controller, "adapter", None)
        current = getattr(adapter, "current", None)
        if (
            current is None
            or not getattr(current, "can_set", False)
            or not getattr(adapter, "current_enabled", False)
            or not current.policy.regulator_writes
        ):
            return 0.0, 0.0, None
        wait_s = float(current.wait_s())
        interval_s = float(current.policy.min_interval_s)
        setpoint = current.setpoint_a()
        if setpoint is None:
            setpoint = getattr(charger_controller, "requested_current_a", None)
        set_a = float(setpoint) if isinstance(setpoint, (int, float)) and not isinstance(setpoint, bool) else None
        return (
            wait_s if math.isfinite(wait_s) and wait_s > 0 else 0.0,
            interval_s if math.isfinite(interval_s) and interval_s > 0 else 0.0,
            set_a,
        )
    except Exception:  # noqa: BLE001 - a charger that cannot say counts as one written at once, as before
        _LOGGER.debug("A charger's current write lock could not be read", exc_info=True)
        return 0.0, 0.0, None


def _must_lower(decision: RegulatorDecision) -> bool:
    """Whether `decision` lowers a charger for an overload: an import overload (`is_import_overload`), or one by |I|
    where the direction is not known (`REASON_MAGNITUDE_LOWERING`). Neither is held by anything that is not about
    safety; both are re-checked on the fresh reading before they are written."""
    return is_import_overload(decision) or decision.reason == REASON_MAGNITUDE_LOWERING


#: The regulator writes kept per charger, newest last, to tell which of them a site reading may already show
#: (`_written_at_reading`).
_WRITES_KEPT = 4

#: How long (s) one site calculation stands for the same readings (`SiteCapacityController._calculate`). A reading's
#: age is taken when it is calculated, so a result is reused only within this, and only while no reading changed.
CALCULATION_REUSE_S = 1.0


@dataclass(frozen=True, slots=True)
class BalancingPauseState:
    """A charge load balancing holds back, as the status says it: when it is tried again (`None`: as soon as there
    is room, with no time to name) and why there is no room (`LIMIT_CAUSE_*`)."""

    retry_at: datetime | None
    cause: str


@dataclass(frozen=True, slots=True)
class ImportTargetStatus:
    """What the site's import target does to one charger now (`SiteCapacityController.import_target_status`): the
    target and the hour's mean so far (kW); the amps it holds the charger to (`None`: not held); the end of the hour
    it is paused to (`None`: not paused by it); the departure it is set aside for (`None`: not); and whether the
    house alone ends the hour above the target."""

    target_kw: float
    #: The import target as set; below `target_kw` where the month's free level lifts it this hour.
    set_kw: float
    hour_kw: float
    held_a: float | None
    paused_until: datetime | None
    released_for: str | None
    house_over: bool
    #: Whether the plan's own choice set it aside (`released_for` then says until when), not a person's press.
    released_automatically: bool = False
    #: The departure the charge will miss under the target, and by how much (`TargetRisk`), else `None`.
    at_risk: TargetRisk | None = None
    #: What a release would mean, for the card's confirmation: until when it would last (the plan's departure ahead,
    #: `None`: until the car is unplugged), and the hour's import it may reach (kW, whole: the house's typical load and
    #: the charge's full power at its setting; `None` when not known).
    release_until: str | None = None
    release_peak_kw: float | None = None


@dataclass(frozen=True, slots=True)
class TargetRisk:
    """A plan's departure the import target makes it miss (`SiteCapacityController._hold_to_import_target`): the
    departure (ISO), the energy (kWh) the held power leaves short of it, and the target."""

    departure: str
    missing_kwh: float
    target_kw: float
    #: The plan's `if_not_ready`: `ask` is told and offered "Release the import target for this charge", `hold` only said, `release` set aside.
    choice: str = IF_NOT_READY_ASK
    #: The import target as set, below `target_kw` where the month's peak lifts the running hour (`None`: the same).
    set_kw: float | None = None

#: Decision-log details of the battery-on-the-fuse handling (`site/battery_probe.py`).
DETAIL_HELD_BATTERY_AT_LIMIT = "held_battery_at_limit"
#: Decision-log detail of a proposal held because a meter load balancing reads updates too seldom
#: (`site/meter_cadence.py`).
DETAIL_HELD_METER_TOO_SLOW = "meter_too_slow"

_LOGGER = logging.getLogger(__name__)


#: Stable codes of the active-control transitions that are not a charger's own restore code.
RESTORE_CHARGER_NOT_LOADED = "charger_not_loaded"
RESTORE_MEMBERSHIP_CONFLICT = "membership_conflict"

ENABLE_ENABLED = "enabled"
ENABLE_ALREADY = "already_enabled"
ENABLE_UNAVAILABLE = "active_control_unavailable"


class SiteControllerClosed(Exception):
    """The controller was shut down (its entry unloaded or reloading): it accepts no transition."""


@dataclass(frozen=True, slots=True)
class ChargerRestoreReport:
    """One member charger's part of an off transition."""

    charger_entry_id: str
    restore: CurrentRestore


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """What disabling active control did about currents balancing may have lowered.

    `outcome` is `failed` if any charger's restore failed, else `restored` if any was written and
    confirmed, else `not_needed`. Never `restored` while a charger is still unrestored.
    """

    outcome: str
    chargers: tuple[ChargerRestoreReport, ...]


@dataclass(frozen=True, slots=True)
class SiteEvaluation:
    """What the site's sensors read besides `result`, computed once per evaluation in `_recompute`.

    Building these (the capability snapshot re-reads every measurement and charger; the measured
    currents and the grid power read states) per entity and per state write made a sensor update
    slow, so the entities read this and never recompute.
    """

    capability: SiteCapabilitySnapshot
    charger_measured_current: Mapping[str, DirectPhaseMeasurement | None]
    grid_power: dict[str, Any]


class SiteCapacityController:
    """Computes and exposes the site capacity snapshot and, when opted in, applies active control.

    `membership_conflicts` is computed live: a charger shared with another site entry blocks the
    apply step (the regulator calculation continues), so a charger of doubtful ownership is never
    written from here, but for an import overload's lowering.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, config: dict[str, Any]) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.config = config
        # The last site calculation, the readings it was made from and when (`_calculate`).
        self._calculation: tuple[tuple[Any, ...], datetime, SiteCapacityResult] | None = None
        self._reading_entities: tuple[str, ...] | None = None
        # A recompute queued for the next turn of the event loop (`_schedule_recompute`).
        self._recompute_pending = False
        self._timer_cancel: Callable[[], None] | None = None
        self._state_listener_cancel: Callable[[], None] | None = None
        self._controller_listener_cancels: list[Callable[[], None]] = []
        self._listeners: set[Callable[[], None]] = set()
        # A solar coordinator's re-render is on its way to the listeners (`notify_solar_surplus_changed`).
        self._rerendering = False
        # The latest active-control apply pass, and whether one was created and has not begun yet (two
        # recomputes in one tick make one pass). A pass only decides and spawns: each charger's step runs as
        # that charger's own task (`_charger_ops`), so a pass never waits for a charger's slow command before
        # the next pass may run.
        self._apply_task: asyncio.Task[None] | None = None
        self._apply_pending = False
        # Every pass still running (awaiting the steps it spawned), for shutdown and disable.
        self._apply_passes: set[asyncio.Task[None]] = set()
        # The one operation in flight per charger (a write, a resume, a probe's start or pass): a pass skips
        # a charger while its previous operation runs, and runs again when that one returns
        # (`_skipped_while_busy`), so a newer decision (a lowering one above all) goes out at once.
        self._charger_ops: dict[str, asyncio.Task[None]] = {}
        self._skipped_while_busy: set[str] = set()
        # Switch-only pauses that could not be sent in a pass, with the phases they were to free: the pass that
        # sent them pauses the chargers it held at their minimum on those phases instead (`_async_pause_fallbacks`).
        self._failed_switched_pauses: list[tuple[str, frozenset[PhaseName]]] = []
        # Serialises active-control transitions for this instance; a transition on a closed (shut-
        # down) controller is refused.
        self.transition_lock = asyncio.Lock()
        self._closed = False
        # Per-charger damping state (`site/regulator_damping.py`).
        self._dampers: dict[str, RegulatorDamper] = {}
        # Per-charger step guard (`site/step_guard.py`): no step to the edge, no cut on a stale reading.
        self._step_guards: dict[str, StepGuard] = {}
        # Per-charger yield steppers, created lazily; an options change reloads the entry and builds
        # a fresh controller.
        self._yield_steppers: dict[str, YieldStepper] = {}
        # Latest yield verdict per charger, for the diagnostic attribute only; never read back into
        # a decision.
        self._yield_stepping_last: dict[str, dict[str, Any]] = {}
        # Per-charger battery probes (`site/battery_probe.py`), created lazily, and the one timer
        # that re-evaluates a running probe when its window ends.
        self._battery_probes: dict[str, BatteryProbe] = {}
        # When a paused charge first had headroom for its minimum again (for the resume's dwell).
        self._resume_ready_since: dict[str, float] = {}
        # When each charger's paused charge was last resumed (within `RESUME_WINDOW_S`), and until when no resume
        # goes out after it was resumed `RESUME_LIMIT` times in it.
        self._resume_times: dict[str, deque[float]] = {}
        self._resume_backoff_until: dict[str, float] = {}
        self._probe_timer_cancel: Callable[[], None] | None = None
        # When (`_yield_now`) the probe timer is armed for, and every probe check still due.
        self._probe_timer_due: float | None = None
        self._probe_checks_due: set[float] = set()
        # Last logged yield-stepping signature per charger (logged once per change).
        self._logged_yield_stepping: dict[str, tuple[Any, ...]] = {}
        # Clock for `YieldObservation.now`, in seconds. Monotonic so a wall-clock step cannot freeze
        # the stepper; replaceable in tests.
        self._yield_now: Callable[[], float] = time.monotonic
        # Each "Automatic" single-phase charger's followed phase (`site/auto_phase.py`), by `_yield_now`.
        self._auto_phases: dict[str, AutoPhaseTracker] = {}
        # Starts on their way, per charger: the amps a start was given, the phases it draws from, and until
        # when (`_yield_now` seconds) the reservation holds. What another start may take is the measured
        # margin less what these have not drawn yet (`start_allowance_a`). A charger whose own current cannot
        # be read holds its whole share until it can or the time is up: a fall of the site's margin may be
        # another load, and counting a start twice is the safe side.
        self._start_reservations: dict[str, tuple[float, tuple[PhaseName, ...], float]] = {}
        # On a direct site, where the sun and active control may each write a charger of their own: the sun's
        # writes and active control's raises, held the same way until the charger draws them or
        # `START_RESERVATION_S` pass, so neither writer is given the room the other has just given away
        # (`_reserved_by_others_a`, `_solar_reserved_a`). What a charger already draws is in the site's reading.
        self._solar_write_reservations: dict[str, tuple[float, tuple[PhaseName, ...], float]] = {}
        self._regulator_write_reservations: dict[str, tuple[float, tuple[PhaseName, ...], float]] = {}
        # Chargers just started, credited with the draw the site shows while their own reading lags
        # (`_credited_measurements`); what was credited per phase on the last decision; and whether each charger was
        # seen charging at the last recompute (a charger seen on by itself is credited as a start of ours is).
        self._start_credits: dict[str, StartCredit] = {}
        self._credited_a: dict[str, dict[PhaseName, float]] = {}
        self._start_credit_seq = 0
        # The home battery's power (W) and its current per phase on fresh readings at the last recompute
        # (`_battery_reading`), the baseline of a start seen then.
        self._recompute_battery: tuple[float | None, dict[PhaseName, float] | None] = (None, None)
        # The regulator's last raise of a charger already drawing, credited the same way until the charger's own
        # reading has had the time to show it (`_raise_credited_measurements`), and what it credited per phase on the
        # last decision.
        self._raise_credits: dict[str, RaiseCredit] = {}
        self._raise_credited_a: dict[str, dict[PhaseName, float]] = {}
        self._seen_charging: dict[str, bool] = {}
        # A switch-only charger with no reading of its own (`_learn_switched_currents`): whether it was seen charging,
        # and the site's phase currents net of the other chargers' own readings on the last recompute.
        self._switched_seen_charging: dict[str, bool] = {}
        self._switched_net_site: dict[str, dict[PhaseName, float | None]] = {}
        # The home battery's power on recent recomputes (`_yield_now` seconds, W), for a rise toward a charge.
        self._battery_samples: deque[tuple[float, float]] = deque()
        # The regulator's own writes per charger (`_async_write_regulated`): when (wall clock), the current written
        # and the one in force before it (`None` when not known), newest last.
        self._regulated_writes: dict[str, deque[tuple[datetime, float, float | None]]] = {}
        # When (epoch seconds) a current last went to each charger by any path the site knows of: its regulator, a
        # resume or probe, or a write it was told of (`note_charger_write`). An overload trusts the charger's own
        # reading over the damper's memory only on a reading taken after that (`charger_above_written`).
        self._written_at: dict[str, float] = {}
        # Last logged apply outcome per charger, so a line is emitted per change rather than per
        # recompute.
        self._logged_apply_outcomes: dict[str, tuple[Any, ...]] = {}
        # Bounded in-memory log of what the regulator did, newest last; read through
        # `regulator_decision_log` by the diagnostics and the debug bundle, never by a decision.
        self._decision_log: deque[dict[str, Any]] = deque(maxlen=DECISION_LOG_LENGTH)
        # The last 24 hours, one row a minute (`site/site_history.py`), for the debug bundle only.
        self._history = SiteHistory(hass, entry_id)
        self._history_cancel: Callable[[], None] | None = None
        # Each clock hour's mean import, for a demand charge and the import target (`site/demand_tracker.py`). Its
        # clock is the electricity price's area's (`_demand_zone`), resolved again at most every
        # `_DEMAND_ZONE_TTL_S`. Only the import target reads it for a decision (`_hold_to_import_target`).
        self.demand = SiteDemand(hass, entry_id, _home_zone(hass))
        self._demand_zone: tuple[float, str] | None = None
        # The import target (`_hold_to_import_target`), as of the last recompute: the hour's budget (`None` while not
        # applied, and why), what it does to each charger, and which chargers' decisions it lowered. Per charger the
        # hour the target paused it in (it stays paused until the next) and the departure it set the target aside
        # for. Not persisted: after a restart the hour's budget decides again from the hour the tracker kept.
        self._target_budget: HourBudget | None = None
        # The level the hold keeps the running hour under (`_effective_import_target`), as of the last recompute, with
        # the key it was worked out for (the month's peaks change only when an hour completes). The running hour's
        # hold only: plans and the later hours of the departure's check count on the target as set.
        self._target_effective: EffectiveTarget | None = None
        self._target_effective_key: tuple[Any, ...] | None = None
        self._target_not_applied: str | None = None
        self._target_shares: dict[str, TargetShare] = {}
        self._target_lowered: set[str] = set()
        self._target_raise_lock: dict[str, float] = {}
        # Each member's wait for its next current write as the last share saw it (diagnostics only).
        self._target_write_wait: dict[str, float] = {}
        # Why the target paused a charger this hour, where a rule beyond the share decided (`slow_charger`): the
        # pause's hour and reason, for the diagnostics and the change log.
        self._target_pause_reasons: dict[str, tuple[datetime, str]] = {}
        # The amps the target last held each charger to this hour (the lock in the hour's last minutes keeps no more),
        # the chargers the lock alone kept from a raise, and the chargers the target could not hold, and why.
        self._target_last_cap: dict[str, tuple[datetime, float]] = {}
        self._target_locked: set[str] = set()
        self._target_inert: dict[str, str] = {}
        # The chargers the target lowered this pass, but not as far as their share: the floor stopped it.
        self._target_floor_limited: set[str] = set()
        self._target_failed_logged = False
        self._target_paused_hour: dict[str, datetime] = {}
        self._target_pause_pending: dict[str, datetime] = {}
        # Per charger the departure (ISO) a person set the import target aside until (`async_release_import_target`),
        # kept in the site's store so a restart keeps it; and the departures the target makes a plan miss.
        self._target_released: dict[str, str] = {}
        self._target_release_store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.import_target_release.{entry_id}"
        )
        self._target_risk: dict[str, TargetRisk] = {}
        # The departures a plan's own choice (`if_not_ready` `release`) sets the target aside for, while holding
        # would miss them: decided again on every recompute, never stored.
        self._target_auto_released: dict[str, str] = {}
        # The import target's change log lives with the minute rows (`SiteHistory.note_target`), kept 24 hours across
        # restarts; the state it last told (`_note_target_state`).
        self._target_logged_state: str | None = None
        self._target_noticed: dict[str, tuple[str, float]] = {}
        # Logging-only memory of the last conflict list; `membership_conflicts` itself is always
        # live.
        self._last_logged_conflicts: list[SiteMembershipConflict] = self.membership_conflicts
        # Per-phase rolling direction classifications for the regulator's confirmation
        # requirement. Not persisted: an empty history means nothing is confirmed yet.
        self._direction_history: dict[PhaseName, deque[DirectionClass | None]] = {
            phase: deque(maxlen=_DIRECTION_HISTORY_LENGTH) for phase in PHASES
        }
        # Last seen `last_reported` of each phase's active-power entity, to detect a genuinely new
        # report (see `_update_direction_history`). `None` means not observed or unavailable.
        self._last_seen_active_power_report_at: dict[PhaseName, datetime | None] = {
            phase: None for phase in PHASES
        }
        # A direct site's signed current, confirmed per phase against the meter's total before its sign is
        # taken as the phase's direction (`site/current_sign.py`). Not persisted.
        self._current_sign = CurrentSignCheck(
            zero_margin_w=DEFAULT_ZERO_MARGIN_W, min_agreements=DEFAULT_MIN_CONSECUTIVE_CONFIRMATIONS
        )
        # On a direct site, per charger in a charge: whether its direction was known when the charge started,
        # which keeps the charge's current writer (active control or the sun) until it ends
        # (`_update_charge_writers`). Not persisted: a charge found running after a restart starts unknown.
        self._direction_at_charge_start: dict[str, bool] = {}
        # How often each measurement entity reports (`site/meter_cadence.py`): fed from every state
        # write Home Assistant reports, changed or not, and sampled on each recompute.
        self._cadence = ReportCadence()
        self._report_listener_cancel: Callable[[], None] | None = None
        # Per charger load balancing governs, since when (`_yield_now`) the site's measurement has been lost on a
        # phase it may draw on (`_update_measurement_lost`); and how often, and when last (wall clock), a charger was
        # stepped to its minimum for it (`measurement_lost_snapshot`).
        self._measurement_lost_since: dict[str, float] = {}
        self._measurement_lost_stepped: set[str] = set()
        self._measurement_lost_steps = 0
        self._measurement_lost_last_step_at: datetime | None = None
        # Since when (`_yield_now`) the site's measurement has been unhealthy at all (`UNHEALTHY_STATES`), whichever
        # charger it concerns; the status line waits on it (`lasting_measurement_problem`).
        self._measurement_unhealthy_since: float | None = None
        # Whether the grid reading's sign looks set the wrong way, from what the chargers' own steps do to it
        # (`site/grid_sign.py`). Only ever a warning: no setting is changed here. The tally is kept in the site's
        # store, so a restart keeps it, and dropped once the setting it was counted under is changed.
        self._grid_sign = GridSignCheck(self._grid_sign_flags())
        self._grid_sign_store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.grid_sign.{entry_id}")
        # A changed tally not yet written: written at the latest when the site shuts down (a reload, a restart).
        self._grid_sign_unsaved = False
        self.result: SiteCapacityResult = self._calculate()
        self._update_direction_history()
        # One regulator decision per associated charger (`site/regulator.py`); acting on them is
        # `_async_apply_active_control`'s job.
        self.regulator_decisions: dict[str, RegulatorDecision] = (
            self._compute_regulator_decisions()
        )
        self.evaluation: SiteEvaluation = self._build_evaluation()

    def _build_evaluation(self) -> SiteEvaluation:
        """The derived reads the site's entities show, taken once per evaluation (see `SiteEvaluation`)."""
        charger_entry_ids: list[str] = list(self.config.get(CONF_CHARGER_ENTRY_IDS) or [])
        return SiteEvaluation(
            capability=self.capability_snapshot,
            charger_measured_current={
                charger_entry_id: self.charger_measured_current(charger_entry_id)
                for charger_entry_id in charger_entry_ids
            },
            grid_power=self.grid_power_snapshot(),
        )

    @property
    def membership_conflicts(self) -> list[SiteMembershipConflict]:
        """Computed fresh on every read, so a sibling site's change is visible without waiting for a
        recompute.
        """
        return self._check_membership()

    async def async_initialize(self) -> None:
        """Recompute once, then on a periodic timer, tracked-entity state changes (once per turn of the event
        loop, `_schedule_recompute`) and each charger controller's listener (a requested-current change need not
        change any HA state; at once, in step with the charger's own commands).
        """
        self._recompute()
        self._timer_cancel = async_track_time_interval(
            self.hass,
            self._async_periodic_recompute,
            timedelta(seconds=SITE_RECOMPUTE_INTERVAL_S),
        )
        tracked_entity_ids = self._tracked_entity_ids()
        if tracked_entity_ids:
            self._state_listener_cancel = async_track_state_change_event(
                self.hass, tracked_entity_ids, self._async_state_changed
            )
        cadence_entity_ids = self.cadence_entity_ids()
        if cadence_entity_ids:
            self._report_listener_cancel = async_track_state_report_event(
                self.hass, cadence_entity_ids, self._async_state_reported
            )
        self._register_controller_listeners()
        await self.demand.async_load()
        await self._async_load_released()
        await self._async_load_grid_sign()
        await self._history.async_load()
        self._history_cancel = async_track_time_interval(
            self.hass, self._async_sample_history, timedelta(seconds=HISTORY_SAMPLE_INTERVAL_S)
        )

    @callback
    def _async_sample_history(self, _now: datetime) -> None:
        self._history.sample(self)

    @property
    def history_samples(self) -> dict[str, Any]:
        """The last 24 hours' minute rows, the site's and each member charger's (`site/site_history.py`)."""
        return self._history.samples

    async def async_shutdown(self) -> None:
        """Cancel the timer and listeners and any unfinished apply pass."""
        self._closed = True
        self._cancel_probe_timer()
        if self._history_cancel is not None:
            self._history_cancel()
            self._history_cancel = None
        await self._history.async_close()
        await self.demand.async_close()
        if self._grid_sign_unsaved:
            self._grid_sign_unsaved = False
            await self._grid_sign_store.async_save(self._grid_sign.as_dict())
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None
        if self._state_listener_cancel is not None:
            self._state_listener_cancel()
            self._state_listener_cancel = None
        if self._report_listener_cancel is not None:
            self._report_listener_cancel()
            self._report_listener_cancel = None
        for cancel in self._controller_listener_cancels:
            cancel()
        self._controller_listener_cancels.clear()
        await self._async_retire_operations()

    async def _async_retire_operations(self) -> None:
        """Cancel every apply pass and every charger's operation in flight, and wait for them to end (at most
        `RETIRE_TIMEOUT_S`): nothing this controller decided reaches a charger afterwards. A cancelled
        command may already have left; what the charger then carries is for the next reader to see."""
        self._apply_task = None
        self._apply_pending = False
        self._skipped_while_busy.clear()
        tasks = [task for task in (*self._apply_passes, *self._charger_ops.values()) if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=RETIRE_TIMEOUT_S)
            if pending:
                _LOGGER.warning(
                    "SpotNav site %s: %s charger operation(s) did not end within %ss of being cancelled",
                    self.entry_id,
                    len(pending),
                    RETIRE_TIMEOUT_S,
                )
            for task in tasks:
                if task.done() and not task.cancelled() and task.exception() is not None:
                    _LOGGER.debug("Retired active-control operation had failed", exc_info=task.exception())
        self._charger_ops.clear()

    def _register_controller_listeners(self) -> None:
        """Subscribe to each associated charger controller once per controller lifetime; unloaded or
        removed chargers are skipped.
        """
        for charger_entry_id in self.config.get(CONF_CHARGER_ENTRY_IDS) or []:
            charger_controller = controller_for(self.hass, charger_entry_id)
            if charger_controller is not None:
                self._controller_listener_cancels.append(
                    charger_controller.add_listener(self._on_charger_controller_changed)
                )
                charger_controller.set_start_cap(
                    lambda charger_entry_id=charger_entry_id: self.start_allowance_a(charger_entry_id),
                    reserve=lambda amps, charger_entry_id=charger_entry_id: self.reserve_start(
                        charger_entry_id, amps
                    ),
                    record=lambda amps, reason, charger_entry_id=charger_entry_id: self.note_charger_write(
                        charger_entry_id, amps, reason
                    ),
                )
                self._controller_listener_cancels.append(
                    lambda controller=charger_controller: controller.set_start_cap(None)
                )

    @callback
    def _on_charger_controller_changed(self) -> None:
        self._recompute()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _notify(self) -> None:
        for listener in self._listeners:
            listener()

    @callback
    def notify_solar_surplus_changed(self) -> None:
        """Re-render this site's listeners now; called by `SolarExecutionCoordinator`.

        A solar verdict is applied asynchronously, after this controller's own `_notify()` for the
        tick, so the sensor would otherwise show the previous snapshot. It carries no new reading:
        while it runs `rerendering` says so, and no solar coordinator evaluates again for it (one
        charger's re-render would otherwise make the next one's evaluation, and that one's the first's).
        """
        self._rerendering = True
        try:
            self._notify()
        finally:
            self._rerendering = False

    @property
    def rerendering(self) -> bool:
        """Whether the listeners are being told of a solar re-render (`notify_solar_surplus_changed`), not of a
        recompute."""
        return self._rerendering

    @callback
    def _async_periodic_recompute(self, _now: Any) -> None:
        self._recompute()

    @callback
    def _async_state_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._schedule_recompute()

    @callback
    def _schedule_recompute(self) -> None:
        """Recompute once for every change in this turn of the event loop: a meter that writes its phases one entity
        at a time, and the chargers that tell the site of each of their own changes, would otherwise recompute (and
        queue an apply pass on) every partial state. The recompute runs at the next turn (`call_soon`), so it sees
        every change of this one.

        While the site is over its limit on an import (`_import_overload_now`) a change is recomputed at once, as
        before: an overload's lowering waits for nothing, not even the rest of the loop's turn."""
        if self._closed:
            return
        if self._import_overload_now():
            self._recompute()
            return
        if self._recompute_pending:
            return
        self._recompute_pending = True
        self.hass.loop.call_soon(self._run_scheduled_recompute)

    @callback
    def _run_scheduled_recompute(self) -> None:
        self._recompute_pending = False
        if not self._closed:
            self._recompute()

    def _import_overload_now(self) -> bool:
        """Whether the last recompute found an import overload: a decision that must lower (`_must_lower`)."""
        return any(_must_lower(decision) for decision in self.regulator_decisions.values())

    @callback
    def _async_state_reported(self, event: Event[EventStateReportedData]) -> None:
        """A write that changed nothing: only its instant is noted, for the meter's cadence."""
        new_state = event.data.get("new_state")
        if new_state is not None:
            self._cadence.observe(event.data["entity_id"], getattr(new_state, "last_reported", None))

    def _observe_reports(self) -> None:
        for entity_id in self.cadence_entity_ids():
            state = self.hass.states.get(entity_id)
            if state is not None:
                self._cadence.observe(entity_id, getattr(state, "last_reported", None) or state.last_updated)

    def load_balancing_entity_ids(self) -> list[str]:
        """The entities load balancing reads the site's phases from: the direct source's, or every
        per-phase entity of a derived site."""
        entity_ids: set[str] = set()
        mode = self.config.get(CONF_MEASUREMENT_MODE)
        if mode == MEASUREMENT_MODE_DIRECT:
            site_source = self._resolve_site_current_source()
            if site_source is not None:
                entity_ids.update(_source_entity_ids(site_source))
        elif mode == MEASUREMENT_MODE_DERIVED:
            for phase_entities in (self.config.get(CONF_DERIVED_ENTITIES) or {}).values():
                entity_ids.update(value for value in (phase_entities or {}).values() if value)
        return sorted(entity_ids)

    def cadence_entity_ids(self) -> list[str]:
        """The measurement entities whose update interval is watched: load balancing's, the total grid
        power's and the home battery's."""
        entity_ids = set(self.load_balancing_entity_ids())
        grid_total = grid_power_source_from_dict(self.config.get(CONF_GRID_POWER_SOURCE))
        if grid_total is not None:
            entity_ids.update(grid_total.entity_ids)
        entity_ids.update(self.battery_entity_ids())
        return sorted(entity_ids)

    def battery_entity_ids(self) -> list[str]:
        return [
            entity_id
            for entity_id in (
                self.config.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY),
                self.config.get(CONF_BATTERY_DISCHARGE_POWER_ENTITY),
            )
            if entity_id
        ]

    def report_interval_s(self, entity_id: str) -> float | None:
        """The median interval between `entity_id`'s reports, once enough are seen."""
        return self._cadence.interval_s(entity_id)

    @property
    def slow_meters(self) -> dict[str, float]:
        """The measurement entities that update too seldom for load balancing, with their interval."""
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        return self._cadence.slow(self.cadence_entity_ids(), max_age_s)

    @property
    def load_balancing_slow_meters(self) -> dict[str, float]:
        """The entities load balancing reads that update too seldom for it: it is held while any is."""
        lb = set(self.load_balancing_entity_ids())
        return {entity_id: interval for entity_id, interval in self.slow_meters.items() if entity_id in lb}

    def _change_only_alive(self, entity_id: str) -> bool:
        """Whether `entity_id` is from an integration that writes only on change, is available, and its
        integration's entry is loaded: silent, but not dead."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None:
            return False
        behaviour = UPDATE_BEHAVIOUR.get(entry.platform)
        if behaviour is None or not behaviour.on_change_only:
            return False
        config_entry = (
            self.hass.config_entries.async_get_entry(entry.config_entry_id) if entry.config_entry_id else None
        )
        return config_entry is not None and config_entry.state is ConfigEntryState.LOADED

    def solar_accepts(self, value: PhaseValue | None, entity_ids: list[str]) -> bool:
        """Whether solar may use `value`, read from `entity_ids`: usable, and live by solar's age limit
        for the slowest of them (`site/meter_cadence.py`)."""
        if value is None or value.problem is not None or value.value is None or not entity_ids:
            return False
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        intervals = [self._cadence.interval_s(entity_id) for entity_id in entity_ids]
        known = [interval for interval in intervals if interval is not None]
        liveness = solar_liveness(
            value.age_s,
            value.report_age_s,
            max_age_s=max_age_s,
            # The slowest half decides, and only when every half's interval is known.
            interval_s=max(known) if len(known) == len(entity_ids) else None,
            change_only_alive=all(self._change_only_alive(entity_id) for entity_id in entity_ids),
        )
        return liveness in ("fresh", "confirmed_unchanged") and value.age_s is not None

    def _tracked_entity_ids(self) -> list[str]:
        """Entities whose state change triggers a recompute: the site's measurement entities for its
        mode, plus each charger's charge-control and current-limit entities. Computed once; config
        changes reload the entry.
        """
        entity_ids: set[str] = set()
        mode = self.config.get(CONF_MEASUREMENT_MODE)
        if mode == MEASUREMENT_MODE_DIRECT:
            site_source = self._resolve_site_current_source()
            if site_source is not None:
                entity_ids.update(_source_entity_ids(site_source))
        elif mode == MEASUREMENT_MODE_DERIVED:
            for phase_entities in (self.config.get(CONF_DERIVED_ENTITIES) or {}).values():
                entity_ids.update((phase_entities or {}).values())
        phase_wiring: dict[str, dict[str, Any]] = self.config.get(CONF_PHASE_WIRING) or {}
        for charger_entry_id in self.config.get(CONF_CHARGER_ENTRY_IDS) or []:
            charger_controller = controller_for(self.hass, charger_entry_id)
            if charger_controller is not None:
                entity_ids.add(charger_controller.charge_control)
                if charger_controller.current_limit:
                    entity_ids.add(charger_controller.current_limit)
            measured_source = source_from_dict(
                (phase_wiring.get(charger_entry_id) or {}).get(CONF_MEASURED_CURRENT_SOURCE)
            )
            if measured_source is not None:
                entity_ids.update(_source_entity_ids(measured_source))
        grid_total = grid_power_source_from_dict(self.config.get(CONF_GRID_POWER_SOURCE))
        if grid_total is not None:
            entity_ids.update(grid_total.entity_ids)
        battery_source = source_from_dict(self.config.get(CONF_BATTERY_PER_PHASE_SOURCE))
        if battery_source is not None:
            entity_ids.update(_source_entity_ids(battery_source))
        for battery_key in (CONF_BATTERY_AGGREGATE_POWER_ENTITY, CONF_BATTERY_DISCHARGE_POWER_ENTITY):
            battery_entity_id = self.config.get(battery_key)
            if battery_entity_id:
                entity_ids.add(battery_entity_id)
        return sorted(entity_ids)

    def _check_membership(self) -> list[SiteMembershipConflict]:
        charger_entry_ids = list(self.config.get(CONF_CHARGER_ENTRY_IDS) or [])
        return find_site_membership_conflicts(
            self.hass, charger_entry_ids=charger_entry_ids, exclude_entry_id=self.entry_id
        )

    def _recompute(self, *, schedule_apply: bool = True) -> None:
        previous = self.result
        previous_conflicts = self._last_logged_conflicts
        current_conflicts = self.membership_conflicts
        self._observe_reports()
        self.result = self._calculate()
        self._observe_demand()
        self._sample_battery()
        previous_battery = self._recompute_battery
        self._recompute_battery = self._battery_reading()
        self._observe_charger_starts(previous, previous_battery)
        try:
            self._learn_switched_currents()
        except Exception:  # noqa: BLE001 - learning is optional: it never holds back a decision or a lowering
            _LOGGER.exception("SpotNav site %s: learning a switched current failed", self.entry_id)
        try:
            self._observe_grid_sign()
        except Exception:  # noqa: BLE001 - a warning only: it never holds back a decision or a lowering
            _LOGGER.exception("SpotNav site %s: checking the grid meter's sign failed", self.entry_id)
        self._update_direction_history()
        self._update_measurement_lost()
        previous_decisions = self.regulator_decisions
        self.regulator_decisions = self._compute_regulator_decisions()
        _log_transitions(self.entry_id, previous, self.result)
        _log_membership_conflicts(self.entry_id, previous_conflicts, current_conflicts)
        _log_regulator_decisions(
            self.entry_id, previous_decisions, self.regulator_decisions
        )
        self._last_logged_conflicts = current_conflicts
        self.evaluation = self._build_evaluation()
        # The only path from a regulator decision to a charger command: queued, never awaited
        # (`_recompute` is a sync callback), and only when both gates hold.
        if schedule_apply and self._active_control_allowed():
            self._schedule_apply_active_control()
        self._notify()

    def _observe_demand(self) -> None:
        """Give the hour tracker this recompute's net grid power and the chargers' draw (at most every few seconds).
        A measurement only: it never raises into the recompute and nothing here decides anything (the import target
        reads the hour it keeps, `_hold_to_import_target`)."""
        if not self.demand.due():
            return
        try:
            from ..execution.solar_execution import site_car_w, site_net_grid_w  # noqa: PLC0415 - it imports us
            from ..pricing.price_source import site_demand_components  # noqa: PLC0415 - it imports the planner

            grid_w = site_net_grid_w(self)
            car_w = None if grid_w is None else site_car_w(self.hass, self)
            # A battery's discharge is kept beside the hour for an import target under `battery_first` only.
            discharge_w = self._battery_first_discharge()[1]
            self.demand.observe(
                grid_w,
                car_w,
                self.demand_zone(),
                discharge_w,
                target=self._target_mark(),
                components=lambda: site_demand_components(self.hass, self.entry_id),
            )
        except Exception:  # noqa: BLE001 - a measurement must never disturb the site
            _LOGGER.debug("SpotNav site %s: the hour's import could not be measured", self.entry_id, exc_info=True)
        if self.demand.backfill_due():
            self.async_start_backfill()

    @callback
    def async_start_backfill(self) -> bool:
        """Read the hours before SpotNav measured from Home Assistant's statistics, once, in the background
        (`SiteDemand.async_backfill`): only when the recorder runs. Whether it started. Changes nothing a charger is
        told now: an imported hour is shown and counted in the month's peaks, and in the import target's lift only
        when it is a running month's hour shown complete by the 5-minute statistics."""
        from homeassistant.core import CoreState  # noqa: PLC0415 - kept beside its only use

        from .hour_statistics import recorder_ready, RecorderReader, site_series  # noqa: PLC0415

        if self._closed or self.hass.state is not CoreState.running or not recorder_ready(self.hass):
            return False
        from ..pricing.price_source import site_demand_components  # noqa: PLC0415 - it imports the planner

        try:
            series = site_series(self.hass, self.config)
            components = site_demand_components(self.hass, self.entry_id)
        except Exception:  # noqa: BLE001 - a back-fill must never disturb the site
            _LOGGER.debug("SpotNav site %s: the statistics' sources could not be read", self.entry_id, exc_info=True)
            return False
        # Marked as running at once, so the next recompute does not start another.
        self.demand._backfill_running = True  # noqa: SLF001 - the demand's own flag, set before the task runs

        async def run() -> None:
            self.demand._backfill_running = False  # noqa: SLF001 - async_backfill sets it again
            await self.demand.async_backfill(RecorderReader(self.hass), series, components)

        self.hass.async_create_background_task(run(), f"spotnav_site_backfill_{self.entry_id}")
        return True

    def _target_mark(self) -> TargetMark | None:
        """What the import target did this hour as of its last decision (held a charger's current down, or paused a
        charger), for the power view's marks; `None` while it holds nothing."""
        budget = self._target_budget
        if budget is None:
            return None
        held = bool(self._target_lowered or self._target_locked)
        paused = any(hour == budget.hour_start for hour in self._target_paused_hour.values())
        if not (held or paused):
            return None
        return TargetMark(hour_start=budget.hour_start, held=held, paused=paused)

    def demand_zone(self) -> str:
        """The clock the site's hours and months are on: its electricity price's area's, else Home Assistant's."""
        now = time.monotonic()
        if self._demand_zone is not None and now - self._demand_zone[0] < _DEMAND_ZONE_TTL_S:
            return self._demand_zone[1]
        zone = _home_zone(self.hass)
        try:
            from ..pricing.price_source import site_price_source  # noqa: PLC0415 - it imports the planner

            source = site_price_source(self.hass, self.entry_id)
            if source is not None and source.area is not None and source.area.tz:
                zone = source.area.tz
        except Exception:  # noqa: BLE001 - Home Assistant's own zone is the fallback
            _LOGGER.debug("SpotNav site %s: no price area for the hour's clock", self.entry_id, exc_info=True)
        self._demand_zone = (now, zone)
        return zone

    @staticmethod
    def _is_commandable_charger(charger_controller: object) -> bool:
        """The predicate active control gates each write on.

        Commandable means a loaded `ChargingController` that opted in to a way of setting its
        current (`ChargingController.is_commandable`: OCPP's `ChangeConfiguration`, or an adapter
        that can set one). `capability_snapshot` reports the same notion as `active_available`, so
        the two must agree. Not the same as `charger_has_current_control`, which only says a
        current-limit entity exists.
        """
        return charger_controller is not None and charger_controller.is_commandable

    @staticmethod
    def _is_switch_only_charger(charger_controller: object) -> bool:
        """A loaded charger SpotNav can start and stop but whose current it cannot set
        (`ChargingController.is_switch_only`): load balancing pauses and resumes it whole, at its switched current."""
        return charger_controller is not None and getattr(charger_controller, "is_switch_only", False) is True

    def _is_governed_charger(self, charger_controller: object) -> bool:
        """Whether active control acts on this charger at all: one whose current it sets, or one it can only pause."""
        return self._is_commandable_charger(charger_controller) or self._is_switch_only_charger(charger_controller)

    def _log_active_control_outcome(
        self,
        charger_entry_id: str,
        decision: RegulatorDecision,
        *,
        outcome: Literal["wrote", "held"],
        detail: str | None = None,
        detail_phase: PhaseName | None = None,
        setpoint: int | None = None,
        previous_setpoint: int | None = None,
        yield_verdict: YieldVerdict | None = None,
    ) -> None:
        """Log one INFO line per charger per change, not per pass.

        The signature is the whole logged content, so an identical outcome is logged once. Held
        decisions are logged too, since they leave no other trace; `detail` names the check that
        stopped it.
        """
        signature: tuple[Any, ...] = (
            outcome,
            detail,
            detail_phase,
            setpoint,
            previous_setpoint,
            decision.proposed_current_a,
            decision.reason,
            None if yield_verdict is None else yield_verdict.reason,
        )
        if self._logged_apply_outcomes.get(charger_entry_id) == signature:
            return
        self._logged_apply_outcomes[charger_entry_id] = signature
        self._record_decision(
            charger_entry_id,
            decision,
            outcome=outcome,
            detail=detail,
            detail_phase=detail_phase,
            setpoint=setpoint,
            previous_setpoint=previous_setpoint,
            yield_verdict=yield_verdict,
        )
        was = "none this session" if previous_setpoint is None else f"{previous_setpoint}A"
        if outcome == "wrote":
            _LOGGER.info(
                "SpotNav site %s active control: charger %s set to %sA (previous %s) "
                "damping=%s reason=%s limiting_phase=%s",
                self.entry_id,
                charger_entry_id,
                setpoint,
                was,
                detail,
                decision.reason,
                decision.limiting_phase,
            )
            return
        _LOGGER.info(
            "SpotNav site %s active control: charger %s held at %s, would have written "
            "%sA -- stopped_by=%s%s reason=%s",
            self.entry_id,
            charger_entry_id,
            was,
            decision.proposed_current_a,
            detail,
            f" phase={detail_phase}" if detail_phase is not None else "",
            decision.reason,
        )

    def _record_decision(
        self,
        charger_entry_id: str,
        decision: RegulatorDecision,
        *,
        outcome: str,
        detail: str | None,
        detail_phase: PhaseName | None,
        setpoint: int | None,
        previous_setpoint: int | None,
        yield_verdict: YieldVerdict | None,
    ) -> None:
        """Append one entry to the decision log: when, from and to amps, why, which phase limited,
        the battery's power and the measured currents. Plain values only, so it serialises as is.
        """
        damper = self._dampers.get(charger_entry_id)
        last_written = None if damper is None else damper.last_written_a
        from_a = previous_setpoint if previous_setpoint is not None else (
            None if last_written is None else round(last_written)
        )
        battery = self.battery_aggregate_power()
        wiring: dict[str, Any] = (self.config.get(CONF_PHASE_WIRING) or {}).get(
            charger_entry_id
        ) or {}
        delivered = self._read_charger_measured_current(wiring)
        requested = next(
            (basis.requested_current_a for basis in decision.basis.values()), None
        )
        entry: dict[str, Any] = {
            "time": dt_util.utcnow().isoformat(),
            "charger_id": charger_entry_id,
            "outcome": outcome,
            "from_a": from_a,
            "to_a": setpoint,
            "proposed_a": decision.proposed_current_a,
            "requested_a": requested,
            "reason": decision.reason,
            "detail": detail,
            "yield_reason": None if yield_verdict is None else yield_verdict.reason,
            "yield_state": None if yield_verdict is None else yield_verdict.state,
            "limiting_phase": detail_phase or decision.limiting_phase,
            "battery_power_w": None if battery is None else battery.value,
            "site_current_a": dict(self.result.measured_phase_current_a),
            "charger_current_a": (
                None
                if delivered is None
                else {phase: delivered.get(phase).value for phase in PHASES}
            ),
            # What a charger just started was credited with while its own reading lagged (`START_CREDIT_S`).
            "start_credit_a": dict(self._credited_a.get(charger_entry_id) or {}) or None,
            # What a charger was credited with after a raise its own reading did not show yet.
            "raise_credit_a": dict(self._raise_credited_a.get(charger_entry_id) or {}) or None,
        }
        self._decision_log.append(entry)
        # The charger's plug-in keeps it too, across restarts (`debug_day.ChargerDay`).
        data = charger_data(self.hass, charger_entry_id)
        day = None if data is None else data.debug_day
        if day is not None:
            day.note_regulator(entry)

    @property
    def regulator_decision_log(self) -> list[dict[str, Any]]:
        """A copy of this site's bounded decision log, oldest first. Generic on purpose: a list of
        plain dicts that any bundle or diagnostic can include as it is.
        """
        return [dict(entry) for entry in self._decision_log]

    def load_balancing_limit(self, charger_entry_id: str) -> tuple[float | None, str | None]:
        """`(limit_a, cause)` while active control holds this charger below what it asked for, else
        `(None, None)`. `limit_a` is the current it was last given; `cause` is the battery sharing
        the fuse, house consumption, the site's measurement lost (`limit_a` is then the charger's minimum), the
        site's import target (`_hold_to_import_target`), or `None` when it is not known.
        """
        decision = self.regulator_decisions.get(charger_entry_id)
        damper = self._dampers.get(charger_entry_id)
        if decision is None or damper is None or not self._active_control_allowed():
            return None, None
        if self._is_switch_only_charger(controller_for(self.hass, charger_entry_id)):
            # Nothing is ever applied to its current: it runs whole or is paused (`balancing_pause`).
            return None, None
        if self._measurement_lost_due(charger_entry_id, self.result) is not None:
            # Held at its minimum until the measurement is back, whatever it asked for.
            return self._minimum_setpoint(charger_entry_id), LIMIT_CAUSE_MEASUREMENT_LOST
        assigned = damper.last_written_a
        share = self._target_shares.get(charger_entry_id)
        if (
            charger_entry_id in self._target_lowered
            and share is not None
            and share.action == ACTION_HELD
            and share.cap_a is not None
            and assigned is not None
            and DEFAULT_MIN_CURRENT_A <= assigned <= share.cap_a + 0.5
        ):
            # The import target, not the fuse, holds it: the hour's budget leaves it no more.
            return assigned, LIMIT_CAUSE_IMPORT_TARGET
        requested = next((basis.requested_current_a for basis in decision.basis.values()), None)
        if assigned is None or requested is None or assigned >= requested - 0.5:
            return None, None
        margins = [
            self.result.measured_margin_a.get(phase) for phase in decision.basis
        ]
        usable = [margin for margin in margins if margin is not None]
        if not usable or min(usable) > LIMIT_AT_FUSE_MARGIN_A:
            return assigned, None
        battery = self.battery_aggregate_power()
        battery_charging = (
            battery is not None
            and battery.value is not None
            and battery.problem is None
            and battery.value >= LIMIT_BATTERY_CHARGING_W
        )
        return assigned, (
            LIMIT_CAUSE_BATTERY_SHARES_FUSE if battery_charging else LIMIT_CAUSE_HOUSE_CONSUMPTION
        )

    def start_credit_snapshot(self) -> dict[str, dict[str, Any]]:
        """The start credits in force, for the diagnostics: per charger the amps, the seconds since the start and
        the amps credited per phase on the last decision."""
        now = self._yield_now()
        return {
            charger_entry_id: {
                "amps": credit.amps,
                "age_s": round(now - credit.since, 1),
                "credited_a": dict(self._credited_a.get(charger_entry_id) or {}),
            }
            for charger_entry_id, credit in self._start_credits.items()
        }

    def _begin_start_credit(
        self,
        charger_entry_id: str,
        amps: float,
        baseline: Mapping[PhaseName, float | None],
        battery: tuple[float | None, dict[PhaseName, float] | None],
    ) -> None:
        self._start_credits.pop(charger_entry_id, None)
        self._credited_a.pop(charger_entry_id, None)
        self._start_credit_seq += 1
        self._start_credits[charger_entry_id] = StartCredit(
            float(amps),
            self._yield_now(),
            dict(baseline),
            {
                other: draw
                for other, draw in self._drawn_a(self._build_requests(credit=False)).items()
                if other != charger_entry_id
            },
            self._start_credit_seq,
            battery[0],
            battery[1],
        )

    def _drawn_a(
        self, requests: list[ChargerRequest], *, credited: bool = True
    ) -> dict[str, dict[PhaseName, float]]:
        """What each charger draws per phase: its own reading, or (with `credited`) what its start or its last raise
        was credited with on the last decision where that is more."""
        drawn: dict[str, dict[PhaseName, float]] = {}
        for request in requests:
            measured = request.measured_current_a
            charger_entry_id = request.charger_entry_id
            credits = (
                [self._credited_a.get(charger_entry_id) or {}, self._raise_credited_a.get(charger_entry_id) or {}]
                if credited
                else []
            )
            drawn[charger_entry_id] = {
                phase: max(
                    [
                        0.0 if measured is None else (measured.get(phase).value or 0.0),
                        *(credit.get(phase, 0.0) for credit in credits),
                    ]
                )
                for phase in (request.phases_used() or ())
            }
        return drawn

    def _end_start_credit(self, charger_entry_id: str) -> None:
        self._start_credits.pop(charger_entry_id, None)
        self._credited_a.pop(charger_entry_id, None)

    def _observe_charger_starts(
        self, previous: SiteCapacityResult, previous_battery: tuple[float | None, dict[PhaseName, float] | None]
    ) -> None:
        """A charger seen charging that was not (it began by itself, or a start of ours without a reservation):
        its start is credited as one of ours is, against the site's current and the battery's power before this
        recompute."""
        for charger_entry_id in list(self.config.get(CONF_CHARGER_ENTRY_IDS) or []):
            charger_controller = controller_for(self.hass, charger_entry_id)
            charging = charger_controller is not None and bool(charger_controller.charging)
            was = self._seen_charging.get(charger_entry_id)
            self._seen_charging[charger_entry_id] = charging
            if not charging:
                if charger_controller is None or not charger_controller.start_pending:
                    self._end_start_credit(charger_entry_id)
                continue
            if was is False and charger_entry_id not in self._start_credits:
                amps = charger_controller.resolve_current().amps
                if amps is not None and amps > 0:
                    self._begin_start_credit(
                        charger_entry_id, amps, previous.measured_phase_current_a, previous_battery
                    )

    def _credited_measurements(
        self, requests: list[ChargerRequest], site_a: Mapping[PhaseName, float | None]
    ) -> dict[str, DirectPhaseMeasurement]:
        """Each charger just started credited with the draw the site shows it took, where that is more than its own
        reading; the chargers not credited are left out.

        While a start credit holds (`START_CREDIT_S`) and the charger's own reading still shows it drawing nothing
        on every phase it uses, each such phase is credited with what the site's current rose by since the start,
        never more than the charger was given: the amps of the start, the current on record, and what the
        regulator last wrote, whichever is least. Exactly as a measured draw would be. One rise is credited once:
        the credits are taken in the order the starts went out, and each gets only what the site rose by that the
        other chargers' draws (their readings, or the credits before it) do not already account for, plus what the
        home battery stopped drawing since (less what it began to draw), on fresh readings of it then and now: a
        battery that stops charging as the car starts (05:15 on 2026-11-01, 8 kW off as the car's 14.6 A came on)
        leaves a rise far smaller than the car's draw (`_battery_fall_credit`). A reading
        that is missing, unusable or stale credits nothing (it stays what it is). Once the charger's own reading
        shows it drawing, the measurement counts and the credit ends. The credit never hides an overload: the
        site's current itself is never changed, only how much of it is the car's, so a phase over the fuse with
        the credit applied is still over the fuse and is lowered or paused at once. Nor does it ever raise a
        charger (`START_CREDIT_RAISE_TOLERANCE_A`).
        """
        if not self._start_credits:
            return {}
        by_id = {request.charger_entry_id: request for request in requests}
        # Every charger by its own reading; a credit taken below then counts for the credits after it.
        drawn = self._drawn_a(requests, credited=False)
        now = self._yield_now()
        battery_now = self._battery_reading()
        result: dict[str, DirectPhaseMeasurement] = {}
        for charger_entry_id, credit in sorted(
            self._start_credits.items(), key=lambda item: (item[1].since, item[1].seq)
        ):
            request = by_id.get(charger_entry_id)
            measured = None if request is None else request.measured_current_a
            phases = None if request is None else request.phases_used()
            if measured is None or not phases:
                continue
            if now - credit.since > START_CREDIT_S:
                self._end_start_credit(charger_entry_id)
                continue
            values = {phase: measured.get(phase) for phase in phases}
            if any(value.value is not None and value.value >= PROBE_IDLE_BELOW_A for value in values.values()):
                # The charger's own reading shows the car: it counts from now on.
                self._end_start_credit(charger_entry_id)
                continue
            given = [credit.amps]
            charger_controller = controller_for(self.hass, charger_entry_id)
            if charger_controller is not None:
                requested = charger_controller.resolve_current().amps
                if requested is not None:
                    given.append(float(requested))
            damper = self._dampers.get(charger_entry_id)
            written = None if damper is None else damper.last_written_a
            credited: dict[PhaseName, float] = {}
            replaced: dict[PhaseName, PhaseValue] = {}
            # What the other chargers began to draw since this start, per phase, is theirs, not this charger's.
            theirs_by_phase = {
                phase: sum(
                    max(0.0, draw.get(phase, 0.0) - credit.others_a.get(other, {}).get(phase, 0.0))
                    for other, draw in drawn.items()
                    if other != charger_entry_id
                )
                for phase in PHASES
            }
            battery_bound = self._battery_fall_credit(credit, battery_now, site_a, theirs_by_phase, tuple(values))
            for phase, value in values.items():
                if value.value is None or value.problem is not None:
                    continue
                # What the regulator last wrote, as far as this phase's reading can show it: a reading taken before
                # that write still shows the current before it (`_written_at_reading`).
                in_force = self._written_at_reading(charger_entry_id, written, self._reading_at(phase))
                cap = max(0.0, min(given + ([in_force] if in_force is not None and in_force > 0 else [])))
                now_a = site_a.get(phase)
                before_a = credit.baseline_a.get(phase)
                if now_a is None or before_a is None:
                    continue
                rise = now_a - before_a - theirs_by_phase[phase]
                if battery_bound is not None and phase in battery_bound:
                    rise = battery_bound[phase]
                amps = min(cap, max(0.0, rise))
                # A rise below what counts as drawing is the house's flicker, not a car.
                if amps >= PROBE_IDLE_BELOW_A and amps > value.value:
                    credited[phase] = round(amps, 2)
                    replaced[phase] = replace(value, value=amps)
                    drawn.setdefault(charger_entry_id, {})[phase] = amps
            self._credited_a[charger_entry_id] = credited
            if replaced:
                result[charger_entry_id] = DirectPhaseMeasurement(
                    l1=replaced.get("L1", measured.l1),
                    l2=replaced.get("L2", measured.l2),
                    l3=replaced.get("L3", measured.l3),
                )
        return result

    def _battery_reading(self) -> tuple[float | None, dict[PhaseName, float] | None]:
        """The home battery's power (W, positive while it charges) on a fresh reading, else `None`, and its current
        per phase (A, positive while it charges) where the site reads it per phase (`CONF_BATTERY_PER_PHASE_SOURCE`)
        and every phase is fresh and usable, else `None`. The per-phase reading is a magnitude: its sign is the
        power's, so it needs the fresh power too."""
        watts = self._fresh_battery_w()
        if watts is None:
            return None, None
        source = source_from_dict(self.config.get(CONF_BATTERY_PER_PHASE_SOURCE))
        if source is None:
            return watts, None
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        values = read_phase_measurement(self.hass, source, classify_current)
        sign = 1.0 if watts >= 0.0 else -1.0
        per_phase: dict[PhaseName, float] = {}
        for phase in PHASES:
            value = values[phase]
            if value.value is None or value.problem is not None or value.age_s is None or value.age_s > max_age_s:
                return watts, None
            per_phase[phase] = sign * abs(float(value.value))
        return watts, per_phase

    @staticmethod
    def _battery_fall_credit(
        credit: StartCredit,
        battery_now: tuple[float | None, dict[PhaseName, float] | None],
        site_a: Mapping[PhaseName, float | None],
        theirs_by_phase: Mapping[PhaseName, float],
        car_phases: tuple[PhaseName, ...],
    ) -> dict[PhaseName, float] | None:
        """What a just-started car drew per phase it uses, by the site's rise since the start and what the home battery
        stopped drawing since, where the battery changed; `None` where it cannot be said (no fresh battery reading at
        the start or now, a site phase unread), and the rise alone then counts.

        With the battery read per phase, each phase is credited its own rise plus its own fall. With its power alone
        nothing says on which phases it fell, and an equal split would credit a phase it is not on (a single-phase
        battery) with a fall that never was there. What the site shows whatever the split: the car draws the same on
        each phase it uses, so what it drew is the site's rise over every phase plus the battery's whole fall, shared
        over the car's phases. No phase is credited more than its own rise plus that whole fall, nor, where the battery
        began to draw more, more than its own rise."""
        before_w, before_phase = credit.battery_w, credit.battery_phase_a
        now_w, now_phase = battery_now
        if before_w is None or now_w is None:
            return None
        rises: dict[PhaseName, float] = {}
        for phase in PHASES:
            now_a, before_a = site_a.get(phase), credit.baseline_a.get(phase)
            if now_a is None or before_a is None:
                return None
            rises[phase] = now_a - before_a - theirs_by_phase.get(phase, 0.0)
        if before_phase is not None and now_phase is not None:
            return {phase: rises[phase] + before_phase[phase] - now_phase[phase] for phase in car_phases}
        fall_a = (before_w - now_w) / DIRECTION_NOMINAL_PHASE_VOLTAGE_V
        shared = (sum(rises.values()) + fall_a) / len(car_phases)
        return {phase: min(rises[phase] + max(0.0, fall_a), shared) for phase in car_phases}

    def _begin_raise_credit(self, charger_entry_id: str, at: float, amps: float) -> None:
        """The regulator raised this charger to `amps` at `at` (epoch seconds): what the site rises by from the
        readings just before may be the car's, until its own reading has had the time to show it
        (`_raise_credited_measurements`). A raise on one still open is measured from the readings before that one;
        a charger not drawing on every phase it uses, or drawing on one phase only, is credited nothing (a start has
        its own credit)."""
        earlier = self._raise_credits.get(charger_entry_id)
        if earlier is not None:
            self._raise_credits[charger_entry_id] = replace(earlier, writes=(*earlier.writes, (at, amps)))
            return
        requests = self._build_requests(credit=False)
        request = next((r for r in requests if r.charger_entry_id == charger_entry_id), None)
        measured = None if request is None else request.measured_current_a
        phases = None if request is None else request.phases_used()
        # A single-phase charger's raise and any load on its phase show alike on the site: never credited.
        if measured is None or not phases or len(phases) < 2:
            return
        delivered: dict[PhaseName, float] = {}
        for phase in phases:
            value = measured.get(phase)
            if value.value is None or value.problem is not None or value.value < PROBE_IDLE_BELOW_A:
                return
            delivered[phase] = value.value
        self._raise_credits[charger_entry_id] = RaiseCredit(
            at,
            ((at, amps),),
            dict(self._calculate().measured_phase_current_a),
            delivered,
            {other: draw for other, draw in self._drawn_a(requests).items() if other != charger_entry_id},
        )

    def _end_raise_credit(self, charger_entry_id: str) -> None:
        self._raise_credits.pop(charger_entry_id, None)
        self._raise_credited_a.pop(charger_entry_id, None)

    def _raise_credit_writes(self, charger_entry_id: str, credit: RaiseCredit, now: float) -> RaiseCredit | None:
        """`credit` with every write to the charger since its last one added (a lowering, a pause, the floor a pause
        left, a resume, another path's write), whatever path wrote it: what the charger was last given
        (`RegulatorDamper.last_written_a`) and when (`_written_at`; now where the path does not say). `None` once the
        regulator no longer knows what the charger was given (a start)."""
        damper = self._dampers.get(charger_entry_id)
        amps = None if damper is None else damper.last_written_a
        if amps is None:
            return None
        last_at, last_amps = credit.writes[-1]
        written_at = self._written_at.get(charger_entry_id)
        if written_at is not None and written_at > last_at:
            credit = replace(credit, writes=(*credit.writes, (written_at, float(amps))))
        elif amps != last_amps:
            credit = replace(credit, writes=(*credit.writes, (max(now, last_at), float(amps))))
        self._raise_credits[charger_entry_id] = credit
        return credit

    @staticmethod
    def _raise_credit_in_force(credit: RaiseCredit, reading_at: float) -> float | None:
        """What was on the charger, by the credit's writes, when a site reading was taken at `reading_at`, `None` before
        the raise. A raise counts on a reading taken after it (one of the same moment is not in it yet); a lowering or a
        pause only on one taken more than `RAISE_CREDIT_LOWERING_SETTLE_S` after it, the car having had the time to come
        down: until then the current before it stays in force, as the site still shows it."""
        in_force: float | None = None
        for at, amps in credit.writes:
            lowering = in_force is not None and amps < in_force
            if (reading_at - at > RAISE_CREDIT_LOWERING_SETTLE_S) if lowering else (at < reading_at):
                in_force = amps
        return in_force

    def _raise_credited_measurements(
        self,
        requests: list[ChargerRequest],
        site_a: Mapping[PhaseName, float | None],
        start_credited: Mapping[str, DirectPhaseMeasurement],
    ) -> dict[str, DirectPhaseMeasurement]:
        """Each charger the regulator raised while it drew, credited with the step written where the site shows the
        car took it, until its own reading has had the time to show it; the chargers not credited are left out.
        `start_credited` is what `_credited_measurements` credited the same decision with.

        An OCPP charger's meter values come half a minute apart: right after a raise its reading still shows the
        current before it, while the site meter already shows the car at the new one. Reckoned at its old reading,
        the car's raise reads as house load, and a house load that comes on then pauses the car where lowering it
        would do (the morning of 2026-11-01: 8 to 16 A, a washing machine's heater on L1 six seconds later, the car
        paused for an overload a lowering to about 10 A would have ended).

        The step is measured from the readings before it was written, the site's and the charger's. On each phase the
        step is what is on the charger as the site read the phase (the last write before that reading, a lowering or
        a pause included: `_raise_credit_in_force`) less the charger's reading before the raise, and the site's rise
        since is less what the other chargers began to draw. Only a rise that matches the step is the car's: at least
        the step, less `RAISE_CREDIT_STEP_TOLERANCE_A`, on every phase the charger uses (a car's raise shows on each of
        them), and no more than the step, plus that tolerance, on at least one (a load that came on with the raise
        shows more; a house load on some of the phases is no matter). Anything else credits nothing, and the car is
        reckoned on its reading as before; so does a single-phase charger (its raise and a load on its phase look
        alike), and so does every charger while more than one has a raise open (which rise is whose is not known).
        The credit is the reading before the raise plus the step, never more than is on the charger and never less
        than its own reading now (a reading that shows part of the step is not counted twice). Only a raise is
        credited: a lowering keeps the reading, the higher of the two.

        As a last line, a lowering made on the credit that the site does not show more than
        `RAISE_CREDIT_LOWERING_SETTLE_S` after it, with a phase still over the limit, ends the credit, and the same decision is taken on the
        charger's own reading. The credit also ends once the charger's own reading was taken `CATCH_UP_S` after the
        raise (it has had the time to show it, `charger_above_written`), once that reading is missing, unusable or
        shows the car idle, after `START_CREDIT_S`, at a start (the regulator no longer knows what it gave the
        charger), or when a start of the charger is credited instead (`_credited_measurements`). The site's current
        itself is never changed, only how much of it is the car's, and a decision on the credit never raises the
        charger (`START_CREDIT_RAISE_TOLERANCE_A`)."""
        if not self._raise_credits:
            return {}
        by_id = {request.charger_entry_id: request for request in requests}
        drawn = self._drawn_a(
            [
                replace(request, measured_current_a=start_credited[request.charger_entry_id])
                if request.charger_entry_id in start_credited
                else request
                for request in requests
            ],
            credited=False,
        )
        now = dt_util.utcnow().timestamp()
        reading_at: dict[PhaseName, float] = {}
        for phase in PHASES:
            at = self._reading_at(phase)
            reading_at[phase] = now if at is None else at.timestamp()
        margins = self.result.measured_margin_a
        open_credits: dict[str, tuple[RaiseCredit, DirectPhaseMeasurement, dict[PhaseName, PhaseValue]]] = {}
        for charger_entry_id, credit in list(self._raise_credits.items()):
            request = by_id.get(charger_entry_id)
            measured = None if request is None else request.measured_current_a
            phases = None if request is None else request.phases_used()
            synced = None if measured is None else self._raise_credit_writes(charger_entry_id, credit, now)
            if (
                synced is None
                or measured is None
                or not phases
                or len(phases) < 2
                or charger_entry_id in self._start_credits
                or now - credit.at > START_CREDIT_S
            ):
                self._end_raise_credit(charger_entry_id)
                continue
            credit = synced
            values = {phase: measured.get(phase) for phase in phases}
            if any(
                value.value is None
                or value.problem is not None
                or value.value < PROBE_IDLE_BELOW_A
                or value.age_s is None
                or credit.delivered_a.get(phase) is None
                or now - min(value.age_s, value.age_s if value.report_age_s is None else value.report_age_s)
                >= credit.at + CATCH_UP_S
                for phase, value in values.items()
            ):
                # The charger's own reading has had the time to show the raise (or shows nothing to go by): it counts
                # alone from now on.
                self._end_raise_credit(charger_entry_id)
                continue
            lowered_at = next(
                (
                    at
                    for index, (at, amps) in reversed(list(enumerate(credit.writes)))
                    if index > 0 and amps < max(earlier for _, earlier in credit.writes[:index])
                ),
                None,
            )
            if lowered_at is not None and any(
                reading_at[phase] - lowered_at > RAISE_CREDIT_LOWERING_SETTLE_S
                and (margins.get(phase) is not None and margins[phase] < 0.0)
                for phase in phases
            ):
                # Lowered on the credit, and the site is still over the limit once it had the time to show it: the
                # credit is not the car's after all.
                self._end_raise_credit(charger_entry_id)
                continue
            open_credits[charger_entry_id] = (credit, measured, values)
        result: dict[str, DirectPhaseMeasurement] = {}
        for charger_entry_id, (credit, measured, values) in open_credits.items():
            credited: dict[PhaseName, float] = {}
            steps: dict[PhaseName, float] = {}
            rises: dict[PhaseName, float] = {}
            for phase in values:
                in_force = self._raise_credit_in_force(credit, reading_at[phase])
                now_a = site_a.get(phase)
                before_a = credit.baseline_a.get(phase)
                if in_force is None or now_a is None or before_a is None:
                    break
                steps[phase] = in_force - credit.delivered_a[phase]
                # What the other chargers began to draw since this raise is theirs, not this charger's.
                theirs = sum(
                    max(0.0, draw.get(phase, 0.0) - credit.others_a.get(other, {}).get(phase, 0.0))
                    for other, draw in drawn.items()
                    if other != charger_entry_id
                )
                rises[phase] = now_a - before_a - theirs
            matches = (
                # More than one raise open: which rise is whose is not known, so none is credited.
                len(open_credits) == 1
                and len(steps) == len(values)
                and all(step >= PROBE_IDLE_BELOW_A for step in steps.values())
                and all(rises[phase] >= steps[phase] - RAISE_CREDIT_STEP_TOLERANCE_A for phase in steps)
                and min(rises[phase] - steps[phase] for phase in steps) <= RAISE_CREDIT_STEP_TOLERANCE_A
            )
            replaced: dict[PhaseName, PhaseValue] = {}
            if matches:
                for phase, value in values.items():
                    amps = credit.delivered_a[phase] + min(steps[phase], max(0.0, rises[phase]))
                    if value.value is not None and amps > value.value:
                        credited[phase] = round(amps, 2)
                        replaced[phase] = replace(value, value=amps)
            self._raise_credited_a[charger_entry_id] = credited
            if replaced:
                result[charger_entry_id] = DirectPhaseMeasurement(
                    l1=replaced.get("L1", measured.l1),
                    l2=replaced.get("L2", measured.l2),
                    l3=replaced.get("L3", measured.l3),
                )
        return result

    def _reading_at(self, phase: PhaseName) -> datetime | None:
        """When the site's reading of `phase` was last reported (changed or not), or `None`: its active-power
        entity's own `last_reported` where the site is derived, else what `self.result` says of its age."""
        reported = self._active_power_last_reported(phase)
        if reported is not None:
            return reported
        age = self.result.phase_report_age_s.get(phase)
        if age is None:
            age = self.result.phase_age_s.get(phase)
        return None if age is None else dt_util.utcnow() - timedelta(seconds=age)

    def _written_at_reading(
        self, charger_entry_id: str, written: float | None, reading_at: datetime | None
    ) -> float | None:
        """The regulator's last written current `written` as a reading taken at `reading_at` shows it: the current in
        force before that write when the write went out after the reading (`None` when that is not known), else
        `written`. One reading is the measure of one current: a pass on a reading the site took before the
        regulator's last write must not credit the car with that write's lower current, or the same excess lowers the
        car again on every pass until a new reading comes (02:15:12, 2026-10-09: 16, 15, 11, 7 A within 0.6 s on one
        reading). A new reading that is still over lowers it again at once."""
        writes = self._regulated_writes.get(charger_entry_id)
        if written is None or not writes or reading_at is None:
            return written
        at, amps, before = writes[-1]
        if amps != written or at <= reading_at:
            return written
        return before

    def start_allowance_a(self, charger_entry_id: str) -> float | None:
        """What a start may give this charger now, in amps: the tightest phase's uncredited margin
        plus what the charger already delivers there, and no more than the car's minimum while the home
        battery fills the fuse (`_battery_takes_the_fuse`). `None` (no cap) unless active control is on,
        nothing blocks writing, and the measurement is usable on every phase the charger uses."""
        if not self._active_control_allowed() or self.membership_conflicts:
            return None
        fresh = self._calculate()
        if fresh.state != "observing":
            return None
        # Its own reading only: a start credit may keep a charger from a pause, never give it more.
        request = next(
            (r for r in self._build_requests(credit=False) if r.charger_entry_id == charger_entry_id), None
        )
        phases = None if request is None else request.phases_used()
        if not phases:
            return None
        delivered = request.measured_current_a
        reserved = self._reserved_by_others_a(charger_entry_id)
        # Where the direction is not known, the lowering by |I| is the only thing that brings the car down, and a
        # charger read far less often than the site (a cloud) still reports what it drew before that lowering: what
        # it is credited with is never more than was last written to it, so the sun's write, a resend or a changed
        # setting do not give the lowered current straight back.
        written = (
            None if self._direction_known_for(charger_entry_id) else self._damper_for(charger_entry_id).last_written_a
        )
        allowances: list[float] = []
        for phase in phases:
            margin = fresh.measured_margin_a.get(phase)
            if margin is None:
                return None
            own = 0.0 if delivered is None else (delivered.get(phase).value or 0.0)
            if written is not None:
                own = min(own, written)
            allowances.append(margin + own - reserved.get(phase, 0.0))
        allowance = max(0.0, min(allowances))
        minimum = self._min_current_a(charger_entry_id)
        if allowance >= minimum and self._battery_takes_the_fuse():
            # The battery fills the fuse as the car starts: the car goes in at its minimum, so the battery has a car
            # it can give way to rather than a full one on top of it, and yield stepping raises it from there.
            return minimum
        return allowance

    def reserve_start(self, charger_entry_id: str, amps: float | None) -> None:
        """A start of `amps` is on its way to this charger (`None`: it did not go out, or is over). Until
        the charger draws it or `START_RESERVATION_S` pass, another start on the site is given only what is
        left (`start_allowance_a`): two starts in one tick never both take the whole margin."""
        if amps is None or amps <= 0:
            self._start_reservations.pop(charger_entry_id, None)
            self._end_start_credit(charger_entry_id)
            return
        request = next((r for r in self._build_requests() if r.charger_entry_id == charger_entry_id), None)
        phases = None if request is None else request.phases_used()
        if not phases:
            return
        # The site's current just before the start goes out: what it rises by from here may be the car's.
        self._begin_start_credit(
            charger_entry_id, amps, self._calculate().measured_phase_current_a, self._battery_reading()
        )
        self._start_reservations[charger_entry_id] = (
            float(amps),
            tuple(phases),
            self._yield_now() + START_RESERVATION_S,
        )

    def note_charger_write(self, charger_entry_id: str, amps: float, reason: str) -> None:
        """A current went to this charger by a path other than the regulator (a start, a resend after a status
        blip, the deferred session write, the sun's own write). Never measured against a value from before it:

        * a start (`WRITE_SESSION_START`, its deferred session write included) begins a charge, and the damper
          forgets what it last wrote, as for any charge whose current the regulator has not written yet: its
          first word goes out at once (`first_write`), whatever it is;
        * any other write is taken as the value on the charger, as for the regulator's own write: the damper,
          the step guard and the record of writes measure the next decision against it, so an overload above
          an older, lower value is never "no change", and a reading taken before it is not its result.

        Nothing to do while active control is off: enabling it starts from clean state."""
        if self._closed or not self._active_control_allowed():
            return
        damper = self._damper_for(charger_entry_id)
        previous = damper.last_written_a
        now = dt_util.utcnow()
        self._written_at[charger_entry_id] = now.timestamp()
        if reason == WRITE_SESSION_START:
            damper.forget_write(None)
            return
        if reason == WRITE_SOLAR:
            self._reserve_write(self._solar_write_reservations, charger_entry_id, amps)
        if previous == amps:
            return
        damper.record_write(amps)
        self._step_guard_for(charger_entry_id).note_write(
            now=now.timestamp(), previous_a=previous, written_a=amps, site_read_at=None
        )
        writes = self._regulated_writes.setdefault(charger_entry_id, deque(maxlen=_WRITES_KEPT))
        writes.append((now, float(amps), previous))
        _LOGGER.debug(
            "SpotNav site %s active control: charger %s was written %sA outside the regulator (%s), was %sA",
            self.entry_id,
            charger_entry_id,
            amps,
            reason,
            previous,
        )

    def _reserved_by_others_a(self, charger_entry_id: str) -> dict[PhaseName, float]:
        """Per phase, the amps the other chargers' starts on their way have not drawn yet. What a charger
        has drawn is read from its own current only: while that reads nothing the whole share is held (a
        change of the site's margin may be another load's), until `START_RESERVATION_S` pass."""
        return self._undrawn_a(
            (self._start_reservations, self._solar_write_reservations, self._regulator_write_reservations),
            exclude=charger_entry_id,
        )

    def _solar_reserved_a(self) -> dict[PhaseName, float]:
        """Per phase, what the sun's own writes on a direct site gave its chargers and they have not drawn yet:
        room the regulator does not give to its own chargers' raises meanwhile."""
        return self._undrawn_a((self._solar_write_reservations,), exclude=None)

    def _undrawn_a(
        self,
        tables: Sequence[dict[str, tuple[float, tuple[PhaseName, ...], float]]],
        *,
        exclude: str | None,
    ) -> dict[PhaseName, float]:
        """Per phase, what the reservations in `tables` (but `exclude`'s) have not drawn yet, read from each
        charger's own current; expired ones are dropped."""
        now = self._yield_now()
        for table in tables:
            for other, (_amps, _phases, until) in list(table.items()):
                if now >= until:
                    del table[other]
        if not any(tables):
            return {}
        requests = {r.charger_entry_id: r for r in self._build_requests(credit=False)}
        reserved: dict[PhaseName, float] = {}
        for table in tables:
            for other, (amps, phases, _until) in table.items():
                if other == exclude:
                    continue
                delivered = None if other not in requests else requests[other].measured_current_a
                for phase in phases:
                    drawn = 0.0 if delivered is None else (delivered.get(phase).value or 0.0)
                    reserved[phase] = reserved.get(phase, 0.0) + max(0.0, amps - drawn)
        return reserved

    def _reserve_write(
        self,
        table: dict[str, tuple[float, tuple[PhaseName, ...], float]],
        charger_entry_id: str,
        amps: float,
    ) -> None:
        """Hold a write of `amps` to this charger in `table` (direct sites only, see `__init__`)."""
        if self.config.get(CONF_MEASUREMENT_MODE) != MEASUREMENT_MODE_DIRECT:
            return
        request = next((r for r in self._build_requests() if r.charger_entry_id == charger_entry_id), None)
        phases = None if request is None else request.phases_used()
        if not phases or amps <= 0:
            table.pop(charger_entry_id, None)
            return
        table[charger_entry_id] = (float(amps), tuple(phases), self._yield_now() + START_RESERVATION_S)

    def active_control_writes(self, charger_entry_id: str) -> bool:
        """Whether the active-control pass writes this charger's requested current at all: active control on, a
        site that knows the direction on every phase the charger may draw on (a derived site, or a direct one whose
        signed current is confirmed there; the regulator decides nothing elsewhere, `phase_direction_unknown`), its
        own measured current configured, and a charger it can command. An "Automatic" single-phase charger counts: the regulator
        reckons it on its followed or worst phase. Wherever it does not, the sun writes its own current
        (`execution/solar_execution.py`); wherever it does, it stays the only writer. On a direct site the direction
        is the one the charge started with (`_update_charge_writers`), so the writer does not change mid-charge.
        Whoever writes, a charge on a phase whose direction is not known is still lowered on an overload by |I| and
        raised again within the room |I| leaves, never above its request (`regulator.magnitude_decision`): that
        path judges the fuse on the magnitude alone and writes nothing the writer did not ask for.
        """
        if not self._active_control_allowed():
            return False
        if not self._writer_direction_known(charger_entry_id):
            return False
        wiring = (self.config.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id) or {}
        if source_from_dict(wiring.get(CONF_MEASURED_CURRENT_SOURCE)) is None:
            return False
        return self._is_commandable_charger(controller_for(self.hass, charger_entry_id))

    def _writer_direction_known(self, charger_entry_id: str) -> bool:
        """`_direction_known_for`, except on a direct site in a charge, where it is what it was when the charge
        started."""
        held = self._direction_at_charge_start.get(charger_entry_id)
        if held is not None and self._in_charge(charger_entry_id):
            return held
        return self._direction_known_for(charger_entry_id)

    def _in_charge(self, charger_entry_id: str) -> bool:
        """Whether this charger is in a charge: charging, a start of ours on its way, or paused by load balancing
        (the charge goes on once there is room). Ends when the charger stops or the car is unplugged."""
        charger_controller = controller_for(self.hass, charger_entry_id)
        if charger_controller is None:
            return False
        return bool(
            charger_controller.charging or charger_controller.start_pending or charger_controller.paused_by_balancing
        )

    def _update_charge_writers(self) -> None:
        """On a direct site, note for each charger whose charge just started whether its direction is known now,
        and forget it for a charger no longer in a charge. A charge that started with the sun writing its current
        keeps the sun as its writer, and the regulator's full rules off it, even once its phases are confirmed; one
        that started with active control keeps active control, which falls back to the magnitude's rules if a
        confirmation is withdrawn (a lowering on an overload by |I|, a raise within the room |I| leaves) rather
        than handing the charge to the sun."""
        if self.config.get(CONF_MEASUREMENT_MODE) != MEASUREMENT_MODE_DIRECT:
            self._direction_at_charge_start.clear()
            return
        for charger_entry_id in list(self.config.get(CONF_CHARGER_ENTRY_IDS) or []):
            if not self._in_charge(charger_entry_id):
                self._direction_at_charge_start.pop(charger_entry_id, None)
            elif charger_entry_id not in self._direction_at_charge_start:
                self._direction_at_charge_start[charger_entry_id] = self._direction_known_for(charger_entry_id)

    def _direction_known_for(self, charger_entry_id: str) -> bool:
        """Whether the latest result knows the direction on every phase this charger may draw on
        (`regulator.direction_phases`), as the regulator requires before deciding on it."""
        mode = self.config.get(CONF_MEASUREMENT_MODE)
        if mode == MEASUREMENT_MODE_DERIVED:
            return True
        if mode != MEASUREMENT_MODE_DIRECT:
            return False
        request = next(
            (request for request in self._build_requests() if request.charger_entry_id == charger_entry_id), None
        )
        if request is None:
            return False
        sources = self.result.phase_direction_source
        return all(sources.get(phase) for phase in direction_phases(request))

    def _active_control_allowed(self) -> bool:
        """Both gates: the compile-time `ACTIVE_CONTROL_READY` and this site's
        `CONF_ACTIVE_CONTROL_ENABLED` option (off by default). They are independent so neither alone
        can move a charger.
        """
        return bool(ACTIVE_CONTROL_READY) and bool(
            self.config.get(CONF_ACTIVE_CONTROL_ENABLED, False)
        )

    def _schedule_apply_active_control(self) -> None:
        """Queue one apply pass unless one is queued and has not begun (it reads the latest decisions when it
        does). A pass that is waiting for its chargers' operations holds no other pass back: a charger whose
        operation still runs is skipped, and looked at again when that operation returns.
        """
        if self._closed or self._apply_pending:
            return
        self._apply_pending = True
        task = self.hass.async_create_task(
            self._async_apply_active_control(), f"spotnav site {self.entry_id} apply pass"
        )
        self._apply_task = task
        self._apply_passes.add(task)
        task.add_done_callback(self._apply_passes.discard)

    async def _async_apply_active_control(self) -> None:
        """Write this recompute's decisions to the chargers that opted in.

        The only place a regulator decision becomes a charger command; gated by
        `_active_control_allowed`. Per charger, in decision order:

        1. skip a charger that is not commandable (`_is_commandable_charger`);
        2. skip a decision with no proposed current (a refusal is never a silent keep);
        3. hold unless `applyability_failure` passes on freshly re-read measurements;
        4. if yield stepping is enabled, consult the `YieldStepper`:
           "hold" writes nothing, "write" replaces the proposal, "passthrough" keeps it (all pass
           `urgent` on);
        5. let the charger's `RegulatorDamper` decide between write and hold;
        6. round to whole amps (debug-logged when it changes the value) and call the charger's
           `_async_assign_current`.

        An import overload (`is_import_overload`: `MUST_LOWER_REASONS`, or the pause an overload was legalised to) passes every gate that is not about safety: a membership
        conflict, a slow meter, a resume or probe about to start, the step guard's holds, yield stepping's holds
        and steps, the band of a battery at the limit and the damper. Steps 1 and 3 still apply, a running battery
        probe still judges its car, and the lowering is written at once (`_async_apply_must_lower`), unless the
        battery's yield is verified on the site and the site is below the yield ceiling (the stepper then judges
        it as in 4).

        Outcomes are logged once per change (`_log_active_control_outcome`). This method knows
        nothing about why a decision says what it says; that is `site/regulator.py`.
        """
        self._apply_pending = False
        if not self._active_control_allowed() or self._closed:
            return
        decisions = dict(self.regulator_decisions)
        # A charger of doubtful ownership is not regulated, and nothing is regulated on a meter load balancing
        # reads that is fresh only now and then. Neither holds an import overload's lowering: less current is
        # safe whoever owns the charger, and the lowering is still re-checked on the fresh readings below.
        # Held proposals are still logged.
        held_detail: str | None = None
        if self.membership_conflicts:
            held_detail = "membership_conflict"
        elif self.load_balancing_slow_meters:
            held_detail = DETAIL_HELD_METER_TOO_SLOW
        if held_detail is not None:
            for charger_entry_id, decision in decisions.items():
                if decision.proposed_current_a is not None and not _must_lower(decision):
                    self._log_active_control_outcome(
                        charger_entry_id, decision, outcome="held", detail=held_detail
                    )
            decisions = {
                charger_entry_id: decision
                for charger_entry_id, decision in decisions.items()
                if _must_lower(decision)
            }
            if not decisions:
                return
        fresh = self._calculate()
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        margin_by_charger = self._most_restrictive_margin_by_charger(fresh)
        # Each charger's step runs as its own task: one charger's slow service call (a write, a resume's or a
        # probe's start through a cloud that takes its time to answer) never holds another charger's
        # must-lower write, in this pass or the next. Steps start in decision order; one step per charger.
        spawned: list[asyncio.Task[None]] = []
        for charger_entry_id, decision in decisions.items():
            if not self._active_control_allowed():
                break
            running = self._charger_ops.get(charger_entry_id)
            if running is not None and not running.done():
                # Its previous operation still runs: decided again as soon as that returns.
                self._skipped_while_busy.add(charger_entry_id)
                _LOGGER.debug(
                    "SpotNav site %s active control: charger %s skipped this pass, its previous command runs",
                    self.entry_id,
                    charger_entry_id,
                )
                continue
            task = self.hass.async_create_task(
                self._async_charger_step(charger_entry_id, decision, fresh, max_age_s, margin_by_charger),
                f"spotnav site {self.entry_id} step for {charger_entry_id}",
            )
            self._charger_ops[charger_entry_id] = task
            task.add_done_callback(
                lambda done, charger_entry_id=charger_entry_id: self._on_charger_op_done(charger_entry_id, done)
            )
            spawned.append(task)
        if not spawned:
            return
        # Awaited so that whoever awaits this pass sees its steps through; no later pass waits for it.
        try:
            await asyncio.wait(spawned)
        except asyncio.CancelledError:
            # A retired pass takes its steps with it: none is left running unowned.
            for task in spawned:
                task.cancel()
            raise
        await self._async_pause_fallbacks(decisions, fresh, max_age_s, margin_by_charger)

    async def _async_pause_fallbacks(
        self,
        decisions: Mapping[str, RegulatorDecision],
        fresh: SiteCapacityResult,
        max_age_s: float,
        margin_by_charger: dict[str, float | None],
    ) -> None:
        """A switch-only charger's pause that could not be sent in this pass: the chargers this pass held at their
        minimum for it (`RegulatorDecision.pause_if_switched_pause_fails`) on a phase it shares are paused instead,
        in this same pass. Their lowering to the minimum already went out; this only follows a failed command."""
        if not self._failed_switched_pauses:
            return
        failed = self._failed_switched_pauses
        self._failed_switched_pauses = []
        phases: set[PhaseName] = set()
        for _charger_entry_id, freed in failed:
            phases.update(freed)
        steps: list[asyncio.Task[None]] = []
        for charger_entry_id, decision in decisions.items():
            if not decision.pause_if_switched_pause_fails or not phases.intersection(decision.basis):
                continue
            if not self._active_control_allowed():
                break
            running = self._charger_ops.get(charger_entry_id)
            if running is not None and not running.done():
                continue
            pause = replace(
                decision,
                proposed_current_a=0.0,
                reason="paused_safe_current_below_charger_minimum",
                pause_if_switched_pause_fails=False,
            )
            _LOGGER.warning(
                "SpotNav site %s active control: a switch-only charger could not be paused; charger %s is paused instead",
                self.entry_id,
                charger_entry_id,
            )
            task = self.hass.async_create_task(
                self._async_charger_step(charger_entry_id, pause, fresh, max_age_s, margin_by_charger),
                f"spotnav site {self.entry_id} fallback pause for {charger_entry_id}",
            )
            self._charger_ops[charger_entry_id] = task
            task.add_done_callback(
                lambda done, charger_entry_id=charger_entry_id: self._on_charger_op_done(charger_entry_id, done)
            )
            steps.append(task)
        if steps:
            await asyncio.wait(steps)

    async def _async_charger_step(
        self,
        charger_entry_id: str,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        max_age_s: float,
        margin_by_charger: dict[str, float | None],
    ) -> None:
        """One charger's step of a pass, as that charger's own task; its failure is its own."""
        try:
            await self._async_apply_to_charger(charger_entry_id, decision, fresh, max_age_s, margin_by_charger)
        except Exception:  # noqa: BLE001 - one charger's failure must not cost the others their writes
            self._log_charger_pass_failure(charger_entry_id)

    def _on_charger_op_done(self, charger_entry_id: str, task: asyncio.Task[None]) -> None:
        """A charger's operation ended: forget it, and if a pass skipped the charger meanwhile, decide again now
        on the latest decisions (a lower current that came up meanwhile goes out at once)."""
        if self._charger_ops.get(charger_entry_id) is task:
            del self._charger_ops[charger_entry_id]
        if charger_entry_id in self._skipped_while_busy:
            self._skipped_while_busy.discard(charger_entry_id)
            if not self._closed and not task.cancelled():
                self._schedule_apply_active_control()

    def _log_charger_pass_failure(self, charger_entry_id: str) -> None:
        _LOGGER.exception(
            "SpotNav site %s active control: charger %s failed; the pass goes on with the others",
            self.entry_id,
            charger_entry_id,
        )

    def _held_while_phases_settle(
        self, charger_entry_id: str, charger_controller: Any, decision: RegulatorDecision
    ) -> bool:
        """Whether this pass decides nothing on a charger whose phase switch settles (`ChargingController.
        phase_switch_settling`): no raise, no resume, no probe on a charge the charger itself interrupts. A
        must-lower (an import overload) and any lowering still act: a phase switch never holds back the fuse's
        protection."""
        if getattr(charger_controller, "phase_switch_settling", False) is not True:
            return False
        if is_import_overload(decision) or decision.reason == REASON_MAGNITUDE_LOWERING:
            return False
        proposed = decision.proposed_current_a
        previous = self._damper_for(charger_entry_id).last_written_a
        return proposed is None or previous is None or proposed >= previous

    async def _async_apply_to_charger(
        self,
        charger_entry_id: str,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        max_age_s: float,
        margin_by_charger: dict[str, float | None],
    ) -> bool:
        """One charger's step of `_async_apply_active_control`, run as that charger's operation
        (`_async_charger_step`). `False` when active control was turned off meanwhile."""
        # Re-checked per charger: a pass admitted while active control was on must not
        # keep writing after it was turned off (see `async_disable_active_control`).
        if not self._active_control_allowed():
            return False
        charger_controller = controller_for(self.hass, charger_entry_id)
        if not self._is_commandable_charger(charger_controller):
            if self._is_switch_only_charger(charger_controller):
                await self._async_apply_to_switch_only(charger_entry_id, charger_controller, decision, fresh, max_age_s)
                return True
            self._log_active_control_outcome(
                charger_entry_id,
                decision,
                outcome="held",
                detail=(
                    "charger_not_commandable"
                    if charger_controller is not None
                    else "charger_not_loaded"
                ),
            )
            return True
        probe = self._battery_probes.get(charger_entry_id)
        if probe is not None and probe.probing:
            # A battery probe is running (or its stop is not confirmed yet): it alone judges
            # this charger until it is over, on the freshly read measurements. It is a safety judge of its
            # own, an import overload included: bounded by the fuse (`PROBE_MAX_FUSE_FACTOR`), by what the car
            # can explain and by its window, and ended by a hard stop (`_async_battery_probe_pass`).
            await self._async_battery_probe_pass(
                charger_entry_id, charger_controller, decision, fresh, probe
            )
            return True
        lost = self._measurement_lost_due(charger_entry_id, fresh)
        if lost is not None and self._draws_above_minimum(charger_entry_id, charger_controller):
            await self._async_step_to_minimum(charger_entry_id, charger_controller, decision, lost)
            return True
        if self._held_while_phases_settle(charger_entry_id, charger_controller, decision):
            self._log_active_control_outcome(charger_entry_id, decision, outcome="held", detail="phase_switch_settling")
            return True
        proposed = decision.proposed_current_a
        if proposed is None:
            if decision.reason in DIRECTION_UNKNOWN_REASONS and charger_controller.paused_by_balancing:
                # A charge an overload by |I| paused on a phase whose direction is not known: resumed as any balancing
                # pause is, within the room |I| leaves (a start needs no direction), never probed.
                damper = self._damper_for(charger_entry_id)
                await self._async_maybe_resume_paused_charge(
                    charger_entry_id, charger_controller, decision, fresh, damper, damper.last_written_a
                )
                return True
            if decision.reason == REASON_CHARGER_MEASUREMENT_UNUSABLE and charger_controller.paused_by_balancing:
                # A charger load balancing stopped is off by its own control, and an OCPP charger sends no meter
                # values then: its reading grows too old to decide a current on, but a stopped charger draws
                # nothing, and the resume and the probe judge the site's own readings. Without this the charge it
                # holds back is never tried again (the night of 2026-10-09).
                damper = self._damper_for(charger_entry_id)
                if not await self._async_maybe_resume_paused_charge(
                    charger_entry_id, charger_controller, decision, fresh, damper, damper.last_written_a
                ):
                    await self._async_maybe_start_battery_probe(
                        charger_entry_id, charger_controller, decision, fresh, damper, damper.last_written_a
                    )
            return True
        if decision.reason == REASON_MAGNITUDE_LOWERING:
            await self._async_apply_magnitude_lowering(charger_entry_id, charger_controller, decision, fresh, max_age_s)
            return True
        if decision.reason == REASON_MAGNITUDE_RAISE:
            await self._async_apply_magnitude_raise(
                charger_entry_id, charger_controller, decision, fresh, max_age_s, margin_by_charger.get(charger_entry_id)
            )
            return True
        failure = applyability_failure(
            decision=decision,
            signed_active_power_w=fresh.phase_signed_active_power_w,
            signed_active_power_age_s=fresh.phase_signed_active_power_age_s,
            signed_active_power_reason=fresh.phase_signed_active_power_reason,
            max_age_s=max_age_s,
            zero_margin_w=DEFAULT_ZERO_MARGIN_W,
        )
        if failure is not None:
            self._log_active_control_outcome(
                charger_entry_id,
                decision,
                outcome="held",
                detail=failure.kind,
                detail_phase=failure.phase,
            )
            return True
        # Damping decides write or hold; consulted every pass so a pending value's dwell
        # advances.
        damper = self._damper_for(charger_entry_id)
        # Captured before `consider`, which advances this charger's own
        # Captured before `consider` advances the damper; also the yield stepper's `assigned_a`
        # (the value last written).
        previous_setpoint = damper.last_written_a
        min_current_a = float(
            ((self.config.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id) or {}).get(
                "min_current_a", DEFAULT_MIN_CURRENT_A
            )
        )
        previous_setpoint = self._forget_pause_when_charging(
            damper, charger_controller, previous_setpoint, min_current_a, proposed
        )

        # What was written, before an overload takes a reading above it for what is on the charger.
        written_a = previous_setpoint
        if is_import_overload(decision):
            on_charger = self._overload_from_charger(
                charger_entry_id, charger_controller, decision, damper, previous_setpoint, min_current_a
            )
            if on_charger is not None and previous_setpoint is not None:
                # The proposal never goes above what was written on that account.
                if proposed > previous_setpoint:
                    proposed = previous_setpoint
                    decision = replace(decision, proposed_current_a=proposed)
                previous_setpoint = on_charger

        # An import overload (`is_import_overload`, its pause included): from here on no gate that is not about safety holds its lowering.
        # It ends in a write of a lower current (or, refused, the charger's safety stop), unless what is on the
        # charger is already at or below the proposal (`_async_apply_must_lower`).
        overload = is_import_overload(decision)
        lowering_due = overload and (previous_setpoint is None or proposed < previous_setpoint)

        # A grid-charging battery that may give way to the car: start the car at its minimum as
        # a probe (only where nothing else would start it; see `site/battery_probe.py`). Never in place of a
        # lowering that is due; a charger the overload has already stopped may still be resumed or probed.
        if not lowering_due and await self._async_maybe_resume_paused_charge(
            charger_entry_id, charger_controller, decision, fresh, damper, previous_setpoint
        ):
            return True
        if not lowering_due and await self._async_maybe_start_battery_probe(
            charger_entry_id, charger_controller, decision, fresh, damper, previous_setpoint
        ):
            return True

        minimum = self._min_current_a(charger_entry_id)
        if (
            not lowering_due
            and previous_setpoint is not None
            and previous_setpoint < minimum
            and proposed > previous_setpoint
            and charger_controller.paused_by_balancing
        ):
            # A charge load balancing paused is given back by the resume or the probe above, with their margin and
            # dwell, never by a raise of the regulator's: the charger is stopped, and a current written to it would
            # stand for one on it (the damper's), which no resume follows any more. The raise comes from the stopped
            # charger's last reading (an OCPP charger sends none without a transaction), the car's draw from before
            # the pause, which the regulator keeps "unchanged" once the overload is gone.
            self._log_active_control_outcome(charger_entry_id, decision, outcome="held", detail="paused_by_balancing")
            return True
        if (
            previous_setpoint is None
            and proposed > minimum
            and not self._car_draws(charger_entry_id, decision)
            and self._battery_takes_the_fuse()
        ):
            # The first word on a charge that starts with its current not known here (a start, a charger that began
            # by itself): with the battery filling the fuse it is the car's minimum, never the whole request on a
            # basis yield stepping has not had yet. A lowering only, and never of a car already drawing; the stepper
            # raises it on what the battery gives up.
            decision = replace(decision, proposed_current_a=minimum)
            proposed = minimum

        # No step to the very edge, no cut on a charger reading that cannot show the last step yet, one cut per
        # site reading (`site/step_guard.py`). A real overload passes through untouched.
        site_read_at = self._site_read_at(fresh)
        guarded = self._step_guard_for(charger_entry_id).guard(
            now=dt_util.utcnow().timestamp(),
            proposed_a=proposed,
            previous_a=previous_setpoint,
            requested_a=next((basis.requested_current_a for basis in decision.basis.values()), None),
            margin_a=margin_by_charger.get(charger_entry_id),
            charger_read_at=self._charger_read_at(decision),
            site_read_at=site_read_at,
            min_current_a=self._min_current_a(charger_entry_id),
        )
        if (
            guarded.rule is not None
            and guarded.proposed_a != proposed
            # An overload takes the guard's gentler lowering (a trimmed overshoot), never its hold.
            and not (overload and (previous_setpoint is None or guarded.proposed_a >= previous_setpoint))
        ):
            _LOGGER.debug(
                "SpotNav site %s active control: charger %s proposal %sA taken as %sA (%s)",
                self.entry_id,
                charger_entry_id,
                proposed,
                guarded.proposed_a,
                guarded.rule,
            )
            decision = replace(decision, proposed_current_a=guarded.proposed_a)
            proposed = guarded.proposed_a

        # Yield-verified stepping, only when enabled.
        yield_verdict: YieldVerdict | None = None
        if bool(self.config.get(CONF_YIELD_STEPPING_ENABLED, DEFAULT_YIELD_STEPPING_ENABLED)):
            yield_verdict = self._consult_yield_stepper(
                charger_entry_id=charger_entry_id,
                decision=decision,
                fresh=fresh,
                assigned_a=previous_setpoint,
            )
            self._record_yield_stepping_verdict(charger_entry_id, yield_verdict)
            self._log_yield_stepping_transition(charger_entry_id, yield_verdict, fresh)
            yield_verdict = self._yield_verdict_under_target(
                charger_entry_id, decision, yield_verdict, previous_setpoint
            )
            if (
                decision.reason == REASON_HELD_STILL_NO_RAISE
                and yield_verdict is not None
                and yield_verdict.action == "write"
                and yield_verdict.current_a is not None
                and (previous_setpoint is None or yield_verdict.current_a > previous_setpoint)
            ):
                # A still phase is trusted for holding and lowering, never for raising: not the stepper's step or
                # probe either (its readings of the phase come from the same still power).
                yield_verdict = replace(yield_verdict, action="hold", current_a=None, reason=REASON_HELD_STILL_NO_RAISE)

        if overload and not (
            yield_verdict is not None
            and yield_verdict.yield_verified
            and yield_verdict.reason != "passthrough_ceiling"
        ):
            # The stepper is still fed the observation (its bookkeeping follows the site), but only its own lowering
            # is taken from it; a hold, a step up or a passthrough leaves the overload's proposal as it is. The one
            # exception is a battery whose yield is verified on this site, below the yield ceiling (the stepper's
            # hard ceiling, `yield_ceiling_a`): the stepper judges that overload as before.
            stepped = (
                yield_verdict.current_a
                if yield_verdict is not None
                and yield_verdict.action == "write"
                and yield_verdict.current_a is not None
                and previous_setpoint is not None
                and yield_verdict.current_a < previous_setpoint
                else None
            )
            target_a = proposed if stepped is None else stepped
            if self._lowering_held_by_battery(
                charger_entry_id, charger_controller, decision, fresh, previous_setpoint, target_a
            ):
                # Within the band, the battery's own regulation: the car keeps its current (the fuse still holds as
                # the ceiling of every step until the battery is seen to give way). Nothing is lowered, so what is
                # on the charger is what was written: a reading above it that the overload took for the charger's
                # (an OCPP meter value half a minute behind a cut) must not stand for it, or the next raise to the
                # plan's current would be lost in the deadband.
                if written_a is not None and damper.last_written_a != written_a:
                    damper.forget_write(written_a)
                self._log_active_control_outcome(
                    charger_entry_id,
                    decision,
                    outcome="held",
                    detail=DETAIL_HELD_BATTERY_AT_LIMIT,
                    yield_verdict=yield_verdict,
                )
                return True
            return await self._async_apply_must_lower(
                charger_entry_id,
                charger_controller,
                decision,
                damper,
                target_a,
                previous_setpoint,
                margin_by_charger.get(charger_entry_id),
                yield_verdict,
                site_read_at,
            )

        if yield_verdict is not None and yield_verdict.action == "hold":
            # A hold carries no current: skip the damper and write nothing.
            self._log_active_control_outcome(
                charger_entry_id,
                decision,
                outcome="held",
                detail=f"yield_stepping_{yield_verdict.reason}",
                yield_verdict=yield_verdict,
            )
            return True

        if (
            not overload
            and not self._target_lowering(decision, previous_setpoint)
            and yield_verdict is not None
            and yield_verdict.action == "passthrough"
            and (
                (
                    yield_verdict.reason != "passthrough_ceiling"
                    and self._battery_holds_grid_at_limit(
                        decision, fresh, charger_controller, charger_entry_id, previous_setpoint
                    )
                )
                # A grid read at exactly the limit is the unverified ceiling's hit, not an overload: held the same
                # way as an overload within the band.
                or self._lowering_held_by_battery(
                    charger_entry_id, charger_controller, decision, fresh, previous_setpoint, proposed
                )
            )
        ):
            # The battery regulating the grid to the limit, not an overload: the car is not
            # stepped down for it. Anything above the band never gets here.
            self._log_active_control_outcome(
                charger_entry_id,
                decision,
                outcome="held",
                detail=DETAIL_HELD_BATTERY_AT_LIMIT,
                yield_verdict=yield_verdict,
            )
            return True

        # An overload the stepper judges (a verified battery below the yield ceiling) is never held for the dwell:
        # urgency only ever hastens a reduction. The import target's lowering to a current is not held either: an amp's
        # lowering kept back by the deadband would be drawn to the hour's end, past the target. Its pause still waits
        # for the dwell, as every pause does: a pause decided in the hour's last seconds is then never sent.
        target_lowering = (
            self._target_lowering(decision, previous_setpoint) and decision.reason == REASON_IMPORT_TARGET_HELD
        )
        if yield_verdict is not None and yield_verdict.action == "write":
            # The stepper's own current replaces the raw proposal; `urgent` lets a revert skip
            # deadband and dwell.
            damping = damper.consider(
                proposed_current_a=yield_verdict.current_a,
                margin_a=margin_by_charger.get(charger_entry_id),
                urgent=yield_verdict.urgent or overload or target_lowering,
                # A step sized from a battery that was verified to give way is paced by the
                # stepper's own settle time and gap, not by the dwell.
                verified_step=(
                    yield_verdict.battery_credit_a is not None
                    or yield_verdict.reason == "down_step_absorbed_by_battery"
                ),
                # The last step to the request may be smaller than the deadband; it still waits
                # for the dwell.
                reaches_request=yield_verdict.reaches_request
                or self._restores_target_lowering(charger_entry_id, yield_verdict.current_a, previous_setpoint),
            )
        else:
            # Passthrough: the raw proposal, urgent only if the stepper says so. A raise that gives back the target's
            # own lowering this hour is not swallowed by the deadband (it still waits for the dwell), or a 1 A lowering
            # written at once would never come back within the hour.
            damping = damper.consider(
                proposed_current_a=proposed,
                margin_a=margin_by_charger.get(charger_entry_id),
                urgent=(yield_verdict.urgent if yield_verdict is not None else False) or overload or target_lowering,
                reaches_request=self._restores_target_lowering(charger_entry_id, proposed, previous_setpoint),
            )
        if not damping.write or damping.current_a is None:
            self._log_active_control_outcome(
                charger_entry_id,
                decision,
                outcome="held",
                detail=f"damping_{damping.reason}",
                yield_verdict=yield_verdict,
            )
            return True
        setpoint = round(damping.current_a)
        if setpoint != damping.current_a:
            _LOGGER.debug(
                "SpotNav site %s active control: charger %s proposal %sA is "
                "rounded to %sA before writing",
                self.entry_id,
                charger_entry_id,
                damping.current_a,
                setpoint,
            )
        # Last synchronous check before the write; nothing awaits between here and the charger's
        # lock.
        if not self._active_control_allowed() or self._closed:
            return False
        await self._async_write_regulated(
            charger_entry_id,
            charger_controller,
            decision,
            damper,
            damping,
            int(setpoint),
            previous_setpoint,
            yield_verdict,
            site_read_at=site_read_at,
        )
        return True

    async def _async_apply_to_switch_only(
        self,
        charger_entry_id: str,
        charger_controller: ChargingController,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        max_age_s: float,
    ) -> None:
        """One switch-only charger's step (`ChargingController.is_switch_only`): its current cannot be set, so there is
        nothing to lower, to step to its minimum or to probe with. Two actions only:

        * an import overload (`is_import_overload`, `decide_switched_load`'s pause) still shown on the freshly read
          measurements pauses the charge at once, through the regulator's own pause (`_async_write_regulated` with
          0 A: `ChargingController.async_apply_regulated_current`, a balancing pause the core sees as any other);
        * a charge balancing paused (or whose start it held back) is resumed by `_async_maybe_resume_paused_charge`
          once every phase has room for its whole switched current plus the usual margin, for the dwell.

        A lost site measurement does nothing here: the charger cannot be lowered, and pausing it on no evidence of
        an overload would stop charges on every meter hiccup, as for a charger already at its minimum.
        """
        damper = self._damper_for(charger_entry_id)
        if decision.reason == REASON_MAGNITUDE_LOWERING and decision.proposed_current_a is not None:
            # A charge whose direction was lost mid-charge on a direct site, over the limit by |I|: paused while the
            # fresh reading still shows it (as `_async_apply_magnitude_lowering` lowers a charger whose current is set).
            request = next(
                (r for r in self._build_requests(credit=False) if r.charger_entry_id == charger_entry_id), None
            )
            again = (
                None
                if request is None or fresh.state != "observing"
                else magnitude_overload_lowering(site_result=fresh, request=request, max_age_s=max_age_s)
            )
            if again is None or again.proposed_current_a is None:
                self._log_active_control_outcome(charger_entry_id, decision, outcome="held", detail="overload_over")
                return
            await self._async_pause_switch_only(charger_entry_id, charger_controller, decision, damper)
            return
        if is_import_overload(decision) and decision.proposed_current_a is not None:
            failure = applyability_failure(
                decision=decision,
                signed_active_power_w=fresh.phase_signed_active_power_w,
                signed_active_power_age_s=fresh.phase_signed_active_power_age_s,
                signed_active_power_reason=fresh.phase_signed_active_power_reason,
                max_age_s=max_age_s,
                zero_margin_w=DEFAULT_ZERO_MARGIN_W,
            )
            if failure is not None:
                self._log_active_control_outcome(
                    charger_entry_id, decision, outcome="held", detail=failure.kind, detail_phase=failure.phase
                )
                return
            await self._async_pause_switch_only(charger_entry_id, charger_controller, decision, damper)
            return
        if decision.reason == REASON_IMPORT_TARGET_PAUSED and decision.proposed_current_a == 0.0:
            # The import target: its whole switched power no longer fits the hour. Paused on the fresh readings
            # (`_hold_to_import_target` judged that the pause keeps every phase it draws on an import).
            failure = applyability_failure(
                decision=decision,
                signed_active_power_w=fresh.phase_signed_active_power_w,
                signed_active_power_age_s=fresh.phase_signed_active_power_age_s,
                signed_active_power_reason=fresh.phase_signed_active_power_reason,
                max_age_s=max_age_s,
                zero_margin_w=DEFAULT_ZERO_MARGIN_W,
            )
            if failure is not None:
                self._log_active_control_outcome(
                    charger_entry_id, decision, outcome="held", detail=failure.kind, detail_phase=failure.phase
                )
                return
            await self._async_pause_switch_only(charger_entry_id, charger_controller, decision, damper)
            return
        if charger_controller.paused_by_balancing:
            await self._async_maybe_resume_paused_charge(
                charger_entry_id, charger_controller, decision, fresh, damper, damper.last_written_a
            )
            return
        self._log_active_control_outcome(charger_entry_id, decision, outcome="held", detail="switch_only_kept")

    async def _async_pause_switch_only(
        self,
        charger_entry_id: str,
        charger_controller: ChargingController,
        decision: RegulatorDecision,
        damper: RegulatorDamper,
    ) -> None:
        """Pause a switch-only charger in a charge, through the regulator's own pause. A pause of ours still settling
        (the charger has not answered it yet) is not sent again, as for a charger whose current is set; once it has
        had its time, a charger still charging is paused again."""
        if not (charger_controller.charging or charger_controller.start_pending):
            self._log_active_control_outcome(charger_entry_id, decision, outcome="held", detail="not_charging")
            return
        previous = damper.last_written_a
        if previous is not None and previous < DEFAULT_MIN_CURRENT_A:
            if charger_controller.stop_settling:
                self._log_active_control_outcome(
                    charger_entry_id, decision, outcome="held", detail="overload_already_lowered"
                )
                return
            # Paused before, and charging again: what it draws is not what was last written.
            damper.forget_write(None)
            previous = None
        if not self._active_control_allowed() or self._closed:
            return
        try:
            write = await self._async_write_regulated(
                charger_entry_id,
                charger_controller,
                decision,
                damper,
                DampingDecision(True, 0.0, "urgent"),
                0,
                previous,
                None,
            )
        except Exception:
            self._failed_switched_pauses.append((charger_entry_id, frozenset(decision.basis)))
            raise
        if write.outcome != REGULATED_STOPPED:
            # Not paused (the command failed or was refused): the chargers held at their minimum for it are paused.
            self._failed_switched_pauses.append((charger_entry_id, frozenset(decision.basis)))

    async def _async_apply_must_lower(
        self,
        charger_entry_id: str,
        charger_controller: ChargingController,
        decision: RegulatorDecision,
        damper: RegulatorDamper,
        target_a: float,
        previous_setpoint: float | None,
        margin_a: float | None,
        yield_verdict: YieldVerdict | None,
        site_read_at: float | None,
    ) -> bool:
        """The end of an import overload's step (`is_import_overload`; a pause below the minimum stops the charge): `target_a` goes out at once, whole amps
        rounded down, past the damper's deadband, dwell and an unusable margin, as a must-lower write (a charger
        that cannot take it in time is stopped, `ChargingController.async_apply_regulated_current`).

        The one case written nothing: what is on the charger (what was last written, or the charger's own reading
        above it, `charger_above_written`) is already at or below `target_a`. There is nothing to lower; the
        regulator lowers again from the next reading that still shows the overload."""
        setpoint = math.floor(target_a + 1e-9)
        if previous_setpoint is not None and setpoint >= previous_setpoint:
            self._log_active_control_outcome(
                charger_entry_id,
                decision,
                outcome="held",
                detail="overload_already_lowered",
                previous_setpoint=round(previous_setpoint),
                yield_verdict=yield_verdict,
            )
            return True
        damping = damper.consider(proposed_current_a=float(setpoint), margin_a=margin_a, urgent=True)
        if not damping.write or damping.current_a != setpoint:
            # Only the damper's bookkeeping is kept from it: an overload is not held by the damper.
            damper.record_write(float(setpoint))
            damping = DampingDecision(True, float(setpoint), "urgent")
        if not self._active_control_allowed() or self._closed:
            return False
        await self._async_write_regulated(
            charger_entry_id,
            charger_controller,
            decision,
            damper,
            damping,
            setpoint,
            previous_setpoint,
            yield_verdict,
            site_read_at=site_read_at,
        )
        return True

    @staticmethod
    def _forget_pause_when_charging(
        damper: RegulatorDamper,
        charger_controller: ChargingController,
        previous_setpoint: float | None,
        min_current_a: float,
        proposed: float,
    ) -> float | None:
        """What was written, with a remembered pause forgotten when the charger charges again (a start, a resume, a
        person): what it is at is not known here, so the next proposal is a first write, at once (a lowering one
        above all), never the pause again. Not while a stop of ours settles: the charger still reporting the charge
        has not answered that stop yet. Paused again it is sent nothing, but the site is told again and decides again
        at once (a pass, the pause, a recompute, a pass, as fast as the loop runs, until the charger reports off). The
        pause stays remembered; once the stop has had its time, a charger still charging is paused again."""
        if (
            previous_setpoint is not None
            and previous_setpoint < min_current_a
            and (charger_controller.charging or charger_controller.start_pending)
            and not (charger_controller.stop_settling and proposed < DEFAULT_MIN_CURRENT_A)
        ):
            damper.forget_write(None)
            return None
        return previous_setpoint

    def _overload_from_charger(
        self,
        charger_entry_id: str,
        charger_controller: ChargingController,
        decision: RegulatorDecision,
        damper: RegulatorDamper,
        written_a: float | None,
        min_current_a: float,
    ) -> float | None:
        """For an overload (by import, or by |I| where the direction is not known): the charger's fresh reading
        when it is a step above `written_a`, what was last written (`charger_above_written`: a limit the charger
        dropped, a current set in its own app, a write the site was not told of), else `None`. Then the damper
        takes that reading for what is on the charger and the lowering starts from there; the caller keeps its
        proposal at or below `written_a`.

        Never while a stop of ours settles (`written_a` below the minimum): the charger reads its old current until
        it answers, which is the stop on its way, not a current above the one written (the pause would otherwise be
        sent again and again)."""
        if written_a is None or (written_a < min_current_a and charger_controller.stop_settling):
            return None
        measured = [basis.measured_current_a for basis in decision.basis.values() if basis.measured_current_a is not None]
        on_charger = charger_above_written(
            written_a=written_a,
            measured_a=max(measured) if measured else None,
            charger_read_at=self._charger_read_at(decision),
            written_at=self._written_at.get(charger_entry_id),
        )
        if on_charger is None:
            return None
        _LOGGER.info(
            "SpotNav site %s active control: charger %s reads %.1fA, above the %sA written to it; "
            "the overload is lowered from what it draws",
            self.entry_id,
            charger_entry_id,
            on_charger,
            written_a,
        )
        damper.record_write(on_charger)
        return on_charger

    async def _async_apply_magnitude_lowering(
        self,
        charger_entry_id: str,
        charger_controller: ChargingController,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        max_age_s: float,
    ) -> None:
        """Write a `magnitude_overload_lowering` decision: only while the fresh reading still shows the overload
        by |I|, never above it again, and never above what was last written (a lowering only), at once."""
        request = next(
            (r for r in self._build_requests(credit=False) if r.charger_entry_id == charger_entry_id), None
        )
        again = (
            None
            if request is None or fresh.state != "observing"
            else magnitude_overload_lowering(site_result=fresh, request=request, max_age_s=max_age_s)
        )
        if again is None or again.proposed_current_a is None or decision.proposed_current_a is None:
            self._log_active_control_outcome(charger_entry_id, decision, outcome="held", detail="overload_over")
            return
        damper = self._damper_for(charger_entry_id)
        setpoint = min(decision.proposed_current_a, again.proposed_current_a)
        # A pause remembered while the charger charges again (a person restarted it) is a first write, as for an
        # import overload: the charge is lowered, not paused again.
        previous_setpoint = self._forget_pause_when_charging(
            damper, charger_controller, damper.last_written_a, self._min_current_a(charger_entry_id), setpoint
        )
        measured = [b.measured_current_a for b in decision.basis.values() if b.measured_current_a is not None]
        ceiling = previous_setpoint if previous_setpoint is not None else (max(measured) if measured else None)
        # A charger reading above what was written is lowered from what it draws, as an import overload is: the
        # ceiling is its reading, and the setpoint never goes above what was written (a car drawing 16 A on a
        # remembered 8 A, 3 A over, was held `not_a_lowering` at 13 A on every pass, the phase over the fuse).
        on_charger = self._overload_from_charger(
            charger_entry_id,
            charger_controller,
            decision,
            damper,
            previous_setpoint,
            self._min_current_a(charger_entry_id),
        )
        if on_charger is not None and previous_setpoint is not None:
            setpoint = min(setpoint, previous_setpoint)
            ceiling = previous_setpoint = on_charger
        if ceiling is None or setpoint >= ceiling:
            self._log_active_control_outcome(charger_entry_id, decision, outcome="held", detail="not_a_lowering")
            return
        damping = damper.consider(
            proposed_current_a=setpoint,
            margin_a=fresh.measured_margin_a.get(again.limiting_phase) if again.limiting_phase else None,
            urgent=True,
        )
        if not damping.write or damping.current_a is None or damping.current_a > setpoint:
            self._log_active_control_outcome(
                charger_entry_id, decision, outcome="held", detail=f"damping_{damping.reason}"
            )
            return
        if not self._active_control_allowed() or self._closed:
            return
        await self._async_write_regulated(
            charger_entry_id,
            charger_controller,
            replace(decision, proposed_current_a=setpoint),
            damper,
            damping,
            int(setpoint),
            previous_setpoint,
            None,
            site_read_at=self._site_read_at(fresh),
        )

    async def _async_apply_magnitude_raise(
        self,
        charger_entry_id: str,
        charger_controller: ChargingController,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        max_age_s: float,
        margin_a: float | None,
    ) -> None:
        """Write a `magnitude_decision` raise: a charge on a phase whose direction is not known, below its request
        with no phase over, is raised within the room the measured |I| leaves. Judged again on the fresh reading
        (`magnitude_decision` on the site's own margins, less what other chargers' starts and writes on their way have
        not drawn yet), and the step is reckoned from no more than was last written: a charger read seldom (a cloud)
        still reports the current from before a lowering, and a raise reckoned from that reading would give it that
        current straight back. Never above the proposal, the request or the fresh room, never at or below what was
        written (that is no raise), and then as any raise: the step guard stops a step short of the edge and keeps
        off a level that overshot, the damper's deadband and dwell hold it until the room has lasted (a cycling load
        such as an oven lowers at once and raises once it has stayed off), and the charger's own write policy may
        refuse it until its interval has passed."""
        request = next(
            (r for r in self._build_requests(credit=False) if r.charger_entry_id == charger_entry_id), None
        )
        phases = None if request is None else request.phases_used()
        delivered = None if request is None else request.measured_current_a
        if (
            request is None
            or not phases
            or delivered is None
            or fresh.state != "observing"
            or decision.proposed_current_a is None
            or charger_controller.paused_by_balancing
        ):
            self._log_active_control_outcome(charger_entry_id, decision, outcome="held", detail="room_not_judged")
            return
        again = magnitude_decision(site_result=fresh, request=request, max_age_s=max_age_s)
        if again.reason != REASON_MAGNITUDE_RAISE or again.proposed_current_a is None:
            self._log_active_control_outcome(charger_entry_id, decision, outcome="held", detail="room_gone")
            return
        damper = self._damper_for(charger_entry_id)
        # A pause remembered while the charger charges again (a person restarted it) is a first write, as everywhere.
        previous_setpoint = self._forget_pause_when_charging(
            damper, charger_controller, damper.last_written_a, self._min_current_a(charger_entry_id), again.proposed_current_a
        )
        drawn = max((delivered.get(phase).value or 0.0) for phase in phases)
        base = drawn if previous_setpoint is None else min(drawn, previous_setpoint)
        reserved = self._reserved_by_others_a(charger_entry_id)
        rooms = [fresh.measured_margin_a.get(phase) for phase in phases]
        if any(room is None for room in rooms):
            self._log_active_control_outcome(charger_entry_id, decision, outcome="held", detail="room_not_judged")
            return
        room = min(max(0.0, room - reserved.get(phase, 0.0)) for phase, room in zip(phases, rooms))  # type: ignore[operator]
        setpoint = math.floor(min(decision.proposed_current_a, again.proposed_current_a, base + room) + 1e-9)
        if setpoint <= base or (previous_setpoint is not None and setpoint <= previous_setpoint):
            self._log_active_control_outcome(charger_entry_id, decision, outcome="held", detail="not_a_raise")
            return
        guarded = self._step_guard_for(charger_entry_id).guard(
            now=dt_util.utcnow().timestamp(),
            proposed_a=float(setpoint),
            previous_a=previous_setpoint,
            requested_a=request.requested_current_a,
            margin_a=margin_a,
            charger_read_at=self._charger_read_at(decision),
            site_read_at=None,
            min_current_a=self._min_current_a(charger_entry_id),
        )
        if guarded.rule is not None and guarded.proposed_a != setpoint:
            _LOGGER.debug(
                "SpotNav site %s active control: charger %s proposal %sA taken as %sA (%s)",
                self.entry_id,
                charger_entry_id,
                setpoint,
                guarded.proposed_a,
                guarded.rule,
            )
            setpoint = math.floor(guarded.proposed_a + 1e-9)
            if setpoint <= base or (previous_setpoint is not None and setpoint <= previous_setpoint):
                self._log_active_control_outcome(charger_entry_id, decision, outcome="held", detail=guarded.rule)
                return
        decision = replace(decision, proposed_current_a=float(setpoint))
        damping = damper.consider(proposed_current_a=float(setpoint), margin_a=margin_a)
        if not damping.write or damping.current_a is None:
            self._log_active_control_outcome(
                charger_entry_id, decision, outcome="held", detail=f"damping_{damping.reason}"
            )
            return
        if damping.current_a > setpoint:
            # The dwell's pending value was a little above today's room: today's room decides.
            damper.record_write(float(setpoint))
            damping = DampingDecision(True, float(setpoint), damping.reason)
        if not self._active_control_allowed() or self._closed:
            return
        await self._async_write_regulated(
            charger_entry_id,
            charger_controller,
            decision,
            damper,
            damping,
            int(damping.current_a),
            previous_setpoint,
            None,
            site_read_at=None,
        )

    # -- the site's measurement lost

    def _measurement_lost_failure(self, charger_entry_id: str, result: SiteCapacityResult) -> ApplyabilityFailure | None:
        """How the site's measurement is lost on a phase this charger may draw on (`regulator.measurement_lost`), or
        `None`: always `None` for a charger load balancing does not govern (`active_control_writes`) and on a site
        that is not configured to measure at all."""
        if result.state not in UNHEALTHY_STATES or not self.active_control_writes(charger_entry_id):
            return None
        request = next(
            (request for request in self._build_requests(credit=False) if request.charger_entry_id == charger_entry_id),
            None,
        )
        if request is None:
            return None
        return measurement_lost(
            site_result=result,
            phases=direction_phases(request),
            max_age_s=float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S)),
        )

    def _update_measurement_lost(self) -> None:
        """Note, per charger, since when the site's measurement has been lost for it, and forget it once it is
        read again: from then on the regulator decides as before (it may raise again by its own rules)."""
        now = self._yield_now()
        if self.result.state in UNHEALTHY_STATES:
            if self._measurement_unhealthy_since is None:
                self._measurement_unhealthy_since = now
        else:
            self._measurement_unhealthy_since = None
        for charger_entry_id in list(self.config.get(CONF_CHARGER_ENTRY_IDS) or []):
            if self._measurement_lost_failure(charger_entry_id, self.result) is None:
                self._measurement_lost_stepped.discard(charger_entry_id)
                if self._measurement_lost_since.pop(charger_entry_id, None) is not None:
                    _LOGGER.info(
                        "SpotNav site %s load balancing: the measurement is back for charger %s; regulated again",
                        self.entry_id,
                        charger_entry_id,
                    )
            else:
                self._measurement_lost_since.setdefault(charger_entry_id, now)

    def _measurement_lost_due(self, charger_entry_id: str, result: SiteCapacityResult) -> ApplyabilityFailure | None:
        """How the measurement is lost for this charger on `result`, once it has been for `MEASUREMENT_LOST_GRACE_S`;
        else `None`."""
        since = self._measurement_lost_since.get(charger_entry_id)
        if since is None or self._yield_now() - since < MEASUREMENT_LOST_GRACE_S:
            return None
        return self._measurement_lost_failure(charger_entry_id, result)

    def _minimum_setpoint(self, charger_entry_id: str) -> int:
        """The charger's minimum current in whole amps, never below the floor below which a write is a pause."""
        return max(math.ceil(self._min_current_a(charger_entry_id) - 1e-9), int(DEFAULT_MIN_CURRENT_A))

    def _draws_above_minimum(self, charger_entry_id: str, charger_controller: Any) -> bool:
        """Whether this charger is in a charge at a current above its minimum: what the site last gave it, or where
        the site gave it nothing it knows of (a charge that just started), its own fresh reading, or, with none,
        taken as above (a charge at an unknown current). Never a charger that is off or paused: nothing is
        started."""
        if not (charger_controller.charging or charger_controller.start_pending):
            return False
        if charger_controller.paused_by_balancing or charger_controller.stop_settling:
            return False
        minimum = self._minimum_setpoint(charger_entry_id)
        written = self._damper_for(charger_entry_id).last_written_a
        if written is not None:
            return written > minimum
        wiring: dict[str, Any] = (self.config.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id) or {}
        delivered = self._read_charger_measured_current(wiring)
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        readings = (
            []
            if delivered is None
            else [
                value.value
                for value in (delivered.get(phase) for phase in PHASES)
                if value.value is not None
                and value.problem is None
                and value.age_s is not None
                and value.age_s <= max_age_s
            ]
        )
        if not readings:
            return True
        return max(readings) > minimum + MEASUREMENT_LOST_ABOVE_MINIMUM_A

    async def _async_step_to_minimum(
        self,
        charger_entry_id: str,
        charger_controller: ChargingController,
        decision: RegulatorDecision,
        lost: ApplyabilityFailure,
    ) -> None:
        """Step a charger to its minimum current while the site's measurement is lost (`MEASUREMENT_LOST_GRACE_S`).

        A lowering needs no headroom proof, so `applyability_failure` (which refuses every write on a lost
        measurement) is not asked; the write goes the way of the regulator's other lowerings
        (`_async_write_regulated`: the charger's write path and its rate limit, the damper, the step guard and the
        record of writes), at once, but never as a must-lower: nothing shows an overload, so a charger that will not
        take it is not stopped, and the next pass tries again."""
        setpoint = self._minimum_setpoint(charger_entry_id)
        damper = self._damper_for(charger_entry_id)
        previous_setpoint = damper.last_written_a
        stepped = RegulatorDecision(float(setpoint), MEASUREMENT_LOST_REASON, lost.phase, decision.basis)
        damper.record_write(float(setpoint))
        if not self._active_control_allowed() or self._closed:
            damper.forget_write(previous_setpoint)
            return
        if charger_entry_id not in self._measurement_lost_stepped:
            # Said and counted once per lost spell; a write the charger refused is tried again quietly.
            self._measurement_lost_stepped.add(charger_entry_id)
            self._measurement_lost_steps += 1
            self._measurement_lost_last_step_at = dt_util.utcnow()
            _LOGGER.warning(
                "SpotNav site %s load balancing: the site's measurement is lost (%s on %s) for %.0f s or more; "
                "charger %s is held at its minimum of %sA until it is back",
                self.entry_id,
                lost.kind,
                lost.phase,
                MEASUREMENT_LOST_GRACE_S,
                charger_entry_id,
                setpoint,
            )
        await self._async_write_regulated(
            charger_entry_id,
            charger_controller,
            stepped,
            damper,
            DampingDecision(True, float(setpoint), "measurement_lost"),
            setpoint,
            previous_setpoint,
            None,
        )

    def measurement_lost_snapshot(self) -> dict[str, Any]:
        """For the diagnostics: how often a charger was stepped to its minimum on a lost measurement and when last,
        and per charger the seconds its measurement has been lost (only those it is lost for)."""
        now = self._yield_now()
        return {
            "grace_s": MEASUREMENT_LOST_GRACE_S,
            "steps_to_minimum": self._measurement_lost_steps,
            "last_step_at": None
            if self._measurement_lost_last_step_at is None
            else self._measurement_lost_last_step_at.isoformat(),
            "lost_for_s": {
                charger_entry_id: round(now - since, 1) for charger_entry_id, since in self._measurement_lost_since.items()
            },
        }

    async def _async_write_regulated(
        self,
        charger_entry_id: str,
        charger_controller: ChargingController,
        decision: RegulatorDecision,
        damper: RegulatorDamper,
        damping: DampingDecision,
        setpoint: int,
        previous_setpoint: float | None,
        yield_verdict: YieldVerdict | None,
        *,
        site_read_at: float | None = None,
    ) -> RegulatedWrite:
        """The write of one charger's regulated current and what it means for its damper and the log; returns what
        the charger's write path said."""
        # The charger's own write path owns the OCPP read-modify-write and stays best-effort; any
        # other adapter may refuse by policy, and then the fuse decides (stop or hold).
        write = await charger_controller.async_apply_regulated_current(
            setpoint,
            # The import target's urgent lowering is no fuse matter: a write the charger refuses is retried, never a stop.
            must_lower=(
                is_import_overload(decision)
                or damping.reason == "protection"
                or (damping.reason == "urgent" and not self._target_lowering(decision, previous_setpoint))
            ),
        )
        if not write.written:
            if write.outcome == REGULATED_STOPPED and decision.reason == REASON_IMPORT_TARGET_PAUSED:
                # The import target's pause went out: it holds to the hour's end from here, whatever the charger's
                # status says a moment later (`_latch_target_pauses` for one taken without this write's word).
                self._latch_target_pause(charger_entry_id)
            if write.outcome == REGULATED_STOPPED:
                # Paused (or stopped for safety): the charger is at nothing now. Remembered so the next pass
                # does not pause the stopped charger again, and the resume (`_async_maybe_resume_paused_charge`)
                # is not held back by the current it had before the pause. When: a reading the charger sent before
                # the pause shows the current before it, not a charger above the nothing written
                # (`charger_above_written`), which paused the stopped charger again on every pass until that reading
                # was too old to use (the night of 2026-11-01). Only when a stop went out, or at the first pause on
                # record: a pass that finds the stop still on its way sends nothing, and moving the time would keep
                # sliding the window in which a reading counts as the charge before the pause
                # (`_stopped_charger_reading`), so a charger that ignores the stop would never be seen drawing.
                if write.stop_sent or damper.last_written_a != 0.0:
                    self._written_at[charger_entry_id] = dt_util.utcnow().timestamp()
                damper.record_write(0.0)
            elif write.outcome == REGULATED_FLOORED:
                # The stop did not pause the charge: the charger holds it at the 6 A floor (a Peblar out of
                # pauses), and that is what is counted until a later pass pauses it.
                damper.record_write(float(DEFAULT_MIN_CURRENT_A))
            elif damper.last_written_a == damping.current_a:
                # Nothing went out: the damper must not believe it did, or the next deadband and the
                # restore would be measured against a value the charger never had. (Unless another path's
                # write was told meanwhile: that one is on the charger.)
                damper.forget_write(previous_setpoint)
            self._log_active_control_outcome(
                charger_entry_id,
                decision,
                outcome="held",
                detail=f"adapter_{write.code}" if write.outcome == "held" else write.code,
                yield_verdict=yield_verdict,
            )
            return write
        if damper.last_written_a != damping.current_a:
            # Another path's write was told to the site while this one was on its way (`note_charger_write`):
            # this write landed last, so it is what the charger carries.
            damper.record_write(float(setpoint))
        if previous_setpoint is None or setpoint > previous_setpoint:
            # A raise: the sun's own writes to other chargers are not given this room meanwhile.
            self._reserve_write(self._regulator_write_reservations, charger_entry_id, float(setpoint))
        writes = self._regulated_writes.setdefault(charger_entry_id, deque(maxlen=_WRITES_KEPT))
        written_at = dt_util.utcnow()
        self._written_at[charger_entry_id] = written_at.timestamp()
        writes.append((written_at, float(setpoint), previous_setpoint))
        if previous_setpoint is not None and setpoint > previous_setpoint:
            self._begin_raise_credit(charger_entry_id, written_at.timestamp(), float(setpoint))
        self._step_guard_for(charger_entry_id).note_write(
            now=written_at.timestamp(),
            previous_a=previous_setpoint,
            written_a=float(setpoint),
            site_read_at=site_read_at,
        )
        self._log_active_control_outcome(
            charger_entry_id,
            decision,
            outcome="wrote",
            detail=damping.reason,
            setpoint=setpoint,
            previous_setpoint=(
                None if previous_setpoint is None else round(previous_setpoint)
            ),
            yield_verdict=yield_verdict,
        )
        return write

    # -- a home battery charging from the grid as the car starts

    def _fresh_battery_w(self) -> float | None:
        """The home battery's power (W, positive while it charges) on a fresh, usable reading, else `None`."""
        battery = self.battery_aggregate_power()
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        if (
            battery is None
            or battery.value is None
            or battery.problem is not None
            or battery.age_s is None
            or battery.age_s > max_age_s
        ):
            return None
        return float(battery.value)

    def _sample_battery(self) -> None:
        """Keep the battery's power of the last `BATTERY_RISE_WINDOW_S`, for `_battery_takes_the_fuse`."""
        now = self._yield_now()
        samples = self._battery_samples
        while samples and now - samples[0][0] > BATTERY_RISE_WINDOW_S:
            samples.popleft()
        watts = self._fresh_battery_w()
        if watts is not None:
            samples.append((now, watts))

    def _battery_takes_the_fuse(self) -> bool:
        """Whether the home battery charges from the grid, or its power is rising toward a charge, so a car that
        starts now meets a fuse the battery is filling: `car_first` with yield stepping on (the battery is one that
        gives way to the car, `_battery_rules_apply`), and on a fresh reading the battery charges at least
        `LIMIT_BATTERY_CHARGING_W`, or rose by that much within `BATTERY_RISE_WINDOW_S` and now takes power. A start
        then goes in at the car's minimum (`start_allowance_a`), and so does the regulator's first word on a charge
        it knows no current of (`_async_apply_to_charger`); yield stepping raises it on what the battery gives up.
        An unread battery, or none configured, changes nothing."""
        if not self._battery_rules_apply():
            return False
        watts = self._fresh_battery_w()
        if watts is None:
            return False
        if watts >= LIMIT_BATTERY_CHARGING_W:
            return True
        lowest = min((sample for _, sample in self._battery_samples), default=watts)
        return watts > 0.0 and watts - lowest >= LIMIT_BATTERY_CHARGING_W

    def _car_draws(self, charger_entry_id: str, decision: RegulatorDecision) -> bool:
        """Whether the charger's own reading shows the car drawing on a phase of `decision`'s."""
        wiring: dict[str, Any] = (self.config.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id) or {}
        delivered = self._read_charger_measured_current(wiring)
        return delivered is not None and any(
            (delivered.get(phase).value or 0.0) >= PROBE_IDLE_BELOW_A for phase in decision.basis
        )

    def _min_current_a(self, charger_entry_id: str) -> float:
        wiring: dict[str, Any] = (self.config.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id) or {}
        return float(wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A))

    # -- a grid-charging battery that holds the grid at the fuse (`site/battery_probe.py`)

    def _battery_rules_apply(self) -> bool:
        """The band and the probe only exist under `car_first` with yield stepping on: with
        `battery_first` the battery has the sun and the car yields to it, as ever."""
        return bool(
            self.config.get(CONF_YIELD_STEPPING_ENABLED, DEFAULT_YIELD_STEPPING_ENABLED)
        ) and (
            self.config.get(CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY) == SOLAR_PRIORITY_CAR_FIRST
        )

    def _battery_covers_car(
        self,
        fresh: SiteCapacityResult,
        phases: tuple[PhaseName, ...],
        min_current_a: float,
    ) -> bool:
        """Whether the battery is charging, on a fresh reading, at least as hard as the car draws at
        its minimum current over `phases`."""
        battery = self.battery_aggregate_power()
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        if (
            battery is None
            or battery.value is None
            or battery.problem is not None
            or battery.age_s is None
            or battery.age_s > max_age_s
        ):
            return False
        needed_w = car_minimum_power_w(
            min_current_a, [fresh.phase_voltage_v.get(phase) for phase in phases]
        )
        return needed_w is not None and battery.value >= needed_w

    def _battery_holds_grid_at_limit(
        self,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        charger_controller: Any,
        charger_entry_id: str,
        previous_setpoint: float | None,
    ) -> bool:
        """Whether this reduction is only the battery holding the grid at the limit: `car_first`
        with yield stepping, the decision lowers the car below what it asked for and below what it
        has, the battery charges at least as hard as the car's minimum draws and every phase reads
        within the band above the limit. Anything else (a phase above the band, a measurement
        missing, a request the person lowered) is `False`, and the reduction goes ahead as ever.
        """
        proposed = decision.proposed_current_a
        requested = next((basis.requested_current_a for basis in decision.basis.values()), None)
        if (
            not self._battery_rules_apply()
            or proposed is None
            or requested is None
            or previous_setpoint is None
            or proposed >= previous_setpoint - 1e-9
            or proposed >= requested - 1e-9
            or fresh.state != "observing"
        ):
            return False
        wiring: dict[str, Any] = (self.config.get(CONF_PHASE_WIRING) or {}).get(
            charger_entry_id
        ) or {}
        min_current_a = float(wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A))
        phases = tuple(decision.basis.keys())
        if not phases or not within_held_band(fresh.measured_margin_a, phases):
            return False
        return self._battery_covers_car(fresh, phases, min_current_a)

    def _lowering_held_by_battery(
        self,
        charger_entry_id: str,
        charger_controller: Any,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        previous_setpoint: float | None,
        target_a: float,
    ) -> bool:
        """Whether a lowering (an import overload's included) is only a grid-charging battery holding the grid at the
        limit, so the running car keeps its current: the band (`_battery_holds_grid_at_limit`) for an overload and for
        a grid read at the unverified ceiling. `car_first` with yield stepping,
        the car charging at its minimum or above, every phase within `HELD_BAND_A` above the limit, and the battery
        charging, on a fresh reading, at least as hard as the cut would free (all of the car's minimum for a pause):
        it can take back what the car would give up, so lowering the car only fills the battery (the night of
        2026-11-01: 16 A to a pause in three and a half minutes, the battery growing to 12.5 kW). Nothing is verified
        by it: the ceiling of every step stays the fuse until a step or a probe of a paused charge sees the battery
        give way, and anything above the band is lowered at once, as ever."""
        if not self._battery_rules_apply():
            return False
        min_current_a = self._min_current_a(charger_entry_id)
        phases = tuple(decision.basis.keys())
        requested = next((basis.requested_current_a for basis in decision.basis.values()), None)
        if (
            not phases
            or previous_setpoint is None
            or previous_setpoint < min_current_a - 1e-9
            or target_a >= previous_setpoint
            # A request lowered below what the car has (a person's, the plan's) is followed, never held.
            or requested is None
            or requested < previous_setpoint - 1e-9
            or fresh.state != "observing"
            or charger_controller.charging is not True
            or not within_held_band(fresh.measured_margin_a, phases)
        ):
            return False
        # What the battery must be able to give up, on a fresh reading: the cut, all of the car's minimum for a pause.
        cut_a = previous_setpoint - (target_a if target_a >= min_current_a else 0.0)
        battery_w = self._fresh_battery_w()
        needed_w = car_minimum_power_w(cut_a, [fresh.phase_voltage_v.get(phase) for phase in phases])
        return battery_w is not None and needed_w is not None and battery_w >= needed_w

    def _probe_for(self, charger_entry_id: str) -> BatteryProbe:
        probe = self._battery_probes.get(charger_entry_id)
        if probe is None:
            probe = BatteryProbe(
                window_s=REGULATOR_DWELL_S
            )
            self._battery_probes[charger_entry_id] = probe
        return probe

    def _cancel_probe_timer(self) -> None:
        """Drop the probe timer and every check due (a reset or a shutdown)."""
        self._probe_checks_due.clear()
        self._disarm_probe_timer()

    def _disarm_probe_timer(self) -> None:
        if self._probe_timer_cancel is not None:
            self._probe_timer_cancel()
            self._probe_timer_cancel = None
        self._probe_timer_due = None

    def _schedule_probe_check(self, delay_s: float) -> None:
        """Re-evaluate the running probes `delay_s` from now, whatever else triggers a pass.

        One timer serves every charger's probe, armed for the earliest check due: a check asked for
        later never moves an earlier one (another charger's window end) out. A check left over from a
        probe that has ended only runs one more pass."""
        now = self._yield_now()
        self._probe_checks_due.add(round(now + delay_s, 1))
        self._arm_probe_timer(now)

    def _arm_probe_timer(self, now: float) -> None:
        # A check is over once its timer (one second after it) has had its time.
        self._probe_checks_due = {due for due in self._probe_checks_due if due + 1.0 >= now}
        if not self._probe_checks_due:
            return
        due = min(self._probe_checks_due)
        if (
            self._probe_timer_cancel is not None
            and self._probe_timer_due is not None
            and now <= self._probe_timer_due + 1.0
            and self._probe_timer_due <= due
        ):
            return  # an earlier check is armed and still to come
        self._disarm_probe_timer()

        @callback
        def fire(_now: Any) -> None:
            self._probe_timer_cancel = None
            self._probe_timer_due = None
            if self._closed:
                return
            fired_at = self._yield_now()
            self._probe_checks_due = {due for due in self._probe_checks_due if due > fired_at}
            self._arm_probe_timer(fired_at)
            self._recompute()

        self._probe_timer_due = due
        self._probe_timer_cancel = async_call_later(self.hass, max(0.0, due - now) + 1.0, fire)

    def _charger_reports_charging(self, charger_controller: Any) -> bool | None:
        """Whether the charger's own status says it charges, or `None` when no status says anything (none
        configured, unavailable, unknown or unmapped). Never inferred from the start we sent: a charger read
        through its switch alone has no status to say it."""
        connection, source = charger_controller.connection()
        if source is None or connection == CONNECTION_UNKNOWN:
            return None
        return connection == CONNECTION_CHARGING

    @staticmethod
    def _connector_status(charger_controller: Any) -> str | None:
        """The charger's connector status in the progress check's vocabulary (`SuspendedEV`, `Charging`, the raw
        text), or `None` when none can be read."""
        try:
            return charger_controller.charge_progress_facts().connector_status
        except Exception:  # noqa: BLE001 - an unreadable status says nothing
            return None

    def _record_probe_event(
        self,
        charger_entry_id: str,
        decision: RegulatorDecision,
        *,
        outcome: str,
        detail: str,
        setpoint: int | None,
        previous_setpoint: float | None,
    ) -> None:
        """One decision-log entry and one INFO line for a probe's start or end."""
        self._record_decision(
            charger_entry_id,
            decision,
            outcome=outcome,
            detail=detail,
            detail_phase=None,
            setpoint=setpoint,
            previous_setpoint=None if previous_setpoint is None else round(previous_setpoint),
            yield_verdict=None,
        )
        _LOGGER.info(
            "SpotNav site %s battery probe: charger %s %s (%s), set to %sA",
            self.entry_id,
            charger_entry_id,
            outcome,
            detail,
            setpoint,
        )

    def _charge_still_wanted(self, charger_entry_id: str, charger_controller: Any) -> bool:
        """Whether a charge balancing paused is still wanted: Auto is not paused, or the charge is a
        person's (their Start, under any pause but their own Stop), or a person's Start paused Auto (a stop,
        window end, solar off or new plan outside every window has already cleared the controller's own
        mark). When it is not wanted the mark is dropped."""
        return charge_still_wanted(self.hass, charger_entry_id, charger_controller)

    async def _async_maybe_resume_paused_charge(
        self,
        charger_entry_id: str,
        charger_controller: Any,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        damper: RegulatorDamper,
        previous_setpoint: float | None,
    ) -> bool:
        """Restart a charge load balancing paused once the site has headroom for the minimum again.

        Only for a charge the regulator's own pause stopped and nothing has touched since, with a
        vehicle plugged in, the request on record at least the minimum, the car not drawing, every
        phase measured, and a margin of one amp above the minimum (so it does not flap) that has
        lasted for the dwell. It restarts at `min(request, allowance)`. `True` when it started.
        Never a plan's charge the car ended by itself (`car_ended_holds_charge`): no window starts that car
        again either, until its level is read or the wait ends otherwise.
        """
        wiring: dict[str, Any] = (self.config.get(CONF_PHASE_WIRING) or {}).get(
            charger_entry_id
        ) or {}
        min_current_a = float(wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A))
        phases = tuple(decision.basis.keys())
        if not phases and decision.reason in DIRECTION_UNKNOWN_REASONS:
            # Nothing was decided on a phase whose direction is not known: the phases the charger draws on.
            request = next(
                (r for r in self._build_requests(credit=False) if r.charger_entry_id == charger_entry_id), None
            )
            phases = tuple((None if request is None else request.phases_used()) or ())
        requested: float | None = charger_controller.requested_current_a
        switch_only = self._is_switch_only_charger(charger_controller)
        if switch_only:
            # Resumed whole or not at all: its switched current is both its minimum and what it asks for.
            min_current_a = float(math.ceil(charger_controller.switched_current().amps - 1e-9))
            requested = min_current_a
        now = self._yield_now()
        if (
            not phases
            or requested is None
            or requested < min_current_a
            or fresh.state != "observing"
            or not charger_controller.paused_by_balancing
            or not self._charge_still_wanted(charger_entry_id, charger_controller)
            or charger_controller.held_by_charger
            or not _plugged_in_for_resume(charger_controller.adapter)
            or (previous_setpoint is not None and previous_setpoint >= min_current_a)
            or charger_controller.car_ended_holds_charge
        ):
            self._resume_ready_since.pop(charger_entry_id, None)
            return False
        target_cap = self._target_start_cap(charger_entry_id)
        if target_cap is not None and target_cap < min_current_a:
            # The import target holds it back until the next clock hour, or the hour's budget has no room for even
            # its minimum.
            self._resume_ready_since.pop(charger_entry_id, None)
            return False
        delivered = self._stopped_charger_reading(charger_entry_id, wiring)
        if delivered is None and switch_only:
            phase_count, chosen = self.reckoned_phases(charger_entry_id)
            delivered = self._switched_measurement(charger_controller, phase_count, chosen, min_current_a)
        if delivered is None or any(
            delivered.get(phase).value is None or delivered.get(phase).value >= PROBE_IDLE_BELOW_A
            for phase in phases
        ):
            return False
        margins = [fresh.measured_margin_a.get(phase) for phase in phases]
        if any(margin is None for margin in margins) or min(margins) < min_current_a + 1.0:
            self._resume_ready_since.pop(charger_entry_id, None)
            return False
        since = self._resume_ready_since.setdefault(charger_entry_id, now)
        dwell_s = REGULATOR_DWELL_S
        if now - since < dwell_s:
            return False
        if self._resume_backed_off(charger_entry_id, decision, now, previous_setpoint):
            return False
        amps = int(min(float(requested), min(margins), math.inf if target_cap is None else target_cap))
        started = await charger_controller.async_battery_probe_start(amps, capped=True)
        if not started:
            return False
        self._resume_ready_since.pop(charger_entry_id, None)
        self._resume_times.setdefault(charger_entry_id, deque()).append(now)
        damper.record_write(float(amps))
        self._written_at[charger_entry_id] = dt_util.utcnow().timestamp()
        self._record_probe_event(
            charger_entry_id,
            decision,
            outcome="resumed",
            detail="headroom_returned",
            setpoint=amps,
            previous_setpoint=previous_setpoint,
        )
        return True

    def _resumes_in_window(self, charger_entry_id: str, now: float) -> int:
        times = self._resume_times.get(charger_entry_id)
        if not times:
            return 0
        while times and now - times[0] >= RESUME_WINDOW_S:
            times.popleft()
        return len(times)

    def _resume_backed_off(
        self, charger_entry_id: str, decision: RegulatorDecision, now: float, previous_setpoint: float | None
    ) -> bool:
        """Whether a resume due now is held back: `RESUME_LIMIT` resumes went out within `RESUME_WINDOW_S`, so none
        goes out for `RESUME_BACKOFF_S` (said once in the decision log, and in `balancing_resume_snapshot`)."""
        until = self._resume_backoff_until.get(charger_entry_id)
        if until is not None:
            if now < until:
                return True
            del self._resume_backoff_until[charger_entry_id]
            self._resume_times.pop(charger_entry_id, None)
            return False
        if self._resumes_in_window(charger_entry_id, now) < RESUME_LIMIT:
            return False
        self._resume_backoff_until[charger_entry_id] = now + RESUME_BACKOFF_S
        self._record_probe_event(
            charger_entry_id,
            decision,
            outcome="held",
            detail="resume_backed_off",
            setpoint=None,
            previous_setpoint=previous_setpoint,
        )
        return True

    def balancing_resume_snapshot(self) -> dict[str, dict[str, Any]]:
        """Per charger the resumes of a balancing pause within `RESUME_WINDOW_S` and the back-off left (seconds, or
        `None`), for the site sensor and the diagnostics."""
        now = self._yield_now()
        snapshot: dict[str, dict[str, Any]] = {}
        for charger_entry_id in list(self.config.get(CONF_CHARGER_ENTRY_IDS) or []):
            until = self._resume_backoff_until.get(charger_entry_id)
            snapshot[charger_entry_id] = {
                "resumes_in_window": self._resumes_in_window(charger_entry_id, now),
                "backoff_remaining_s": None if until is None or until <= now else round(until - now, 1),
            }
        return snapshot

    def balancing_pause(self, charger_entry_id: str) -> BalancingPauseState | None:
        """The charge load balancing holds back at this charger, as the status says it, or `None` when it holds
        none back. `retry_at` is when it is tried again: the battery probe's or the resume's back-off end, the
        probe's first; `None` when nothing holds it back but room (the probe tries at once on a battery that fills
        the fuse, the resume after its dwell). `cause` is the battery filling the fuse (a fresh reading charging at
        least `LIMIT_BATTERY_CHARGING_W`), else the house's own consumption; the import target where it paused the
        charge this hour (`retry_at` is then the next hour's start, when it is tried again)."""
        charger_controller = controller_for(self.hass, charger_entry_id)
        if charger_controller is None or not charger_controller.paused_by_balancing:
            return None
        budget = self._target_budget
        if budget is not None and self._target_paused_hour.get(charger_entry_id) == budget.hour_start:
            # The import target paused it: it is tried again when the next clock hour begins.
            return BalancingPauseState(retry_at=budget.hour_end, cause=LIMIT_CAUSE_IMPORT_TARGET)
        now = self._yield_now()
        remaining: float | None = None
        probe = self._battery_probes.get(charger_entry_id)
        if probe is not None:
            remaining = probe.snapshot(now)["backoff_remaining_s"]
        if remaining is None:
            until = self._resume_backoff_until.get(charger_entry_id)
            remaining = None if until is None or until <= now else until - now
        watts = self._fresh_battery_w()
        return BalancingPauseState(
            retry_at=None if remaining is None else dt_util.utcnow() + timedelta(seconds=remaining),
            cause=(
                LIMIT_CAUSE_BATTERY_SHARES_FUSE
                if watts is not None and watts >= LIMIT_BATTERY_CHARGING_W
                else LIMIT_CAUSE_HOUSE_CONSUMPTION
            ),
        )

    async def _async_maybe_start_battery_probe(
        self,
        charger_entry_id: str,
        charger_controller: Any,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        damper: RegulatorDamper,
        previous_setpoint: float | None,
    ) -> bool:
        """Resume a charge that load balancing paused, at the car's minimum current, as a probe;
        `True` when a probe was started (the pass is then this probe's).

        Every condition must hold, each a refusal when unknown: `car_first` with yield stepping on;
        the charger is stopped because balancing paused it and nothing has touched it since (so a
        person's Stop, a window's end or a target stop is never undone) and the charger's own
        scheduler does not hold it; a vehicle is plugged in; the plan's charge is not one the car ended by
        itself (`car_ended_holds_charge`, a wait for its level among them); the request on record is at least the
        minimum; the car is not drawing; the grid has no headroom for the minimum but no phase is
        over the band (never into a real overload: a decision to lower for an overload within the band is the
        battery's own regulation, which a stopped car cannot lower anyway); the battery charges at least as
        hard as the car's minimum would draw; the car's minimum would not take any phase past
        `PROBE_MAX_FUSE_FACTOR` times the fuse; and no back-off is running. A start the charger
        refuses counts as a failure.
        """
        if not self._battery_rules_apply():
            return False
        wiring: dict[str, Any] = (self.config.get(CONF_PHASE_WIRING) or {}).get(
            charger_entry_id
        ) or {}
        min_current_a = float(wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A))
        phases = tuple(decision.basis.keys())
        main_fuse_a = self.config.get(CONF_MAIN_FUSE_A)
        requested = charger_controller.requested_current_a
        if (
            not phases
            or main_fuse_a is None
            or requested is None
            or requested < min_current_a
            or (previous_setpoint is not None and previous_setpoint >= min_current_a)
            or fresh.state != "observing"
            or not charger_controller.paused_by_balancing
            or not self._charge_still_wanted(charger_entry_id, charger_controller)
            or charger_controller.held_by_charger
            or charger_controller.adapter.vehicle_connected() is not True
            or charger_controller.car_ended_holds_charge
            or _held_still_on(fresh, phases)
        ):
            return False
        target_cap = self._target_start_cap(charger_entry_id)
        if target_cap is not None and target_cap < min_current_a:
            return False  # the import target holds it back: a probe at the minimum would break the hour
        delivered = self._stopped_charger_reading(charger_entry_id, wiring)
        if delivered is None:
            return False
        delivered_a = [delivered.get(phase).value for phase in phases]
        if any(value is None or value >= PROBE_IDLE_BELOW_A for value in delivered_a):
            return False
        margins = fresh.measured_margin_a
        if not within_held_band(margins, phases):
            return False
        if min(margins[phase] for phase in phases if margins[phase] is not None) >= min_current_a:
            return False  # there is headroom for the minimum: the ordinary path restarts the car
        if not self._battery_covers_car(fresh, phases, min_current_a):
            return False
        probe_a = float(math.ceil(min_current_a))
        currents = fresh.measured_phase_current_a
        if any(currents.get(phase) is None for phase in PHASES) or any(
            currents[phase] + probe_a > PROBE_MAX_FUSE_FACTOR * float(main_fuse_a)
            for phase in PHASES
        ):
            return False
        now = self._yield_now()
        probe = self._probe_for(charger_entry_id)
        if not probe.may_start(now):
            return False
        # The status before the start goes out: one that turns to charging while the start is on its way
        # is the car starting, not a status left over from before.
        charging_before = self._charger_reports_charging(charger_controller)
        started = await charger_controller.async_battery_probe_start(int(probe_a))
        if not started:
            probe.refused(now, "start_refused")
            self._record_probe_event(
                charger_entry_id,
                decision,
                outcome="probe_failed",
                detail="start_refused",
                setpoint=None,
                previous_setpoint=previous_setpoint,
            )
            return True
        damper.record_write(probe_a)
        self._written_at[charger_entry_id] = dt_util.utcnow().timestamp()
        probe.start(
            now,
            probe_a,
            {phase: float(currents[phase]) for phase in PHASES},
            phases,
            dict(zip(phases, delivered_a)),
            charger_charging=charging_before,
            plug_in=charger_controller.plug_in_generation,
        )
        self._schedule_probe_check(probe.window_s)
        self._record_probe_event(
            charger_entry_id,
            decision,
            outcome="probe_started",
            detail="battery_charging_at_the_fuse",
            setpoint=int(probe_a),
            previous_setpoint=previous_setpoint,
        )
        return True

    async def _async_battery_probe_pass(
        self,
        charger_entry_id: str,
        charger_controller: Any,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        probe: BatteryProbe,
    ) -> None:
        """One pass of a running probe: judge it on the fresh readings, and stop the car if it
        failed (retrying the stop each pass until it is confirmed)."""
        now = self._yield_now()
        damper = self._damper_for(charger_entry_id)
        if probe.state == "probing":
            wiring: dict[str, Any] = (self.config.get(CONF_PHASE_WIRING) or {}).get(
                charger_entry_id
            ) or {}
            # A probe of a stopped charge: a reading the charger has not sent since the probe's start is no sign.
            delivered = self._stopped_charger_reading(charger_entry_id, wiring)
            observing = fresh.state == "observing"
            site_current_a = (
                dict(fresh.measured_phase_current_a)
                if observing
                else {phase: None for phase in PHASES}
            )
            limit_a: dict[PhaseName, float | None] = {}
            # No more than the import target leaves the car, as for a yield step (`_consult_yield_stepper`).
            target_a = self._target_ceiling_a(charger_entry_id)
            for phase in PHASES:
                measured = fresh.measured_phase_current_a.get(phase)
                margin = fresh.measured_margin_a.get(phase)
                limit_a[phase] = (
                    None if not observing or measured is None or margin is None else measured + margin
                )
                car_a = None if delivered is None else delivered.get(phase).value
                if target_a is not None and limit_a[phase] is not None and measured is not None and car_a is not None:
                    limit_a[phase] = min(cast(float, limit_a[phase]), measured - car_a + target_a)
            main_fuse_a = self.config.get(CONF_MAIN_FUSE_A)
            verdict = probe.evaluate(
                now,
                site_current_a=site_current_a,
                limit_a=limit_a,
                delivered_a={
                    phase: None if delivered is None else delivered.get(phase).value
                    for phase in PHASES
                },
                main_fuse_a=float(main_fuse_a) if main_fuse_a is not None else 0.0,
                charger_charging=self._charger_reports_charging(charger_controller),
                connector_status=self._connector_status(charger_controller),
            )
            if verdict.state == "verifying":
                if verdict.reason != "verifying":
                    # Waiting for a car that has started: judged again by the end of the longest wait,
                    # whatever else triggers a pass.
                    remaining = probe.extension_remaining_s(now)
                    if remaining is not None:
                        self._schedule_probe_check(remaining)
                if verdict.extended:
                    self._record_probe_event(
                        charger_entry_id,
                        decision,
                        outcome="probe_extended",
                        detail=verdict.reason,
                        setpoint=None,
                        previous_setpoint=damper.last_written_a,
                    )
                return
            if verdict.state == "succeeded":
                probe.succeeded(now)
                # The car runs at its minimum on a grid the battery holds at the limit; the stepper
                # starts from nothing and climbs it as it always does, on a battery now verified to give way
                # (the yield ceiling holds, `YieldConfig.unverified_ceiling_a`).
                self._yield_steppers.pop(charger_entry_id, None)
                self._yield_stepper_for(charger_entry_id).note_battery_yielded()
                self._record_probe_event(
                    charger_entry_id,
                    decision,
                    outcome="probe_succeeded",
                    detail=verdict.reason,
                    setpoint=None,
                    previous_setpoint=damper.last_written_a,
                )
                return
            probe.failed(now, verdict.reason)
            self._record_probe_event(
                charger_entry_id,
                decision,
                outcome="probe_failed",
                detail=verdict.reason,
                setpoint=None,
                previous_setpoint=damper.last_written_a,
            )
        # Stopping: the failed probe's stop, hard (a refusal that the fuse needs is a safety stop).
        write = await charger_controller.async_apply_regulated_current(0, must_lower=True)
        if write.written or write.outcome == REGULATED_STOPPED:
            damper.record_write(0.0)
            probe.stopped()
            if probe.car_refused:
                # The car refused the probes in a row with the charger's own reports agreeing: it has ended the
                # charge by itself. Told once, after the stop went out, so the plan's car-ended path sees the charge
                # load balancing holds back, as it is.
                probe.forget_refusals()
                self._record_probe_event(
                    charger_entry_id,
                    decision,
                    outcome="probe_car_ended",
                    detail="car_refused_probes",
                    setpoint=None,
                    previous_setpoint=damper.last_written_a,
                )
                charger_controller.note_car_refused_probe()
            return
        if write.outcome == REGULATED_FLOORED:
            # Not paused: the charge runs on at the floor, which is what the charger draws until the stop is taken.
            damper.record_write(float(DEFAULT_MIN_CURRENT_A))
        _LOGGER.warning(
            "SpotNav site %s battery probe: charger %s could not be stopped (%s); trying again",
            self.entry_id,
            charger_entry_id,
            write.code,
        )

    @property
    def battery_probe_snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-charger battery-probe diagnostics for the site sensor and the diagnostics dump:
        state, the last outcome and why, probes so far and the back-off left. Present for every
        associated charger."""
        now = self._yield_now()
        applies = self._battery_rules_apply()
        snapshot: dict[str, dict[str, Any]] = {}
        for charger_entry_id in list(self.config.get(CONF_CHARGER_ENTRY_IDS) or []):
            probe = self._battery_probes.get(charger_entry_id)
            entry: dict[str, Any] = (
                probe.snapshot(now)
                if probe is not None
                else {
                    "state": "idle",
                    "last_outcome": None,
                    "last_reason": None,
                    "last_age_s": None,
                    "backoff_remaining_s": None,
                    "probes": 0,
                    "car_refusals": 0,
                }
            )
            snapshot[charger_entry_id] = {"enabled": applies, **entry}
        return snapshot

    @property
    def active_control_enabled(self) -> bool:
        """The person's opt-in as this running controller holds it -- the value every gate reads."""
        return bool(self.config.get(CONF_ACTIVE_CONTROL_ENABLED, False))

    def _reset_active_control_state(self) -> None:
        """Forget damping, yield-stepping state, its diagnostics and log memory; the observation
        side keeps running.
        """
        self._dampers.clear()
        self._step_guards.clear()
        self._yield_steppers.clear()
        self._battery_probes.clear()
        self._resume_ready_since.clear()
        self._resume_times.clear()
        self._resume_backoff_until.clear()
        self._start_credits.clear()
        self._credited_a.clear()
        self._raise_credits.clear()
        self._raise_credited_a.clear()
        self._cancel_probe_timer()
        self._yield_stepping_last.clear()
        self._logged_yield_stepping.clear()
        self._logged_apply_outcomes.clear()

    async def async_enable_active_control(self) -> str:
        """Turn active control on in place; returns `ENABLE_ENABLED`, `ENABLE_ALREADY` or
        `ENABLE_UNAVAILABLE`. Never writes to a charger.

        Requires the capability snapshot to report `active_available`. Starts from clean damping and
        yield state and an empty direction history, and recomputes without scheduling an apply pass;
        the first pass runs on the next ordinary trigger. Callers hold `transition_lock`.
        """
        if self._closed:
            raise SiteControllerClosed
        if self.active_control_enabled:
            return ENABLE_ALREADY
        if not ACTIVE_CONTROL_READY or not self.capability_snapshot.load_balancing.active_available:
            return ENABLE_UNAVAILABLE
        self._reset_active_control_state()
        for history in self._direction_history.values():
            history.clear()
        self.config[CONF_ACTIVE_CONTROL_ENABLED] = True
        self._recompute(schedule_apply=False)
        return ENABLE_ENABLED

    async def async_disable_active_control(self) -> RestoreReport:
        """Turn active control off in place and restore any current balancing lowered.

        Order: (1) flip the opt-in off so every gate refuses immediately; (2) cancel and await any
        in-flight apply pass (a write already dispatched is not assumed to have failed or succeeded;
        the restore reads the charger); (3) capture per-charger evidence of what was written, then
        drop damping and yield state; (4) restore each member charger through
        `ChargingController.async_restore_current`, one after another; (5) give back or forget a charge it held
        back (`async_release_held_charges`), which no regulator resumes any more; (6) notify listeners.

        A charger that cannot be restored is reported `failed` with a stable code; the opt-in stays
        off regardless. Callers hold `transition_lock`.
        """
        if self._closed:
            raise SiteControllerClosed
        was_enabled = self.active_control_enabled
        self.config[CONF_ACTIVE_CONTROL_ENABLED] = False
        # Every pass and every charger's operation in flight is cancelled and waited for (briefly), so no
        # write of theirs lands after the restore below.
        await self._async_retire_operations()
        evidence = {
            charger_entry_id: damper.last_written_a is not None
            for charger_entry_id, damper in self._dampers.items()
        }
        self._reset_active_control_state()
        if not was_enabled:
            # Already off: nothing could have been lowered.
            return RestoreReport(RESTORE_NOT_NEEDED, ())
        conflicted = {conflict.charger_entry_id for conflict in self.membership_conflicts}
        reports: list[ChargerRestoreReport] = []
        for charger_entry_id in list(self.config.get(CONF_CHARGER_ENTRY_IDS) or []):
            lowered = evidence.get(charger_entry_id, False)
            charger_controller = controller_for(self.hass, charger_entry_id)
            if charger_controller is None:
                restore = CurrentRestore(
                    RESTORE_FAILED if lowered else RESTORE_NOT_NEEDED,
                    RESTORE_CHARGER_NOT_LOADED if lowered else None,
                    None,
                    None,
                )
            elif not self._is_commandable_charger(charger_controller):
                # Never written by the apply path, so nothing here to give back.
                restore = CurrentRestore(RESTORE_NOT_NEEDED, None, None, None)
            elif charger_entry_id in conflicted:
                # Ownership in doubt: never written to from here, restore included.
                restore = CurrentRestore(
                    RESTORE_FAILED if lowered else RESTORE_NOT_NEEDED,
                    RESTORE_MEMBERSHIP_CONFLICT if lowered else None,
                    None,
                    None,
                )
            else:
                restore = await charger_controller.async_restore_current(
                    lowered_by_balancing=lowered
                )
            reports.append(ChargerRestoreReport(charger_entry_id, restore))
            if restore.outcome != RESTORE_NOT_NEEDED:
                _LOGGER.info(
                    "SpotNav site %s active control turned off: charger %s restore %s "
                    "(from %s A to %s A, code %s)",
                    self.entry_id,
                    charger_entry_id,
                    restore.outcome,
                    restore.from_a,
                    restore.to_a,
                    restore.code,
                )
        # Load balancing governs these chargers no more, and nothing else would resume a charge it holds back.
        await async_release_held_charges(
            self.hass,
            [
                charger_entry_id
                for charger_entry_id in self.config.get(CONF_CHARGER_ENTRY_IDS) or []
                if charger_entry_id not in conflicted
            ],
        )
        outcomes = {report.restore.outcome for report in reports}
        if RESTORE_FAILED in outcomes:
            outcome = RESTORE_FAILED
        elif RESTORE_RESTORED in outcomes:
            outcome = RESTORE_RESTORED
        else:
            outcome = RESTORE_NOT_NEEDED
        self._notify()
        return RestoreReport(outcome, tuple(reports))

    def _step_guard_for(self, charger_entry_id: str) -> StepGuard:
        """This charger's `StepGuard`, created on first use."""
        guard = self._step_guards.get(charger_entry_id)
        if guard is None:
            guard = self._step_guards[charger_entry_id] = StepGuard()
        return guard

    @staticmethod
    def _site_read_at(fresh: SiteCapacityResult) -> float | None:
        """When the newest of the site's phase readings was taken (epoch seconds), or `None`."""
        ages = [age for age in fresh.phase_signed_active_power_age_s.values() if age is not None]
        if not ages:
            return None
        return dt_util.utcnow().timestamp() - min(ages)

    @staticmethod
    def _charger_read_at(decision: RegulatorDecision) -> float | None:
        """When the oldest of the charger's own phase readings the decision used was taken, or `None`."""
        ages = [basis.measured_current_age_s for basis in decision.basis.values()]
        if not ages or any(age is None for age in ages):
            return None
        return dt_util.utcnow().timestamp() - max(ages)

    def _damper_for(self, charger_entry_id: str) -> RegulatorDamper:
        """This charger's damping state, created on first use with the fixed deadband and dwell.
        Per charger because the rule depends on that charger's own history.
        """
        damper = self._dampers.get(charger_entry_id)
        if damper is None:
            damper = RegulatorDamper(deadband_a=REGULATOR_DEADBAND_A, dwell_s=REGULATOR_DWELL_S)
            self._dampers[charger_entry_id] = damper
        return damper

    def _yield_stepper_for(self, charger_entry_id: str) -> YieldStepper:
        """This charger's `YieldStepper`, created on first use. `ceiling_a` is the fixed multiple of the
        main fuse (`yield_ceiling_a`), `min_current_a` from the charger's wiring (as for the regulator).
        """
        stepper = self._yield_steppers.get(charger_entry_id)
        if stepper is None:
            wiring: dict[str, Any] = (self.config.get(CONF_PHASE_WIRING) or {}).get(
                charger_entry_id
            ) or {}
            main_fuse_a = self.config.get(CONF_MAIN_FUSE_A)
            stepper = YieldStepper(
                YieldConfig(
                    ceiling_a=yield_ceiling_a(float(main_fuse_a)) if main_fuse_a is not None else 0.0,
                    # Above the fuse only on a battery whose yield is verified on this site.
                    unverified_ceiling_a=float(main_fuse_a) if main_fuse_a is not None else 0.0,
                    min_current_a=float(wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A)),
                    real_excess_a=REGULATOR_DEADBAND_A,
                )
            )
            self._yield_steppers[charger_entry_id] = stepper
        return stepper

    def _consult_yield_stepper(
        self,
        *,
        charger_entry_id: str,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        assigned_a: float | None,
    ) -> YieldVerdict:
        """Feed this charger's `YieldStepper` one fresh observation and return its verdict.

        Callers have already confirmed the gates. `phases` is `decision.basis`'s keys (non-empty,
        since `applyability_failure` refuses an empty basis). `site_current_a` comes from the fresh
        calculation; `delivered_current_a` is re-read live from the charger's measured-current
        source and is `None` per phase when none is configured, so the stepper passes through.
        """
        stepper = self._yield_stepper_for(charger_entry_id)
        phases = tuple(decision.basis.keys())
        wiring: dict[str, Any] = (self.config.get(CONF_PHASE_WIRING) or {}).get(
            charger_entry_id
        ) or {}
        delivered = self._read_charger_measured_current(wiring)
        if delivered is None:
            delivered_current_a: dict[PhaseName, float | None] = {
                phase: None for phase in phases
            }
        else:
            delivered_current_a = {
                phase: delivered.get(phase).value for phase in phases
            }
        site_current_a = {phase: fresh.measured_phase_current_a.get(phase) for phase in phases}
        # Every basis entry carries the same `requested_current_a`, so any one is this charger's.
        requested_a = next(iter(decision.basis.values())).requested_current_a
        battery = self.battery_aggregate_power()
        battery_power_w = (
            battery.value if battery is not None and battery.problem is None else None
        )
        # The limit per phase: what is measured plus the uncredited margin (fuse less safety margin), and no more than
        # the import target leaves the car (`_target_ceiling_a`): the site as it is, with the car at that ceiling.
        target_a = self._target_ceiling_a(charger_entry_id)
        if target_a is not None:
            requested_a = min(requested_a, max(target_a, float(decision.proposed_current_a or 0.0)))
        limit_a: dict[PhaseName, float | None] = {}
        for phase in phases:
            measured = fresh.measured_phase_current_a.get(phase)
            margin = fresh.measured_margin_a.get(phase)
            limit_a[phase] = None if measured is None or margin is None else measured + margin
            car_a = delivered_current_a.get(phase)
            if target_a is not None and limit_a[phase] is not None and measured is not None and car_a is not None:
                limit_a[phase] = min(cast(float, limit_a[phase]), measured - car_a + target_a)
        observation = YieldObservation(
            now=self._yield_now(),
            site_current_a=site_current_a,
            delivered_current_a=delivered_current_a,
            phases=phases,
            battery_charge_a=self._battery_charge_a(fresh, phases),
            battery_power_w=battery_power_w,
            battery_fresh=self._fresh_battery_w() is not None,
            limit_a=limit_a,
            credit_allowed=(
                self.config.get(CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY)
                == SOLAR_PRIORITY_CAR_FIRST
            ),
        )
        return stepper.observe(decision.proposed_current_a, requested_a, assigned_a, observation)

    def _restores_target_lowering(
        self, charger_entry_id: str, proposed: float | None, previous_setpoint: float | None
    ) -> bool:
        """Whether a raise only gives back what the target itself lowered this charger by this hour: no higher than the
        most it held it to this hour (`_target_last_cap`)."""
        budget = self._target_budget
        held = self._target_last_cap.get(charger_entry_id)
        return (
            budget is not None
            and held is not None
            and held[0] == budget.hour_start
            and proposed is not None
            and previous_setpoint is not None
            and previous_setpoint < proposed <= held[1]
        )

    @staticmethod
    def _target_lowering(decision: RegulatorDecision, previous_setpoint: float | None) -> bool:
        """Whether `decision` is the import target lowering the charger below what was last written to it: a decision
        the target made that gives the charger more than it has (the fuse would give it more still) is a raise."""
        proposed = decision.proposed_current_a
        return (
            decision.reason in IMPORT_TARGET_REASONS
            and proposed is not None
            and previous_setpoint is not None
            and proposed < previous_setpoint
        )

    def _yield_verdict_under_target(
        self,
        charger_entry_id: str,
        decision: RegulatorDecision,
        verdict: YieldVerdict,
        previous_setpoint: float | None,
    ) -> YieldVerdict | None:
        """Yield stepping's verdict as the import target allows it (`None`: the raw proposal goes as it is).

        A lowering the target made (`_target_lowering`) is the target's: no hold of the stepper's keeps it back, and
        only a write of the stepper's that is lower still is taken. Otherwise a verdict is touched only where it writes
        more than the raw proposal: such a step never goes above what the target leaves the charger
        (`_target_ceiling_a`), taken at it, or not at all where that is no step up. A verdict that writes less than the
        proposal (a revert, a step back) is always kept. The fuse is untouched either way: each only gives the charger
        less than the stepper would."""
        proposed = decision.proposed_current_a
        if self._target_lowering(decision, previous_setpoint):
            if (
                verdict.action == "write"
                and verdict.current_a is not None
                and proposed is not None
                and verdict.current_a < proposed
            ):
                return verdict
            return None
        if verdict.action != "write" or verdict.current_a is None or proposed is None or verdict.current_a <= proposed:
            return verdict
        ceiling = self._target_ceiling_a(charger_entry_id)
        if ceiling is None or verdict.current_a <= ceiling:
            return verdict
        stepped = float(math.floor(ceiling))
        if stepped <= proposed or stepped < self._min_current_a(charger_entry_id):
            return None
        return replace(verdict, current_a=stepped, reaches_request=False)

    @staticmethod
    def _battery_charge_a(
        fresh: SiteCapacityResult, phases: tuple[PhaseName, ...]
    ) -> float | None:
        """The battery's charge current per phase, the smallest across the charger's phases: the
        diagnostic estimate's headroom gain over the measured margin (`site/site_capacity.py`),
        `None` when the battery is not charging or no phase has a usable estimate.
        """
        gains: list[float] = []
        for phase in phases:
            estimate = fresh.estimated_headroom_if_battery_yields_a.get(phase)
            margin = fresh.measured_margin_a.get(phase)
            if estimate is None or margin is None:
                return None
            gains.append(max(0.0, estimate - margin))
        return min(gains) if gains else None

    def _record_yield_stepping_verdict(
        self, charger_entry_id: str, verdict: YieldVerdict
    ) -> None:
        """Remember the latest verdict for the `yield_stepping` diagnostic attribute; never read
        back into a decision.
        """
        self._yield_stepping_last[charger_entry_id] = {
            "state": verdict.state,
            "y": verdict.y,
            "y_age_s": verdict.y_age_s,
            "reference_a": dict(verdict.reference_a),
            "action": verdict.action,
            "reason": verdict.reason,
            "battery_verified": verdict.battery_verified,
            "battery_credit_a": verdict.battery_credit_a,
        }

    def _log_yield_stepping_transition(
        self, charger_entry_id: str, verdict: YieldVerdict, fresh: SiteCapacityResult
    ) -> None:
        """Debug-log once per change (like `_log_active_control_outcome`). `YIELD_STEPPING` is a
        greppable token; the site current is included because `y` is defined from it.
        """
        signature: tuple[Any, ...] = (verdict.action, verdict.reason, verdict.state)
        if self._logged_yield_stepping.get(charger_entry_id) == signature:
            return
        self._logged_yield_stepping[charger_entry_id] = signature
        _LOGGER.debug(
            "YIELD_STEPPING site %s charger %s action=%s reason=%s state=%s y=%s "
            "site_current_a=%s",
            self.entry_id,
            charger_entry_id,
            verdict.action,
            verdict.reason,
            verdict.state,
            verdict.y,
            dict(fresh.measured_phase_current_a),
        )

    @property
    def yield_stepping_snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-charger yield-stepping diagnostics for the site sensor's `yield_stepping` attribute.

        Present for every associated charger even when the option is off. `enabled` is read live;
        the rest is the last verdict, or "unknown" defaults if none yet.
        """
        enabled = bool(self.config.get(CONF_YIELD_STEPPING_ENABLED, DEFAULT_YIELD_STEPPING_ENABLED))
        charger_entry_ids: list[str] = list(self.config.get(CONF_CHARGER_ENTRY_IDS) or [])
        snapshot: dict[str, dict[str, Any]] = {}
        for charger_entry_id in charger_entry_ids:
            last = self._yield_stepping_last.get(charger_entry_id)
            snapshot[charger_entry_id] = {
                "enabled": enabled,
                "state": last["state"] if last else "unknown",
                "y": last["y"] if last else None,
                "y_age_s": last["y_age_s"] if last else None,
                "reference_a": last["reference_a"] if last else {},
                "action": last["action"] if last else None,
                "reason": last["reason"] if last else None,
                "battery_verified": bool(last["battery_verified"]) if last else False,
                "battery_credit_a": last["battery_credit_a"] if last else None,
            }
        return snapshot

    @property
    def solar_surplus_snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-charger solar-surplus diagnostics for the site sensor's `solar_surplus` attribute,
        present for every associated charger.

        `enabled` is read live from the settings store (the strategy is per charger, not site
        config). The rest is `SolarExecutionCoordinator`'s last state, or "unknown" defaults.
        """
        store = domain_data(self.hass).auto_store
        charger_entry_ids: list[str] = list(self.config.get(CONF_CHARGER_ENTRY_IDS) or [])
        snapshot: dict[str, dict[str, Any]] = {}
        for charger_entry_id in charger_entry_ids:
            settings = None if store is None else store.settings(charger_entry_id)
            enabled = settings is not None and settings.strategy == STRATEGY_SOLAR
            data = charger_data(self.hass, charger_entry_id)
            state = None if data is None or data.solar is None else data.solar.state
            snapshot[charger_entry_id] = {
                "enabled": enabled,
                "state": state.state if state else "unknown",
                "action": state.action if state else None,
                "reason": state.reason if state else None,
                "requested_a": state.requested_a if state else None,
                "available_w": state.available_w if state else None,
                "available_a": state.available_a if state else None,
                # The start minimum a solar charge starts at (`SolarConfig.start_a`).
                "start_a": state.start_a if state else None,
                "net_grid_w": state.net_grid_w if state else None,
                "car_w": state.car_w if state else None,
                "battery_w": state.battery_w if state else None,
                "export_w": state.export_w if state else None,
                "priority_effective": state.priority_effective if state else None,
                # What keeps solar from a full basis (`execution/solar_execution.py`'s `SolarBasis`).
                "basis_problem": state.basis.problem if state else None,
                "basis_entity": state.basis.problem_entity if state else None,
                "charger_current": state.basis.charger_current if state else None,
                "charger_current_entity": state.basis.charger_current_entity if state else None,
                "site_incomplete_phases": list(state.basis.site_incomplete_phases) if state else [],
                # When a car that stopped charging by itself is tried again (`car_stopped`).
                "retry_at": state.retry_at.isoformat() if state and state.retry_at else None,
                # Who writes the sun's current to the charger: `active_control`, `solar` (its own write, where
                # active control does not), or `none` (a charger whose current cannot be set).
                "solar_current_writer": state.solar_current_writer if state else None,
                # The car's own limit while its target is reached and the sun fills on up to it.
                "sun_fill_limit_percent": state.sun_fill_limit_percent if state else None,
            }
        return snapshot

    @property
    def hybrid_snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-charger hybrid-mode diagnostics for the site sensor's `hybrid` attribute, present for
        every associated charger.

        `enabled` is read live from the settings store (the strategy is per charger). The rest is
        `hybrid_execution.HybridChargerState`, or "unknown" defaults when the charger is not on
        hybrid or its Auto preview has not calculated yet.
        """
        store = domain_data(self.hass).auto_store
        charger_entry_ids: list[str] = list(self.config.get(CONF_CHARGER_ENTRY_IDS) or [])
        snapshot: dict[str, dict[str, Any]] = {}
        for charger_entry_id in charger_entry_ids:
            settings = None if store is None else store.settings(charger_entry_id)
            enabled = settings is not None and settings.strategy == STRATEGY_HYBRID
            data = charger_data(self.hass, charger_entry_id)
            state = None if data is None else data.hybrid_state
            snapshot[charger_entry_id] = {
                "enabled": enabled,
                "state": state.state if state else "unknown",
                "reason": state.reason if state else None,
                "grid_kwh": state.grid_kwh if state else None,
                "credit_kwh": state.credit_kwh if state else None,
                "forecast_credit_kwh": state.forecast_credit_kwh if state else None,
                "slack_kwh": state.slack_kwh if state else None,
                "plan_window_active": state.plan_window_active if state else False,
                "forecast_sources": list(state.forecast_sources) if state else [],
                "sun_start": None if state is None or state.sun_start is None else state.sun_start.isoformat(),
                "sun_end": None if state is None or state.sun_end is None else state.sun_end.isoformat(),
            }
        return snapshot

    def _most_restrictive_margin_by_charger(
        self, result: SiteCapacityResult
    ) -> dict[str, float | None]:
        """Each charger's tightest remaining margin in amps: the minimum across its own phases.

        `None` if its wiring is unknown or no phase has a usable margin, which the damper reads as
        hold.
        """
        margins: dict[str, float | None] = {}
        for request in self._build_requests():
            phases = request.phases_used() or ()
            usable = [
                value
                for value in (result.measured_margin_a.get(phase) for phase in phases)
                if value is not None
            ]
            margins[request.charger_entry_id] = min(usable) if usable else None
        return margins

    def _calculate(self) -> SiteCapacityResult:
        """The site's figures on the latest readings, calculated once per reading: a recompute, the apply pass it
        queues, a start's allowance and its reservation all read the same readings, so the result is reused while
        no reading changed (`_readings_key`) and for at most `CALCULATION_REUSE_S`, over which a reading's age would
        drift."""
        key = self._readings_key()
        now = dt_util.utcnow()
        cached = self._calculation
        if (
            cached is not None
            and cached[0] == key
            and 0.0 <= (now - cached[1]).total_seconds() <= CALCULATION_REUSE_S
        ):
            return cached[2]
        # The site's own figures never rest on a credit (which is read from them). An "Automatic" charger not
        # followed to a phase is reckoned on the worst phase of the figures being calculated, not the last ones.
        result = calculate_site_capacity(
            self._build_config(), self._build_requests(credit=False, resolve=False)
        )
        self._calculation = (key, now, result)
        return result

    def _readings_key(self) -> tuple[Any, ...]:
        """What `_calculate` reads, by its readings' timestamps: each measurement entity's state with its
        `last_updated` and `last_reported`, each charger's requested current and priority, and the phases whose
        current sign is confirmed."""
        if self._reading_entities is None:
            self._reading_entities = tuple(self._tracked_entity_ids())
        states = []
        for entity_id in self._reading_entities:
            state = self.hass.states.get(entity_id)
            states.append(
                None
                if state is None
                else (state.state, state.last_updated, getattr(state, "last_reported", None), id(state))
            )
        charger_entry_ids = list(self.config.get(CONF_CHARGER_ENTRY_IDS) or [])
        return (
            id(self.config),
            tuple(states),
            tuple(self._charger_requested_current(charger_entry_id) for charger_entry_id in charger_entry_ids),
            tuple(self.charger_priority(charger_entry_id) for charger_entry_id in charger_entry_ids),
            self._current_sign.confirmed,
        )

    def _build_config(self) -> SiteCalculationConfig:
        mode = self.config.get(CONF_MEASUREMENT_MODE)
        main_fuse_a = self.config.get(CONF_MAIN_FUSE_A)
        direct = self._read_direct_entities() if mode == MEASUREMENT_MODE_DIRECT else None
        derived = self._read_derived_entities() if mode == MEASUREMENT_MODE_DERIVED else None
        direct_signed = self._read_direct_signed_entities() if mode == MEASUREMENT_MODE_DIRECT else None
        return SiteCalculationConfig(
            main_fuse_a=float(main_fuse_a) if main_fuse_a is not None else None,
            safety_margin_a=float(self.config.get(CONF_SAFETY_MARGIN_A, 0.0)),
            measurement_mode=mode if mode in (MEASUREMENT_MODE_DIRECT, MEASUREMENT_MODE_DERIVED) else "unavailable",
            max_age_s=float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S)),
            direct=direct,
            derived=derived,
            battery=self._read_battery_estimate(),
            direct_signed=direct_signed,
            direct_sign_confirmed=self._current_sign.confirmed if direct_signed is not None else (),
        )

    def _read_battery_estimate(self) -> BatteryYieldEstimate | None:
        """Optional diagnostic home-battery input (`site/site_capacity.py`); `None` when neither a
        per-phase source nor an aggregate power entity is configured.
        """
        per_phase_source = source_from_dict(self.config.get(CONF_BATTERY_PER_PHASE_SOURCE))
        per_phase_current = None
        if per_phase_source is not None:
            values = read_phase_measurement(self.hass, per_phase_source, classify_current)
            per_phase_current = DirectPhaseMeasurement(l1=values["L1"], l2=values["L2"], l3=values["L3"])

        aggregate_power = self.battery_aggregate_power()

        # A per-phase current magnitude alone never implies charging; it needs a fresh signed
        # direction reading. The aggregate power entity serves as that signal when both are
        # configured.
        if per_phase_current is None and aggregate_power is None:
            return None
        return BatteryYieldEstimate(
            per_phase_charge_current_a=per_phase_current,
            direction_confirmation_power_w=aggregate_power,
            aggregate_charge_power_w=aggregate_power,
        )

    def _build_requests(self, *, credit: bool = True, resolve: bool = True) -> list[ChargerRequest]:
        """One request per charger; with `credit`, a charger just started is credited with the draw the latest
        result shows it took (`_credited_measurements`).

        An "Automatic" single-phase charger (no phase chosen) is on the phase its own measured current shows it
        drawing on (`_followed_phase`), else, with `resolve`, on the phase with the least headroom in the latest
        result (`resolve_worst_case_phases`)."""
        charger_entry_ids: list[str] = list(self.config.get(CONF_CHARGER_ENTRY_IDS) or [])
        phase_wiring: dict[str, dict[str, Any]] = self.config.get(CONF_PHASE_WIRING) or {}
        requests: list[ChargerRequest] = []
        for order, charger_entry_id in enumerate(charger_entry_ids):
            wiring = phase_wiring.get(charger_entry_id) or {}
            phases, phase = self.reckoned_phases(charger_entry_id)
            measured = self._read_charger_measured_current(wiring)
            basis: PhaseBasis = "chosen"
            requested = self._charger_requested_current(charger_entry_id)
            min_current_a = float(wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A))
            charger_controller = controller_for(self.hass, charger_entry_id)
            switch_only = self._is_switch_only_charger(charger_controller)
            if switch_only:
                # Reckoned at its whole switched current, never an unknown one: it draws all of it or nothing.
                switched = float(math.ceil(charger_controller.switched_current().amps - 1e-9))
                min_current_a = switched
                if requested is None or requested > 0:
                    requested = switched
                if measured is None:
                    measured = self._switched_measurement(charger_controller, phases, phase, switched)
            if phases == 1 and phase is None:
                phase = self._followed_phase(charger_entry_id, measured)
                basis = "measured" if phase is not None else "worst_case"
            request = ChargerRequest(
                charger_entry_id=charger_entry_id,
                requested_current_a=requested,
                phases=phases,
                phase=phase,
                min_current_a=min_current_a,
                measured_current_a=measured,
                priority=self.charger_priority(charger_entry_id),
                order=order,
                phase_basis=basis,
                switch_only=switch_only,
            )
            requests.append(request)
        latest: SiteCapacityResult | None = getattr(self, "result", None)
        if resolve and latest is not None:
            requests = resolve_worst_case_phases(requests, latest.measured_margin_a)
        if not credit:
            return requests
        return self._with_credits(requests)

    def _with_credits(self, requests: list[ChargerRequest]) -> list[ChargerRequest]:
        """`requests` (built without the credit) with a charger just started, or just raised, credited
        (`_build_requests`)."""
        site_a = self.result.measured_phase_current_a
        credited = self._credited_measurements(requests, site_a)
        credited = {**credited, **self._raise_credited_measurements(requests, site_a, credited)}
        return [
            replace(request, measured_current_a=credited[request.charger_entry_id])
            if request.charger_entry_id in credited
            else request
            for request in requests
        ]

    def _followed_phase(
        self, charger_entry_id: str, measured: DirectPhaseMeasurement | None
    ) -> PhaseName | None:
        """The phase an "Automatic" single-phase charger is followed to (`site/auto_phase.py`), or `None` for the
        worst phase: its own measured current shows it drawing on that one phase alone, the site's reading of it
        bears that out, and it has held for the settle time."""
        tracker = self._auto_phases.setdefault(charger_entry_id, AutoPhaseTracker())
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        # The first calculation has no result yet to bear a draw out: the worst phase until there is one.
        result: SiteCapacityResult | None = getattr(self, "result", None)
        drawn = drawn_phase(measured, max_age_s, None if result is None else result.measured_phase_current_a)
        charger_controller = controller_for(self.hass, charger_entry_id)
        settling = getattr(charger_controller, "phase_switch_settling", False) is True
        return tracker.observe(drawn, self._yield_now(), settling=settling)

    def reckoned_phases(self, charger_entry_id: str) -> tuple[int, PhaseName | None]:
        """The phases a charger is reckoned on and its chosen phase: its wiring, except that a three-phase charger
        the sun forced to one phase (`ChargingController.switched_phases`) is a single-phase one with no phase
        chosen ("Automatic": the phase its measured current shows, else the worst), exactly as such a charger is
        reckoned. A must-lower never waits for, and never uses, a phase switch."""
        wiring = (self.config.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id) or {}
        phases = wiring.get("phases", 3)
        phase = wiring.get("phase")
        if phases == 3:
            charger_controller = controller_for(self.hass, charger_entry_id)
            if getattr(charger_controller, "switched_phases", None) == 1:
                return 1, None
        return phases, phase

    def charger_phase_snapshot(self) -> dict[str, dict[str, Any]]:
        """Per charger, for the diagnostics: its phases, the phase a single-phase charger is reckoned on now and
        how that was decided (`chosen`, `measured` or `worst_case`; `None` while no phase can be reckoned)."""
        snapshot: dict[str, dict[str, Any]] = {}
        for request in self._build_requests(credit=False):
            single = request.phases == 1
            snapshot[request.charger_entry_id] = {
                "phases": request.phases,
                "phase": request.phase if single else None,
                "phase_basis": request.phase_basis if not single or request.phase is not None else None,
                "automatic": single and request.phase_basis != "chosen",
            }
        return snapshot

    def followed_phase(self, charger_entry_id: str) -> PhaseName | None:
        """The phase an "Automatic" single-phase charger was last followed to by its measured current, else
        `None` (a chosen phase, three phases, or reckoned on the worst phase)."""
        tracker = self._auto_phases.get(charger_entry_id)
        return None if tracker is None else tracker.current

    def charger_priority(self, charger_entry_id: str) -> str:
        """The charger's own `CONF_CHARGER_PRIORITY`, "normal" when none is stored or its entry is gone."""
        entry = self.hass.config_entries.async_get_entry(charger_entry_id)
        if entry is None:
            return DEFAULT_CHARGER_PRIORITY
        return str(entry.data.get(CONF_CHARGER_PRIORITY) or DEFAULT_CHARGER_PRIORITY)

    def _active_power_last_reported(self, phase: PhaseName) -> datetime | None:
        """The `last_reported` timestamp of this phase's active-power entity, read directly off
        `hass.states`.

        On a direct site it is the phase's current entity (the one entity of a source carrying all three).
        `None` when it cannot be determined: neither mode, no entity configured, no state yet, or no
        `last_reported` support.
        """
        if self.config.get(CONF_MEASUREMENT_MODE) == MEASUREMENT_MODE_DIRECT:
            entity_id = self.phase_entities().get(phase)
            state = self._state_or_none(entity_id)
            return getattr(state, "last_reported", None) if state is not None else None
        if self.config.get(CONF_MEASUREMENT_MODE) != MEASUREMENT_MODE_DERIVED:
            return None
        entities: dict[str, dict[str, str]] = self.config.get(CONF_DERIVED_ENTITIES) or {}
        phase_entities = entities.get(phase) or {}
        # An import/export pair reports new data whenever either half does.
        reported = [
            getattr(state, "last_reported", None)
            for state in (
                self._state_or_none(phase_entities.get("power")),
                self._state_or_none(phase_entities.get("power_export")),
            )
            if state is not None
        ]
        reported = [value for value in reported if value is not None]
        return max(reported) if reported else None

    def _update_direction_history(self) -> None:
        """Append each phase's direction classification to its history, only when a new report has
        arrived, and on a direct site feed that report to the current-sign check (`_update_current_sign`).

        Recomputes fire far more often than the entity reports (timer, charger listeners, charge-
        control changes), so counting each one would satisfy the consecutive-confirmation
        requirement through the passage of time alone. A new report is detected by comparing the
        active-power entity's own absolute `last_reported` with the last one seen: a recompute-
        relative age is fooled by irregular recompute gaps, and the combined P/Q/V age would let a
        slow Q or V sensor mask a fresh P reading. Whether the reading is usable is gated separately
        by `max_age_s`.

        If `last_reported` is unavailable the history is left as is, and the stored timestamp is
        never overwritten with `None`. A new HA report is still not proof of a new physical
        measurement, so this makes confirmation harder to satisfy by accident, not airtight.
        """
        any_new_report = False
        for phase in PHASES:
            last_reported = self._active_power_last_reported(phase)
            previous = self._last_seen_active_power_report_at.get(phase)
            is_new_report = last_reported is not None and (
                previous is None or last_reported > previous
            )
            if last_reported is not None:
                self._last_seen_active_power_report_at[phase] = last_reported
            if not is_new_report:
                continue
            any_new_report = True
            signed_w = self.result.phase_signed_active_power_w.get(phase)
            self._direction_history[phase].append(classify_direction(signed_w, DEFAULT_ZERO_MARGIN_W))
        if any_new_report:
            self._update_current_sign()

    def _update_current_sign(self) -> None:
        """Feed a direct site's new signed reading and the meter's total to `CurrentSignCheck`; only fresh,
        usable values count. Takes effect from the next calculation."""
        if self.config.get(CONF_MEASUREMENT_MODE) != MEASUREMENT_MODE_DIRECT:
            return
        signed = self._read_direct_signed_entities()
        if signed is None:
            return
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))

        def fresh(value: PhaseValue | None) -> float | None:
            if value is None or value.value is None or value.problem is not None:
                return None
            if value.age_s is None or value.age_s > max_age_s:
                return None
            return value.value

        self._current_sign.observe(
            {phase: fresh(signed.get(phase)) for phase in PHASES}, fresh(self.grid_total_power())
        )

    def _compute_regulator_decisions(self) -> dict[str, RegulatorDecision]:
        """One regulator decision per associated charger on top of `self.result`
        (`site/regulator.py`); calls no service.

        `allocate_regulator_decisions` uses a shared remaining-headroom pool so chargers sharing a
        phase are not offered the same headroom.
        """
        config = self._build_config()
        voltage_by_phase: dict[PhaseName, float | None] = (
            {phase: config.derived.get(phase).voltage_v.value for phase in PHASES}
            if config.measurement_mode == MEASUREMENT_MODE_DERIVED and config.derived is not None
            else {
                # A direct site's signed current stands in for power at the nominal voltage it was multiplied by.
                phase: DIRECTION_NOMINAL_PHASE_VOLTAGE_V
                if self.result.phase_direction_source.get(phase) == "signed_current"
                else None
                for phase in PHASES
            }
        )
        confirmed_direction_by_phase: dict[PhaseName, DirectionClass | None] = {
            phase: confirmed_direction(
                list(self._direction_history[phase]), DEFAULT_MIN_CONSECUTIVE_CONFIRMATIONS
            )
            for phase in PHASES
        }
        self._update_charge_writers()
        # A charge on a phase whose direction is not known (never confirmed, lost meanwhile, or a charge that started
        # before it was), whoever started it, is decided on the measured magnitude (`regulator.magnitude_decision`):
        # lowered on an overload by |I|, raised within the room |I| leaves, never above its request. The one case this
        # does worse is a phase over the limit by export, which the car's draw was reducing; a direct site cannot
        # tell that from import, and the limit read on |I| is the fuse's own measure. A direct site's charge that
        # started without a known direction stays the sun's (or nobody's) to write: the regulator's full rules never
        # reach it, even once its phases are confirmed.
        in_charge = {
            charger_entry_id
            for charger_entry_id in (self.config.get(CONF_CHARGER_ENTRY_IDS) or [])
            if self._in_charge(charger_entry_id)
        }
        not_ours = {
            charger_entry_id
            for charger_entry_id, known in self._direction_at_charge_start.items()
            if not known and charger_entry_id in in_charge
        }
        # Built once: the credited requests are the same requests with a start's credit applied.
        uncredited = self._build_requests(credit=False)
        credited = self._with_credits(uncredited)
        decisions = allocate_regulator_decisions(
            site_result=self.result,
            requests=credited,
            # A credited start or raise is judged on its own reading too: the credit never stands for a raise.
            own_measured_current_a={request.charger_entry_id: request.measured_current_a for request in uncredited},
            credit_raise_tolerance_a=START_CREDIT_RAISE_TOLERANCE_A,
            voltage_by_phase=voltage_by_phase,
            confirmed_direction_by_phase=confirmed_direction_by_phase,
            max_age_s=config.max_age_s,
            zero_margin_w=DEFAULT_ZERO_MARGIN_W,
            help_margin_factor=DEFAULT_HELP_MARGIN_FACTOR,
            increase_reserved_by_phase=self._solar_reserved_a() if not_ours else None,
            direction_unknown_in_charge=in_charge,
            direction_unknown_at_start=not_ours,
        )
        for request in credited:
            decision = decisions.get(request.charger_entry_id)
            if decision is not None and decision.reason == REASON_HELD_STILL_NO_RAISE:
                decisions[request.charger_entry_id] = self._held_still_kept(request, decision)
        try:
            return self._hold_to_import_target(decisions, credited, voltage_by_phase, config.max_age_s)
        except Exception:  # noqa: BLE001 - the import target never stands in the fuse's way: its decisions go as they are
            if not self._target_failed_logged:
                self._target_failed_logged = True
                _LOGGER.exception("SpotNav site %s: the import target could not be applied; nothing is held for it", self.entry_id)
            self._target_budget = None
            self._target_shares = {}
            self._target_lowered = set()
            self._target_raise_lock = {}
            self._target_not_applied = TARGET_NOT_APPLIED_ERROR
            self._note_target_state(TARGET_NOT_APPLIED_ERROR)
            return decisions

    def _held_still_kept(self, request: ChargerRequest, decision: RegulatorDecision) -> RegulatorDecision:
        """A raise refused on a still phase (`held_still_no_raise`) keeps what the charger was last written, never
        less: the regulator's "current" is what the car draws, which is 0 A for a charge just started (or below the
        written current while it ramps), and taking that would lower it, or pause it below its minimum. With no
        written current at or above the charger's minimum known, no decision (nothing is written, nothing paused), as
        for a phase that cannot be judged. Lowerings (an overload, the import target) still act on the kept value."""
        charger_controller = controller_for(self.hass, request.charger_entry_id)
        written = None if charger_controller is None else getattr(charger_controller.adapter, "last_current_a", None)
        if (
            isinstance(written, (int, float))
            and not isinstance(written, bool)
            and written >= request.min_current_a
            and (decision.proposed_current_a is None or written >= decision.proposed_current_a)
        ):
            return replace(decision, proposed_current_a=float(written))
        if decision.proposed_current_a is not None and decision.proposed_current_a >= request.min_current_a:
            return decision
        return replace(decision, proposed_current_a=None)

    # -- the import target (`site/import_target.py`)

    def _import_target_kw(self) -> float | None:
        """The site's import target as stored, read from its entry: it is written without a reload."""
        from ..api.entity_fields import stored_import_target_kw  # noqa: PLC0415 - the API imports this module

        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        return stored_import_target_kw(self.config if entry is None else entry.data)

    def effective_import_target_kw(self, set_kw: float | None) -> float | None:
        """The level the running hour is held under now (`_effective_import_target`): the import target lifted to the
        month's free level, for the dashboard's line beside the target. `None` without a target. Worked out on its
        own, never through the hold's cache: reading it changes nothing a charger is told."""
        if set_kw is None:
            return None
        return self._worked_out_import_target(set_kw, self.demand.now())[0].kw

    def _effective_import_target(self, set_kw: float, now: datetime) -> EffectiveTarget:
        """The import target lifted to the month's free level for the running hour (`effective_target`), from the
        hours the site's tracker kept this month and the electricity price's demand-charge parts. Worked out again
        when an hour completes, the hour turns or the parts or the target change."""
        found, key = self._worked_out_import_target(set_kw, now, self._target_effective_key)
        if found is None:
            assert self._target_effective is not None
            return self._target_effective
        self._target_effective_key = key
        return found

    def _worked_out_import_target(
        self, set_kw: float, now: datetime, known_key: Any = None
    ) -> tuple[Any, Any]:
        """`(effective target, its key)`; `(None, key)` when `known_key` is the key and the hold's own figure stands."""
        from ..planning.demand_charge import hour_start  # noqa: PLC0415 - kept beside its only use
        from ..pricing.price_source import site_demand_components  # noqa: PLC0415 - it imports the planner

        components = site_demand_components(self.hass, self.entry_id)
        tracker = self.demand.tracker
        key = (set_kw, components, tracker.month, len(tracker.hours), hour_start(now, tracker.tz), tracker.tz)
        if known_key is not None and key == known_key and self._target_effective is not None:
            return None, key
        if not components:
            return effective_target(set_kw, ()), key
        from ..planning.demand_charge import component_month, free_level_hours  # noqa: PLC0415 - kept beside its only use

        # Each hour at its floor (its import over the whole hour, a gap counted as none): never above what the
        # operator billed, where the mean over the time measured may be. An hour read from Home Assistant's
        # statistics (`imported`) is an approximation: it lifts the target only when shown `complete` (all twelve
        # 5-minute rows, read once with the back-fill, never here).
        hours = free_level_hours(tracker.month_hours(now))
        levels = [component_month(component, hours, tracker.tz, now).free_kw for component in components]
        return effective_target(set_kw, levels), key

    def _battery_first_discharge(self) -> tuple[bool, float | None]:
        """Whether a home battery's discharge counts as import for the target (`battery_first` with a battery set),
        and its discharge now (W, positive while it discharges), `None` when it cannot be read live."""
        if self.config.get(CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY) != SOLAR_PRIORITY_BATTERY_FIRST:
            return False, None
        battery = self.battery_aggregate_power()
        if battery is None:
            return False, None
        if battery.value is None or not self.solar_accepts(battery, self.battery_entity_ids()):
            return True, None
        return True, max(0.0, -float(battery.value))

    def _import_target_budget(self, target_kw: float, now: datetime) -> tuple[HourBudget | None, str | None]:
        """The running hour's budget (`hour_budget`) on this recompute's readings, or why it is not applied."""
        from ..execution.solar_execution import site_car_w, site_net_grid_w  # noqa: PLC0415 - it imports us

        if not self._active_control_allowed():
            return None, TARGET_NOT_APPLIED_LOAD_BALANCING_OFF
        battery_counts, discharge_w = self._battery_first_discharge()
        if battery_counts and discharge_w is None:
            return None, TARGET_NOT_APPLIED_BATTERY_UNKNOWN
        grid_w = site_net_grid_w(self)
        car_w = None if grid_w is None else site_car_w(self.hass, self)
        house_w = None if grid_w is None or car_w is None else grid_w + (discharge_w or 0.0) - car_w
        if house_w is not None and self._a_start_not_yet_drawing():
            # A charger just started (written at or above its minimum) still reads below its minimum while the meter,
            # or a battery covering the car, already sees the car: its draw would count as the house's. Never more
            # than what the house typically draws then, so the hold does not flash on a start.
            typical_kw = self._typical_house_kw()
            if typical_kw is not None:
                house_w = min(house_w, max(0.0, typical_kw) * 1000.0)
        running = self.demand.tracker.running_hour(now)
        main_fuse_a = self.config.get(CONF_MAIN_FUSE_A)
        ceiling_w = float("inf") if main_fuse_a is None else float(main_fuse_a) * len(PHASES) * HOLD_VOLTAGE_V
        return hour_budget(
            target_kw=target_kw,
            running=running,
            now=now,
            house_w=house_w,
            ceiling_w=ceiling_w,
            extra_wh=0.0 if running is None else running.discharge_wh,
            extra_covered_s=(None if running is None else running.discharge_covered_s) if battery_counts else None,
        )

    def _target_member(
        self,
        request: ChargerRequest,
        decision: RegulatorDecision | None,
        budget: HourBudget,
        released: Collection[str] = (),
    ) -> TargetMember:
        """One charger as the target's budget is shared: what it would draw, and whether the target may hold it (not
        a person's Start, not the car's minimum level's charge, nor a charge in `released`, set aside by a person
        until its departure)."""
        charger_entry_id = request.charger_entry_id
        charger_controller = controller_for(self.hass, charger_entry_id)
        phases = len(request.phases_used() or ()) or request.phases
        drawn = 0.0
        if request.measured_current_a is not None:
            values = [request.measured_current_a.get(phase).value for phase in (request.phases_used() or ())]
            drawn = max((value for value in values if value is not None), default=0.0)
        origin = None
        if charger_controller is not None:
            origin = getattr(charger_controller, "charge_origin", None) or getattr(
                charger_controller, "paused_charge_origin", None
            )
        paused = bool(getattr(charger_controller, "paused_by_balancing", False))
        proposed = None if decision is None else decision.proposed_current_a
        holdable = (
            charger_controller is not None
            # A person's Start charges now, within the target, as any charge does (its way past the target is a release,
            # `async_release_import_target`). Below the car's minimum level SpotNav charges at once
            # (`execution/min_soc_floor.py`): the floor wins over the target.
            and origin != ORIGIN_MIN_SOC
            # A person set the target aside for this charge until its departure (`async_release_import_target`).
            and charger_entry_id not in released
            # Neither an overload's lowering nor a decision with nothing to write is the target's to change.
            and (paused or (proposed is not None and decision is not None and not _must_lower(decision)))
        )
        if paused and holdable:
            # What the paused charge would resume at: a resume or a probe gives it no more (`_target_start_cap`).
            requested = getattr(charger_controller, "requested_current_a", None)
            wants = request.min_current_a if request.switch_only else float(requested or 0.0)
        elif holdable:
            wants = float(proposed or 0.0)
        elif paused:
            # A charge the target does not hold, paused by the fuse: it draws nothing while it waits, and when the fuse
            # resumes it, it is counted at what it draws then.
            wants = 0.0
        else:
            wants = drawn if proposed is None else max(0.0, proposed)
        write_wait_s, write_interval_s, set_a = _current_write_lock(charger_controller)
        return TargetMember(
            charger_entry_id=charger_entry_id,
            phases=phases,
            min_a=request.min_current_a,
            wants_a=wants,
            holdable=holdable,
            switch_only=request.switch_only,
            paused_this_hour=self._target_paused_hour.get(charger_entry_id) == budget.hour_start,
            draws_a=0.0 if paused else drawn,
            set_a=0.0 if paused else set_a,
            write_wait_s=write_wait_s,
            write_interval_s=write_interval_s,
        )

    def _a_start_not_yet_drawing(self) -> bool:
        """Whether a member charger is in a charge it was written at least its minimum for, but its own reading is
        still below that minimum: a start (or a resume) whose draw the charger has not reported yet."""
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        wiring_by_charger: dict[str, Any] = self.config.get(CONF_PHASE_WIRING) or {}
        for charger_entry_id in self.config.get(CONF_CHARGER_ENTRY_IDS) or []:
            charger_controller = controller_for(self.hass, charger_entry_id)
            if charger_controller is None or not (charger_controller.charging or charger_controller.start_pending):
                continue
            minimum = self._min_current_a(charger_entry_id)
            written = getattr(charger_controller.adapter, "last_current_a", None)
            if not isinstance(written, (int, float)) or isinstance(written, bool) or written < minimum:
                continue
            delivered = self._read_charger_measured_current(wiring_by_charger.get(charger_entry_id) or {})
            readings = (
                []
                if delivered is None
                else [
                    value.value
                    for value in (delivered.get(phase) for phase in PHASES)
                    if value.value is not None and value.problem is None and value.age_s is not None
                    and value.age_s <= max_age_s
                ]
            )
            if readings and max(readings) < minimum:
                return True
        return False

    def _typical_house_kw(self) -> float | None:
        """What the house typically draws (kW): its mean over its last `TARGET_HOUSE_HOURS` measured hours and the
        running hour so far, each by the time it was measured; `None` before any is. A kettle's half minute weighs half
        a minute in it."""
        tracker = self.demand.tracker
        # Only SpotNav's own hours: one read from Home Assistant's statistics never changes what a charger is told.
        hours = [
            hour.house_kw
            for hour in (*tracker.previous, *tracker.hours)
            if hour.house_kw is not None and not hour.imported
        ]
        recent = hours[-TARGET_HOUSE_HOURS:]
        energy_kwh, span_h = sum(recent), float(len(recent))
        running = tracker.running
        if running is not None and running.house_covered_s > 0:
            energy_kwh += running.house_wh / 1000.0
            span_h += running.house_covered_s / HOUR_S
        return None if span_h <= 0 else energy_kwh / span_h

    def _target_shortfall(
        self, member: TargetMember, budget: HourBudget, now: datetime
    ) -> tuple[str, float] | None:
        """`(departure, kWh)`: how much a plan's charge would miss its departure by under the target
        (`departure_shortfall_kwh`, negative: to spare), or `None` for a charge with no plan and departure ahead.

        This hour and later ones are reckoned at the target less the house's typical load (`_typical_house_kw`, the
        estimate the plan counts on too, `import_target_plan_kw`), never its load this instant: a kettle's half minute
        must not say the car will not be ready. What it still needs is what the plan expects of its windows' time
        left, plus what the running window is already behind (`PlanWatch.shortfall_kwh`): a window the target held
        is planned again at its end, so earlier windows' shortfalls are in the plan already
        (`planning/plan_watch.py`)."""
        charger_controller = controller_for(self.hass, member.charger_entry_id)
        plan = None if charger_controller is None else getattr(charger_controller, "plan", None)
        if plan is None or not plan.departure or not getattr(plan, "auto_owned", False):
            return None
        departure = dt_util.parse_datetime(plan.departure)
        if departure is None or departure.tzinfo is None:
            return None
        # What the plan still expects to deliver before the departure: its energy spread over its windows (the power
        # it planned at, which may already be the target's, `import_target_plan_kw`), for the windows' time left.
        windows = plan.windows
        window_h = sum(max(0.0, (end - start).total_seconds()) for start, end in windows) / 3600.0
        if plan.energy_kwh and window_h > 0:
            power_kw = plan.energy_kwh / window_h
        else:
            power_kw = plan.power_kw if plan.power_kw else member_w(plan.phases, plan.amps) / 1000.0
        need_kwh = plan_remaining_kwh(windows, power_kw, now, departure)
        preview = preview_for(self.hass, member.charger_entry_id)
        if preview is not None:
            need_kwh += preview.import_target_shortfall_kwh()
        house_kw = self._typical_house_kw()
        if house_kw is None:
            house_kw = max(0.0, budget.house_w) / 1000.0
        # This hour at what its budget leaves beside the house's typical load (never above the plan's power, nothing
        # below the charger's minimum), every later hour at the target less that load.
        car_kw = member_w(member.phases, max(member.min_a, member.wants_a)) / 1000.0
        ceiling_kw = max(power_kw, car_kw)
        held_kw = min(ceiling_kw, max(0.0, budget.rate_w / 1000.0 - max(0.0, house_kw)))
        # Later hours at the target as set, less its margin as the hold keeps it: the month's free level lifts the
        # running hour only.
        set_kw = held_level_kw(budget.target_kw if self._target_effective is None else self._target_effective.set_kw)
        later_kw = min(ceiling_kw, max(0.0, set_kw - max(0.0, house_kw)))
        minimum_kw = member_w(member.phases, member.min_a) / 1000.0
        held_kw = 0.0 if held_kw < minimum_kw else held_kw
        later_kw = 0.0 if later_kw < minimum_kw else later_kw
        shortfall = departure_shortfall_kwh(
            need_kwh=need_kwh,
            now=now,
            departure=departure,
            hour_end=budget.hour_end,
            held_kw=held_kw,
            later_kw=later_kw,
        )
        return None if shortfall is None else (plan.departure, shortfall)

    def _target_floor_a(
        self,
        request: ChargerRequest,
        voltage_by_phase: Mapping[PhaseName, float | None],
        max_age_s: float,
        battery_discharge_w: float = 0.0,
    ) -> float | None:
        """How far the target may lower this charger without raising what the fuse carries (`lowering_floor_a`), on
        this recompute's own readings; `None` when a phase cannot be judged. Under `battery_first` with the home
        battery read live and discharging, a drop up to this charger's part of its discharge (`battery_discharge_w`, the
        caller's share of `_battery_first_discharge`) only lowers the discharge, within the smallest fuse margin over
        its phases; under `car_first`, or with a battery that cannot be read live, the grid's phases alone decide."""
        phases = request.phases_used() or ()
        result = self.result
        if not phases or request.measured_current_a is None:
            return None
        for phase in phases:
            age = result.phase_signed_active_power_age_s.get(phase)
            if result.phase_signed_active_power_reason.get(phase) is not None or age is None or age > max_age_s:
                return None
        signed_current = any(result.phase_direction_source.get(phase) == "signed_current" for phase in phases)
        return lowering_floor_a(
            phases=phases,
            measured_a={phase: request.measured_current_a.get(phase).value for phase in phases},
            signed_power_w={phase: result.phase_signed_active_power_w.get(phase) for phase in phases},
            voltage_v={phase: voltage_by_phase.get(phase) for phase in phases},
            margin_a={phase: result.measured_margin_a.get(phase) for phase in phases} if signed_current else None,
            battery_discharge_w=battery_discharge_w,
            fuse_margin_a={phase: result.measured_margin_a.get(phase) for phase in phases},
        )

    def _hold_to_import_target(
        self,
        decisions: dict[str, RegulatorDecision],
        requests: Sequence[ChargerRequest],
        voltage_by_phase: Mapping[PhaseName, float | None],
        max_age_s: float,
    ) -> dict[str, RegulatorDecision]:
        """The fuse's decisions with the import target min-ed in (`site/import_target.py`).

        Only ever lowers: a decision is replaced only by a smaller proposal, so the smaller of the fuse's ceiling and
        the target's always wins. An overload's lowering (`_must_lower`) and a decision with nothing to write are
        never touched, and a lowering never goes below what keeps every phase the charger draws on an import
        (`_target_floor_a`), so the target can never raise the current the fuse carries. Not applied (nothing held)
        while load balancing is off, the hour cannot be measured well enough, or the site's target is not set. A
        failure here leaves the fuse's decisions as they are (`_compute_regulator_decisions`)."""
        previously_held = self._target_lowered | self._target_locked
        self._target_shares = {}
        self._target_lowered = set()
        self._target_locked = set()
        self._target_raise_lock = {}
        self._target_write_wait = {}
        self._target_inert = {}
        self._target_floor_limited = set()
        self._target_budget = None
        target_kw = self._import_target_kw()
        if target_kw is None:
            self._target_not_applied = None
            self._note_target_state(TARGET_STATE_OFF)
            self._target_effective = None
            self._target_paused_hour.clear()
            self._target_risk = {}
            for charger_entry_id, until in self._target_auto_released.items():
                self._log_target(charger_entry_id, "release_ended", reason=RELEASE_END_TARGET_REMOVED, until=until, automatic=True)
            self._target_auto_released = {}
            self._tell_target_risk()
            if self._target_released:
                for charger_entry_id, until in self._target_released.items():
                    self._log_target(charger_entry_id, "release_ended", reason=RELEASE_END_TARGET_REMOVED, until=until)
                self._target_released.clear()
                self._save_released()
            self._target_last_cap.clear()
            return decisions
        now = dt_util.utcnow()
        # The target lifted to the month's free level where a demand-charge part counts the running hour: only ever
        # higher than the target, to a level already paid for, and only the budget's; the fuse is untouched.
        self._target_effective = self._effective_import_target(target_kw, now)
        budget, reason = self._import_target_budget(self._target_effective.kw, now)
        self._target_not_applied = reason
        self._note_target_state(TARGET_STATE_APPLIED if reason is None else reason)
        if budget is None:
            return decisions
        self._target_budget = budget
        # A pause and a held current hold to the end of their hour only.
        self._target_paused_hour = {
            charger_entry_id: hour
            for charger_entry_id, hour in self._target_paused_hour.items()
            if hour == budget.hour_start
        }
        self._target_last_cap = {
            charger_entry_id: held
            for charger_entry_id, held in self._target_last_cap.items()
            if held[0] == budget.hour_start
        }
        self._latch_target_pauses(budget)
        ordered = allocation_order(requests)
        self._prune_released(now)
        members = [
            self._target_member(request, decisions.get(request.charger_entry_id), budget, self._target_released)
            for request in ordered
        ]
        shares = share_budget(budget.allowed_w, members, remaining_s=budget.remaining_s)
        # Above the car's minimum level the target holds a plan's charge even past its departure: whether it would miss
        # it is said (`_target_risk`, decided again on every recompute, with a margin either way), and a person may set
        # the target aside for it (`async_release_import_target`).
        risks: dict[str, TargetRisk] = {}
        # A charge the plan's own choice set aside stays so until its departure (or an unplug, or a new departure),
        # as a person's does: no hold again mid-hour that would then pause it for the rest of the hour.
        automatic: dict[str, str] = dict(self._target_auto_released)
        for member in members:
            if not member.holdable or member.charger_entry_id in automatic:
                continue
            found = self._target_shortfall(member, budget, now)
            if found is None:
                continue
            departure, shortfall = found
            was = self._target_risk.get(member.charger_entry_id)
            if not departure_at_risk(shortfall, at_risk=was is not None and was.departure == departure):
                continue
            choice = self._if_not_ready(member.charger_entry_id)
            risks[member.charger_entry_id] = TargetRisk(
                departure=departure,
                missing_kwh=round(max(0.0, shortfall), 1),
                target_kw=budget.target_kw,
                choice=choice,
                set_kw=budget.target_kw if self._target_effective is None else self._target_effective.set_kw,
            )
            if choice == IF_NOT_READY_RELEASE:
                # The plan's own choice: the departure wins by itself, as long as holding would miss it.
                automatic[member.charger_entry_id] = departure
        for charger_entry_id, risk in risks.items():
            if charger_entry_id not in self._target_risk:
                self._note_target(charger_entry_id, "at_risk", budget, departure=risk.departure)
        for charger_entry_id, departure in automatic.items():
            if charger_entry_id not in self._target_auto_released:
                self._log_target(
                    charger_entry_id, "release", budget, source=RELEASE_SOURCE_PLAN, until=departure, automatic=True
                )
            risks.pop(charger_entry_id, None)
        self._target_risk = risks
        self._target_auto_released = automatic
        self._tell_target_risk()
        if automatic:
            released = {**self._target_released, **automatic}
            members = [
                self._target_member(request, decisions.get(request.charger_entry_id), budget, released)
                for request in ordered
            ]
            shares = share_budget(budget.allowed_w, members, remaining_s=budget.remaining_s)
        self._target_shares = shares
        self._target_write_wait = {member.charger_entry_id: member.write_wait_s for member in members}
        # The hour's last minutes raise nothing the target held this hour above what it held it to: what is left of
        # the budget is spread over too little time to be worth a raise the next hour takes back (and the lowering
        # then waits for the dwell, past the hour's start). A charger the target did not hold (one the fuse lowered,
        # say) is the fuse's and the budget's to raise, as at any other time. For a charger whose current may be
        # written only every few minutes the lock starts a whole interval and a dwell before the hour's end
        # (`raise_lock_s`), so the lowering that takes a raise back always comes within the hour, and it is kept
        # where it is set: a raise there would hold into the next hour, as no write may follow it before then. Such
        # a charger is locked whenever the target held it this hour, also when the budget freed it just before the
        # window; never below its minimum (a current set below it by hand is no reason to pause it).
        for member in members:
            held_cap = self._target_last_cap.get(member.charger_entry_id)
            lock = hour_end_lock_a(
                member,
                held_cap_a=None if held_cap is None else held_cap[1],
                held_last_pass=member.charger_entry_id in previously_held,
                remaining_s=budget.remaining_s,
            )
            if lock is not None:
                self._target_raise_lock[member.charger_entry_id] = lock
        held = dict(decisions)

        def holds_now(request: ChargerRequest) -> bool:
            """Whether this charger is one the target may lower in this pass (running, held, above its share)."""
            share = shares.get(request.charger_entry_id)
            decision = decisions.get(request.charger_entry_id)
            member = next(m for m in members if m.charger_entry_id == request.charger_entry_id)
            return not (
                share is None
                or decision is None
                or decision.proposed_current_a is None
                or not member.holdable
                or not request.requested_current_a
            )

        # Under `battery_first` the battery's live discharge is shared out among the chargers the target lowers in
        # this pass, so two cannot each count on the whole of it (`_target_floor_a`).
        discharge_shares = battery_discharge_shares_w(
            self._battery_first_discharge()[1] or 0.0,
            [
                request.charger_entry_id
                for request in ordered
                if holds_now(request)
                and (cap := shares[request.charger_entry_id].cap_a) is not None
                and decisions[request.charger_entry_id].proposed_current_a > cap  # type: ignore[operator]
            ],
        )
        for request in ordered:
            charger_entry_id = request.charger_entry_id
            share = shares.get(charger_entry_id)
            decision = decisions.get(charger_entry_id)
            if not holds_now(request):
                # Nothing held, or a charge that is not running (a paused one is held back by `_target_start_cap`).
                continue
            proposed = decision.proposed_current_a
            locked = None if share.action == ACTION_PAUSED else self._target_raise_lock.get(charger_entry_id)
            if share.cap_a is None or proposed <= share.cap_a:
                if locked is not None and proposed > locked:
                    # The budget has room, the hour's end does not: kept where the target held it, under the fuse's
                    # own reason (the target does not claim to hold what its budget would allow).
                    held[charger_entry_id] = replace(decision, proposed_current_a=locked)
                    self._target_locked.add(charger_entry_id)
                continue
            cap_a = share.cap_a if locked is None else min(share.cap_a, locked)
            floor = self._target_floor_a(
                request, voltage_by_phase, max_age_s, discharge_shares.get(charger_entry_id, 0.0)
            )
            if floor is None:
                self._target_inert[charger_entry_id] = TARGET_INERT_NOT_JUDGED
                continue
            if share.action == ACTION_PAUSED and floor <= 0.0:
                held[charger_entry_id] = replace(decision, proposed_current_a=0.0, reason=REASON_IMPORT_TARGET_PAUSED)
                if charger_entry_id not in self._target_paused_hour:
                    # Why this pause, until it is taken (a pause already latched keeps the reason it was taken for).
                    if share.reason is not None:
                        self._target_pause_reasons[charger_entry_id] = (budget.hour_start, share.reason)
                    else:
                        self._target_pause_reasons.pop(charger_entry_id, None)
                # Kept to the hour's end once the charger is seen paused for it (`_latch_target_pauses`): a pause
                # decided for a kettle's half minute and taken back before the dwell let it out pauses nothing.
                self._target_pause_pending[charger_entry_id] = budget.hour_start
                self._target_lowered.add(charger_entry_id)
                continue
            if request.switch_only:
                # It runs whole or not at all, and the pause would turn a phase it draws on to export.
                self._target_inert[charger_entry_id] = TARGET_INERT_EXPORTING
                continue
            lowered = max(cap_a, request.min_current_a, float(math.ceil(floor - 1e-9)))
            if lowered < proposed:
                held[charger_entry_id] = replace(decision, proposed_current_a=lowered, reason=REASON_IMPORT_TARGET_HELD)
                self._target_lowered.add(charger_entry_id)
                # The highest the target held it to this hour: the lock at the hour's end keeps no less, and a raise
                # back to it is the target giving back its own lowering (`_restores_target_lowering`).
                was_held = self._target_last_cap.get(charger_entry_id)
                highest = lowered if was_held is None or was_held[0] != budget.hour_start else max(was_held[1], lowered)
                self._target_last_cap[charger_entry_id] = (budget.hour_start, highest)
                self._note_target(charger_entry_id, "held", budget, cap_a=lowered)
            if lowered > max(cap_a, request.min_current_a):
                if lowered >= proposed:
                    # Nothing could be lowered: every amp of the drop would turn a phase it draws on to export.
                    self._target_inert[charger_entry_id] = TARGET_INERT_EXPORTING
                else:
                    # Lowered, but not as far as its share: the floor stopped it (`floor_limited`).
                    self._target_floor_limited.add(charger_entry_id)
        return held

    def _if_not_ready(self, charger_entry_id: str) -> str:
        """The plan's choice for a departure the target makes it miss (`AutoSettings.if_not_ready`)."""
        store = domain_data(self.hass).auto_store
        return IF_NOT_READY_ASK if store is None else store.settings(charger_entry_id).if_not_ready

    def _tell_target_risk(self) -> None:
        """Give each charger's notifier the departure the target makes it miss, or none (`ChargerNotifier.
        import_target_risk`): told once, and taken off the phones once it is met again."""
        for charger_entry_id in self.config.get(CONF_CHARGER_ENTRY_IDS) or []:
            data = charger_data(self.hass, charger_entry_id)
            notifier = None if data is None else data.notifier
            risk = self._target_risk.get(charger_entry_id)
            if notifier is not None:
                # Told only where the plan asks; `hold` and `release` are said in the status alone.
                told = risk if risk is not None and risk.choice == IF_NOT_READY_ASK else None
                outcome = notifier.import_target_risk(told)
                if outcome is not None:
                    # What the phones were told, for the change log: told, told again (grown) or taken off (with
                    # the departure and shortfall last told).
                    if told is not None:
                        self._target_noticed[charger_entry_id] = (told.departure, told.missing_kwh)
                    departure, missing_kwh = (
                        self._target_noticed.pop(charger_entry_id, (None, None))
                        if told is None
                        else (told.departure, told.missing_kwh)
                    )
                    self._log_target(
                        charger_entry_id, f"notice_{outcome}", departure=departure, missing_kwh=missing_kwh
                    )

    # -- a person sets the import target aside until a charge's departure

    async def _async_load_released(self) -> None:
        """Read the departures a person set the target aside until; a malformed record is dropped."""
        try:
            raw = await self._target_release_store.async_load()
        except Exception:  # noqa: BLE001 - what was kept is a person's choice: a site never fails to start on it
            _LOGGER.debug("SpotNav site %s: the import target's set-asides could not be read", self.entry_id, exc_info=True)
            return
        released = raw.get("released") if isinstance(raw, dict) else None
        if isinstance(released, dict):
            kept: dict[str, str] = {}
            for key, value in released.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                since = _unplug_release_since(value)
                if since is not None:
                    kept[key] = unplug_release_value(since)
                    continue
                moment = _aware_instant(value)
                if moment is not None:
                    # Kept as written where it has its offset (the plan's departure is compared as text).
                    kept[key] = value if dt_util.parse_datetime(value).tzinfo is not None else moment.isoformat()
            self._target_released = kept

    def _save_released(self) -> None:
        self._target_release_store.async_delay_save(lambda: {"released": dict(self._target_released)}, 1.0)

    @staticmethod
    async def async_remove_released(hass: HomeAssistant, entry_id: str) -> None:
        """The site is gone for good: forget what was set aside, and the grid sign's tally."""
        await Store(hass, 1, f"{DOMAIN}.import_target_release.{entry_id}").async_remove()
        await Store(hass, 1, f"{DOMAIN}.grid_sign.{entry_id}").async_remove()

    # -- whether the grid reading's sign looks set the wrong way (`site/grid_sign.py`)

    def _grid_sign_flags(self) -> dict[str, bool]:
        """The readings the step test checks on this site and their setting's value: the grid power (a total grid
        power, or a derived site's per-phase power, `grid_power_inverted`) and a direct site's signed phase currents
        (`site_current_signed`, only while it is on)."""
        flags: dict[str, bool] = {}
        mode = self.config.get(CONF_MEASUREMENT_MODE)
        if mode == MEASUREMENT_MODE_DERIVED or grid_power_source_from_dict(self.config.get(CONF_GRID_POWER_SOURCE)):
            flags[GRID_SIGN_POWER] = bool(self.config.get(CONF_GRID_POWER_INVERTED, False))
        if mode == MEASUREMENT_MODE_DIRECT and self.config.get(CONF_SITE_CURRENT_SIGNED):
            flags[GRID_SIGN_CURRENT] = True
        return flags

    def grid_sign_warned(self, kind: str) -> bool:
        """Whether the step test says this reading's sign (`grid_power` or `grid_current`) looks reversed."""
        return self._grid_sign.warned(kind)

    def grid_sign_flag(self, kind: str) -> bool | None:
        """The value of the setting the reading's tally was counted under; `None` when it is not tested."""
        return self._grid_sign.flag(kind)

    def grid_sign_snapshot(self) -> dict[str, Any]:
        """For the diagnostics: the tallies and the last counted step."""
        return self._grid_sign.snapshot()

    async def _async_load_grid_sign(self) -> None:
        try:
            raw = await self._grid_sign_store.async_load()
        except Exception:  # noqa: BLE001 - a warning's tally: a site never fails to start on it
            _LOGGER.debug("SpotNav site %s: the grid sign's tally could not be read", self.entry_id, exc_info=True)
            return
        changes = self._grid_sign.load(raw)
        self._grid_sign_changed(changes, save=bool(changes))

    @staticmethod
    def _fresh_reading(value: PhaseValue | None, now: float, max_age_s: float) -> GridSignReading | None:
        """A usable value with when its source last reported it, if within `max_age_s`; else `None`."""
        if value is None or value.value is None or value.problem is not None or value.age_s is None:
            return None
        age_s = value.age_s if value.report_age_s is None else min(value.age_s, value.report_age_s)
        if age_s > max_age_s:
            return None
        return GridSignReading(float(value.value), now - age_s)

    def _grid_sign_power(self, now: float, max_age_s: float) -> GridSignReading | None:
        """The grid power as the site reads it (positive = import): the meter's total, else a derived site's phases
        added up."""
        total = self.grid_total_power()
        if total is not None:
            return self._fresh_reading(total, now, max_age_s)
        powers = self.result.phase_signed_active_power_w
        ages = self.result.phase_signed_active_power_age_s
        if any(powers.get(phase) is None or ages.get(phase) is None for phase in PHASES):
            return None
        oldest = max(cast(float, ages[phase]) for phase in PHASES)
        if oldest > max_age_s:
            return None
        return GridSignReading(sum(cast(float, powers[phase]) for phase in PHASES), now - oldest)

    def _grid_sign_current(self, now: float, max_age_s: float) -> GridSignReading | None:
        """A direct site's signed phase currents added up at the nominal voltage (positive = import as read)."""
        signed = self._read_direct_signed_entities()
        if signed is None:
            return None
        readings = [self._fresh_reading(signed.get(phase), now, max_age_s) for phase in PHASES]
        if any(reading is None for reading in readings):
            return None
        found = [cast(GridSignReading, reading) for reading in readings]
        return GridSignReading(
            sum(reading.value for reading in found) * DIRECTION_NOMINAL_PHASE_VOLTAGE_V,
            min(reading.at for reading in found),
        )

    def _grid_sign_charger(self, charger_entry_id: str, now: float, max_age_s: float) -> GridSignReading | None:
        """A member charger's own draw in watts: its measured current source on the site, else its own current
        sensors or its smart plug's power. Nothing while it is off (no charge, no start of ours on its way); `None`
        while it charges and nothing fresh measures it."""
        charger_controller = controller_for(self.hass, charger_entry_id)
        if charger_controller is None:
            return None
        if not charger_controller.charging and not charger_controller.start_pending:
            # An OCPP charger keeps its last value after a stop: off draws nothing.
            return GridSignReading(0.0, now)
        wiring = (self.config.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id) or {}
        delivered = self._read_charger_measured_current(wiring)
        if delivered is not None:
            readings = [self._fresh_reading(delivered.get(phase), now, max_age_s) for phase in PHASES]
            if any(reading is None for reading in readings):
                return None
            found = [cast(GridSignReading, reading) for reading in readings]
            return GridSignReading(
                sum(reading.value for reading in found) * DIRECTION_NOMINAL_PHASE_VOLTAGE_V,
                min(reading.at for reading in found),
            )
        phases, _phase = self.reckoned_phases(charger_entry_id)
        own = self._switched_own_reading(charger_controller, phases)
        if own is None or own[1] > max_age_s:
            return None
        return GridSignReading(own[0] * phases * DIRECTION_NOMINAL_PHASE_VOLTAGE_V, now - own[1])

    def _observe_grid_sign(self) -> None:
        """Give the step test this recompute's readings."""
        if not self._grid_sign_flags():
            return
        now = dt_util.utcnow().timestamp()
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        grid: dict[str, GridSignReading | None] = {}
        if self._grid_sign.flag(GRID_SIGN_POWER) is not None:
            grid[GRID_SIGN_POWER] = self._grid_sign_power(now, max_age_s)
        if self._grid_sign.flag(GRID_SIGN_CURRENT) is not None:
            grid[GRID_SIGN_CURRENT] = self._grid_sign_current(now, max_age_s)
        battery_known = bool(self.config.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY))
        sample = GridSignSample(
            at=now,
            chargers={
                charger_entry_id: self._grid_sign_charger(charger_entry_id, now, max_age_s)
                for charger_entry_id in self.config.get(CONF_CHARGER_ENTRY_IDS) or []
            },
            grid=grid,
            battery=self._fresh_reading(self.battery_aggregate_power(), now, max_age_s) if battery_known else None,
            battery_known=battery_known,
        )
        counted = self._grid_sign.steps_counted
        changes = self._grid_sign.observe(sample)
        self._grid_sign_changed(changes, save=self._grid_sign.steps_counted != counted)

    def _grid_sign_changed(self, changes: list[GridSignChange], *, save: bool) -> None:
        """Log a warning raised or cleared, and keep the tally."""
        for change in changes:
            setting = "grid_power_inverted" if change.kind == GRID_SIGN_POWER else "site_current_signed"
            if change.raised:
                _LOGGER.info(
                    "SpotNav site %s: the %s's sign looks reversed (%s is %s): %d of the last counted charger steps "
                    "moved the grid the other way, %d the same way",
                    self.entry_id, change.kind.replace("_", " "), setting, self._grid_sign.flag(change.kind),
                    change.inverted, change.consistent,
                )
            elif change.cause == "setting":
                _LOGGER.info(
                    "SpotNav site %s: the %s's sign warning is cleared, %s was changed (tally was %d the other way, "
                    "%d the same way)",
                    self.entry_id, change.kind.replace("_", " "), setting, change.inverted, change.consistent,
                )
            else:
                _LOGGER.info(
                    "SpotNav site %s: the %s's sign warning is cleared: charger steps move the grid the same way "
                    "again (%d of the last counted the other way, %d the same way)",
                    self.entry_id, change.kind.replace("_", " "), change.inverted, change.consistent,
                )
        if save:
            self._grid_sign_unsaved = True
            self._grid_sign_store.async_delay_save(self._grid_sign_to_store, 5.0)

    def _grid_sign_to_store(self) -> dict[str, Any]:
        self._grid_sign_unsaved = False
        return self._grid_sign.as_dict()

    def _release_over(self, charger_entry_id: str, departure: str, now: datetime) -> bool:
        return self._release_end(charger_entry_id, departure, now) is not None

    def _release_end(self, charger_entry_id: str, departure: str, now: datetime) -> str | None:
        """Whether a set-aside until `departure` has ended: the departure passed, the car was unplugged, or the plan
        is now for another departure (earlier or later), which the set-aside was not given for. One with no departure
        (`RELEASED_UNTIL_UNPLUG`) ends at the unplug, when the car ends the charge by itself after it was given
        (`car_ended_at`), or at the next local noon (`unplug_release_latest`), whichever comes first: a charger that
        cannot report the plug, or a car that is full, does not keep it into the next day."""
        charger_controller = controller_for(self.hass, charger_entry_id)
        unplugged = charger_controller is not None and charger_controller.adapter.vehicle_connected() is False
        if unplugged:
            return RELEASE_END_UNPLUGGED
        since = _unplug_release_since(departure)
        if since is not None:
            ended = None if charger_controller is None else getattr(charger_controller, "car_ended_at", None)
            if isinstance(ended, datetime) and ended > since:
                return RELEASE_END_CHARGE_ENDED
            return RELEASE_END_NOON if now >= unplug_release_latest(since) else None
        moment = _aware_instant(departure)
        if moment is None or moment <= now:
            return RELEASE_END_DEPARTURE_PASSED
        plan = None if charger_controller is None else getattr(charger_controller, "plan", None)
        if plan is None or plan.departure != departure:
            return RELEASE_END_DEPARTURE_CHANGED
        return None

    def _prune_released(self, now: datetime) -> None:
        """A person's set-aside, and one the plan's choice made, end as `_release_over` says. A person's set-aside
        until a departure whose plan a person's Start then dropped is kept, until the unplug (`unplug_release_value`,
        from now): the Start is a charge of theirs, and they released the target for this plug-in."""
        changed = False
        for charger_entry_id, departure in list(self._target_released.items()):
            if self._dropped_by_a_start(charger_entry_id, departure, now):
                kept = unplug_release_value(now)
                self._target_released[charger_entry_id] = kept
                self._log_target(charger_entry_id, "release_kept", reason="person_start", until=kept, was=departure)
                changed = True
                continue
            reason = self._release_end(charger_entry_id, departure, now)
            if reason is not None:
                del self._target_released[charger_entry_id]
                self._log_target(charger_entry_id, "release_ended", reason=reason, until=departure)
                changed = True
        if changed:
            self._save_released()
        kept_auto: dict[str, str] = {}
        for charger_entry_id, departure in self._target_auto_released.items():
            reason = self._release_end(charger_entry_id, departure, now)
            if reason is None:
                kept_auto[charger_entry_id] = departure
            else:
                self._log_target(charger_entry_id, "release_ended", reason=reason, until=departure, automatic=True)
        self._target_auto_released = kept_auto

    def _dropped_by_a_start(self, charger_entry_id: str, departure: str, now: datetime) -> bool:
        """Whether a person's set-aside until `departure` (still ahead, the car still plugged in) lost its plan to a
        person's Start (no plan, and the charge a person's, `_persons_start`): kept then, until the unplug."""
        if _unplug_release_since(departure) is not None:
            return False
        charger_controller = controller_for(self.hass, charger_entry_id)
        if charger_controller is None or getattr(charger_controller, "plan", None) is not None:
            return False
        if not self._persons_start(charger_entry_id, charger_controller):
            return False
        if charger_controller.adapter.vehicle_connected() is False:
            return False
        moment = _aware_instant(departure)
        return moment is not None and moment > now

    def _persons_start(self, charger_entry_id: str, charger_controller: Any) -> bool:
        """Whether the charge is a person's Start: charging as one (`charge_origin` `manual`), held back by load
        balancing as one (for room, or paused by the fuse: `paused_charge_origin`), or stored as one (the manual pause
        with action `start`, as `charge_still_wanted` reads it)."""
        if getattr(charger_controller, "charge_origin", None) == "manual":
            return True
        if getattr(charger_controller, "paused_charge_origin", None) == "manual":
            return True
        store = domain_data(self.hass).auto_store
        settings = None if store is None else store.settings(charger_entry_id)
        if settings is None:
            return False
        pause = settings.pause
        return bool(pause.admitted and pause.manual and pause.action == MANUAL_START)

    def async_release_import_target(
        self, charger_entry_id: str, *, for_departure: int | None = None, source: str | None = None
    ) -> str | None:
        """Set the import target aside for this charger's charge (a person's choice, from the notification, the card or
        the app): until its plan's departure where one is ahead, else (a person's Start, a plan without a departure)
        until the car is unplugged. The error's code, else `None`: `spotnav_no_import_target` (the site has none set),
        `spotnav_stale_departure` (a phone's button told for a departure, `for_departure` in epoch seconds, that is not
        the plan's any more). `source` (`phone`, `card`, `app`) is only kept in the change log, with any refusal."""
        if self._import_target_kw() is None:
            self._log_target(charger_entry_id, "release_refused", source=source, error=ERROR_NO_IMPORT_TARGET)
            return ERROR_NO_IMPORT_TARGET
        plan = getattr(controller_for(self.hass, charger_entry_id), "plan", None)
        departure = None if plan is None or not plan.departure else dt_util.parse_datetime(plan.departure)
        if departure is not None and departure <= dt_util.utcnow():
            departure = None
        if departure is None:
            if for_departure is not None:
                _LOGGER.info(
                    "SpotNav site %s: a release of the import target for charger %s was for a departure its plan no "
                    "longer has; nothing is set aside",
                    self.entry_id,
                    charger_entry_id,
                )
                self._log_target(charger_entry_id, "release_refused", source=source, error=ERROR_STALE_DEPARTURE)
                return ERROR_STALE_DEPARTURE
            return self._release(charger_entry_id, unplug_release_value(dt_util.utcnow()), source)
        assert plan is not None
        if for_departure is not None and for_departure != int(departure.timestamp()):
            _LOGGER.info(
                "SpotNav site %s: a Release the import target for this charge for charger %s was for another departure than its plan's %s; "
                "nothing is set aside",
                self.entry_id,
                charger_entry_id,
                plan.departure,
            )
            self._log_target(charger_entry_id, "release_refused", source=source, error=ERROR_STALE_DEPARTURE)
            return ERROR_STALE_DEPARTURE
        _LOGGER.info(
            "SpotNav site %s: the import target is set aside for charger %s until %s, as a person asked",
            self.entry_id,
            charger_entry_id,
            plan.departure,
        )
        return self._release(charger_entry_id, plan.departure, source)

    def _release(self, charger_entry_id: str, until: str, source: str | None = None) -> str | None:
        """Keep the target aside for this charger `until` a departure (ISO) or the unplug (`unplug_release_value`)."""
        self._target_released[charger_entry_id] = until
        self._target_risk.pop(charger_entry_id, None)
        self._tell_target_risk()
        self._target_paused_hour.pop(charger_entry_id, None)
        self._save_released()
        self._log_target(charger_entry_id, "release", self._target_budget, source=source, until=until)
        self._recompute()
        return None

    def _latch_target_pause(self, charger_entry_id: str) -> None:
        """The target's pause of this charger was sent: it stays paused to the end of this hour."""
        budget = self._target_budget
        if budget is None or self._target_paused_hour.get(charger_entry_id) == budget.hour_start:
            return
        self._target_paused_hour[charger_entry_id] = budget.hour_start
        self._target_pause_pending.pop(charger_entry_id, None)
        self._note_target(charger_entry_id, "paused", budget, reason=self._target_pause_reason(charger_entry_id, budget))

    def _latch_target_pauses(self, budget: HourBudget) -> None:
        """A pause the target decided this hour that the charger has taken (paused by load balancing) holds to the
        hour's end: at most one pause per charger per hour, and no resume until the next."""
        pending = self._target_pause_pending
        self._target_pause_pending = {}
        for charger_entry_id, hour in pending.items():
            if hour != budget.hour_start:
                continue
            charger_controller = controller_for(self.hass, charger_entry_id)
            if bool(getattr(charger_controller, "paused_by_balancing", False)):
                if self._target_paused_hour.get(charger_entry_id) != hour:
                    self._target_paused_hour[charger_entry_id] = hour
                    self._note_target(
                        charger_entry_id, "paused", budget, reason=self._target_pause_reason(charger_entry_id, budget)
                    )
            # Not taken yet: this recompute decides again whether the target still pauses it.

    def _target_pause_reason(self, charger_entry_id: str, budget: HourBudget | None) -> str | None:
        """Why the target paused this charger this hour, where a rule beyond the share decided (`slow_charger`)."""
        found = self._target_pause_reasons.get(charger_entry_id)
        if found is None or budget is None or found[0] != budget.hour_start:
            return None
        return found[1]

    def import_target_holds(self, charger_entry_id: str) -> bool:
        """Whether the site's import target holds this charge back now: lowered, kept at the hour's end, or paused this
        hour (`planning/plan_watch.py` buys a window it held back again at its end)."""
        budget = self._target_budget
        if budget is None:
            return False
        if charger_entry_id in self._target_lowered or charger_entry_id in self._target_locked:
            return True
        return self._target_paused_hour.get(charger_entry_id) == budget.hour_start

    def _note_target(
        self,
        charger_entry_id: str,
        action: str,
        budget: HourBudget,
        *,
        cap_a: float | None = None,
        departure: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Keep a change of what the target does to a charger, for the diagnostics (one entry per change)."""
        log = self._history.target_log
        last = next((entry for entry in reversed(log) if entry.get("charger") == charger_entry_id), None)
        hour = budget.hour_start.isoformat()
        if last is not None and (last.get("hour"), last.get("action"), last.get("cap_a")) == (hour, action, cap_a):
            return
        self._log_target(charger_entry_id, action, budget, cap_a=cap_a, departure=departure, reason=reason)

    def _log_target(
        self, charger_entry_id: str | None, action: str, budget: HourBudget | None = None, **fields: Any
    ) -> None:
        """One entry of the import target's change log (`SiteHistory.note_target`), kept 24 hours across restarts:
        when, which charger (`None` for the site), what, the hour's budget where there is one, and the action's own
        fields. For the debug bundle only; never raises."""
        try:
            entry: dict[str, Any] = {"at": dt_util.utcnow().isoformat(), "charger": charger_entry_id, "action": action}
            if budget is not None:
                entry.update(
                    hour=budget.hour_start.isoformat(),
                    hold_kw=round(budget.hold_kw, 3),
                    hour_kw=round(budget.hour_kw, 3),
                    allowed_w=round(budget.allowed_w),
                    house_w=round(budget.house_w),
                )
            # Empty fields left out: a day of entries stays small.
            entry.update({key: value for key, value in fields.items() if value is not None})
            self._history.note_target(entry)
        except Exception:  # noqa: BLE001 - a diagnostic log never disturbs a decision
            _LOGGER.debug("SpotNav site %s: the import target's change could not be logged", self.entry_id, exc_info=True)

    def _note_target_state(self, state: str) -> None:
        """Log the import target's state (off, applied, or why it is not applied) when it changes."""
        if state == self._target_logged_state:
            return
        self._target_logged_state = state
        if state in (TARGET_STATE_APPLIED, TARGET_STATE_OFF):
            self._log_target(None, state)
        else:
            self._log_target(None, "not_applied", reason=state)

    def import_target_row(self) -> list[Any]:
        """The import target's columns on the site's minute row (`site_history.TARGET_SITE_COLUMNS`): the level the
        hour is held under (kW), what the chargers may draw (W), the house's power the budget used (W), and why it is
        not applied. All empty while no target is set."""
        budget = self._target_budget
        if budget is None:
            return [None, None, None, self._target_not_applied]
        return [round(budget.hold_kw, 3), round(budget.allowed_w), round(budget.house_w), None]

    def import_target_charger_row(self, charger_entry_id: str) -> list[Any]:
        """The import target's columns on a member charger's minute row (`site_history.TARGET_CHARGER_COLUMNS`): what
        it does to the charger (`free`, `held`, `paused`, `released`, `exempt`) and the amps it caps it at."""
        if self._target_budget is None:
            return [None, None]
        if charger_entry_id in self._target_released or charger_entry_id in self._target_auto_released:
            return ["released", None]
        share = self._target_shares.get(charger_entry_id)
        if share is None:
            return [None, None]
        return [TARGET_ROW_ACTIONS.get(share.action, share.action), share.cap_a]

    def _target_ceiling_a(self, charger_entry_id: str) -> float | None:
        """The most the import target leaves this charger now (`TargetShare.room_a`; `0.0` while it pauses it), `None`
        when it holds nothing back: no yield step and no probe goes above it."""
        share = self._target_shares.get(charger_entry_id)
        if share is None or share.action == ACTION_UNHELD:
            return None
        if share.action == ACTION_PAUSED:
            return 0.0
        locked = self._target_raise_lock.get(charger_entry_id)
        if locked is None or share.room_a is None:
            return share.room_a
        return min(share.room_a, locked)

    def import_target_plan_kw(self, charger_entry_id: str) -> float | None:
        """The power a plan for this charger counts on under the site's import target (kW): the target less what the
        house typically draws (`_typical_house_kw`), and never below the charger's minimum, which is all the hold ever
        gives a charge it runs. `None` while the target holds nothing (not set, load balancing off), cannot hold this
        charger (no current of its own, `_target_inert`) or held it only part of the way (`_target_floor_limited`), or
        the house's load is not known: the plan then counts on the car's
        own power, and a window the hold keeps short is planned again at its end (`planning/plan_watch.py`)."""
        target_kw = self._import_target_kw()
        if target_kw is None or not self._active_control_allowed():
            return None
        wiring = (self.config.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id) or {}
        if self._if_not_ready(charger_entry_id) == IF_NOT_READY_RELEASE:
            # The plan lets the departure win: it counts on the car's own power, and the target gives way when needed.
            return None
        switch_only = self._is_switch_only_charger(controller_for(self.hass, charger_entry_id))
        if (
            (not switch_only and not wiring.get(CONF_MEASURED_CURRENT_SOURCE))
            or charger_entry_id in self._target_inert
            or charger_entry_id in self._target_floor_limited
        ):
            # The hold cannot act on this charger (no current of its own to judge a lowering on, or a phase that cannot
            # be judged now): the plan counts on the car's own power, not on a hold that does not come.
            return None
        house_kw = self._typical_house_kw()
        if house_kw is None:
            return None
        # The target as set, less its margin as the hold keeps it (`held_level_kw`): the month's free level lifts the
        # running hour's hold only, and a plan's later hours may be counted by no part, a new day or a new month.
        phases, _phase = self.reckoned_phases(charger_entry_id)
        minimum_kw = member_w(phases, float(wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A))) / 1000.0
        return max(minimum_kw, held_level_kw(target_kw) - house_kw)

    def _target_start_cap(self, charger_entry_id: str) -> float | None:
        """The most a resume or a probe may give this charger for the import target: `0.0` while the target pauses
        it, the held amps while it holds it (no more than the hour's end keeps it at), `None` when the target holds
        nothing back."""
        share = self._target_shares.get(charger_entry_id)
        if share is None:
            return None
        if share.action == ACTION_PAUSED:
            return 0.0
        cap = share.cap_a if share.action == ACTION_HELD else None
        # The hour's end raises nothing the target held above where it keeps it, and a resume is a raise.
        locked = None if share.action == ACTION_UNHELD else self._target_raise_lock.get(charger_entry_id)
        if locked is not None and locked > 0:
            cap = locked if cap is None else min(cap, locked)
        return cap

    @staticmethod
    def _release_until(charger_controller: Any) -> str | None:
        """The departure a release would last until (the plan's, while ahead), else `None`: until the unplug."""
        plan = getattr(charger_controller, "plan", None)
        departure = None if plan is None or not plan.departure else dt_util.parse_datetime(plan.departure)
        return None if departure is None or departure <= dt_util.utcnow() else plan.departure

    def _release_peak_kw(self, charger_entry_id: str, charger_controller: Any, budget: HourBudget) -> float | None:
        """The hour's import a release may reach (kW, whole): the house's typical load (its load now while none is
        measured) and the charge's full power at the current it is set to."""
        amps = getattr(charger_controller, "requested_current_a", None)
        plan = getattr(charger_controller, "plan", None)
        if not amps and plan is not None:
            amps = plan.amps
        if not amps:
            return None
        phases, _phase = self.reckoned_phases(charger_entry_id)
        house_kw = self._typical_house_kw()
        if house_kw is None:
            house_kw = max(0.0, budget.house_w) / 1000.0
        return float(round(max(0.0, house_kw) + member_w(phases, float(amps)) / 1000.0))

    def import_target_status(self, charger_entry_id: str) -> ImportTargetStatus | None:
        """What the import target does to this charger now, as the status says it, or `None` while it does nothing
        (not set, or not applied this hour)."""
        budget = self._target_budget
        if budget is None:
            return None
        charger_controller = controller_for(self.hass, charger_entry_id)
        paused = (
            self._target_paused_hour.get(charger_entry_id) == budget.hour_start
            and charger_controller is not None
            and bool(getattr(charger_controller, "paused_by_balancing", False))
        )
        held_a: float | None = None
        if charger_entry_id in self._target_lowered and not paused:
            limit_a, cause = self.load_balancing_limit(charger_entry_id)
            if cause == LIMIT_CAUSE_IMPORT_TARGET:
                held_a = limit_a
        effective = self._target_effective
        return ImportTargetStatus(
            target_kw=budget.target_kw,
            set_kw=budget.target_kw if effective is None else effective.set_kw,
            hour_kw=round(budget.hour_kw, 2),
            held_a=held_a,
            paused_until=budget.hour_end if paused else None,
            released_for=self._target_released.get(charger_entry_id)
            or self._target_auto_released.get(charger_entry_id),
            house_over=budget.house_over,
            released_automatically=charger_entry_id not in self._target_released
            and charger_entry_id in self._target_auto_released,
            release_until=self._release_until(charger_controller),
            release_peak_kw=self._release_peak_kw(charger_entry_id, charger_controller, budget),
            at_risk=None if charger_entry_id in self._target_auto_released else self._target_risk.get(charger_entry_id),
        )

    def import_target_snapshot(self) -> dict[str, Any]:
        """The import target as the diagnostics show it: the hour's budget, or why it is not applied; what it does to
        each charger; the hour each was paused in, the departure it was set aside for, and its recent changes."""
        budget = self._target_budget
        return {
            "target_kw": self._import_target_kw(),
            # The level held to this hour: the target lifted to the month's free level (`free_kw`) where a part counts it.
            "effective": None
            if self._target_effective is None
            else {
                "kw": self._target_effective.kw,
                "free_kw": self._target_effective.free_kw,
                # What the hour is held under: the level less its margin, once.
                "hold_kw": round(self._target_effective.hold_kw, 3),
            },
            "not_applied": self._target_not_applied,
            "budget": None
            if budget is None
            else {
                "hour_start": budget.hour_start.isoformat(),
                # The level shown (the target, or the month's peak) and what the hour is held under.
                "target_kw": budget.target_kw,
                "hold_kw": round(budget.hold_kw, 3),
                "hour_kw": round(budget.hour_kw, 3),
                "so_far_kwh": round(budget.so_far_kwh, 3),
                "remaining_s": round(budget.remaining_s, 1),
                "house_w": round(budget.house_w),
                "allowed_w": round(budget.allowed_w),
                "house_over": budget.house_over,
            },
            "chargers": {
                charger_entry_id: {
                    "action": share.action,
                    "cap_a": share.cap_a,
                    "lowered": charger_entry_id in self._target_lowered,
                    "kept_at_hour_end": charger_entry_id in self._target_locked,
                    "inert": self._target_inert.get(charger_entry_id),
                    "floor_limited": charger_entry_id in self._target_floor_limited,
                    # Why a rule beyond the share decided (`slow_charger`), and the wait for its next current write.
                    "reason": share.reason
                    or (self._target_pause_reason(charger_entry_id, budget) if share.action == ACTION_PAUSED else None),
                    "write_wait_s": round(self._target_write_wait.get(charger_entry_id, 0.0), 1),
                }
                for charger_entry_id, share in self._target_shares.items()
            },
            "paused_hour": {key: value.isoformat() for key, value in self._target_paused_hour.items()},
            "released": dict(self._target_released),
            "released_by_plan": dict(self._target_auto_released),
            "at_risk": {
                charger_entry_id: {"departure": risk.departure, "missing_kwh": risk.missing_kwh, "choice": risk.choice}
                for charger_entry_id, risk in self._target_risk.items()
            },
            "log": self._history.target_log,
        }

    def charger_measured_current(self, charger_entry_id: str) -> DirectPhaseMeasurement | None:
        """One associated charger's raw measured current, for entities and diagnostics."""
        phase_wiring: dict[str, dict[str, Any]] = self.config.get(CONF_PHASE_WIRING) or {}
        return self._read_charger_measured_current(phase_wiring.get(charger_entry_id) or {})

    def grid_total_power(self) -> PhaseValue | None:
        """The meter's total grid power, live: signed (positive = import) with its freshness.

        `None` only when no total is configured (`CONF_GRID_POWER_SOURCE`); a configured one always
        returns a `PhaseValue`, even if currently unusable. An import/export pair is import minus
        export, and a missing or invalid half makes the whole value missing or invalid, never zero.
        `CONF_GRID_POWER_INVERTED` negates it for an export-positive meter. A total beyond what the main
        fuse could carry on all three phases is invalid (`implausible_power`). Used by the solar
        executor and diagnostics, and on a direct site only to confirm the sign of its per-phase currents
        (`site/current_sign.py`); never as amps by the fuse protection, which stays per phase on the
        measured currents.
        """
        source = grid_power_source_from_dict(self.config.get(CONF_GRID_POWER_SOURCE))
        if source is None:
            return None
        total = combine_power_pair(
            self._power_phase_value(source.power),
            self._power_phase_value(source.power_export) if source.power_export else None,
            invert=bool(self.config.get(CONF_GRID_POWER_INVERTED, False)),
        )
        main_fuse_a = self.config.get(CONF_MAIN_FUSE_A)
        if total.value is not None and implausible_power(
            total.value, float(main_fuse_a) if main_fuse_a is not None else None, len(PHASES)
        ):
            return PhaseValue(None, total.age_s, "invalid", report_age_s=total.report_age_s)
        return total

    def grid_total_reading(self) -> tuple[float | None, str]:
        """`(watts, state)` of the meter's total grid power, signed (positive = import).

        `state` is `not_configured`, `missing`, `invalid`, `stale` or, for a usable reading, `fresh` or
        `confirmed_unchanged` (as `classify_phase_liveness` grades the phases: a value Home Assistant
        stopped updating but keeps hearing from is accepted). A meter that reports too seldom for load
        balancing, or only on change, is accepted for longer (`solar_accepts`): solar and the dashboard
        read this, never the fuse protection. Watts only for a usable reading, so a stale or missing
        total is unknown, never zero export.
        """
        total = self.grid_total_power()
        if total is None:
            return None, "not_configured"
        if total.problem == "invalid":
            return None, "invalid"
        if total.problem is not None or total.value is None:
            return None, "missing"
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        source = grid_power_source_from_dict(self.config.get(CONF_GRID_POWER_SOURCE))
        if source is not None and self.solar_accepts(total, list(source.entity_ids)):
            # Graded at the maximum age for the state's name: `fresh` within it, else accepted as
            # unchanged (a slow or change-only meter, see `site/meter_cadence.py`).
            liveness = classify_phase_liveness(total.age_s, total.report_age_s, max_age_s)
            return total.value, "fresh" if liveness == "fresh" else "confirmed_unchanged"
        return None, "stale"

    def grid_power_snapshot(self) -> dict[str, Any]:
        """The total grid power's diagnostics for the site sensor and the diagnostics dump: whether
        and where it is configured, its value and age, and the export derived from it."""
        source = grid_power_source_from_dict(self.config.get(CONF_GRID_POWER_SOURCE))
        total = self.grid_total_power()
        value_w, state = self.grid_total_reading()
        return {
            "configured": source is not None,
            "power_entity_id": None if source is None else source.power,
            "power_export_entity_id": None if source is None else source.power_export,
            "inverted": bool(self.config.get(CONF_GRID_POWER_INVERTED, False)),
            "state": state,
            "value_w": value_w,
            "age_s": None if total is None else total.age_s,
            "export_w": None if value_w is None else max(0.0, -value_w),
            "used_for_surplus": source is not None
            and self.config.get(CONF_MEASUREMENT_MODE) == MEASUREMENT_MODE_DIRECT,
        }

    def battery_aggregate_power(self) -> PhaseValue | None:
        """This site's `battery_aggregate_power_entity` reading, live: signed (positive = charging)
        with its freshness.

        `None` only when no aggregate entity is configured; a configured one always returns a
        `PhaseValue`, even if currently unusable. Used by the solar controller; this controller's
        own calculation goes through `_read_battery_estimate`.
        """
        aggregate_entity_id = self.config.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY)
        if not aggregate_entity_id:
            return None
        discharge_entity_id = self.config.get(CONF_BATTERY_DISCHARGE_POWER_ENTITY)
        return combine_power_pair(
            self._power_phase_value(aggregate_entity_id),
            self._power_phase_value(discharge_entity_id) if discharge_entity_id else None,
            invert=bool(self.config.get(CONF_BATTERY_POWER_INVERTED, False)),
        )

    def _read_charger_measured_current(
        self, wiring: dict[str, Any]
    ) -> DirectPhaseMeasurement | None:
        """One charger's optional measured per-phase current. `None` when not configured, which is
        treated as crediting nothing. Any device can be mapped via the generic
        `PhaseMeasurementSource`.
        """
        source = source_from_dict(wiring.get(CONF_MEASURED_CURRENT_SOURCE))
        if source is None:
            return None
        values = read_phase_measurement(self.hass, source, classify_current)
        return DirectPhaseMeasurement(l1=values["L1"], l2=values["L2"], l3=values["L3"])

    def _stopped_charger_reading(
        self, charger_entry_id: str, wiring: dict[str, Any]
    ) -> DirectPhaseMeasurement | None:
        """A stopped charger's own reading, as the resume and the battery probe judge whether the car draws: a phase
        read before the last write to the charger (its pause, or the probe's start) shows the current before that
        write, and one not reported for longer than the site's maximum age says nothing of now. Both read 0 A. An
        OCPP charger sends no meter values while no transaction runs, so its current reads its last value, the car's
        draw before the pause, until it charges again: 6.02 A all night on 2026-11-01, which kept the paused charge
        from ever being resumed or probed (a reading frozen at 0 A, the night of 2026-10-09, did not).

        After a pause the charger is given `CATCH_UP_S` to stop, as after any write (`charger_above_written`): a phase
        reported within that of the pause is the charge before it. An OCPP charger sends one last meter value with the
        stop, a few seconds after it and still showing the car's current (8.07 A at 10:19:05 on 2026-11-01, the pause
        at 10:19:02), and then nothing: taken as the car drawing, it held the resume back until it was too old to use,
        two minutes."""
        delivered = self._read_charger_measured_current(wiring)
        if delivered is None:
            return None
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        written_at = self._written_at.get(charger_entry_id)
        if written_at is not None:
            damper = self._dampers.get(charger_entry_id)
            if damper is not None and damper.last_written_a == 0.0:
                written_at += CATCH_UP_S
        now = dt_util.utcnow().timestamp()

        def idle(value: PhaseValue) -> PhaseValue:
            if value.value is None or value.age_s is None:
                return value
            # The charger's last word on it: a value sent again unchanged after the write still holds.
            reported_age_s = value.age_s if value.report_age_s is None else min(value.age_s, value.report_age_s)
            before_write = written_at is not None and now - reported_age_s <= written_at
            # Stale as the charger's own reports say: a flat reading it keeps sending is alive.
            if before_write or reported_age_s > max_age_s:
                return replace(value, value=0.0)
            return value

        return DirectPhaseMeasurement(l1=idle(delivered.l1), l2=idle(delivered.l2), l3=idle(delivered.l3))

    # -- chargers that can only be switched on and off (`execution/switched_current.py`)

    def _switched_own_reading(self, charger_controller: Any, phases: int) -> tuple[float, float] | None:
        """`(amps, age_s)` of a switch-only charger's own draw: its current sensors' highest phase, else its smart
        plug's power over its phases at the nominal voltage; `None` with neither. The age is the oldest report of
        the entities read (taken as fresh where the charger's own path names none, an OCPP connector's)."""
        now = dt_util.utcnow()
        amps = charger_controller.measured_current_a()
        entity_ids: tuple[str, ...] = ()
        if amps is not None:
            adapter = charger_controller.adapter
            entity_ids = () if adapter.is_ocpp else tuple(getattr(adapter, "current_entity_ids", ()) or ())
        elif charger_controller.power_entity_id:
            amps, _source = measured_amps(current_a=None, power_w=charger_controller.measured_power_w(), phases=phases)
            entity_ids = (charger_controller.power_entity_id,)
        if amps is None:
            return None
        ages: list[float] = []
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None:
                return None
            ages.append(max(0.0, (now - state.last_reported).total_seconds()))
        return float(amps), max(ages, default=0.0)

    def _switched_measurement(
        self, charger_controller: Any, phases: int, phase: PhaseName | None, switched_a: float
    ) -> DirectPhaseMeasurement:
        """A switch-only charger's draw per phase where the site has no measured-current source for it: its own
        reading (`_switched_own_reading`), else, with nothing that reads it, its whole switched current while it
        charges and nothing while it does not (the safe side: on, it is taken to draw all of it). A single-phase
        charger on a chosen phase draws there alone; otherwise every phase it may use carries the draw."""
        reading = self._switched_own_reading(charger_controller, phases)
        if reading is None:
            amps, age_s = (switched_a if charger_controller.charging else 0.0), 0.0
        else:
            amps, age_s = reading
        drawn = PhaseValue(amps, age_s=age_s, report_age_s=age_s)
        idle = PhaseValue(0.0, age_s=age_s, report_age_s=age_s)
        if phases == 1 and phase is not None:
            return DirectPhaseMeasurement(
                l1=drawn if phase == "L1" else idle,
                l2=drawn if phase == "L2" else idle,
                l3=drawn if phase == "L3" else idle,
            )
        return DirectPhaseMeasurement(l1=drawn, l2=drawn, l3=drawn)

    def _switched_draw_a(self, charger_controller: Any, wiring: dict[str, Any], phases: int) -> float | None:
        """A switch-only charger's own draw now, in amps (its highest phase): the site's measured-current source for
        it on a fresh reading, else its own reading; `None` when nothing measures it."""
        delivered = self._read_charger_measured_current(wiring)
        if delivered is not None:
            max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
            values = [
                value.value
                for value in (delivered.get(p) for p in PHASES)
                if value.value is not None and value.problem is None and value.age_s is not None and value.age_s <= max_age_s
            ]
            return max(values) if values else None
        reading = self._switched_own_reading(charger_controller, phases)
        return None if reading is None else reading[0]

    def _net_site_a(self, charger_entry_id: str) -> dict[PhaseName, float | None]:
        """The site's measured phase currents less what the other chargers' own readings show them drawing."""
        net: dict[PhaseName, float | None] = dict(self.result.measured_phase_current_a)
        phase_wiring: dict[str, dict[str, Any]] = self.config.get(CONF_PHASE_WIRING) or {}
        for other in list(self.config.get(CONF_CHARGER_ENTRY_IDS) or []):
            if other == charger_entry_id:
                continue
            delivered = self._read_charger_measured_current(phase_wiring.get(other) or {})
            if delivered is None:
                continue
            for phase in PHASES:
                value = delivered.get(phase).value
                if value is not None and net.get(phase) is not None:
                    net[phase] = cast(float, net[phase]) - value
        return net

    def _switched_maximum_a(self, charger_controller: Any) -> float:
        """The most a switch-only charger may be learned to draw from a site step: its current setting's upper bound
        (the current-limit entity's `max`), else the main fuse, else `SWITCHED_FALLBACK_MAXIMUM_A`."""
        entity_id = getattr(charger_controller, "current_limit", None)
        state = self.hass.states.get(entity_id) if entity_id else None
        maximum = None if state is None else state.attributes.get("max")
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and maximum > 0:
            return float(maximum)
        fuse = self.config.get(CONF_MAIN_FUSE_A)
        if isinstance(fuse, (int, float)) and not isinstance(fuse, bool) and fuse > 0:
            return float(fuse)
        return SWITCHED_FALLBACK_MAXIMUM_A

    def _learn_switched_currents(self) -> None:
        """Feed each switch-only charger's learner (`SwitchedCurrent`) on this recompute: its own draw while it
        charges, or, with nothing that measures it, the site's step on an edge SpotNav made (a start or stop sent
        within `SWITCH_EDGE_COMMAND_S` before the charger was seen to change), from the net site reading of the
        recompute before the edge."""
        now = self._yield_now()
        utc_now = dt_util.utcnow()
        phase_wiring: dict[str, dict[str, Any]] = self.config.get(CONF_PHASE_WIRING) or {}
        for charger_entry_id in list(self.config.get(CONF_CHARGER_ENTRY_IDS) or []):
            charger_controller = controller_for(self.hass, charger_entry_id)
            if not self._is_switch_only_charger(charger_controller):
                self._switched_seen_charging.pop(charger_entry_id, None)
                self._switched_net_site.pop(charger_entry_id, None)
                continue
            learner = charger_controller.switched_current_learner
            phases, phase = self.reckoned_phases(charger_entry_id)
            charging = bool(charger_controller.charging)
            own = self._switched_draw_a(charger_controller, phase_wiring.get(charger_entry_id) or {}, phases)
            if own is not None:
                if charging:
                    learner.observe_draw(own)
                charger_controller.switched_current_observed()
                continue
            net = self._net_site_a(charger_entry_id)
            was = self._switched_seen_charging.get(charger_entry_id)
            before = self._switched_net_site.get(charger_entry_id)
            self._switched_seen_charging[charger_entry_id] = charging
            self._switched_net_site[charger_entry_id] = net
            wired: tuple[PhaseName, ...] = PHASES if phases == 3 else (() if phase is None else (phase,))
            command = charger_controller.last_switch_command
            if was is not None and was != charging and before is not None and wired and command is not None:
                kind = "on" if charging else "off"
                sent_s = (utc_now - command[1]).total_seconds()
                if command[0] == kind and 0.0 <= sent_s <= SWITCH_EDGE_COMMAND_S:
                    learner.begin_edge(
                        kind,
                        now=now,
                        phases=wired,
                        baseline=before,
                        maximum_a=self._switched_maximum_a(charger_controller),
                    )
            learner.observe_site(net, now=now)
            charger_controller.switched_current_observed()

    @property
    def capability_snapshot(self) -> SiteCapabilitySnapshot:
        """A capability/health snapshot for this site (`vehicles/capability.py`), built from the
        current config and requests.
        """
        charger_entry_ids: list[str] = list(self.config.get(CONF_CHARGER_ENTRY_IDS) or [])
        charge_control_map: dict[str, bool] = {}
        current_limit_map: dict[str, bool] = {}
        for charger_entry_id in charger_entry_ids:
            charger_controller = controller_for(self.hass, charger_entry_id)
            is_loaded = charger_controller is not None
            charge_control_map[charger_entry_id] = is_loaded
            current_limit_map[charger_entry_id] = bool(
                is_loaded and charger_controller.current_limit
            )
        return build_capability_snapshot(
            config=self._build_config(),
            requests=self._build_requests(),
            charger_has_charge_control=charge_control_map,
            charger_has_current_control=current_limit_map,
            # The apply path's own predicate, not `charger_has_current_control`: a current-limit
            # entity existing is not enough to be written to. A charger it can only pause counts: active
            # control acts on it too.
            charger_is_commandable={
                charger_entry_id: self._is_governed_charger(
                    controller_for(self.hass, charger_entry_id)
                )
                for charger_entry_id in charger_entry_ids
            },
            # Reported as-is, not folded into `active_available`.
            active_control_enabled=bool(self.config.get(CONF_ACTIVE_CONTROL_ENABLED, False)),
            membership_conflict_charger_ids=[
                conflict.charger_entry_id for conflict in self.membership_conflicts
            ],
            solar=solar_capability(
                self.config.get(CONF_MEASUREMENT_MODE), self.config.get(CONF_GRID_POWER_SOURCE)
            ),
            direction_never_known=self.direction_never_known,
        )

    @property
    def direction_never_known(self) -> bool:
        """Whether this site can never know a phase's direction: a direct site without both a signed current and the
        total grid power that confirms its sign (`site/current_sign.py`). Load balancing then only lowers an
        overload by |I| and never raises."""
        if self.config.get(CONF_MEASUREMENT_MODE) != MEASUREMENT_MODE_DIRECT:
            return False
        return not (
            self.config.get(CONF_SITE_CURRENT_SIGNED)
            and grid_power_source_from_dict(self.config.get(CONF_GRID_POWER_SOURCE)) is not None
        )

    def _charger_requested_current(self, charger_entry_id: str) -> float | None:
        """How much this charger asks to draw, for capacity purposes.

        `0.0` when it is off or not loaded; otherwise `ChargingController.resolve_current()`
        (SpotNav's request, else the current-limit entity's setpoint). `None` (unknown, never a
        guessed `0.0`) when it is charging and neither resolves, so the site calculation does not
        treat an unquantified load as requesting nothing.
        """
        charger_controller = controller_for(self.hass, charger_entry_id)
        if charger_controller is None:
            return 0.0
        if not charger_controller.charging and not charger_controller.start_pending:
            # Off, and no start of ours on its way: it asks for nothing. A start the charger has not answered yet
            # asks for what it was given, or the regulator would pause the start it is waiting for.
            return 0.0
        resolved = charger_controller.resolve_current()
        return float(resolved.amps) if resolved.amps is not None else None

    def _resolve_site_current_source(self) -> PhaseMeasurementSource | None:
        """The site's current measurement source: `CONF_SITE_CURRENT_SOURCE` if stored, else adapted
        from `CONF_DIRECT_ENTITIES` (separate entities with L1/L2/L3 keys). `None` when neither is
        configured.
        """
        signed = bool(self.config.get(CONF_SITE_CURRENT_SIGNED, False))
        stored = source_from_dict(self.config.get(CONF_SITE_CURRENT_SOURCE))
        if stored is not None:
            return replace(stored, signed_current=True) if signed else stored
        entities: dict[str, str] = self.config.get(CONF_DIRECT_ENTITIES) or {}
        if not entities:
            return None
        return PhaseMeasurementSource(
            kind="separate_entities", entity_ids=dict(entities), signed_current=signed
        )

    def phase_entities(self) -> dict[PhaseName, str | None]:
        """The entity each phase's measurement is read from, for naming the one that fails: the
        phase's own sensor in direct mode, its current (else apparent, else active power) sensor in
        derived mode, the one entity of a source that carries all three phases.
        """
        found: dict[PhaseName, str | None] = {"L1": None, "L2": None, "L3": None}
        if self.config.get(CONF_MEASUREMENT_MODE) == MEASUREMENT_MODE_DERIVED:
            entities: dict[str, dict[str, str]] = self.config.get(CONF_DERIVED_ENTITIES) or {}
            for phase in found:
                phase_entities = entities.get(phase) or {}
                found[phase] = (
                    phase_entities.get("current")
                    or phase_entities.get("apparent_power")
                    or phase_entities.get("power")
                    or None
                )
            return found
        source = self._resolve_site_current_source()
        if source is None:
            return found
        for phase in found:
            if source.kind == "attributes":
                found[phase] = source.entity_id
            else:
                found[phase] = (source.entity_ids or {}).get(phase)
        return found

    @property
    def measurement_problem(self) -> MeasurementProblem | None:
        """The phases that make the site's measurement unusable right now (`site/measurement_problem.py`)."""
        if self.result.state not in UNHEALTHY_STATES:
            return None
        entities = self.phase_entities()
        named = {entity_id for entity_id in entities.values() if entity_id}
        unavailable: set[str] = set()
        platforms: dict[str, str] = {}
        registry = er.async_get(self.hass)
        for entity_id in named:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                unavailable.add(entity_id)
            entry = registry.async_get(entity_id)
            if entry is not None:
                platforms[entity_id] = entry.platform
        negative: tuple[PhaseName, ...] = ()
        if self.config.get(CONF_MEASUREMENT_MODE) == MEASUREMENT_MODE_DIRECT:
            negative = negative_phases(self.hass, self._resolve_site_current_source())
        return measurement_problem(
            self.result, entities, unavailable=unavailable, platforms=platforms, negative=negative
        )

    @property
    def lasting_measurement_problem(self) -> MeasurementProblem | None:
        """`measurement_problem` for the card's status: only once the measurement has been unhealthy for
        `MEASUREMENT_LOST_GRACE_S`, the grace the regulator waits before it steps a charger down, so a meter that
        drops out for a few seconds (a Shelly reconnecting) shows nothing; at once when a phase reads a negative
        current on an unsigned site, a setting the person can change; gone as soon as the reading is back."""
        problem = self.measurement_problem
        if problem is None or problem.negative_phases:
            return problem
        since = self._measurement_unhealthy_since
        if since is None or self._yield_now() - since < MEASUREMENT_LOST_GRACE_S:
            return None
        return problem

    def _read_direct_entities(self) -> DirectPhaseMeasurement:
        source = self._resolve_site_current_source()
        if source is None:
            return DirectPhaseMeasurement(
                l1=PhaseValue(None, None, problem="missing"),
                l2=PhaseValue(None, None, problem="missing"),
                l3=PhaseValue(None, None, problem="missing"),
            )
        values = read_phase_measurement(self.hass, source, classify_current)
        return DirectPhaseMeasurement(l1=values["L1"], l2=values["L2"], l3=values["L3"])

    def _read_direct_signed_entities(self) -> DirectPhaseMeasurement | None:
        """The site's per-phase current with its sign (+ import, - export), or `None` unless the source is
        `site_current_signed`. Only a direction once `CurrentSignCheck` confirms a phase."""
        if not bool(self.config.get(CONF_SITE_CURRENT_SIGNED, False)):
            return None
        source = self._resolve_site_current_source()
        if source is None:
            return None
        values = read_phase_measurement(self.hass, replace(source, signed_current=False), classify_signed_current)
        return DirectPhaseMeasurement(l1=values["L1"], l2=values["L2"], l3=values["L3"])

    def _read_derived_entities(self) -> DerivedPhaseMeasurement:
        entities: dict[str, dict[str, str]] = self.config.get(CONF_DERIVED_ENTITIES) or {}
        invert = bool(self.config.get(CONF_GRID_POWER_INVERTED, False))
        signed_current = bool(self.config.get(CONF_SITE_CURRENT_SIGNED, False))
        nominal_phase_voltage_v = stored_voltage_between_phases_v(self.config) / math.sqrt(3)

        def build(phase: str) -> DerivedPhaseInput:
            phase_entities = entities.get(phase) or {}
            export_id = phase_entities.get("power_export")
            reactive_id = phase_entities.get("reactive_power")
            apparent_id = phase_entities.get("apparent_power")
            current_id = phase_entities.get("current")
            voltage_id = phase_entities.get("voltage")
            # A phase with its own current needs no voltage entity (a delta meter has none): the site's
            # nominal phase voltage stands in, fresh by definition.
            nominal = not voltage_id and bool(current_id)
            return DerivedPhaseInput(
                active_power_w=combine_power_pair(
                    self._power_phase_value(phase_entities.get("power")),
                    self._power_phase_value(export_id) if export_id else None,
                    invert=invert,
                ),
                # Reactive power keeps the grid meter's sign convention, like active power.
                reactive_power_var=(
                    combine_power_pair(self._power_phase_value(reactive_id), invert=invert)
                    if reactive_id
                    else None
                ),
                voltage_v=(
                    PhaseValue(nominal_phase_voltage_v, 0.0, report_age_s=0.0)
                    if nominal
                    else self._voltage_phase_value(voltage_id)
                ),
                voltage_nominal=nominal,
                apparent_power_va=(
                    self._apparent_phase_value(apparent_id) if apparent_id else None
                ),
                current_a=(
                    self._current_phase_value(current_id, signed=signed_current)
                    if current_id
                    else None
                ),
            )

        return DerivedPhaseMeasurement(l1=build("L1"), l2=build("L2"), l3=build("L3"))

    def _power_phase_value(self, entity_id: str | None) -> PhaseValue:
        state = self._state_or_none(entity_id)
        if state is None:
            return PhaseValue(None, None, problem="missing")
        value, problem = classify_power(state.state, state.attributes.get("unit_of_measurement"))
        return PhaseValue(value, self._age_s(state), problem, report_age_s=self._report_age_s(state))

    def _apparent_phase_value(self, entity_id: str | None) -> PhaseValue:
        state = self._state_or_none(entity_id)
        if state is None:
            return PhaseValue(None, None, problem="missing")
        value, problem = classify_apparent_power(
            state.state, state.attributes.get("unit_of_measurement")
        )
        return PhaseValue(value, self._age_s(state), problem, report_age_s=self._report_age_s(state))

    def _current_phase_value(self, entity_id: str | None, *, signed: bool) -> PhaseValue:
        state = self._state_or_none(entity_id)
        if state is None:
            return PhaseValue(None, None, problem="missing")
        value, problem = classify_current(
            state.state, state.attributes.get("unit_of_measurement"), signed=signed
        )
        return PhaseValue(value, self._age_s(state), problem, report_age_s=self._report_age_s(state))

    def _voltage_phase_value(self, entity_id: str | None) -> PhaseValue:
        state = self._state_or_none(entity_id)
        if state is None:
            return PhaseValue(None, None, problem="missing")
        value, problem = classify_voltage(state.state, state.attributes.get("unit_of_measurement"))
        return PhaseValue(value, self._age_s(state), problem, report_age_s=self._report_age_s(state))

    def _state_or_none(self, entity_id: str | None):
        if not entity_id:
            return None
        return self.hass.states.get(entity_id)

    def _age_s(self, state: Any) -> float:
        return (dt_util.utcnow() - state.last_updated).total_seconds()

    def _report_age_s(self, state: Any) -> float | None:
        """Seconds since HA last heard from this entity, changed or not (`last_reported`); `None` if
        unsupported. Feeds `site_capacity.classify_phase_liveness`.
        """
        last_reported = getattr(state, "last_reported", None)
        if last_reported is None:
            return None
        return (dt_util.utcnow() - last_reported).total_seconds()


#: How long the hour tracker's clock (`SiteCapacityController.demand_zone`) is kept before it is resolved again.
_DEMAND_ZONE_TTL_S = 300.0


def _home_zone(hass: Any) -> str:
    """Home Assistant's own time zone, `UTC` when it names none."""
    zone = getattr(getattr(hass, "config", None), "time_zone", None)
    return zone if isinstance(zone, str) and zone else "UTC"


def charge_still_wanted(hass: HomeAssistant, charger_entry_id: str, charger_controller: Any) -> bool:
    """Whether a charge balancing paused is still wanted (`SiteCapacityController._charge_still_wanted`); when it is
    not, the mark is dropped (`forget_balancing_pause`)."""
    store = domain_data(hass).auto_store
    settings = None if store is None else store.settings(charger_entry_id)
    if settings is None or not settings.pause.admitted:
        return True
    pause = settings.pause
    persons_stop = pause.manual and pause.action == MANUAL_STOP
    persons_charge = getattr(charger_controller, "paused_charge_origin", None) == "manual" or (
        pause.manual and pause.action == MANUAL_START
    )
    if persons_charge and not persons_stop:
        return True
    charger_controller.forget_balancing_pause()
    return False


def _held_still_on(result: SiteCapacityResult, phases: Sequence[str]) -> bool:
    """Whether a phase among `phases` counts as fresh only because its voltage or another reading moves while its
    active power sits still (`phase_signed_active_power_held_still`): never a reason to start or raise a charger."""
    held = result.phase_signed_active_power_held_still
    return any(held.get(phase) is not None for phase in phases)  # type: ignore[call-overload]


def _plugged_in_for_resume(adapter: Any) -> bool:
    """Whether a charge load balancing paused (for the fuse or the import target) may be resumed as far as the plug
    goes: a charger that can say whether a car is plugged in must say one is; one that cannot (a plain switch, an
    OCPP charger whose connector status is not found, a status sensor on a platform with no mapping:
    `reports_connection` false) is resumed while the charge is still wanted, or a pause would hold it off for good."""
    connected = adapter.vehicle_connected()
    if connected is not None:
        return connected is True
    return not getattr(adapter, "reports_connection", True)


async def async_release_held_charges(hass: HomeAssistant, charger_entry_ids: Sequence[str]) -> None:
    """Load balancing governs these chargers no more (turned off, a charger taken off its site, the site removed), so
    no regulator will resume a charge it holds back: one still wanted is given back as the regulator's resume gives it
    back (`async_battery_probe_start`, at the current on record, with no site left to cap it), one no longer wanted is
    forgotten. Not at an unload: a reload's controller and the next start of Home Assistant resume it themselves."""
    for charger_entry_id in charger_entry_ids:
        charger_controller = controller_for(hass, charger_entry_id)
        if charger_controller is None or not charger_controller.paused_by_balancing:
            continue
        if not charge_still_wanted(hass, charger_entry_id, charger_controller):
            continue
        requested = charger_controller.requested_current_a
        if (
            requested is None
            or charger_controller.held_by_charger
            or charger_controller.adapter.vehicle_connected() is False
            or charger_controller.car_ended_holds_charge
        ):
            continue
        try:
            started = await charger_controller.async_battery_probe_start(requested)
        except Exception:  # noqa: BLE001 - the others are still given back
            _LOGGER.warning(
                "SpotNav charger %s: the charge load balancing held back could not be given back", charger_entry_id,
                exc_info=True,
            )
            continue
        if started:
            _LOGGER.info(
                "SpotNav charger %s: load balancing governs it no more; the charge it held back goes on", charger_entry_id
            )


def _source_entity_ids(source: PhaseMeasurementSource) -> set[str]:
    """Every entity a `PhaseMeasurementSource` reads from."""
    if source.kind == "attributes":
        return {source.entity_id} if source.entity_id else set()
    return set((source.entity_ids or {}).values())


def _log_transitions(
    entry_id: str, previous: SiteCapacityResult, current: SiteCapacityResult
) -> None:
    """Debug-log state and allocation transitions, only on change and without entity ids or raw
    measurements.
    """
    if previous.state != current.state:
        _LOGGER.debug(
            "SpotNav site %s: state changed %s -> %s (%s)",
            entry_id,
            previous.state,
            current.state,
            current.reason,
        )
    previous_by_id = {a.charger_entry_id: a for a in previous.allocations}
    for allocation in current.allocations:
        prior = previous_by_id.get(allocation.charger_entry_id)
        if prior is None or prior.state != allocation.state:
            _LOGGER.debug(
                "SpotNav site %s charger %s: allocation state changed to %s (%s), proposed=%sA",
                entry_id,
                allocation.charger_entry_id,
                allocation.state,
                allocation.reason,
                allocation.proposed_current_a,
            )


def _log_membership_conflicts(
    entry_id: str,
    previous: list[SiteMembershipConflict],
    current: list[SiteMembershipConflict],
) -> None:
    """Warn once when a duplicate-membership conflict appears or changes; the apply step refuses
    while any conflict exists.
    """
    if previous == current:
        return
    if not current:
        _LOGGER.info("SpotNav site %s: duplicate-membership conflict cleared", entry_id)
        return
    for conflict in current:
        _LOGGER.warning(
            "SpotNav site %s: charger %s is also claimed by site %s -- observation continues, "
            "but this must be resolved before active control can ever be enabled",
            entry_id,
            conflict.charger_entry_id,
            conflict.conflicting_site_entry_id,
        )


def _log_regulator_decisions(
    entry_id: str,
    previous: dict[str, RegulatorDecision],
    current: dict[str, RegulatorDecision],
) -> None:
    """Debug-log a regulator decision only when its proposed current or reason changed.

    Carries the basis numbers (signed power, measured current and their ages) so a decision can be
    compared with Recorder history later. A second line lists every phase's basis, not only the
    limiting one, since a multi-phase proposal depends on all of them.
    """
    for charger_entry_id, decision in current.items():
        prior = previous.get(charger_entry_id)
        if (
            prior is not None
            and prior.reason == decision.reason
            and prior.proposed_current_a == decision.proposed_current_a
        ):
            continue
        limiting_basis = decision.basis.get(decision.limiting_phase) if decision.limiting_phase else None
        _LOGGER.debug(
            "SpotNav site %s regulator: charger %s proposed=%sA (%s) limiting_phase=%s "
            "signed_active_power_w=%s (age=%ss) measured_current_a=%s (age=%ss)",
            entry_id,
            charger_entry_id,
            decision.proposed_current_a,
            decision.reason,
            decision.limiting_phase,
            limiting_basis.signed_active_power_w if limiting_basis else None,
            limiting_basis.signed_active_power_age_s if limiting_basis else None,
            limiting_basis.measured_current_a if limiting_basis else None,
            limiting_basis.measured_current_age_s if limiting_basis else None,
        )
        # Per-phase basis line for every phase the charger uses, in sorted order so the log stays
        # comparable.
        phases_logged = " ".join(
            (
                f"{phase}=[signed_active_power_w={entry.signed_active_power_w}"
                f" age={entry.signed_active_power_age_s}"
                f" measured_current_a={entry.measured_current_a}"
                f" age={entry.measured_current_age_s}"
                f" requested_current_a={entry.requested_current_a}"
                f" direction={entry.direction}"
                f" confirmed_direction={entry.confirmed_direction}]"
            )
            for phase, entry in sorted(decision.basis.items())
        )
        _LOGGER.debug(
            "SpotNav site %s regulator: charger %s per-phase basis (all phases): %s",
            entry_id,
            charger_entry_id,
            phases_logged,
        )
