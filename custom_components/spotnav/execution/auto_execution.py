"""Auto execution: one serialized authority boundary per charger.

Turns a calculated proposal into an installed `ChargingPlan` through the charging controller and
serializes everything that can happen to a charger.

* One lock per charger covers Auto application, manual actions, pause/resume and shutdown; every
  fact that may have changed during a calculation is re-read inside it.
* Every calculation claims a rising attempt id; one that finds a newer attempt inside the boundary
  applies nothing, so a late older calculation is inert.
* The plan is the record: an installed Auto plan carries its identity, settings revision and price
  identity, so applied state is read back from the charger's plan.
* An identical application is not reinstalled, and a re-description of the same charge (different
  cost or calculation time) is not a change.
* A charging window is never cut short; a newer proposal waits for its boundary.

No service calls here: every charger command goes through `ChargingController`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Final

from homeassistant.core import callback, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_point_in_time, async_track_time_interval
from homeassistant.util import dt as dt_util

from ..planning.auto_settings import (
    AutoSettings,
    AutoSettingsError,
    AutoSettingsStore,
    DRIVER_TARGET_SOC,
    MANUAL_SCOPE_NEXT_PLUG_IN,
    MANUAL_SCOPE_PLUG_IN,
    MANUAL_START,
    MANUAL_STOP,
    PAUSE_MANUAL,
    PAUSE_NEXT_PERIOD,
    PAUSE_UNTIL_RESUMED,
    PAUSE_UNTIL_TOMORROW,
    PauseChoice,
    PauseIntent,
    STRATEGY_HYBRID,
    STRATEGY_SOLAR,
)
from ..planning.phases import effective_phases
from .controller import (
    AUTOMATIC_BALANCING_RESUME,
    AUTOMATIC_PERSON_RESUME,
    AUTOMATIC_STOP,
    ChargingController,
    ChargingExecutionError,
    ChargingPlan,
)
from .manual_pause import ManualChargeWatch
from ..core import events as core_events
from .ownership_shadow import CommandOutcome, INTENT, NullShadow, OwnershipShadow


_LOGGER = logging.getLogger(__name__)


#: What a charger's execution is doing, as one stable fact (about the charger, unlike
#: `AutoSnapshot.state`, which is about the proposal).
EXECUTION_NOT_APPLIED: Final = "not_applied"
EXECUTION_SCHEDULED: Final = "scheduled"
EXECUTION_ACTIVE: Final = "active"
EXECUTION_PAUSED: Final = "paused"
EXECUTION_COMPLETE: Final = "complete"
EXECUTION_PENDING: Final = "apply_pending"
EXECUTION_ERROR: Final = "execution_error"

#: Stable execution error codes; a caller gets a code to act on, not a message.
EXECUTION_INSTALL_FAILED: Final = "install_failed"
EXECUTION_PLAN_UNUSABLE: Final = "plan_unusable"
#: A pause that could not stop the plan it had paused; reported instead of a clean
#: `paused` because the charger may still be charging.
EXECUTION_PAUSE_STOP_FAILED: Final = "pause_stop_failed"
#: An expired pause whose clearing write could not be persisted: the stored intent still
#: stands, and a retry appointment is armed.
EXECUTION_PAUSE_CLEAR_FAILED: Final = "pause_clear_failed"
#: A committed pause clear whose reconciliation failed. The clear cannot be rolled back,
#: so the resume is real and the plan behind it is not yet settled.
EXECUTION_RECONCILE_FAILED: Final = "reconcile_failed"
#: A strategy change to `solar` while a `cheapest` plan Auto installed was running, and stopping
#: the charger to clear it failed.
EXECUTION_SOLAR_STAND_DOWN_FAILED: Final = "solar_stand_down_failed"

#: The one manual-action vocabulary: a control description and the action command that
#: carries it out use the same names, so a client renders what the backend named.
ACTION_START: Final = "start"
ACTION_STOP: Final = "stop"
ACTION_RESUME: Final = "resume"
ACTION_NONE: Final = "none"

#: The two product axes: *immediate* (what the charger may do now, one `start` or `stop`, never touching
#: planning mode, and pausing Auto for the plug-in session as the person's own pause, `manual`) and
#: *automatic* (what may happen to Home Assistant's automatic execution, one `pause` with its choice or one
#: `resume`).
ACTION_PAUSE: Final = "pause"
IMMEDIATE_ACTIONS: Final = (ACTION_START, ACTION_STOP)

#: How long a manual Start stays "just sent" before the boundary stops waiting for the charger to
#: report charging.
MANUAL_START_ACK_TIMEOUT: Final = timedelta(seconds=30)
#: How long (seconds) after a person's pause could not be saved the settings file is written again.
PAUSE_SAVE_RETRY_S: Final = 60.0

#: How often a person's charge under a manual pause is looked at for the car ending it (`ManualChargeWatch`):
#: a car at 0 A reports nothing new.
MANUAL_WATCH_INTERVAL: Final = timedelta(seconds=30)

#: Stable reasons a control description carries when there is no action.
CONTROL_NO_SETTINGS: Final = "no_settings"
CONTROL_PAUSE_UNSETTLED: Final = "pause_unsettled"
#: A manual Start was accepted and the charger has not yet reported charging: we do not
#: know yet, so no second Start.
CONTROL_ACTION_PENDING: Final = "action_pending"

#: Stable codes for an action that is not admissible now, or failed before any effect. (A
#: failure after the effect is `EXECUTION_RECONCILE_FAILED`.)
EXECUTION_ACTION_UNAVAILABLE: Final = "action_unavailable"
EXECUTION_ACTION_FAILED: Final = "action_failed"
#: A Start refused because the charger says no car is plugged in: nothing was sent and nothing paused.
EXECUTION_VEHICLE_NOT_CONNECTED: Final = "vehicle_not_connected"


class AutoControlError(Exception):
    """A manual action that did not happen, carrying a stable code and never any prose."""

    code: str

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code if not message else f"{code}: {message}")
        self.code = code


class AutoControlRefused(AutoControlError):
    """The action was not admissible, or failed before anything changed.

    No write, revision, pause, plan or charger command happened. The code says which:
    `EXECUTION_ACTION_UNAVAILABLE` (not the current action), the domain's own code, or
    `EXECUTION_ACTION_FAILED` (something operational failed first).
    """


class AutoControlCommitted(AutoControlError):
    """The action's effect is already in the record and the follow-up failed."""


@dataclass(frozen=True, slots=True)
class ControlFacts:
    """One moment's control facts, captured once."""

    #: Whether the charger is charging now, from Home Assistant's state for the charge control.
    charging: bool
    #: The persisted pause intent.
    pause: PauseIntent
    #: Whether a settings record exists; without one there is nothing to act on.
    has_settings: bool
    #: Whether the pause is in force at this instant (the time condition, not the record).
    paused: bool
    #: The boundary's last failure code, or `None`.
    last_error: str | None
    #: The pause choices this charger can honour now, in the product's order.
    pause_choices: tuple[PauseChoice, ...]
    #: A manual Start was accepted and the charger has not yet reported charging.
    start_pending: bool = False


@dataclass(frozen=True, slots=True)
class ControlDecision:
    """The one action that is valid right now, and why when there is none."""

    action: str
    reason: str | None
    choices: tuple[PauseChoice, ...]

    def admits(self, action: str, choice: PauseChoice | None) -> bool:
        """Whether this decision admits the requested action, choice included."""
        if action != self.action:
            return False
        if action != ACTION_STOP:
            return choice is None
        return choice in self.choices


@dataclass(frozen=True, slots=True)
class ImmediateDecision:
    """What the charger may be told to do now: `start`, `stop`, or nothing."""

    action: str
    reason: str | None

    def admits(self, action: str) -> bool:
        """Whether this decision admits the requested immediate command."""
        return action == self.action and action in IMMEDIATE_ACTIONS


@dataclass(frozen=True, slots=True)
class AutomaticDecision:
    """What may happen to Home Assistant's automatic execution: `pause`, `resume`, or nothing.

    `choices` is what the pause accepts now and is meaningful only beside a `pause`.
    """

    action: str
    reason: str | None
    choices: tuple[PauseChoice, ...]

    def admits(self, action: str, choice: PauseChoice | None = None) -> bool:
        """Whether this decision admits the requested automatic action, choice included.

        A pause with no choice is refused here rather than resolved into a choice nobody asked for.
        """
        if action == ACTION_PAUSE:
            return self.action == ACTION_PAUSE and choice in self.choices
        if action == ACTION_RESUME:
            return self.action == ACTION_RESUME and choice is None
        return False


def decide_immediate(facts: ControlFacts) -> ImmediateDecision:
    """The immediate command that is truthful right now, and only that.

    Blind to the planning axes (mode, stored pause, plan): a command to the charger is answered by its
    own state. A person can start charging while Auto is paused, and stop a charge Auto installed; either
    pauses Auto for the plug-in session (`AutoExecutor._manual_start_locked`, `_immediate_stop_locked`).

    * no settings record: nothing to command yet;
    * a Start still awaiting the charger's report: nothing, with `action_pending`;
    * otherwise the reported state: charging means `stop`, else `start`.
    """
    if not facts.has_settings:
        return ImmediateDecision(ACTION_NONE, CONTROL_NO_SETTINGS)
    if facts.start_pending:
        return ImmediateDecision(ACTION_NONE, CONTROL_ACTION_PENDING)
    if facts.charging:
        return ImmediateDecision(ACTION_STOP, None)
    return ImmediateDecision(ACTION_START, None)


@dataclass(frozen=True, slots=True)
class ControlAxes:
    """Both axes, answered from one `ControlFacts` value and no other."""

    immediate: ImmediateDecision
    automatic: AutomaticDecision


def decide_axes(facts: ControlFacts) -> ControlAxes:
    """Both axes over exactly [facts]: the paired read from one already-captured snapshot,
    for callers that hold facts of their own (the dashboard capture).
    """
    return ControlAxes(decide_immediate(facts), decide_automatic(facts))


def decide_automatic(facts: ControlFacts) -> AutomaticDecision:
    """What may happen to Auto's own execution right now, from the planning record alone.

    Precedence:

    * no settings record: nothing is configurable yet;
    * a manual pause (a person's Start or Stop): only `resume` is offered, whatever else happened (the
      person's own Stop is the retry of a stop that failed, on the immediate axis), except nothing while
      the person's Start awaits the charger's report;
    * a failed pause stop: re-sending the same choice is the documented retry;
    * a pause whose clearing write failed: still persisted, nothing to resume; the code says why;
    * any other stored pause: only an explicit `resume` is offered;
    * a manual Start awaiting acknowledgement: nothing, with `action_pending`;
    * otherwise `pause` with the choices it can honour, including for an idle charger (a pause is a
      stored intent that blocks the next application).
    """
    if not facts.has_settings:
        return AutomaticDecision(ACTION_NONE, CONTROL_NO_SETTINGS, ())
    if facts.pause.manual:
        if facts.start_pending:
            return AutomaticDecision(ACTION_NONE, CONTROL_ACTION_PENDING, ())
        return AutomaticDecision(ACTION_RESUME, None, ())
    if facts.pause.admitted:
        if facts.last_error == EXECUTION_PAUSE_STOP_FAILED:
            return AutomaticDecision(ACTION_PAUSE, None, facts.pause_choices)
        if facts.last_error == EXECUTION_PAUSE_CLEAR_FAILED:
            return AutomaticDecision(ACTION_NONE, EXECUTION_PAUSE_CLEAR_FAILED, ())
        if facts.paused:
            return AutomaticDecision(ACTION_RESUME, None, ())
        return AutomaticDecision(ACTION_NONE, CONTROL_PAUSE_UNSETTLED, ())
    if facts.start_pending:
        return AutomaticDecision(ACTION_NONE, CONTROL_ACTION_PENDING, ())
    return AutomaticDecision(ACTION_PAUSE, None, facts.pause_choices)


#: How long after a missed or failed expiry the boundary waits before settling the pause
#: again: one appointment, never an immediate re-fire that would spin.
PAUSE_RETRY_DELAY: Final = timedelta(seconds=60)


def pause_blocks_execution(settings: AutoSettings) -> bool:
    """Whether the persisted settings record still forbids every Auto application.

    The one execution gate, and not a clock question: the pause's time condition ends at the stored
    instant (`AutoExecutor.paused`), but the block ends only when the clearing write has committed. An
    expired pause whose clear failed (`pause_clear_failed`) keeps this true. Every install path checks
    it under the lock.
    """
    return settings.pause.admitted


def auto_application_identity(
    *,
    periods: tuple[tuple[str, str], ...],
    amps: int,
    phases: int,
    area_id: str | None,
    requested_kwh: float,
    delivered_kwh: float,
    unpriced: bool,
    target_soc_percent: float | None,
    vehicle_id: str | None,
    driver: str,
    settings_revision: int,
    price_identity: str | None,
    to_vehicle_limit: bool = False,
) -> str:
    """A deterministic identity for one Auto application, over execution-relevant facts: ordered
    bounds, current, phases, area, energy, unpriced flag, target fields, driver, settings revision
    and price identity, and whether the car ends the charge itself (named only when it does, so every
    other identity stays what it was).
    """
    extra: dict[str, Any] = {"to_vehicle_limit": True} if to_vehicle_limit else {}
    canonical = json.dumps(
        {
            **extra,
            "periods": [[start, end] for start, end in periods],
            "amps": amps,
            "phases": phases,
            "area": area_id,
            "requested_kwh": round(requested_kwh, 6),
            "delivered_kwh": round(delivered_kwh, 6),
            "unpriced": unpriced,
            "target_soc_percent": target_soc_percent,
            "vehicle_id": vehicle_id,
            "driver": driver,
            "settings_revision": settings_revision,
            "price_identity": price_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class AutoApplication:
    """What Auto decided to install, and every fact that decided it."""

    identity: str
    plan: ChargingPlan
    settings_revision: int
    price_identity: str | None

    @property
    def periods(self) -> tuple[tuple[str, str], ...]:
        """The ordered period bounds, exactly as the plan states them."""
        return tuple((start.isoformat(), end.isoformat()) for start, end in self.plan.windows)

    @property
    def material_key(self) -> tuple[Any, ...]:
        """What the charger would do: hours, current, phases, area, target, unpriced flag, and whether the
        car ends the charge itself."""
        return (
            self.periods,
            self.plan.amps,
            self.plan.phases,
            self.plan.price_area,
            self.plan.unpriced,
            self.plan.target_soc_percent,
            self.plan.vehicle_id,
            self.plan.to_vehicle_limit,
        )


def manual_to_vehicle_limit(settings: AutoSettings, snapshot: Any) -> bool:
    """A manual amount Auto capped at the room left in the battery (`AutoSnapshot.room_limited`): the car
    ends that charge itself when it is full."""
    return settings.driver != DRIVER_TARGET_SOC and bool(getattr(snapshot, "room_limited", False))


def auto_plan_for(
    settings: AutoSettings, snapshot: Any, application_identity: str, phases: int = 3
) -> ChargingPlan:
    """The typed, Auto-owned plan for one usable proposal. Exact mapping, nothing invented.

    A target is carried only on the target-SoC path with both a vehicle and a percentage: an invented
    one would stop a charge nobody chose to stop, an omitted one would never stop it. `unpriced` is
    true exactly when charged without published prices.
    """
    proposal = snapshot.proposal
    periods = [
        {"start": start.isoformat(), "end": end.isoformat()} for start, end in proposal.periods
    ]
    target = settings.target
    carries_target = settings.driver == DRIVER_TARGET_SOC and target.target_percent is not None
    return ChargingPlan(
        to_vehicle_limit=manual_to_vehicle_limit(settings, snapshot),
        start=periods[0]["start"],
        end=periods[-1]["end"],
        amps=settings.amps if settings.amps is not None else 0,
        phases=phases,
        energy_kwh=proposal.requested_kwh,
        price_area=snapshot.area_id,
        unpriced=bool(proposal.unpriced),
        periods=periods,
        target_soc_percent=float(target.target_percent) if carries_target else None,
        vehicle_id=target.vehicle_id if carries_target else None,
        departure=_departure_text(getattr(snapshot, "departure_at", None)),
        auto_identity=application_identity,
        auto_settings_revision=settings.revision,
        auto_price_identity=snapshot.price_identity,
    )


def _departure_text(departure: Any) -> str | None:
    """The departure a plan is for, as the plan stores it (an ISO instant), or `None`."""
    return departure.isoformat() if isinstance(departure, datetime) else None


def application_from_plan(plan: ChargingPlan) -> AutoApplication | None:
    """The application an Auto-owned installed plan represents, or `None` (never for a plan
    without an Auto identity).
    """
    if not plan.auto_owned:
        return None
    return AutoApplication(
        identity=plan.auto_identity or "",
        plan=plan,
        settings_revision=plan.auto_settings_revision if plan.auto_settings_revision is not None else -1,
        price_identity=plan.auto_price_identity,
    )


def application_for(settings: AutoSettings, snapshot: Any, phases: int = 3) -> AutoApplication | None:
    """The application one snapshot describes, or `None` if not usable to execute: a proposal
    must be ready (or unpriced) and exist, and the settings must carry the current and phases
    a plan needs. `phases` is the charge's effective phases (`planning/phases.py`).
    """
    if snapshot is None or snapshot.proposal is None:
        return None
    if snapshot.state not in ("proposal_ready", "proposal_unpriced"):
        return None
    if snapshot.last_error_code is not None:
        # A snapshot carrying a stable error is not executable: a proposal whose durable summary
        # could not be written is shown but never installed.
        return None
    if settings.amps is None or phases not in (1, 3) or not settings.area_id:
        return None
    proposal = snapshot.proposal
    periods = tuple((start.isoformat(), end.isoformat()) for start, end in proposal.periods)
    if not periods:
        return None
    target = settings.target
    carries_target = settings.driver == DRIVER_TARGET_SOC and target.target_percent is not None
    identity = auto_application_identity(
        to_vehicle_limit=manual_to_vehicle_limit(settings, snapshot),
        periods=periods,
        amps=settings.amps,
        phases=phases,
        area_id=snapshot.area_id,
        requested_kwh=proposal.requested_kwh,
        delivered_kwh=proposal.delivered_kwh,
        unpriced=bool(proposal.unpriced),
        target_soc_percent=float(target.target_percent) if carries_target else None,
        vehicle_id=target.vehicle_id if carries_target else None,
        driver=settings.driver,
        settings_revision=settings.revision,
        price_identity=snapshot.price_identity,
    )
    return AutoApplication(
        identity=identity,
        plan=auto_plan_for(settings, snapshot, identity, phases),
        settings_revision=settings.revision,
        price_identity=snapshot.price_identity,
    )


@dataclass(frozen=True, slots=True)
class PendingApplication:
    """A material change waiting for the boundary of a window that is charging now.

    Carries its attempt, settings revision and price identity, so the boundary can prove it is still
    the current proposal; without them a change could be installed after prices or settings moved on.
    """

    application: AutoApplication
    attempt: int
    settings_revision: int
    price_identity: str | None


class AutoExecutor:
    """The one serialized authority and application boundary for one charger.

    Everything that can install, replace, pause, resume or cancel the plan runs inside `self._lock`;
    `self._attempt` makes late calculations inert. Applied state is read back from the controller's
    plan. `pending` (a proposal waiting for a window boundary) lives only in memory; a restart resolves
    it by recalculating.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        controller: ChargingController,
        store: AutoSettingsStore,
        *,
        live_snapshot: Callable[[], Any] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._hass = hass
        self._controller = controller
        self._store = store
        self._entry_id = controller.entry_id
        # Injected rather than imported: the boundary re-reads the preview's live price and
        # proposal identity through this seam without depending on the preview controller.
        self._live_snapshot = live_snapshot
        self._now: Callable[[], datetime] = now if now is not None else dt_util.utcnow
        self._lock = asyncio.Lock()
        self._attempt = 0
        self._shutdown = False
        self._applied: AutoApplication | None = None
        self._pending: PendingApplication | None = None
        self._last_error: str | None = None
        self._cancel_listener: Callable[[], None] | None = None
        # The one appointment at which a bounded pause ends; cancelled by `async_shutdown` and
        # re-armed from the stored intent on restore (`async_restore_pause`).
        self._cancel_pause: Callable[[], None] | None = None
        # The next try to write a person's pause that memory holds ahead of the file (`_store_manual_pause`).
        self._cancel_save_retry: Callable[[], None] | None = None
        # A manual Start accepted and not yet answered by the charger's state. Not a source of
        # charger truth: it says a command is outstanding, and the two handles below end it.
        self._start_pending = False
        # Whether the recorded acknowledgement failure is this state machine's own (see
        # `_recover_start_ack_error`), so only an error it wrote is ever cleared by its recovery.
        self._start_ack_timed_out = False
        self._cancel_start_listener: Callable[[], None] | None = None
        self._cancel_start_timeout: Callable[[], None] | None = None
        # Set by `attach_preview`: how applied/pending state is published beside an unchanged proposal.
        self._change_hook: Callable[[], Awaitable[Any]] | None = None
        # Every automatic decision of the controller (its window timers, the hold, a stray charge, a
        # top-off, the regulator's resume) asks this boundary first, under its lock.
        controller.set_automatic_gate(self)
        # A manual pause ends with its plug-in session: the controller tells every plug-in and unplug.
        controller.set_connection_observer(self._on_connection)
        # A person's charge under a manual pause, watched for the car ending it (`_sync_manual_watch`).
        self._manual_watch = ManualChargeWatch()
        self._cancel_manual_listener: Callable[[], None] | None = None
        self._cancel_manual_interval: Callable[[], None] | None = None
        self._manual_end_pending = False
        # A person's Start or Stop is between its charger command and its pause's write: a plug-in or an
        # unplug reported meanwhile is decided after the write, against the pause it stored.
        self._manual_write_in_flight = False
        # The charge-ownership core in shadow mode, the controller's (`ownership_shadow.py`): this boundary feeds it
        # a person's Start, Stop, pause and resume, and the sun's commands.
        shadow = getattr(controller, "ownership_shadow", None)
        self._shadow: OwnershipShadow | NullShadow = shadow if isinstance(shadow, OwnershipShadow) else NullShadow()
        # Whether the sun's rules keep the charge that runs now (`SolarExecutionCoordinator.sun_keeps_charge`): a
        # strategy change to `solar` hands it over instead of stopping it. Set at setup; none answers "no".
        self._sun_keeps: Callable[[], bool] | None = None

    def set_sun_keeps_probe(self, probe: Callable[[], bool] | None) -> None:
        """Set (or clear) the probe a strategy change to `solar` asks whether the sun keeps the running charge. A
        setter, as the solar coordinator is built after this boundary."""
        self._sun_keeps = probe

    def _sun_keeps_charge(self) -> bool:
        """Whether the sun keeps the charge that runs now; `False` (stop once, as ever) when nothing can say."""
        probe = self._sun_keeps
        if probe is None or pause_blocks_execution(self._store.settings(self._entry_id)):
            return False
        try:
            return bool(probe()) and self._controller.charge_control_on
        except Exception as err:  # noqa: BLE001 - an unreadable answer is a stop, as before the hand-over
            _LOGGER.warning("Asking whether the sun keeps the charge failed: %s", type(err).__name__)
            return False

    def automatic_allowed(self, kind: str) -> bool:
        """Whether an automatic decision of this kind (`controller.AUTOMATIC_*`) may act now, by the
        persisted record (`pause_blocks_execution`): while a pause holds execution nothing automatic starts
        a charge, claims one or resumes one load balancing paused. A stop still may: a pause wants the
        charger stopped (one whose own stop failed leaves its plan behind it). Under a person's manual
        pause the person owns the charger: nothing automatic stops their Start, and load balancing resumes
        only a charge they started. A charge a person started is resumed by load balancing under any pause
        but their Stop (`AUTOMATIC_PERSON_RESUME`)."""
        settings = self._store.settings(self._entry_id)
        pause = settings.pause
        if kind == AUTOMATIC_PERSON_RESUME:
            return not (pause.manual and pause.action == MANUAL_STOP)
        if not pause_blocks_execution(settings):
            return True
        if pause.manual:
            # The person owns the charger for the plug-in session. A stop agrees with their Stop and never
            # overrules their Start; load balancing resumes only a charge they started.
            if kind == AUTOMATIC_BALANCING_RESUME:
                return pause.action == MANUAL_START
            return kind == AUTOMATIC_STOP and pause.action == MANUAL_STOP
        return kind == AUTOMATIC_STOP

    def holds_charger_off(self) -> bool:
        """Whether a person's Stop pauses Auto: any charge the charger begins by itself is stopped at once,
        whatever the plan's windows say (`ChargingController._observe_person_hold`)."""
        pause = self._store.settings(self._entry_id).pause
        return pause.manual and pause.action == MANUAL_STOP

    @asynccontextmanager
    async def automatic_turn(self, kind: str) -> AsyncIterator[bool]:
        """Hold this boundary's lock for one automatic decision of the controller, and say whether it may
        act (`automatic_allowed`), decided at the moment it acts: a person's Start, Stop or pause cannot
        land between the answer and the command."""
        async with self._lock:
            yield self.automatic_allowed(kind)

    @property
    def controller(self) -> ChargingController:
        """The `ChargingController` this executor serializes access to (read-only).

        Hybrid planning reads `plan_window_active_now` through it, a fact about the installed plan.
        """
        return self._controller

    def begin_attempt(self) -> int:
        """Claim the next attempt id: from here on, anything older applies nothing."""
        self._attempt += 1
        return self._attempt

    @property
    def attempt(self) -> int:
        return self._attempt

    def invalidate(self) -> int:
        """Make every calculation in flight inert: authority moved, or we are stopping."""
        return self.begin_attempt()

    def current(self, attempt: int) -> bool:
        """Whether one attempt is still the newest, and this executor is still running."""
        return not self._shutdown and attempt == self._attempt

    @property
    def shutdown(self) -> bool:
        return self._shutdown

    @property
    def manual_start_pending(self) -> bool:
        """A manual Start was accepted and the charger has not reported charging yet."""
        return self._start_pending

    @property
    def last_error(self) -> str | None:
        """The stable code of the last failed application, or `None`."""
        return self._last_error

    @property
    def applied(self) -> AutoApplication | None:
        """What Auto installed and the charger still holds -- read from the plan itself."""
        plan = self._controller.plan
        return None if plan is None else application_from_plan(plan)

    def materially_applied(self, application: AutoApplication | None) -> bool:
        """Whether the charger is already doing what `application` asks for.

        In force when it is the installed one, or the installed one has the same `material_key` (the case
        where `async_apply` installs nothing); identity alone would call a re-fetched price document "not
        applied". A plan Auto does not own is never applied.
        """
        if application is None:
            return False
        return not self._is_material(application, self.applied)

    @property
    def pending(self) -> AutoApplication | None:
        """The proposal waiting for a window boundary, if there is one."""
        return None if self._pending is None else self._pending.application

    @property
    def pending_attempt(self) -> int | None:
        """The attempt that created the waiting change, if any. It is installed only while that
        attempt is still the newest, so this explains why a change is or is not applied at the
        next boundary.
        """
        return None if self._pending is None else self._pending.attempt

    @property
    def paused(self) -> bool:
        """Whether a timed pause is currently suspending execution: the time condition.

        Presentation only. The execution block ends when the clearing write commits; see
        `pause_blocks_execution`, which every application path checks.
        """
        return self.pause_intent.is_active_at(self._now())

    @property
    def pause_intent(self) -> PauseIntent:
        """The stored pause intent: what was chosen, and the instant it ends."""
        return self._store.settings(self._entry_id).pause

    def execution_state(self) -> str:
        """The stable execution fact, for the snapshot and the dump. Precedence:

        * `paused` first: a paused charger applies nothing;
        * `apply_pending` when a newer proposal waits for a window boundary;
        * the charger's plan: a window open now (or the top-off past its last one) is `active`, one ahead
          `scheduled`, an Auto plan with all windows past `complete`;
        * a recorded failure with nothing on the charger is `execution_error`;
        * otherwise `not_applied`.
        """
        if self.paused:
            if self._last_error == EXECUTION_PAUSE_STOP_FAILED and self.applied is not None:
                # A pause whose stop failed is not a clean pause: the plan is still on the charger and
                # may still be charging. The error code says what to retry.
                return EXECUTION_ERROR
            return EXECUTION_PAUSED
        plan = self._controller.plan
        if plan is not None and plan.auto_owned:
            now = self._now()
            windows = plan.windows
            if self._controller.top_off_until is not None or any(start <= now < end for start, end in windows):
                return EXECUTION_PENDING if self._pending is not None else EXECUTION_ACTIVE
            if any(start > now for start, _ in windows):
                return EXECUTION_PENDING if self._pending is not None else EXECUTION_SCHEDULED
            return EXECUTION_COMPLETE
        if self._pending is not None:
            return EXECUTION_PENDING
        if self._last_error is not None:
            return EXECUTION_ERROR
        if self._applied is not None:
            # Auto's plan is gone: for a plan it installed, its windows have all passed.
            return EXECUTION_COMPLETE
        return EXECUTION_NOT_APPLIED

    def window_charging_now(self) -> bool:
        """Whether a window Auto installed is charging right now, by the injected clock. A top-off past
        its last window is part of it: a change waits for the top-off's end as for a window's."""
        plan = self._controller.plan
        if plan is None or not plan.auto_owned:
            return False
        if self._controller.top_off_until is not None:
            return True
        now = self._now()
        return any(start <= now < end for start, end in plan.windows)

    def successor_continues(self) -> bool:
        """Whether the next plan waits for the window boundary that is now (`PendingApplication`) and a window of it
        is open at this instant: it takes the charge over, so the boundary stops nothing and records no end
        (`ChargingController._async_hand_over`). Read without the lock and without side effects; installing it
        (`async_apply_pending`) checks everything again."""
        pending = self._pending
        if pending is None or not self.current(pending.attempt):
            return False
        try:
            windows = pending.application.plan.windows
        except (AttributeError, ValueError):
            return False
        now = self._now()
        return any(start <= now < end for start, end in windows)

    @staticmethod
    def _is_material(candidate: AutoApplication, applied: AutoApplication | None) -> bool:
        """Whether a candidate changes what the charger does, or only re-describes it."""
        if applied is None:
            return True
        return candidate.material_key != applied.material_key

    @callback
    def _on_controller_changed(self) -> None:
        """The charger's state changed: a window boundary may have arrived."""
        if self._pending is None or self._shutdown:
            return
        self._hass.async_create_task(self.async_apply_pending())

    async def async_start(self) -> None:
        """Adopt what the charger already holds, after a restart or reload."""
        if self._shutdown:
            # Terminal: a shut-down boundary must not start listening again (see `async_shutdown`).
            return
        async with self._lock:
            self._applied = self.applied
            self._pending = None
            self._last_error = None
            if self._cancel_listener is None:
                # One registration per executor lifetime: this listener is the only clock, and two
                # would apply the same change twice.
                self._cancel_listener = self._controller.add_listener(self._on_controller_changed)
        # The pause appointment a restored intent asks for, armed outside the lock because
        # settling an expired pause writes the store and reconciles.
        await self.async_restore_pause()

    async def async_shutdown(self) -> None:
        """Terminal: invalidate every pending attempt and stop listening."""
        async with self._lock:
            self._shutdown = True
            self.begin_attempt()
            self._pending = None
            # A start awaiting acknowledgement can never be acknowledged now: drop the watchers and
            # facts so a late state report finds nothing to resolve.
            self._start_pending = False
            self._start_ack_timed_out = False
            self._disarm_start_ack()
            self._sync_manual_watch()
            if self._controller._connection_observer == self._on_connection:  # noqa: SLF001 - our own hook
                self._controller.set_connection_observer(None)
            if self._cancel_save_retry is not None:
                cancel_save_retry = self._cancel_save_retry
                self._cancel_save_retry = None
                cancel_save_retry()
            if self._cancel_pause is not None:
                # No appointment may survive to write to a store or stop a charger for an ended boundary.
                cancel_pause = self._cancel_pause
                self._cancel_pause = None
                cancel_pause()
            if self._cancel_listener is not None:
                # Called once, with the handle cleared in the same step, to avoid a second deregistration.
                cancel = self._cancel_listener
                self._cancel_listener = None
                cancel()

    async def async_reconcile(
        self, settings: AutoSettings, snapshot: Any, *, attempt: int
    ) -> AutoApplication | None:
        """Apply one snapshot's proposal, if it is still the one that should be applied.

        Facts are re-read inside the boundary (`_may_apply`). An identical application is left alone and a
        re-description is not an installation. A material change is installed unless a window Auto
        installed is charging now, in which case it waits for that window's boundary. A strategy with no
        plan of its own (solar) clears a plan Auto still owns, the same call `_pause_locked` uses.
        """
        async with self._lock:
            return await self._reconcile_locked(settings, snapshot, attempt=attempt)

    async def _reconcile_locked(
        self, settings: AutoSettings, snapshot: Any, *, attempt: int
    ) -> AutoApplication | None:
        """`async_reconcile` itself, with this boundary's lock held."""
        application = application_for(settings, snapshot, effective_phases(self._hass, self._entry_id))
        applied = self.applied
        if application is None:
            # A non-executable result supersedes pending execution: installing what a waiting
            # change was built from would be speculation.
            if self.current(attempt):
                self._pending = None
            if settings.strategy == STRATEGY_SOLAR and applied is not None:
                # A charge the sun's rules keep is handed over to the sun with no stop (the plan's window, or the
                # sun carrying it past one); anything else is stopped once, as ever.
                sun_keeps = self._sun_keeps_charge()
                event = core_events.StrategyChange(strategy=STRATEGY_SOLAR, plan_applied=True, sun_keeps=sun_keeps)
                token = self._shadow.begin()
                stop = not sun_keeps
                verdict = self._shadow.verdict(event)
                if verdict is not None:
                    stop = self._shadow.choose("strategy", stop, ("stop", "strategy") in verdict)
                stopped: CommandOutcome | None = CommandOutcome(False) if stop else None
                try:
                    if stop:
                        await self._controller.async_stop(clear_schedule=True, urgent=False)
                        stopped = self._shadow_stop_outcome()
                    else:
                        await self._controller.async_hand_charge_to_sun()
                except Exception as err:  # noqa: BLE001 - reported, never hidden
                    _LOGGER.warning(
                        "Clearing the plan for solar strategy failed: %s", type(err).__name__
                    )
                    self._last_error = EXECUTION_SOLAR_STAND_DOWN_FAILED
                    await self._notify_change()
                    return applied
                finally:
                    self._shadow.end(token, event, legacy=("stop",) if stop else (), outcome=stopped)
                self._applied = None
                self._last_error = None
                await self._notify_change()
                return None
            if (
                applied is not None
                and self.current(attempt)
                and getattr(snapshot, "state", None) == "nothing_to_charge"
                and not getattr(snapshot, "room_limited", False)
                # The sun is expected to cover the need: hybrid's own hand-off decides, not this.
                and getattr(snapshot, "reason", None) != "solar_covers_need"
                and not pause_blocks_execution(self._store.settings(self._entry_id))
            ):
                # The need is met (the target reached, or the energy delivered) before the plan ran
                # out: its windows still ahead would buy what nobody needs. Not when only the room
                # left in the battery says so: the car ends that charge itself when it is full.
                try:
                    await self._controller.async_end_plan_need_met()
                except Exception as err:  # noqa: BLE001 - reported, the next calculation retries
                    _LOGGER.warning(
                        "Clearing a plan whose need is met failed: %s", type(err).__name__
                    )
                    return applied
                self._applied = applied
                await self._notify_change()
                return None
            return applied
        if not self._may_apply(application, attempt):
            # Nothing installs and a waiting change is superseded by this attempt's answer, but a
            # stale attempt must not touch newer state, so the wait is dropped only if this is newest.
            if self.current(attempt):
                self._pending = None
            return applied
        if applied is not None and applied.identity == application.identity:
            self._applied = applied
            self._pending = None
            return applied
        if not self._is_material(application, applied):
            self._applied = applied
            self._pending = None
            return applied
        if self.window_charging_now():
            # A window is charging and is never shortened: the change waits for the boundary
            # the plan's timers mark.
            self._pending = PendingApplication(
                application=application,
                attempt=attempt,
                settings_revision=self._store.settings(self._entry_id).revision,
                price_identity=application.price_identity,
            )
            await self._notify_change()
            return applied
        if not self._prices_still_live(application, attempt):
            return applied
        await self._install_application(application)
        return self.applied

    async def async_apply_pending(self) -> AutoApplication | None:
        """A boundary arrived: install the waiting proposal, if it still holds.

        Only the settings intent is re-checked; the preview already validated the calculation. The pause
        check is the persisted gate, so a proposal waiting when a pause expired with an uncommitted clear is
        dropped.
        """
        async with self._lock:
            pending = self._pending
            if pending is None:
                return None
            if not self.current(pending.attempt):
                # A newer calculation, authority handover or shutdown superseded it.
                self._pending = None
                return self.applied
            settings = self._store.settings(self._entry_id)
            if self._shutdown or pause_blocks_execution(settings):
                self._pending = None
                return self.applied
            if settings.revision != pending.settings_revision:
                # Its settings are gone: the next calculation decides, not a plan built from changed inputs.
                self._pending = None
                return self.applied
            if self.window_charging_now():
                return self.applied
            if not self._still_the_current_proposal(settings, pending):
                self._pending = None
                return self.applied
            application = pending.application
            applied = self.applied
            if applied is not None and applied.identity == application.identity:
                self._pending = None
                return applied
            try:
                self._controller.validate_plan(application.plan)
            except ValueError:
                self._pending = None
                self._last_error = EXECUTION_PLAN_UNUSABLE
                return self.applied
            await self._install_application(application)
            return self.applied

    def _still_the_current_proposal(
        self, settings: AutoSettings, pending: PendingApplication
    ) -> bool:
        """Whether the current preview still says exactly what the pending change says."""
        snapshot = None if self._live_snapshot is None else self._live_snapshot()
        if snapshot is None:
            return False
        application = application_for(settings, snapshot, effective_phases(self._hass, self._entry_id))
        if application is None:
            return False
        return (
            application.identity == pending.application.identity
            and application.price_identity == pending.price_identity
        )

    def _may_apply(self, application: AutoApplication, attempt: int) -> bool:
        """Whether one application may proceed, with every fact re-read inside the boundary.

        In order: the attempt id, the latest stored intent (same revision the plan was built from, no
        persisted pause; `pause_blocks_execution`, not the clock), then the plan against the live current
        bounds. Nothing is checked outside the lock.
        """
        if not self.current(attempt):
            return False
        settings = self._store.settings(self._entry_id)
        if pause_blocks_execution(settings):
            return False
        if settings.revision != application.settings_revision:
            return False
        try:
            self._controller.validate_plan(application.plan)
        except ValueError:
            self._last_error = EXECUTION_PLAN_UNUSABLE
            return False
        return True

    def _prices_still_live(self, application: AutoApplication, attempt: int) -> bool:
        """Whether the prices a proposal was built from are still the ones in hand.

        The published snapshot is evidence only when it comes from this calculation or a later one. The
        preview publishes after it has handed the proposal over, so an older snapshot (the state before
        the first prices arrived, say) says nothing about these prices and must not hold the plan back.
        """
        snapshot = None if self._live_snapshot is None else self._live_snapshot()
        if snapshot is None or snapshot.attempt < attempt:
            return True
        return snapshot.price_identity == application.price_identity

    async def _install_plan(self, plan: ChargingPlan) -> str | None:
        """Install one plan through the charging controller, once. An error code, or `None`."""
        try:
            await self._controller.async_install(plan)
        except ChargingExecutionError as err:
            self._last_error = err.code
            return err.code
        except Exception as err:  # noqa: BLE001 - any failure to install is one stable code here
            _LOGGER.warning("Installing a charging plan failed: %s", type(err).__name__)
            self._last_error = EXECUTION_INSTALL_FAILED
            return EXECUTION_INSTALL_FAILED
        return None

    async def _install_application(self, application: AutoApplication) -> str | None:
        """Install an Auto application, recording it only once the charger has it."""
        code = await self._install_plan(application.plan)
        if code is not None:
            await self._notify_change()
            return code
        self._applied = application
        self._pending = None
        await self._notify_change()
        return None

    async def _notify_change(self) -> None:
        """Execution changed: let the preview publish the new facts. Never fatal."""
        if self._change_hook is None:
            return
        try:
            await self._change_hook()
        except Exception as err:  # noqa: BLE001 - publishing must not break a transaction
            _LOGGER.warning("Publishing an Auto execution change failed: %s", type(err).__name__)

    async def async_update_settings(
        self,
        *,
        mutate: Callable[[AutoSettings], AutoSettings] | None = None,
        expected_revision: int | None = None,
    ) -> AutoSettings:
        """Every settings change that affects Auto, inside the authority boundary."""
        async with self._lock:
            self.begin_attempt()
            self._pending = None
            return await self._store.async_update(
                self._entry_id, mutate=mutate, expected_revision=expected_revision, confirm=True
            )

    async def async_manual_start(self, amps: int | None = None) -> None:
        """A person starting a charge: it pauses Auto, schedule and strategy, for the plug-in session.

        Inside the boundary because it mutates the plan: the attempt is invalidated and any waiting change
        dropped, so a manual decision cannot be overwritten by an Auto installation.
        """
        async with self._lock:
            await self._manual_start_locked(amps)

    async def _manual_start_locked(self, amps: int | None = None) -> None:
        """The manual start itself, with this boundary's lock held.

        * A charger that says no car is plugged in is refused (`vehicle_not_connected`): nothing is sent and
          nothing paused. One that cannot say (a status that says nothing) is started as ever.
        * The command is sent first and decides: a failing one changes nothing else and keeps a retry
          possible. Nothing automatic can act between the command and the pause: every automatic decision
          waits for this boundary's lock (`automatic_turn`).
        * Once accepted, the manual pause (`PAUSE_MANUAL`, action `start`) is stored, replacing a person's
          Stop; a pause the person chose for a span (until tomorrow, until resumed) already holds Auto and is
          kept. A pause that could not be stored is logged and reported (`reconcile_failed`) after the rest
          is done: the charge the person started stands.
        * A paused Auto's plan is dropped without touching the charger (its windows' ends would stop what
          the person started), and the start is marked as awaiting the charger's report.
        """
        connected = self._shadow_connected(fallback=False)
        token = self._shadow.begin(early=None if connected is ... else core_events.PersonStart(connected=connected))
        note: dict[str, Any] = {"legacy": [], "outcome": None}
        try:
            await self._manual_start_body(amps, note)
        finally:
            if connected is ...:
                self._shadow.cancel(token)
            else:
                self._shadow.end(
                    token,
                    core_events.PersonStart(connected=connected),
                    legacy=note["legacy"],
                    outcome=note["outcome"],
                )

    def _core_stop_scope(self, connected: Any, scope: str | None) -> str | None:
        """When the core drives: the plug-in session a person's Stop pauses Auto for, as it decides it (`None`: a
        pause they chose for a span is kept). Otherwise today's `scope`."""
        if not self._shadow.drives or connected is ...:
            return scope
        try:
            session = self._shadow.session
        except Exception:  # noqa: BLE001 - today's decision stands
            return scope
        return None if session.manual is None else session.manual.scope

    def _shadow_stop_outcome(self) -> CommandOutcome:
        """A stop through the controller that returned: what became of it, for the ownership shadow."""
        read = getattr(self._controller, "_shadow_stop_outcome", None)
        if read is None:
            return CommandOutcome(True)
        try:
            return read(True)
        except Exception:  # noqa: BLE001 - the shadow never raises into the real path
            return CommandOutcome(True)

    def _shadow_connected(self, *, fallback: bool) -> Any:
        """What the charger says of a car now (with `fallback`, the last it stated when it says nothing), for the
        ownership shadow; `...` when it cannot be read."""
        try:
            connected = self._controller.adapter.vehicle_connected()
            if connected is None and fallback:
                connected = self._controller.known_connected
            return connected
        except Exception:  # noqa: BLE001 - the shadow never raises into the real path
            return ...

    async def _manual_start_body(self, amps: int | None, note: dict[str, Any]) -> None:
        if self._controller.adapter.vehicle_connected() is False:
            raise AutoControlRefused(EXECUTION_VEHICLE_NOT_CONNECTED, "no car is plugged in")
        self.begin_attempt()
        self._pending = None
        save_error: Exception | None = None
        self._manual_write_in_flight = True
        try:
            note["legacy"].append("start")
            note["outcome"] = CommandOutcome(False)
            started = await self._controller.async_start(amps, manual=True)
            note["outcome"] = CommandOutcome(
                started is not False, balancing_held=started is False and self._controller.paused_by_balancing
            )
            if started is False and not self._controller.paused_by_balancing:
                # The charger's control was unavailable and the command never went out: a failed command
                # like any other, with nothing pending and a retry possible.
                raise HomeAssistantError("The start command was not executed: the charge control is unavailable")
            # Started, or held back by load balancing for want of headroom: either way the person's charge,
            # which the regulator resumes when there is room.
            before = self._store.settings(self._entry_id).pause
            if not before.admitted or before.manual:
                try:
                    await self._store_manual_pause(self._manual_intent(MANUAL_START, MANUAL_SCOPE_PLUG_IN))
                except Exception as err:  # noqa: BLE001 - the charge stands; reported below
                    _LOGGER.warning("Storing a person's Start as Auto's pause failed: %s", type(err).__name__)
                    save_error = err
        finally:
            self._manual_write_in_flight = False
        if pause_blocks_execution(self._store.settings(self._entry_id)):
            await self._controller.async_drop_plan()
            self._applied = None
        self._sync_manual_watch()
        if started is False:
            # Not executed (yet): said as such, whatever became of the pause's save (it holds in memory).
            await self._notify_change()
            raise HomeAssistantError("The start is held back by load balancing until the site has room")
        self._note_manual_start_sent()
        await self._notify_change()
        if save_error is not None:
            raise AutoControlCommitted(EXECUTION_RECONCILE_FAILED) from save_error

    def _manual_intent(self, action: str, scope: str) -> PauseIntent:
        """The manual pause a person's Start or Stop takes now."""
        return PauseIntent(
            choice=PAUSE_MANUAL, admitted_at=self._now(), expires_at=None, action=action, scope=scope
        )

    async def _store_pause(self, intent: PauseIntent) -> None:
        """Store a pause intent and arm (or drop) its expiry. Raises when it could not be stored."""
        await self._store.async_update(self._entry_id, mutate=lambda current: replace(current, pause=intent))
        self._arm_pause_expiry(intent.expires_at)

    async def _store_manual_pause(self, intent: PauseIntent) -> None:
        """Store the pause of a person's Start or Stop the charger already executed. When the file cannot be
        written the pause is still the in-memory truth at once (everything that reads the settings acts on
        what the person did, never on the pause before it) and the file is written again until it takes it.
        Raises the save's failure, for the caller to report."""
        try:
            await self._store_pause(intent)
        except Exception:
            try:
                await self._store.async_hold_unsaved(
                    self._entry_id, mutate=lambda current: replace(current, pause=intent)
                )
            except Exception as err:  # noqa: BLE001 - logged; the save's failure is what is reported
                _LOGGER.warning("Holding a person's pause in memory failed: %s", type(err).__name__)
            else:
                self._arm_pause_expiry(intent.expires_at)
                self._arm_save_retry()
            raise

    def _arm_save_retry(self) -> None:
        if self._cancel_save_retry is not None or self._shutdown:
            return
        self._cancel_save_retry = async_call_later(self._hass, PAUSE_SAVE_RETRY_S, self._on_save_retry)

    @callback
    def _on_save_retry(self, _now: datetime) -> None:
        self._cancel_save_retry = None
        if self._shutdown:
            return
        self._hass.async_create_task(self._async_retry_save(), "spotnav pause save retry")

    async def _async_retry_save(self) -> None:
        unsaved = getattr(self._store, "unsaved", False)
        if unsaved and not await self._store.async_flush():
            self._arm_save_retry()

    def _note_manual_start_sent(self) -> None:
        """Remember that a manual Start was accepted and is not yet acknowledged.

        Acknowledgement is the charger's own reported state. If the charge control already reports
        charging nothing is pending; otherwise a state listener and one bounding appointment are armed, and
        whichever answers first clears both.
        """
        if self._shutdown or self._controller.charging or self._controller.held_by_charger:
            # Charging, or the charger's own scheduler holds the charge: either way it has answered.
            return
        self._start_pending = True
        self._disarm_start_ack()
        # The controller owns the entity id and the only state subscription (see
        # `ChargingController.add_charge_state_listener`); the boundary arms nothing itself.
        self._cancel_start_listener = self._controller.add_charge_state_listener(
            self._on_charge_state_changed
        )
        self._cancel_start_timeout = async_track_point_in_time(
            self._hass, self._on_start_ack_timeout, self._now() + MANUAL_START_ACK_TIMEOUT
        )

    def _on_charge_state_changed(self) -> None:
        """The charge control reported charging: whatever was outstanding is answered.

        A non-charging report is not an answer (a charger can blink through `unavailable` on the way up).
        After the appointment expired, this listener retires the obsolete failure.
        """
        if self._shutdown or not (self._controller.charging or self._controller.held_by_charger):
            return
        answered = self._start_pending or self._start_ack_timed_out
        self._start_pending = False
        recovered = self._recover_start_ack_error()
        if not (answered or recovered):
            return
        self._disarm_start_ack()
        self._hass.async_create_task(self._notify_change())

    def _recover_start_ack_error(self) -> bool:
        """Clear the manual-Start acknowledgement failure, and nothing else."""
        if not self._start_ack_timed_out:
            return False
        self._start_ack_timed_out = False
        if self._last_error != EXECUTION_ACTION_FAILED:
            return False
        self._last_error = None
        return True

    @callback
    def _on_start_ack_timeout(self, _now: datetime) -> None:
        """The charger never reported charging in time: stop waiting, and say so.

        Recorded as `action_failed` and the pending fact dropped so a person can retry; nothing is undone.
        The listener stays armed, since a later charging report is the only thing allowed to retire the
        failure.
        """
        self._cancel_start_timeout = None
        if not self._start_pending or self._shutdown:
            return
        self._start_pending = False
        self._start_ack_timed_out = True
        self._last_error = EXECUTION_ACTION_FAILED
        self._hass.async_create_task(self._notify_change())

    def _disarm_start_ack(self) -> None:
        """Drop the state listener and the appointment, each exactly once."""
        if self._cancel_start_listener is not None:
            cancel = self._cancel_start_listener
            self._cancel_start_listener = None
            cancel()
        if self._cancel_start_timeout is not None:
            cancel = self._cancel_start_timeout
            self._cancel_start_timeout = None
            cancel()

    async def async_manual_stop(self) -> None:
        """A person stopping a charge: it pauses Auto, schedule and strategy, for the plug-in session."""
        async with self._lock:
            await self._immediate_stop_locked()

    async def async_manual_cancel(self) -> None:
        """A person cancelling the schedule: a Stop, which clears the plan and pauses Auto for the plug-in."""
        async with self._lock:
            await self._immediate_stop_locked()

    async def async_manual_follow(self) -> None:
        """A person re-arming the saved plan. A manual pause ends first (the person hands the charger back
        to Auto, which replans from the live proposal); then the plan in force is followed, if there is one."""
        async with self._lock:
            self.begin_attempt()
            self._pending = None
            if self.pause_intent.manual:
                await self._end_manual_pause_locked("follow")
                if self._controller.plan is None:
                    # Auto has the charger back and nothing to follow yet: the pause's end was the request.
                    await self._notify_change()
                    return
            await self._controller.async_follow_schedule()
            await self._notify_change()

    async def async_pause(self, choice: PauseChoice = PAUSE_UNTIL_RESUMED) -> None:
        """Stop and clear Auto's own plan, and apply nothing until the chosen instant.

        The charger keeps its settings, price subscription and calculations but applies none of it. A plan
        Auto did not install is left alone.

        * The choice is resolved to an absolute instant at admission ("next period" is the next start of
          the running plan, "tomorrow" is midnight in the charger's market zone); one that cannot be
          resolved honestly is refused by code and nothing is stored.
        * The intent is stored before the stop, so no application can race in.
        * If the stop fails, the pause stands, `pause_stop_failed` is recorded and the plan stays
          installed. Calling again with the same choice retries the stop only; a different choice is a new
          admission.
        * A failure to store the pause changes nothing and issues no charger command.
        """
        async with self._lock:
            await self._pause_locked(choice)

    async def _pause_locked(self, choice: PauseChoice = PAUSE_UNTIL_RESUMED) -> None:
        """The pause itself. Runs with this boundary's lock held, and never translates failures."""
        token = self._shadow.begin()
        plan_applied = self.applied is not None
        note: dict[str, Any] = {"legacy": [], "outcome": None, "stored": False}
        try:
            await self._pause_body(choice, note)
        finally:
            if note["stored"]:
                self._shadow.end(
                    token,
                    core_events.PauseChoiceMade(choice=choice, plan_applied=plan_applied),
                    legacy=note["legacy"],
                    outcome=note["outcome"],
                )
            else:
                self._shadow.cancel(token)

    async def _pause_body(self, choice: PauseChoice, note: dict[str, Any]) -> None:
        self.begin_attempt()
        now = self._now()
        settings = self._store.settings(self._entry_id)
        action, intent = self._pause_transition(choice, settings.pause, now)
        if action == "admit":
            # Deliberately not caught: a pause that was never stored must not stop anything.
            await self._store.async_update(
                self._entry_id, mutate=lambda current: replace(current, pause=intent)
            )
            self._arm_pause_expiry(intent.expires_at)
            # A pause the person picks replaces a manual one, and its watch.
            self._sync_manual_watch()
        note["stored"] = True
        self._pending = None
        if self.applied is None:
            self._last_error = None
            await self._notify_change()
            return
        note["legacy"].append("stop")
        note["outcome"] = CommandOutcome(False)
        try:
            # Through the controller: the same path every stop uses, and the only code here that
            # may touch a charger.
            await self._controller.async_stop(clear_schedule=True)
            note["outcome"] = self._shadow_stop_outcome()
        except Exception as err:  # noqa: BLE001 - reported as a partial failure, never hidden
            _LOGGER.warning(
                "Stopping the charger while pausing failed: %s", type(err).__name__
            )
            self._last_error = EXECUTION_PAUSE_STOP_FAILED
            await self._notify_change()
            return
        self._applied = None
        self._last_error = None
        await self._notify_change()

    async def async_resume(self) -> None:
        """Resume: the next calculation may apply again, through the same boundary."""
        async with self._lock:
            await self._resume_locked()

    async def _resume_locked(self) -> None:
        """The resume itself. Runs with this boundary's lock held."""
        token = self._shadow.begin()
        try:
            await self._store.async_update(
                self._entry_id, mutate=lambda settings: replace(settings, pause=PauseIntent())
            )
        except BaseException:
            self._shadow.cancel(token)
            raise
        self._arm_pause_expiry(None)
        self._sync_manual_watch()
        self._shadow.end(token, core_events.Resume(reason=core_events.RESUME_PERSON))
        self.begin_attempt()
        self._last_error = None
        await self._rearm_kept_plan()
        await self._notify_change()

    async def _rearm_kept_plan(self) -> None:
        """A pause ended while a plan it could not clear stayed (its stop failed): arm that plan again, so a
        window its timer left unstarted while the pause held starts now. Logged, never raised: the next
        calculation reconciles."""
        if self._controller.plan is None:
            return
        try:
            await self._controller.async_rearm()
        except Exception as err:  # noqa: BLE001 - the pause is over either way
            _LOGGER.warning("Arming the plan again after a pause failed: %s", type(err).__name__)

    def control_facts(self) -> ControlFacts:
        """One moment's control facts, read here and nowhere else. Memory only, no write or
        service call. Every call is a new observation; a paired read must use `control_axes`.
        """
        settings = self._store.settings(self._entry_id)
        return ControlFacts(
            charging=self._controller.charging,
            pause=settings.pause,
            has_settings=True,
            paused=self.paused,
            last_error=self._last_error,
            pause_choices=self.pause_choices(),
            start_pending=self._start_pending,
        )

    def control_axes(self) -> ControlAxes:
        """Both axes over one capture of this boundary's facts: the paired read."""
        return decide_axes(self.control_facts())

    def immediate_decision(self) -> ImmediateDecision:
        """What the charger may be told to do now, the immediate axis alone (its own capture;
        use `control_axes` for both axes).
        """
        return decide_immediate(self.control_facts())

    def automatic_decision(self) -> AutomaticDecision:
        """What may happen to Auto's execution now, the automatic axis alone (its own capture;
        use `control_axes` for both axes).
        """
        return decide_automatic(self.control_facts())

    async def async_manual_action(
        self, action: str, choice: PauseChoice | None = None
    ) -> ControlDecision:
        """Admit and carry out one manual action against the state that exists now.

        Admission happens inside the lock with the state re-read there; a dashboard read is not an
        authority token. Concurrent requests serialize, and the second is refused as
        `EXECUTION_ACTION_UNAVAILABLE` if the first changed the facts.

        * `start`, and `stop` with no choice, are charger commands judged by the immediate axis.
        * `stop` with a choice, and `resume`, are Auto's own, judged by the automatic axis. A choice-less
          stop is never turned into `until_resumed`.
        * Failure before the effect: `AutoControlRefused`, nothing moved.
        * Failure of the follow-up after the effect is in the record: `AutoControlCommitted` with
          `EXECUTION_RECONCILE_FAILED`, never rolled back.

        A pause whose intent committed but whose physical stop failed is a successful admission with
        `pause_stop_failed` in the refreshed facts.
        """
        async with self._lock:
            # One capture, both axes: the facts are read once and both decisions derive from that
            # object.
            facts = self.control_facts()
            axes = decide_axes(facts)
            if action in IMMEDIATE_ACTIONS and choice is None:
                immediate = axes.immediate
                if not immediate.admits(action):
                    raise AutoControlRefused(
                        EXECUTION_ACTION_UNAVAILABLE,
                        f"{action!r} is not the admissible action right now",
                    )
                admitted = ControlDecision(
                    immediate.action,
                    immediate.reason,
                    # The choices are the same snapshot's pause choices, never a second read.
                    facts.pause_choices if immediate.action == ACTION_STOP else (),
                )
            elif action == ACTION_STOP and choice is not None and facts.pause.manual:
                # A pause the person picks replaces their manual one; the card offers it as Resume only, so
                # the choices are this snapshot's own, never one more read.
                if choice not in facts.pause_choices:
                    raise AutoControlRefused("invalid_pause", f"{choice!r} is not an available pause choice")
                admitted = ControlDecision(ACTION_STOP, None, facts.pause_choices)
            else:
                automatic = axes.automatic
                wanted = ACTION_RESUME if action == ACTION_RESUME else ACTION_PAUSE
                if action not in (ACTION_STOP, ACTION_RESUME) or automatic.action != wanted:
                    raise AutoControlRefused(
                        EXECUTION_ACTION_UNAVAILABLE,
                        f"{action!r} is not the admissible action right now",
                    )
                if not automatic.admits(wanted, choice):
                    # The action is right; this choice is not one the charger can honour now.
                    raise AutoControlRefused(
                        "invalid_pause", f"{choice!r} is not an available pause choice"
                    )
                admitted = ControlDecision(
                    # `pause` is a `stop` on the wire, the one action that carries choices.
                    ACTION_STOP if automatic.action == ACTION_PAUSE else automatic.action,
                    automatic.reason,
                    automatic.choices,
                )
            before = self._store.settings(self._entry_id)
            try:
                await self._apply_manual_action(action, choice)
            except AutoControlError:
                raise
            except AutoSettingsError as err:
                # A refusal from the store's rules: nothing written, the domain's own code.
                raise AutoControlRefused(err.code) from None
            except Exception as err:  # noqa: BLE001 - classified, never re-raised with prose
                committed = self._manual_effect_committed(before, action)
                _LOGGER.error(
                    "A manual %s failed (%s)%s",
                    action,
                    type(err).__name__,
                    " after committing" if committed else "",
                )
                code = err.code if isinstance(err, ChargingExecutionError) else None
                if committed:
                    raise AutoControlCommitted(EXECUTION_RECONCILE_FAILED) from None
                raise AutoControlRefused(code or EXECUTION_ACTION_FAILED) from None
            return admitted

    async def _apply_manual_action(self, action: str, choice: PauseChoice | None) -> None:
        """The effect itself, already admitted, with the lock held."""
        if action == ACTION_START:
            await self._manual_start_locked()
        elif action == ACTION_STOP and choice is not None:
            await self._pause_locked(choice)
        elif action == ACTION_STOP:
            await self._immediate_stop_locked()
        else:
            await self._resume_locked()

    async def _immediate_stop_locked(self) -> None:
        """A person's Stop, with this boundary's lock held.

        The charger is stopped first and Auto's plan cleared, as a pause does: nothing may keep a person's
        Stop from the charger. Then the manual pause (`PAUSE_MANUAL`, action `stop`) is stored: for the
        plug-in the car is in, or, with no car plugged in by the last connection the charger stated (a
        status that says nothing now says nothing either way), for the next one ("do not charge when I plug
        in"). A pause the person chose for a span is kept. A stop that fails still stores the pause, records
        `pause_stop_failed` and raises; Stop again is the retry. A pause that could not be stored is logged
        and reported (`reconcile_failed`); the charger is stopped all the same.
        """
        connected = self._shadow_connected(fallback=True)
        token = self._shadow.begin(early=None if connected is ... else core_events.PersonStop(connected=connected))
        note: dict[str, Any] = {"outcome": None, "connected": connected, "early": token.early is not None}
        try:
            await self._immediate_stop_body(note)
        finally:
            if connected is ...:
                self._shadow.cancel(token)
            else:
                self._shadow.end(
                    token, core_events.PersonStop(connected=connected), legacy=("stop",), outcome=note["outcome"]
                )

    async def _immediate_stop_body(self, note: dict[str, Any]) -> None:
        connected = note.get("connected")
        self.begin_attempt()
        self._pending = None
        # The person acted: a charger SpotNav gave up stopping under their earlier Stop is watched afresh.
        self._controller.reset_person_hold()
        before = self._store.settings(self._entry_id).pause
        scope: str | None = None
        if not before.admitted or before.manual:
            connected = self._controller.adapter.vehicle_connected()
            if connected is None:
                connected = self._controller.known_connected
            waiting = before.manual and before.scope == MANUAL_SCOPE_NEXT_PLUG_IN and connected is not True
            scope = MANUAL_SCOPE_NEXT_PLUG_IN if connected is False or waiting else MANUAL_SCOPE_PLUG_IN
        scope = self._core_stop_scope(note.get("connected"), scope)
        stop_error: Exception | None = None
        save_error: Exception | None = None
        self._manual_write_in_flight = True
        try:
            note["outcome"] = CommandOutcome(False)
            try:
                await self._controller.async_stop(clear_schedule=True)
                note["outcome"] = self._shadow_stop_outcome()
            except Exception as err:  # noqa: BLE001 - raised below, after the pause is stored
                stop_error = err
            if scope is not None:
                try:
                    await self._store_manual_pause(self._manual_intent(MANUAL_STOP, scope))
                except Exception as err:  # noqa: BLE001 - the charger is stopped; reported below
                    _LOGGER.warning("Storing a person's Stop as Auto's pause failed: %s", type(err).__name__)
                    save_error = err
                self._sync_manual_watch()
        finally:
            self._manual_write_in_flight = False
        if stop_error is not None:
            self._last_error = EXECUTION_PAUSE_STOP_FAILED
            await self._notify_change()
            raise stop_error
        self._applied = None
        if self._last_error == EXECUTION_PAUSE_STOP_FAILED:
            self._last_error = None
        await self._notify_change()
        if save_error is not None:
            raise AutoControlCommitted(EXECUTION_RECONCILE_FAILED) from save_error

    # ------------------------------------------------------------------ the manual pause's end

    async def _end_manual_pause_locked(self, reason: str, *, car_ended: bool = False) -> bool:
        """End a manual pause (the plug-in session is over, the car ended the person's charge, the person
        follows the plan again), with this boundary's lock held, and hand the charger back to Auto: the
        live proposal is reconciled at once. Returns whether one was ended.

        After the car ended the charge, a window of the plan already open is not started again
        (`ChargingController.async_note_car_ended`): the car is full.
        """
        intent = self._store.settings(self._entry_id).pause
        if not intent.manual:
            return False
        self.begin_attempt()
        self._pending = None
        # A plug-in's or an unplug's end of the pause the controller already fed; Follow and the car's end are this.
        fed = reason == "follow" or car_ended
        token = self._shadow.begin() if fed else None
        try:
            await self._store.async_update(
                self._entry_id,
                mutate=lambda current: replace(current, pause=PauseIntent()) if current.pause == intent else current,
            )
            self._arm_pause_expiry(None)
            if self._last_error == EXECUTION_PAUSE_STOP_FAILED:
                self._last_error = None
            self._sync_manual_watch()
            _LOGGER.info("SpotNav charger %s: Auto resumes (%s)", self._entry_id, reason)
            if car_ended:
                await self._controller.async_note_car_ended(self._store.settings(self._entry_id).target.vehicle_id)
        except BaseException:
            if token is not None:
                self._shadow.cancel(token)
            raise
        if token is None:
            self._shadow.check(INTENT, "manual_pause_end")
        else:
            self._shadow.end(
                token,
                core_events.CarEnded() if car_ended else core_events.Resume(reason=core_events.RESUME_FOLLOW),
            )
        await self._rearm_kept_plan()
        snapshot = None if self._live_snapshot is None else self._live_snapshot()
        if snapshot is not None:
            try:
                await self._reconcile_locked(
                    self._store.settings(self._entry_id), snapshot, attempt=self.begin_attempt()
                )
            except Exception as err:  # noqa: BLE001 - the pause is over; the next calculation reconciles
                _LOGGER.warning("Reconciling after a manual pause failed: %s", type(err).__name__)
                self._last_error = EXECUTION_RECONCILE_FAILED
        await self._notify_change()
        return True

    @callback
    def _on_connection(self, previous: bool | None, connected: bool) -> None:
        """The charger reported a plug-in, an unplug, or its first connection after a restart: a manual
        pause ends with the plug-in session it was given in (`_async_connection_changed`)."""
        if self._shutdown or not (self.pause_intent.manual or self._manual_write_in_flight):
            return
        want = getattr(self._controller, "core_connection_manual", ...)
        self._hass.async_create_task(self._async_connection_changed(previous, connected, want))

    async def _async_connection_changed(self, previous: bool | None, connected: bool, want: Any = ...) -> None:
        """Decided under the lock against the stored pause:

        * an unplug ends a pause of the plug-in the car was in; one given with no car plugged in waits for
          the plug-in after it;
        * a plug-in starts the session a pause given with no car was for, and so does the first connection
          known after a restart when it is a car (plugged in while Home Assistant was down); any other
          plug-in means the session its pause was given in is over (an unplug nobody saw: a restart, a
          charger offline);
        * the first connection known after a restart, no car: the session ended while nobody looked.

        A status that says nothing is never a plug-in or an unplug: the controller reports only connections
        the charger stated.
        """
        try:
            async with self._lock:
                intent = self.pause_intent
                if self._shutdown or not intent.manual:
                    return
                if self._shadow.drives and want is ... and self._shadow.alone():
                    # Decided while another feed was open: the core's session has decided it since.
                    want = self._shadow.session.manual
                if self._shadow.drives and want is not ...:
                    await self._core_connection_changed(intent, connected, want)
                    return
                waiting = intent.scope == MANUAL_SCOPE_NEXT_PLUG_IN
                if connected and previous is not True and waiting:
                    await self._store.async_update(
                        self._entry_id,
                        mutate=lambda current: replace(
                            current, pause=replace(current.pause, scope=MANUAL_SCOPE_PLUG_IN)
                        )
                        if current.pause == intent
                        else current,
                    )
                    self._shadow.check(INTENT, "manual_pause_plug_in")
                    await self._notify_change()
                    return
                if waiting:
                    return
                if (connected and previous is False) or not connected:
                    await self._end_manual_pause_locked("plugged in" if connected else "unplugged")
        except Exception as err:  # noqa: BLE001 - a callback's task must not raise; the next report decides
            _LOGGER.warning("Ending a manual pause at a plug-in or unplug failed: %s", type(err).__name__)

    async def _core_connection_changed(self, intent: PauseIntent, connected: bool, manual: Any) -> None:
        """When the core drives: a plug-in or an unplug leaves the manual pause the core decided for it when the
        controller saw the connection (`manual`): ended, moved to the plug-in, or as it was."""
        if manual is not None and (manual.action, manual.scope) == (intent.action, intent.scope):
            return
        if manual is None:
            await self._end_manual_pause_locked("plugged in" if connected else "unplugged")
            return
        await self._store.async_update(
            self._entry_id,
            mutate=lambda current: replace(current, pause=replace(current.pause, scope=manual.scope))
            if current.pause == intent
            else current,
        )
        self._shadow.check(INTENT, "manual_pause_plug_in")
        await self._notify_change()

    def _sync_manual_watch(self) -> None:
        """Watch a person's charge for the car ending it while a manual pause of a Start stands, and only
        then: on every change the controller tells, and one regular look (a car at 0 A reports nothing
        new)."""
        wanted = not self._shutdown and self.pause_intent.manual and self.pause_intent.action == MANUAL_START
        if not wanted:
            self._manual_watch.reset()
            if self._cancel_manual_listener is not None:
                self._cancel_manual_listener()
                self._cancel_manual_listener = None
            if self._cancel_manual_interval is not None:
                self._cancel_manual_interval()
                self._cancel_manual_interval = None
            return
        if self._cancel_manual_listener is None:
            self._cancel_manual_listener = self._controller.add_listener(self._look_at_manual_charge)
        if self._cancel_manual_interval is None:
            self._cancel_manual_interval = async_track_time_interval(
                self._hass, self._look_at_manual_charge_timer, MANUAL_WATCH_INTERVAL, cancel_on_shutdown=True
            )
        # A look now: a charge already on (the Start was answered before the watch began) is seen.
        self._look_at_manual_charge()

    @callback
    def _look_at_manual_charge_timer(self, _now: datetime) -> None:
        self._look_at_manual_charge()

    @callback
    def _look_at_manual_charge(self) -> None:
        """One look at the person's charge (`ManualChargeWatch`): the car ending it ends the pause."""
        if self._shutdown or self._manual_end_pending:
            return
        intent = self.pause_intent
        if not intent.manual or intent.action != MANUAL_START:
            return
        controller = self._controller
        ended = self._manual_watch.observe(
            now=dt_util.utcnow(),
            control_on=controller.charge_control_on,
            drawing=controller.car_drawing(),
            held=controller.held_by_charger,
            balancing_paused=controller.paused_by_balancing,
            start_pending=controller.start_pending,
            connection=controller.connection()[0],
            connector_status=controller.charge_progress_facts().connector_status,
        )
        if ended:
            self._manual_end_pending = True
            self._hass.async_create_task(self._async_manual_charge_ended())

    async def _async_manual_charge_ended(self) -> None:
        """The car ended the person's charge by itself: the manual pause of their Start ends, and Auto
        resumes without starting a window that was already open (the car is full)."""
        try:
            async with self._lock:
                intent = self.pause_intent
                if self._shutdown or not intent.manual or intent.action != MANUAL_START:
                    return
                await self._end_manual_pause_locked("the car ended the charge", car_ended=True)
        except Exception as err:  # noqa: BLE001 - a callback's task must not raise; the next look decides
            _LOGGER.warning("Ending a manual pause after the car ended the charge failed: %s", type(err).__name__)
        finally:
            self._manual_end_pending = False

    async def async_after_restore(self) -> None:
        """After the charger's saved state is back (`ChargingController.async_initialize`): a person's Stop
        an older release stored becomes the manual pause it now is, and a manual Start's charge is watched
        again."""
        if self._shutdown:
            return
        async with self._lock:
            legacy = self._controller.take_legacy_person_stop()
            if legacy:
                if not self.pause_intent.admitted:
                    try:
                        await self._store_pause(self._manual_intent(MANUAL_STOP, MANUAL_SCOPE_PLUG_IN))
                    except Exception as err:  # noqa: BLE001 - logged; nothing is started by this either way
                        _LOGGER.warning("Carrying over a stored person's Stop failed: %s", type(err).__name__)
                    else:
                        _LOGGER.info(
                            "SpotNav charger %s: a stored person's Stop is now Auto paused for the plug-in",
                            self._entry_id,
                        )
                # The charger's record is saved without the older key from here on.
                await self._controller.async_save_record()
            self._sync_manual_watch()
        # A charge running under a person's Stop (the charger began it while Home Assistant was down) is
        # stopped now, not at the charger's next report.
        self._controller.note_person_hold()

    async def async_end_plan_need_met(self) -> bool:
        """The manual need is delivered (the register watcher saw it): end Auto's own plan now, without
        waiting for a calculation that may not get that far (prices missing, say). Returns whether a
        plan was cleared."""
        async with self._lock:
            if self._shutdown or self.applied is None:
                return False
            self.begin_attempt()
            self._pending = None
            cleared = await self._controller.async_end_plan_need_met()
            await self._notify_change()
            return cleared

    async def async_start_on_plug_in(self) -> bool:
        """A vehicle was plugged in and Auto has replanned: start the installed plan's window that is
        open now, through the controller's own checks (a person's Stop, the target, load balancing).
        Nothing while a pause holds execution. Returns whether a start was sent.
        """
        async with self._lock:
            if self._shutdown or pause_blocks_execution(self._store.settings(self._entry_id)):
                return False
            if self._controller.plan is None or not self._controller.plan_window_active_now:
                return False
            started = await self._controller.async_start_on_plug_in()
            if started:
                await self._notify_change()
            return started

    async def async_solar_start(self, amps: int) -> bool:
        """One solar start, from `SolarController`'s verdict, through the same boundary, lock and
        `ChargingController.async_start` as every other start.

        Admits `hybrid` as well as `solar`; for `hybrid` the caller has already established that no plan
        window is active. Only the pause and the strategy are re-checked, live under the lock. A manual
        Stop shares the lock, so the two cannot interleave, and solar respects it until it arms a fresh
        start. Returns whether the start went out: refused here, held back by load balancing, or not
        executed by the charger's control is `False`, and solar must not believe it runs a charge.
        """
        async with self._lock:
            settings = self._store.settings(self._entry_id)
            token = self._shadow.begin()
            strategy_ok = settings.strategy in (STRATEGY_SOLAR, STRATEGY_HYBRID)
            legacy: list[str] = []
            outcome: CommandOutcome | None = None
            try:
                verdict = self._shadow.verdict(core_events.SolarStart(strategy_ok=strategy_ok))
                refused = pause_blocks_execution(settings) or not strategy_ok
                if refused if verdict is None else not self._shadow.choose("solar_start", not refused, ("start", "solar") in verdict):
                    return False
                legacy.append("start")
                outcome = CommandOutcome(False)
                started = await self._controller.async_start(amps, cause="solar")
                outcome = CommandOutcome(
                    started, balancing_held=not started and self._controller.paused_by_balancing
                )
            finally:
                self._shadow.end(
                    token, core_events.SolarStart(strategy_ok=strategy_ok), legacy=legacy, outcome=outcome
                )
            await self._notify_change()
            return started

    async def async_solar_stop(self) -> bool:
        """One solar stop, through the same lock and `ChargingController.async_stop` as every other
        stop. Never of a charge a person started under their manual pause (`automatic_allowed`).

        Returns `False` only when the stop did not go out (the charger's control did not execute it, or the
        command failed): the charge and who owns it are kept, logged, never raised into solar's evaluation,
        and solar tries again on its next tick. A stop that is not solar's to make is `True`: nothing is
        left for solar to do.
        """
        async with self._lock:
            token = self._shadow.begin()
            legacy: list[str] = []
            outcome: CommandOutcome | None = None
            try:
                verdict = self._shadow.verdict(core_events.SolarStop())
                allowed = self.automatic_allowed(AUTOMATIC_STOP)
                if not allowed if verdict is None else not self._shadow.choose("solar_stop", allowed, ("stop", "solar") in verdict):
                    return True
                legacy.append("stop")
                outcome = CommandOutcome(False)
                try:
                    await self._controller.async_stop(urgent=False)
                except Exception as err:  # noqa: BLE001 - solar keeps the stop pending and retries
                    _LOGGER.warning("Solar's stop did not go out: %s", getattr(err, "code", type(err).__name__))
                    await self._notify_change()
                    return False
                outcome = self._shadow_stop_outcome()
            finally:
                self._shadow.end(token, core_events.SolarStop(), legacy=legacy, outcome=outcome)
            await self._notify_change()
            return True

    async def async_solar_leave(self) -> bool:
        """The strategy left `solar` and `hybrid` while the sun drives a charge: the sun's stop, as ever
        (`async_solar_stop`), unless a plan window open now has taken the charge over already (its window start
        found it running and made it the plan's, with no command): that charge is not the sun's to stop. Returns
        `False` only when the stop did not go out."""
        async with self._lock:
            settings = self._store.settings(self._entry_id)
            controller = self._controller
            if (
                settings.strategy not in (STRATEGY_SOLAR, STRATEGY_HYBRID)
                and controller.charge_origin == "plan_window"
                and controller.charge_control_on
                and controller.plan_window_active_now
            ):
                return True
        return await self.async_solar_stop()

    async def async_solar_take_over_stop(self) -> bool:
        """The stop of a charge the charger began by itself that solar found nothing to keep on
        (`SolarExecutionCoordinator._take_over`), decided again under the lock: never once Auto is
        paused, the strategy left solar and hybrid, a hybrid plan window or a top-off owns the charger,
        or the charge is no longer one the charger began by itself (a person's Start, a plan's).
        Returns whether it stopped.
        """
        async with self._lock:
            settings = self._store.settings(self._entry_id)
            controller = self._controller
            token = self._shadow.begin()
            facts = self._shadow_take_over_facts(settings)
            legacy: list[str] = []
            outcome: CommandOutcome | None = None
            try:
                verdict = None if facts is None else self._shadow.verdict(core_events.SolarStop(take_over=True, **facts))
                if verdict is not None:
                    if ("stop", "take_over") not in verdict:
                        return False
                elif pause_blocks_execution(settings) or settings.strategy not in (
                    STRATEGY_SOLAR, STRATEGY_HYBRID
                ):
                    return False
                elif controller.top_off_until is not None or (
                    settings.strategy == STRATEGY_HYBRID and controller.plan_window_active_now
                ):
                    return False
                elif not controller.self_started_charge():
                    return False
                legacy.append("stop")
                outcome = CommandOutcome(False)
                await controller.async_stop(urgent=False)
                outcome = self._shadow_stop_outcome()
            finally:
                if facts is None:
                    self._shadow.cancel(token)
                else:
                    self._shadow.end(
                        token, core_events.SolarStop(take_over=True, **facts), legacy=legacy, outcome=outcome
                    )
            await self._notify_change()
            return True

    def _shadow_take_over_facts(self, settings: AutoSettings) -> dict[str, Any] | None:
        """What a take-over decides from, read before it (the ownership shadow), or `None` when unreadable."""
        controller = self._controller
        try:
            return {
                "strategy_ok": settings.strategy in (STRATEGY_SOLAR, STRATEGY_HYBRID),
                "top_off_or_window": controller.top_off_until is not None
                or (settings.strategy == STRATEGY_HYBRID and controller.plan_window_active_now),
                "charging": controller.charging,
                "start_pending": controller._shadow_start_pending(),  # noqa: SLF001 - read without its side effect
                "stop_recent": controller._shadow_stop_recent(),  # noqa: SLF001
            }
        except Exception:  # noqa: BLE001 - the shadow never raises into the real path
            return None

    async def async_solar_set_current(self, amps: int) -> None:
        """One solar modulation: recorded as a *requested* current only
        (`ChargingController.async_set_requested_current`), never `async_start`, which would write
        the charger at once and undamped.
        """
        async with self._lock:
            settings = self._store.settings(self._entry_id)
            if pause_blocks_execution(settings) or settings.strategy not in (
                STRATEGY_SOLAR, STRATEGY_HYBRID
            ):
                return
            await self._controller.async_set_requested_current(amps)

    async def async_note_reconcile_failed(self) -> None:
        """Record a post-commit reconcile failure in the execution vocabulary."""
        self._last_error = EXECUTION_RECONCILE_FAILED
        await self._notify_change()

    def _manual_effect_committed(self, before: AutoSettings, action: str) -> bool:
        """Whether the action's own effect is already in the committed record."""
        after = self._store.settings(self._entry_id)
        if action == ACTION_RESUME:
            return before.pause.admitted and not after.pause.admitted
        if action in (ACTION_STOP, ACTION_START):
            # A pause admitted, or a manual pause taken or changed by a person's Start or Stop.
            return after.pause.admitted and after.pause != before.pause
        return False

    def _pause_transition(
        self, choice: PauseChoice, stored: PauseIntent, now: datetime
    ) -> tuple[str, PauseIntent]:
        """What this pause request means, and the intent it should leave stored.

        * `retry`: the same choice is already admitted and authoritative; the stored intent is reused
          unchanged (no write, revision or timer change) and only the physical stop is retried.
        * `admit`: no pause is stored, it ran out, or a different choice was asked for; the choice is
          resolved once to an absolute instant and written before any stop.
        """
        if stored.admitted and stored.choice == choice:
            if self._last_error == EXECUTION_PAUSE_STOP_FAILED or stored.is_active_at(now):
                return "retry", stored
        return "admit", self._resolve_pause(choice, now)

    async def async_settle_pause(self) -> bool:
        """Clear a pause whose instant has passed, exactly once; returns whether it cleared one.

        Called by the expiry appointment and by any Auto event arriving after it. The write is guarded by
        the intent it replaces, so the second caller writes nothing. The attempt is invalidated first so an
        in-flight calculation cannot install after the clock moved. A clear that cannot be persisted keeps
        the intent, reports `pause_clear_failed` and applies nothing.
        """
        async with self._lock:
            intent = self._store.settings(self._entry_id).pause
            if not intent.admitted or not intent.ended_at(self._now()):
                return False
            self.begin_attempt()
            self._pending = None
            token = self._shadow.begin()
            try:
                await self._store.async_update(
                    self._entry_id, mutate=lambda current: replace(current, pause=PauseIntent())
                )
            except AutoSettingsError as err:
                self._shadow.cancel(token)
                # A refusal (unreadable or conflicting record): the intent stands untouched.
                _LOGGER.warning("Clearing an expired Auto pause was refused: %s", err.code)
                self._last_error = EXECUTION_PAUSE_CLEAR_FAILED
                await self._notify_change()
                return False
            except Exception as err:  # noqa: BLE001 - the persistence boundary itself failed
                self._shadow.cancel(token)
                # A failed write: reported by its stable code, and the intent stands.
                _LOGGER.warning(
                    "Clearing an expired Auto pause failed to persist: %s", type(err).__name__
                )
                self._last_error = EXECUTION_PAUSE_CLEAR_FAILED
                await self._notify_change()
                return False
            self._last_error = None
            self._arm_pause_expiry(None)
            self._shadow.end(token, core_events.Resume(reason=core_events.RESUME_EXPIRED))
            await self._rearm_kept_plan()
            await self._notify_change()
            return True

    async def async_settle_and_reconcile(self) -> bool:
        """Settle an expired pause and, if it cleared one, reconcile the current proposal.

        Reconciliation re-reads the live snapshot under the usual refusal rules. A failure after a
        committed clear cannot be undone: the pause stays cleared, the failure gets its own stable code, and
        the next event reconciles again.
        """
        if not await self.async_settle_pause():
            return False
        snapshot = None if self._live_snapshot is None else self._live_snapshot()
        settings = self._store.settings(self._entry_id)
        if snapshot is None:
            return True
        try:
            await self.async_reconcile(settings, snapshot, attempt=self.begin_attempt())
        except Exception as err:  # noqa: BLE001 - the clear is committed; report, never restore
            _LOGGER.warning(
                "Reconciling after an expired Auto pause failed: %s", type(err).__name__
            )
            self._last_error = EXECUTION_RECONCILE_FAILED
            await self._notify_change()
        return True

    async def async_restore_pause(self) -> None:
        """Re-arm the appointment a stored bounded pause asks for, after a restart."""
        if self._shutdown:
            return
        intent = self.pause_intent
        if not intent.admitted:
            self._arm_pause_expiry(None)
            return
        if intent.ended_at(self._now()):
            await self.async_settle_and_reconcile()
            return
        self._arm_pause_expiry(intent.expires_at)

    def _arm_pause_expiry(self, expires_at: datetime | None) -> None:
        """One appointment at one instant for this charger's pause, or none.

        Not a source of truth: the callback settles the pause through the same guarded store write, so an
        early, late or duplicated firing is recoverable.
        """
        if self._cancel_pause is not None:
            self._cancel_pause()
            self._cancel_pause = None
        if expires_at is None or self._shutdown:
            return
        self._cancel_pause = async_track_point_in_time(
            self._hass, self._async_pause_expired, expires_at
        )

    async def _async_pause_expired(self, now: datetime) -> None:
        """The appointment fired: settle and reconcile if due, else re-arm; each case leaves one
        appointment.

        * due: settled and reconciled, nothing re-armed;
        * early: nothing written, the original instant is armed again;
        * due but not settled (clear refused or not persisted): one retry a documented delay later, never an
          immediate re-fire.
        """
        self._cancel_pause = None
        if self._shutdown:
            return
        settled = False
        try:
            settled = await self.async_settle_and_reconcile()
        except Exception as err:  # noqa: BLE001 - a scheduled callback must never raise
            _LOGGER.warning("Settling an expired Auto pause failed: %s", type(err).__name__)
        if self._shutdown or settled:
            return
        intent = self.pause_intent
        if not intent.admitted or intent.expires_at is None:
            return
        self._arm_pause_expiry(
            intent.expires_at if intent.expires_at > now else now + PAUSE_RETRY_DELAY
        )

    def pause_choices(self) -> tuple[PauseChoice, ...]:
        """The pause choices this charger can honour now, in the product's order.

        `next_period` needs a period of the running plan still ahead, `until_tomorrow` a known market zone,
        `until_resumed` is always available. While a failed stop is current, the stored intent's own choice
        is admissible again (the retry).
        """
        choices: list[PauseChoice] = []
        plan = self._controller.plan
        now = self._now()
        if plan is not None and any(start > now for start, _ in plan.windows):
            choices.append(PAUSE_NEXT_PERIOD)
        if self._next_market_midnight(now) is not None:
            choices.append(PAUSE_UNTIL_TOMORROW)
        choices.append(PAUSE_UNTIL_RESUMED)
        stored = self._store.settings(self._entry_id).pause
        if (
            self._last_error == EXECUTION_PAUSE_STOP_FAILED
            and stored.choice is not None
            and not stored.manual
            and stored.choice not in choices
        ):
            choices.append(stored.choice)
        return tuple(choices)

    def _resolve_pause(self, choice: PauseChoice, now: datetime) -> PauseIntent:
        """One choice, resolved to the instant it ends, or refused by code."""
        if choice not in (PAUSE_NEXT_PERIOD, PAUSE_UNTIL_TOMORROW, PAUSE_UNTIL_RESUMED):
            _refuse_pause(f"{choice!r} is not a pause choice this release knows")
        if choice == PAUSE_UNTIL_RESUMED:
            return PauseIntent(choice=choice, admitted_at=now, expires_at=None)
        if choice == PAUSE_NEXT_PERIOD:
            plan = self._controller.plan
            starts = [] if plan is None else [start for start, _ in plan.windows]
            upcoming = [start for start in starts if start > now]
            if not upcoming:
                _refuse_pause("no planned period is still ahead, so there is nothing to pause until")
            return PauseIntent(choice=choice, admitted_at=now, expires_at=min(upcoming))
        expires_at = self._next_market_midnight(now)
        if expires_at is None:
            _refuse_pause("the charger's market zone is not known, so tomorrow cannot be resolved")
        return PauseIntent(choice=choice, admitted_at=now, expires_at=expires_at)

    def _next_market_midnight(self, now: datetime) -> datetime | None:
        """The next midnight in the charger's market zone, as an absolute instant."""
        snapshot = None if self._live_snapshot is None else self._live_snapshot()
        tz = None if snapshot is None else getattr(snapshot, "timezone", None)
        if not isinstance(tz, str) or not tz:
            return None
        zone = dt_util.get_time_zone(tz)
        if zone is None:
            return None
        local = now.astimezone(zone)
        midnight = datetime.combine(local.date() + timedelta(days=1), dt_time(0, 0), tzinfo=zone)
        return midnight.astimezone(timezone.utc)

    def attach_preview(self, preview: Any) -> None:
        """Take the two facts this boundary needs from the preview, once: the live price
        identity to read and execution changes to publish. Two callables, not the object, so the
        boundary cannot reach for anything else the preview knows.
        """
        self._live_snapshot = preview.snapshot
        # The preview publishes execution facts, so it must hear when they change: an install
        # at a window boundary would otherwise only show on the next price notification.
        self._change_hook = preview.async_note_execution_change


def _refuse_pause(message: str) -> None:
    """A pause this boundary cannot honour, as the settings vocabulary's own code."""
    raise AutoSettingsError("invalid_pause", message)
