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
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from homeassistant.core import callback, Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from ..const import (
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
    CONF_REGULATOR_DEADBAND_A,
    CONF_REGULATOR_DWELL_S,
    CONF_SAFETY_MARGIN_A,
    CONF_SITE_CURRENT_SIGNED,
    CONF_SITE_CURRENT_SOURCE,
    CONF_SITE_ENABLED,
    CONF_YIELD_CEILING_A,
    CONF_YIELD_STEPPING_ENABLED,
    DEFAULT_MAX_AGE_S,
    DEFAULT_MIN_CURRENT_A,
    DEFAULT_REGULATOR_DEADBAND_A,
    DEFAULT_REGULATOR_DWELL_S,
    default_yield_ceiling_a,
    DEFAULT_YIELD_STEPPING_ENABLED,
    max_yield_ceiling_a,
    MEASUREMENT_MODE_DERIVED,
    MEASUREMENT_MODE_DIRECT,
    SITE_RECOMPUTE_INTERVAL_S,
)
from ..execution.controller import (
    CurrentRestore,
    RESTORE_FAILED,
    RESTORE_NOT_NEEDED,
    RESTORE_RESTORED,
)
from ..execution.yield_stepping import YieldConfig, YieldObservation, YieldStepper, YieldVerdict
from ..planning.auto_settings import STRATEGY_HYBRID, STRATEGY_SOLAR
from ..runtime import charger_data, controller_for, domain_data
from ..vehicles.capability import build_capability_snapshot, SiteCapabilitySnapshot
from .measurement_problem import measurement_problem, MeasurementProblem
from .solar_capability import solar_capability
from .measurement_source import (
    combine_power_pair,
    grid_power_source_from_dict,
    PhaseMeasurementSource,
    read_phase_measurement,
    source_from_dict,
)
from .regulator_damping import RegulatorDamper
from .regulator import (
    allocate_regulator_decisions,
    applyability_failure,
    classify_direction,
    confirmed_direction,
    DEFAULT_HELP_MARGIN_FACTOR,
    DEFAULT_MIN_CONSECUTIVE_CONFIRMATIONS,
    DEFAULT_ZERO_MARGIN_W,
    DirectionClass,
    MUST_LOWER_REASONS,
    RegulatorDecision,
)
from .site_capacity import (
    ACTIVE_CONTROL_READY,
    BatteryYieldEstimate,
    calculate_site_capacity,
    ChargerRequest,
    classify_apparent_power,
    classify_current,
    classify_phase_liveness,
    classify_power,
    classify_voltage,
    DerivedPhaseInput,
    DerivedPhaseMeasurement,
    DirectPhaseMeasurement,
    PhaseName,
    PHASES,
    PhaseValue,
    SiteCalculationConfig,
    SiteCapacityResult,
)
from .site_membership import find_site_membership_conflicts, SiteMembershipConflict


# Rolling window per phase for consecutive-direction confirmation; longer than
# DEFAULT_MIN_CONSECUTIVE_CONFIRMATIONS so one bad reading ages out.
_DIRECTION_HISTORY_LENGTH = 10

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


class SiteCapacityController:
    """Computes and exposes the site capacity snapshot and, when opted in, applies active control.

    `membership_conflicts` is computed live: a charger shared with another site entry blocks the
    apply step (the regulator calculation continues), so a charger of doubtful ownership is never
    written from here.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, config: dict[str, Any]) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.config = config
        self._timer_cancel: Callable[[], None] | None = None
        self._state_listener_cancel: Callable[[], None] | None = None
        self._controller_listener_cancels: list[Callable[[], None]] = []
        self._listeners: set[Callable[[], None]] = set()
        # In-flight active-control apply pass, if any: prevents overlapping passes and lets shutdown
        # cancel it.
        self._apply_task: asyncio.Task[None] | None = None
        # Serialises active-control transitions for this instance; a transition on a closed (shut-
        # down) controller is refused.
        self.transition_lock = asyncio.Lock()
        self._closed = False
        # Per-charger damping state (`site/regulator_damping.py`).
        self._dampers: dict[str, RegulatorDamper] = {}
        # Per-charger yield steppers, created lazily; an options change reloads the entry and builds
        # a fresh controller.
        self._yield_steppers: dict[str, YieldStepper] = {}
        # Latest yield verdict per charger, for the diagnostic attribute only; never read back into
        # a decision.
        self._yield_stepping_last: dict[str, dict[str, Any]] = {}
        # Last logged yield-stepping signature per charger (logged once per change).
        self._logged_yield_stepping: dict[str, tuple[Any, ...]] = {}
        # Clock for `YieldObservation.now`, in seconds. Monotonic so a wall-clock step cannot freeze
        # the stepper; replaceable in tests.
        self._yield_now: Callable[[], float] = time.monotonic
        # Last logged apply outcome per charger, so a line is emitted per change rather than per
        # recompute.
        self._logged_apply_outcomes: dict[str, tuple[Any, ...]] = {}
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
        self.result: SiteCapacityResult = self._calculate()
        self._update_direction_history()
        # One regulator decision per associated charger (`site/regulator.py`); acting on them is
        # `_async_apply_active_control`'s job.
        self.regulator_decisions: dict[str, RegulatorDecision] = (
            self._compute_regulator_decisions()
        )

    @property
    def membership_conflicts(self) -> list[SiteMembershipConflict]:
        """Computed fresh on every read, so a sibling site's change is visible without waiting for a
        recompute.
        """
        return self._check_membership()

    async def async_initialize(self) -> None:
        """Recompute once, then on a periodic timer, tracked-entity state changes and each charger
        controller's listener (a requested-current change need not change any HA state).
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
        self._register_controller_listeners()

    async def async_shutdown(self) -> None:
        """Cancel the timer and listeners and any unfinished apply pass."""
        self._closed = True
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None
        if self._state_listener_cancel is not None:
            self._state_listener_cancel()
            self._state_listener_cancel = None
        for cancel in self._controller_listener_cancels:
            cancel()
        self._controller_listener_cancels.clear()
        if self._apply_task is not None and not self._apply_task.done():
            self._apply_task.cancel()
        self._apply_task = None

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
        tick, so the sensor would otherwise show the previous snapshot. Re-entry into the
        coordinator is coalesced by its `_evaluating` guard.
        """
        self._notify()

    @callback
    def _async_periodic_recompute(self, _now: Any) -> None:
        self._recompute()

    @callback
    def _async_state_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._recompute()

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
        self.result = self._calculate()
        self._update_direction_history()
        previous_decisions = self.regulator_decisions
        self.regulator_decisions = self._compute_regulator_decisions()
        _log_transitions(self.entry_id, previous, self.result)
        _log_membership_conflicts(self.entry_id, previous_conflicts, current_conflicts)
        _log_regulator_decisions(
            self.entry_id, previous_decisions, self.regulator_decisions
        )
        self._last_logged_conflicts = current_conflicts
        # The only path from a regulator decision to a charger command: queued, never awaited
        # (`_recompute` is a sync callback), and only when both gates hold.
        if schedule_apply and self._active_control_allowed():
            self._schedule_apply_active_control()
        self._notify()

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
        )
        if self._logged_apply_outcomes.get(charger_entry_id) == signature:
            return
        self._logged_apply_outcomes[charger_entry_id] = signature
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

    def _active_control_allowed(self) -> bool:
        """Both gates: the compile-time `ACTIVE_CONTROL_READY` and this site's
        `CONF_ACTIVE_CONTROL_ENABLED` option (off by default). They are independent so neither alone
        can move a charger.
        """
        return bool(ACTIVE_CONTROL_READY) and bool(
            self.config.get(CONF_ACTIVE_CONTROL_ENABLED, False)
        )

    def _schedule_apply_active_control(self) -> None:
        """Queue one apply pass unless one is in flight; the running pass re-reads its measurements
        and the next recompute supersedes anything skipped.
        """
        if self._apply_task is not None and not self._apply_task.done():
            return
        self._apply_task = self.hass.async_create_task(self._async_apply_active_control())

    async def _async_apply_active_control(self) -> None:
        """Write this recompute's decisions to the chargers that opted in.

        The only place a regulator decision becomes a charger command; gated by
        `_active_control_allowed`. Per charger, in decision order:

        1. skip a charger that is not commandable (`_is_commandable_charger`);
        2. skip a decision with no proposed current (a refusal is never a silent keep);
        3. hold unless `applyability_failure` passes on freshly re-read measurements;
        4. if yield stepping is enabled and no pilot probe is in flight, consult the `YieldStepper`:
           "hold" writes nothing, "write" replaces the proposal, "passthrough" keeps it (all pass
           `urgent` on);
        5. let the charger's `RegulatorDamper` decide between write and hold;
        6. round to whole amps (debug-logged when it changes the value) and call the charger's
           `_async_assign_current`.

        Outcomes are logged once per change (`_log_active_control_outcome`). This method knows
        nothing about why a decision says what it says; that is `site/regulator.py`.
        """
        if not self._active_control_allowed():
            return
        # A charger of doubtful ownership is never written; held proposals are still logged.
        if self.membership_conflicts:
            for charger_entry_id, decision in self.regulator_decisions.items():
                if decision.proposed_current_a is not None:
                    self._log_active_control_outcome(
                        charger_entry_id,
                        decision,
                        outcome="held",
                        detail="membership_conflict",
                    )
            return
        fresh = self._calculate()
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        margin_by_charger = self._most_restrictive_margin_by_charger(fresh)
        for charger_entry_id, decision in self.regulator_decisions.items():
            # Re-checked per charger: a pass admitted while active control was on must not
            # keep writing after it was turned off (see `async_disable_active_control`).
            if not self._active_control_allowed():
                return
            charger_controller = controller_for(self.hass, charger_entry_id)
            if not self._is_commandable_charger(charger_controller):
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
                continue
            proposed = decision.proposed_current_a
            if proposed is None:
                continue
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
                continue
            # Damping decides write or hold; consulted every pass so a pending value's dwell
            # advances.
            damper = self._damper_for(charger_entry_id)
            # Captured before `consider`, which advances this charger's own
            # Captured before `consider` advances the damper; also the yield stepper's `assigned_a`
            # (the value last written).
            previous_setpoint = damper.last_written_a

            # Yield-verified stepping, only when enabled. The probe gate matters even though writes
            # are suppressed during a probe: its artificial steps would contaminate the observation.
            yield_verdict: YieldVerdict | None = None
            if (
                bool(self.config.get(CONF_YIELD_STEPPING_ENABLED, DEFAULT_YIELD_STEPPING_ENABLED))
                and not charger_controller._probe.in_flight
            ):
                yield_verdict = self._consult_yield_stepper(
                    charger_entry_id=charger_entry_id,
                    decision=decision,
                    fresh=fresh,
                    assigned_a=previous_setpoint,
                )
                self._record_yield_stepping_verdict(charger_entry_id, yield_verdict)
                self._log_yield_stepping_transition(charger_entry_id, yield_verdict, fresh)

            if yield_verdict is not None and yield_verdict.action == "hold":
                # A hold carries no current: skip the damper and write nothing.
                self._log_active_control_outcome(
                    charger_entry_id,
                    decision,
                    outcome="held",
                    detail=f"yield_stepping_{yield_verdict.reason}",
                )
                continue

            if yield_verdict is not None and yield_verdict.action == "write":
                # The stepper's own current replaces the raw proposal; `urgent` lets a revert skip
                # deadband and dwell.
                damping = damper.consider(
                    proposed_current_a=yield_verdict.current_a,
                    margin_a=margin_by_charger.get(charger_entry_id),
                    urgent=yield_verdict.urgent,
                )
            else:
                # Passthrough: the raw proposal, urgent only if the stepper says so.
                damping = damper.consider(
                    proposed_current_a=proposed,
                    margin_a=margin_by_charger.get(charger_entry_id),
                    urgent=yield_verdict.urgent if yield_verdict is not None else False,
                )
            if not damping.write or damping.current_a is None:
                self._log_active_control_outcome(
                    charger_entry_id,
                    decision,
                    outcome="held",
                    detail=f"damping_{damping.reason}",
                )
                continue
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
            if not self._active_control_allowed():
                return
            # The charger's own write path owns the OCPP read-modify-write and stays best-effort; any
            # other adapter may refuse by policy, and then the fuse decides (stop or hold).
            write = await charger_controller.async_apply_regulated_current(
                int(setpoint),
                must_lower=(
                    decision.reason in MUST_LOWER_REASONS
                    or damping.reason in ("protection", "urgent")
                ),
            )
            if not write.written:
                # Nothing went out: the damper must not believe it did, or the next deadband and the
                # restore would be measured against a value the charger never had.
                damper.forget_write(previous_setpoint)
                self._log_active_control_outcome(
                    charger_entry_id,
                    decision,
                    outcome="held",
                    detail=f"adapter_{write.code}" if write.outcome == "held" else write.code,
                )
                continue
            self._log_active_control_outcome(
                charger_entry_id,
                decision,
                outcome="wrote",
                detail=damping.reason,
                setpoint=int(setpoint),
                previous_setpoint=(
                    None if previous_setpoint is None else round(previous_setpoint)
                ),
            )

    @property
    def active_control_enabled(self) -> bool:
        """The person's opt-in as this running controller holds it -- the value every gate reads."""
        return bool(self.config.get(CONF_ACTIVE_CONTROL_ENABLED, False))

    def _reset_active_control_state(self) -> None:
        """Forget damping, yield-stepping state, its diagnostics and log memory; the observation
        side keeps running.
        """
        self._dampers.clear()
        self._yield_steppers.clear()
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
        `ChargingController.async_restore_current`, one after another; (5) notify listeners.

        A charger that cannot be restored is reported `failed` with a stable code; the opt-in stays
        off regardless. Callers hold `transition_lock`.
        """
        if self._closed:
            raise SiteControllerClosed
        was_enabled = self.active_control_enabled
        self.config[CONF_ACTIVE_CONTROL_ENABLED] = False
        task = self._apply_task
        self._apply_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a failing pass is being retired, nothing more
                _LOGGER.debug("Retired active-control pass had failed", exc_info=True)
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
        outcomes = {report.restore.outcome for report in reports}
        if RESTORE_FAILED in outcomes:
            outcome = RESTORE_FAILED
        elif RESTORE_RESTORED in outcomes:
            outcome = RESTORE_RESTORED
        else:
            outcome = RESTORE_NOT_NEEDED
        self._notify()
        return RestoreReport(outcome, tuple(reports))

    def _damper_for(self, charger_entry_id: str) -> RegulatorDamper:
        """This charger's damping state, created on first use from the site's deadband and dwell.
        Per charger because the rule depends on that charger's own history.
        """
        damper = self._dampers.get(charger_entry_id)
        if damper is None:
            damper = RegulatorDamper(
                deadband_a=float(
                    self.config.get(CONF_REGULATOR_DEADBAND_A, DEFAULT_REGULATOR_DEADBAND_A)
                ),
                dwell_s=float(
                    self.config.get(CONF_REGULATOR_DWELL_S, DEFAULT_REGULATOR_DWELL_S)
                ),
            )
            self._dampers[charger_entry_id] = damper
        return damper

    def _yield_stepper_for(self, charger_entry_id: str) -> YieldStepper:
        """This charger's `YieldStepper`, created on first use. `ceiling_a` comes from the site
        option, `min_current_a` from the charger's wiring (as for the regulator).
        """
        stepper = self._yield_steppers.get(charger_entry_id)
        if stepper is None:
            wiring: dict[str, Any] = (self.config.get(CONF_PHASE_WIRING) or {}).get(
                charger_entry_id
            ) or {}
            main_fuse_a = self.config.get(CONF_MAIN_FUSE_A)
            default_ceiling_a = (
                default_yield_ceiling_a(float(main_fuse_a)) if main_fuse_a is not None else 0.0
            )
            stepper = YieldStepper(
                YieldConfig(
                    # Clamped as well as validated: a stored value can outlive the fuse it was
                    # chosen for.
                    ceiling_a=min(
                        float(self.config.get(CONF_YIELD_CEILING_A, default_ceiling_a)),
                        max_yield_ceiling_a(float(main_fuse_a)) if main_fuse_a is not None else 0.0,
                    ),
                    min_current_a=float(wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A)),
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
        observation = YieldObservation(
            now=self._yield_now(),
            site_current_a=site_current_a,
            delivered_current_a=delivered_current_a,
            phases=phases,
        )
        return stepper.observe(decision.proposed_current_a, requested_a, assigned_a, observation)

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
                "net_grid_w": state.net_grid_w if state else None,
                "car_w": state.car_w if state else None,
                "battery_w": state.battery_w if state else None,
                "export_w": state.export_w if state else None,
                "priority_effective": state.priority_effective if state else None,
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
        return calculate_site_capacity(self._build_config(), self._build_requests())

    def _build_config(self) -> SiteCalculationConfig:
        mode = self.config.get(CONF_MEASUREMENT_MODE)
        main_fuse_a = self.config.get(CONF_MAIN_FUSE_A)
        direct = self._read_direct_entities() if mode == MEASUREMENT_MODE_DIRECT else None
        derived = self._read_derived_entities() if mode == MEASUREMENT_MODE_DERIVED else None
        return SiteCalculationConfig(
            enabled=bool(self.config.get(CONF_SITE_ENABLED, False)),
            main_fuse_a=float(main_fuse_a) if main_fuse_a is not None else None,
            safety_margin_a=float(self.config.get(CONF_SAFETY_MARGIN_A, 0.0)),
            measurement_mode=mode if mode in (MEASUREMENT_MODE_DIRECT, MEASUREMENT_MODE_DERIVED) else "unavailable",
            max_age_s=float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S)),
            direct=direct,
            derived=derived,
            battery=self._read_battery_estimate(),
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

    def _build_requests(self) -> list[ChargerRequest]:
        charger_entry_ids: list[str] = list(self.config.get(CONF_CHARGER_ENTRY_IDS) or [])
        phase_wiring: dict[str, dict[str, Any]] = self.config.get(CONF_PHASE_WIRING) or {}
        requests: list[ChargerRequest] = []
        for charger_entry_id in charger_entry_ids:
            wiring = phase_wiring.get(charger_entry_id) or {}
            requests.append(
                ChargerRequest(
                    charger_entry_id=charger_entry_id,
                    requested_current_a=self._charger_requested_current(charger_entry_id),
                    phases=wiring.get("phases", 3),
                    phase=wiring.get("phase"),
                    min_current_a=float(wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A)),
                    measured_current_a=self._read_charger_measured_current(wiring),
                )
            )
        return requests

    def _active_power_last_reported(self, phase: PhaseName) -> datetime | None:
        """The `last_reported` timestamp of this phase's active-power entity, read directly off
        `hass.states`.

        `None` when it cannot be determined: not derived mode, no entity configured, no state yet,
        or no `last_reported` support.
        """
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
        arrived.

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
            signed_w = self.result.phase_signed_active_power_w.get(phase)
            self._direction_history[phase].append(classify_direction(signed_w, DEFAULT_ZERO_MARGIN_W))

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
            else {phase: None for phase in PHASES}
        )
        confirmed_direction_by_phase: dict[PhaseName, DirectionClass | None] = {
            phase: confirmed_direction(
                list(self._direction_history[phase]), DEFAULT_MIN_CONSECUTIVE_CONFIRMATIONS
            )
            for phase in PHASES
        }
        return allocate_regulator_decisions(
            site_result=self.result,
            requests=self._build_requests(),
            voltage_by_phase=voltage_by_phase,
            confirmed_direction_by_phase=confirmed_direction_by_phase,
            max_age_s=config.max_age_s,
            zero_margin_w=DEFAULT_ZERO_MARGIN_W,
            help_margin_factor=DEFAULT_HELP_MARGIN_FACTOR,
        )

    def charger_measured_current(self, charger_entry_id: str) -> DirectPhaseMeasurement | None:
        """One associated charger's raw measured current, for entities and diagnostics."""
        phase_wiring: dict[str, dict[str, Any]] = self.config.get(CONF_PHASE_WIRING) or {}
        return self._read_charger_measured_current(phase_wiring.get(charger_entry_id) or {})

    def grid_total_power(self) -> PhaseValue | None:
        """The meter's total grid power, live: signed (positive = import) with its freshness.

        `None` only when no total is configured (`CONF_GRID_POWER_SOURCE`); a configured one always
        returns a `PhaseValue`, even if currently unusable. An import/export pair is import minus
        export, and a missing or invalid half makes the whole value missing or invalid, never zero.
        `CONF_GRID_POWER_INVERTED` negates it for an export-positive meter. Used by the solar
        executor and diagnostics; never by the fuse protection, which stays per phase on the measured
        currents.
        """
        source = grid_power_source_from_dict(self.config.get(CONF_GRID_POWER_SOURCE))
        if source is None:
            return None
        return combine_power_pair(
            self._power_phase_value(source.power),
            self._power_phase_value(source.power_export) if source.power_export else None,
            invert=bool(self.config.get(CONF_GRID_POWER_INVERTED, False)),
        )

    def grid_total_reading(self) -> tuple[float | None, str]:
        """`(watts, state)` of the meter's total grid power, signed (positive = import).

        `state` is `not_configured`, `missing`, `invalid`, `stale` or, for a usable reading, `fresh` or
        `confirmed_unchanged` (as `classify_phase_liveness` grades the phases: a value Home Assistant
        stopped updating but keeps hearing from is accepted). Watts only for a usable reading, so a
        stale or missing total is unknown, never zero export.
        """
        total = self.grid_total_power()
        if total is None:
            return None, "not_configured"
        if total.problem == "invalid":
            return None, "invalid"
        if total.problem is not None or total.value is None:
            return None, "missing"
        max_age_s = float(self.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
        liveness = classify_phase_liveness(total.age_s, total.report_age_s, max_age_s)
        if liveness in ("fresh", "confirmed_unchanged") and total.age_s is not None:
            return total.value, liveness
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
            # entity existing is not enough to be written to.
            charger_is_commandable={
                charger_entry_id: self._is_commandable_charger(
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
        )

    def _charger_requested_current(self, charger_entry_id: str) -> float | None:
        """How much this charger asks to draw, for capacity purposes.

        `0.0` when it is off or not loaded; otherwise `ChargingController.resolve_current()`
        (SpotNav's request, else the current-limit entity's setpoint). `None` (unknown, never a
        guessed `0.0`) when it is charging and neither resolves, so the site calculation does not
        treat an unquantified load as requesting nothing.
        """
        charger_controller = controller_for(self.hass, charger_entry_id)
        if charger_controller is None or not charger_controller.charging:
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
        return measurement_problem(self.result, self.phase_entities())

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

    def _read_derived_entities(self) -> DerivedPhaseMeasurement:
        entities: dict[str, dict[str, str]] = self.config.get(CONF_DERIVED_ENTITIES) or {}
        invert = bool(self.config.get(CONF_GRID_POWER_INVERTED, False))
        signed_current = bool(self.config.get(CONF_SITE_CURRENT_SIGNED, False))

        def build(phase: str) -> DerivedPhaseInput:
            phase_entities = entities.get(phase) or {}
            export_id = phase_entities.get("power_export")
            reactive_id = phase_entities.get("reactive_power")
            apparent_id = phase_entities.get("apparent_power")
            current_id = phase_entities.get("current")
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
                voltage_v=self._voltage_phase_value(phase_entities.get("voltage")),
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
