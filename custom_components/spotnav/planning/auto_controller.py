"""Auto preview controller: one per charger entry.

Reads saved settings, subscribes to the shared price stream for the selected area,
calculates a proposal with the pure planner and publishes an immutable snapshot. Applying a
proposal is delegated to the injected `AutoExecutor`; without one this only calculates.

* A proposal carries a price identity (dates, index revision, fetch time), so a stale
  snapshot is never presented as freshly recalculated.
* Live facts such as a vehicle's state of charge come through an injected reader at
  calculation time, never from the settings store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Final, Literal

from homeassistant.core import callback, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from . import price_wait
from ..execution import hybrid_execution
from ..execution.auto_execution import (
    ACTION_RESUME,
    ACTION_STOP,
    application_for,
    AutoControlCommitted,
    AutoControlError,
    AutoControlRefused,
    AutoExecutor,
    ControlDecision,
    EXECUTION_ACTION_UNAVAILABLE,
    EXECUTION_NOT_APPLIED,
    EXECUTION_RECONCILE_FAILED,
)
from ..execution.solar_execution import site_supports_solar
from ..pricing.market_observation import MarketObservation
from ..pricing.price_refresh import PriceRefreshManager
from ..pricing.relay_contract import AreaEntry
from ..runtime import domain_data
from ..vehicles.soc_estimate import read_energy_register_kwh, SocReader, target_need_kwh
from ..vehicles.vehicle_discovery import resolve_target_vehicle
from .auto_settings import (
    AutoSettings,
    AutoSettingsError,
    AutoSettingsStore,
    DEFAULT_CONSUMPTION_KWH_PER_10KM,
    DRIVER_TARGET_SOC,
    EnergyBaseline,
    PAUSE_UNTIL_RESUMED,
    PauseChoice,
    StoredProposal,
    STRATEGY_HYBRID,
    STRATEGY_SOLAR,
)
from .planner import (
    calculate_plan,
    FiscalChoice,
    plan_unpriced,
    PlannerInputError,
    PlanRequest,
    PlanResult,
    power_kw,
    price_gap,
    resolve_departure,
)


_LOGGER = logging.getLogger(__name__)


#: Owner id prefix for the refresh-manager subscription; never stored.
OWNER_PREFIX: Final = "auto:"

AutoState = Literal[
    "incomplete_settings",
    "waiting_for_prices",
    #: The prices the plan needs are not published yet and everything still fits after they are.
    #: A normal state, not an issue.
    "waiting_for_publication",
    "price_data_stale",
    "proposal_ready",
    "proposal_unpriced",
    "nothing_to_charge",
    "planning_unavailable",
    "error",
]

AutoReason = Literal[
    "settings_missing",
    "area_unknown",
    "fiscal_value_missing",
    "target_capacity_unknown",
    #: A target needs the car's state of charge and no source provides or can estimate one.
    "target_soc_unknown",
    "no_prices_yet",
    "price_data_stale",
    "ready",
    "unpriced",
    #: `waiting_for_publication`: nothing to plan until the missing day's prices arrive.
    "publication_pending",
    #: `proposal_ready`: only the energy that cannot wait is planned, in known prices.
    "buying_before_publication",
    #: `proposal_unpriced`: the prices never came and the latest safe start arrived; the
    #: remainder is charged at once at unknown prices. A notice, not an alarm.
    "charging_without_prices",
    "already_at_target",
    "no_published_prices",
    "price_data_invalid",
    "insufficient_price_horizon",
    "deadline_too_short",
    "missing_fx_rate",
    "unexpected_failure",
    "shutdown",
    #: Strategy is `solar` and the site cannot run it (`solar_execution.site_supports_solar`); the
    #: price planner stands down without being asked for prices.
    "solar_execution_unavailable",
    #: Strategy is `solar` and the site can run it; the price planner stands down and a
    #: `SolarExecutionCoordinator` reports the state (`solar_execution.solar_execution_state`).
    "solar_running",
]


@dataclass(frozen=True, slots=True)
class LiveVehicleFacts:
    """What a vehicle reports right now, supplied at calculation time and never stored.

    `reported_capacity_kwh` is the battery size already resolved for the vehicle; the charger's
    remembered figure is used only when no vehicle resolves. `max_percent` is the vehicle's ceiling.
    """

    vehicle_id: str
    soc_percent: float | None = None
    reported_capacity_kwh: float | None = None
    max_percent: float | None = None
    #: Origin and confidence of `soc_percent`: age of the reading (of the fresh one an estimate was
    #: anchored on), and whether it was carried forward from delivered energy.
    soc_source: str | None = None
    soc_age_s: float | None = None
    soc_estimated: bool = False


def live_vehicle_facts(
    hass: HomeAssistant, soc_source: SocReader, stored_vehicle_id: str
) -> LiveVehicleFacts | None:
    """What the planner knows about the vehicle a target is for, right now.

    Resolved as the dashboard resolves it (`resolve_target_vehicle`), so planning and display agree.
    """
    vehicle_id, _ = resolve_target_vehicle(hass, stored_vehicle_id or None)
    soc_source.ensure_watch(vehicle_id)
    reading = soc_source.read(vehicle_id)
    return LiveVehicleFacts(
        vehicle_id=vehicle_id or "",
        soc_percent=None if reading is None else reading.soc_percent,
        reported_capacity_kwh=soc_source.capacity_kwh(vehicle_id),
        max_percent=soc_source.vehicle_max_percent(vehicle_id),
        soc_source=None if reading is None else reading.source,
        soc_age_s=None if reading is None else reading.age_s,
        soc_estimated=bool(reading is not None and reading.estimated),
    )


@dataclass(frozen=True, slots=True)
class _EnergyResolution:
    """`_energy_for`'s answer: the remaining need, and whether it reflects energy delivered so far.

    A target-SoC need is always trustworthy (live SoC falls as the car charges). A manual need is
    trustworthy only while delivered-energy tracking sees real progress; otherwise `kwh` is the
    stored request unreduced, which stops `hybrid` from crediting forecast sun it cannot verify.
    """

    kwh: float
    delivered_energy_trustworthy: bool


@dataclass(frozen=True, slots=True)
class AutoSnapshot:
    """The controller's whole state as one immutable value.

    Carries a proposal when there is a usable one, plus the compact facts a UI or diagnostics
    dump needs to explain it (no interval arrays).
    """

    state: AutoState
    reason: AutoReason
    settings_revision: int
    generation: int
    calculated_at: datetime | None
    area_id: str | None
    currency: str | None
    major_unit: str | None
    minor_unit: str | None
    timezone: str | None
    missing: tuple[str, ...]
    price_state: str | None
    price_identity: str | None
    today: str | None
    tomorrow: str | None
    proposal: PlanResult | None
    priced_slots: int
    unpriced_slots: int
    unpriced: bool
    applied: bool
    #: Whether `proposal` was calculated by this process; a restored summary never is.
    in_process: bool
    #: The calculation this snapshot came from (a newer attempt makes an older snapshot inert) and
    #: the charger's execution facts, kept separate from this proposal's own state.
    attempt: int
    execution: str
    applied_identity: str | None
    pending_identity: str | None
    last_error_code: str | None
    #: Identity of the application this proposal resolves to, or `None` when it is not executable.
    #: Stamped from the same `application_for` call the execution boundary uses.
    proposal_identity: str | None = None
    #: `waiting`, `buy_now` or `guarantee` when the plan needed unpublished prices (`price_wait.decide`),
    #: else `None`; `publication_at` is the expected publication instant (UTC, margin included) and
    #: `must_buy_kwh` the energy that could not wait.
    price_wait: str | None = None
    publication_at: datetime | None = None
    must_buy_kwh: float | None = None

    def meaningful_key(self) -> tuple[Any, ...]:
        """What a listener hears about, and is not told twice.

        Excludes `calculated_at` and `attempt`: an equal proposal from the same prices is not news.
        Includes the plan's periods and slots, since equal cost at different hours must notify.
        """
        return (
            self.state,
            self.reason,
            self.settings_revision,
            self.area_id,
            self.missing,
            self.price_state,
            self.price_identity,
            self.today,
            self.tomorrow,
            self.priced_slots,
            self.unpriced_slots,
            self.unpriced,
            self.applied,
            self.in_process,
            self.execution,
            self.applied_identity,
            self.pending_identity,
            self.last_error_code,
            self.price_wait,
            self.publication_at,
            self.must_buy_kwh,
            None if self.proposal is None else self._proposal_key(self.proposal),
        )

    @staticmethod
    def _proposal_key(proposal: PlanResult) -> tuple[Any, ...]:
        """A compact deterministic identity for one plan, for change detection.

        Money and slot count are not enough on their own: a plan that moved an hour can cost the same.
        """
        return (
            proposal.reason,
            proposal.slots_needed,
            proposal.estimated_cost,
            tuple((start.isoformat(), end.isoformat()) for start, end in proposal.periods),
            tuple(
                (slot.start.isoformat(), slot.end.isoformat(), slot.unpriced, slot.source)
                for slot in proposal.slots
            ),
        )


#: Stable code reported for a committed-but-unreconciled write; exposed as an attribute so callers never match on a message.
SETTINGS_RECONCILE_FAILED_CODE: Final = "spotnav_settings_reconcile_failed"


class SettingsReconcileError(HomeAssistantError):
    """A settings write that committed and whose reconcile then failed.

    Raised by `AutoPlannerController.async_apply_settings()`. `settings` is the exact committed
    record, captured before reconcile ran. No rollback: it would add a second revision and a second
    chance to fail. The message is a stable sentence because HA surfaces its text to the user.
    """

    code = SETTINGS_RECONCILE_FAILED_CODE

    def __init__(self, settings: AutoSettings) -> None:
        super().__init__(
            "The settings were saved, but applying them failed. Check the charger and retry."
        )
        self.settings = settings


class AutoPlannerController:
    """One charger entry's Auto preview: settings in, a proposal out.

    Subscribes to the refresh manager only while the mode is `auto_price` and an area is chosen.
    Recalculations are generation-guarded, so a callback after an area change, unload or shutdown
    is inert.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        store: AutoSettingsStore,
        manager: PriceRefreshManager,
        *,
        executor: AutoExecutor | None = None,
        vehicle_reader: Callable[[str], LiveVehicleFacts | None] | None = None,
        consumption_reader: Callable[[str], float | None] | None = None,
        observation: MarketObservation | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = store
        self._manager = manager
        # The charger's display subscription, if any: it follows the area whatever the mode, and a
        # settings change is reconciled here. `None` for a controller used on its own (tests).
        self._observation = observation
        # Live-state seam. `None` makes target-SoC mode report a named incomplete state.
        self._vehicle_reader = vehicle_reader
        # The planned vehicle's consumption; `None` means the default.
        self._consumption_reader = consumption_reader
        self._now: Callable[[], datetime] = now if now is not None else dt_util.utcnow
        # The one appointment kept with the clock: the latest safe start of a plan waiting for prices,
        # made through the manager's scheduler so tests can drive time.
        self._wake_cancel: Callable[[], None] | None = None

        self._listeners: list[Callable[[AutoSnapshot], None]] = []
        self._unsubscribe: Callable[[], None] | None = None
        self._subscribed_area: str | None = None
        self._generation = 0
        self._shutdown = False
        self._snapshot: AutoSnapshot | None = None
        self._last_key: tuple[Any, ...] | None = None
        # The per-charger execution boundary, injected. `None` means calculate only.
        self._executor: AutoExecutor | None = executor
        # Attempt id of the calculation behind `self._snapshot`; a newer attempt makes older ones inert.
        self._attempt = 0

    async def async_start(self) -> AutoSnapshot:
        """Bring the controller in line with the stored settings, once."""
        settings = self._store.settings(self._entry_id)
        if settings.area_id:
            await self._subscribe_for(settings)
        # The display need joins after the calculation subscription (the preview is the primary consumer).
        await self._reconcile_observation(settings)
        self._publish(await self._calculate(settings))
        return self.snapshot()

    async def async_shutdown(self) -> None:
        """Stop for good: no more subscription, no more calculation, no more delivery."""
        self._shutdown = True
        self._generation += 1
        self._drop_subscription()
        self._cancel_wake()
        # Forced: shutdown takes a new attempt, so the gate would otherwise drop the stopped state.
        self._publish(await self._state_only("planning_unavailable", "shutdown"), force=True)
        self._listeners.clear()

    def add_listener(self, listener: Callable[[AutoSnapshot], None]) -> Callable[[], None]:
        """A listener for meaningful snapshot changes; the returned handle is idempotent."""
        self._listeners.append(listener)
        if self._snapshot is not None:
            self._deliver(listener, self._snapshot)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def snapshot(self) -> AutoSnapshot:
        """The current immutable snapshot, so a caller never waits for a change to read one."""
        if self._snapshot is None:
            settings = self._store.settings(self._entry_id)
            return self._fresh_snapshot(
                settings,
                "incomplete_settings",
                "settings_missing",
                missing=settings.missing_for_auto(),
            )
        return self._snapshot

    @property
    def entry_id(self) -> str:
        return self._entry_id

    async def async_apply_settings(
        self,
        *,
        mutate: Callable[[AutoSettings], AutoSettings] | None = None,
        expected_revision: int | None = None,
    ) -> AutoSnapshot:
        """The one way a caller changes Auto settings: write them, then reconcile."""
        try:
            if self._executor is not None:
                # Through the executor: the attempt moves before the write so in-flight work cannot
                # act on settings being replaced.
                settings = await self._executor.async_update_settings(
                    mutate=mutate, expected_revision=expected_revision
                )
            else:
                settings = await self._store.async_update(
                    self._entry_id, mutate=mutate, expected_revision=expected_revision, confirm=True
                )
        except AutoSettingsError as err:
            # A refused edit is not a new state: keep the previous snapshot and add the refusal and
            # the current execution facts.
            current = self._store.settings(self._entry_id)
            execution, applied_identity, pending_identity, latest = self._execution_facts()
            snapshot = replace(
                self.snapshot(),
                attempt=latest,
                execution=execution,
                applied_identity=applied_identity,
                pending_identity=pending_identity,
                settings_revision=current.revision,
                last_error_code=err.code,
            )
            self._publish(snapshot, force=True)
            raise
        # The write returned, so `settings` is durable. Anything raised from here is post-commit
        # (including an `AutoSettingsError` from calculation code); no rollback, see the exception.
        committed = settings
        try:
            await self._reconcile(committed)
        except Exception as err:  # noqa: BLE001 - the boundary must classify every failure
            _LOGGER.error(
                "Auto settings committed at revision %s but reconcile failed (%s)",
                committed.revision,
                type(err).__name__,
            )
            raise SettingsReconcileError(committed) from None
        return self.snapshot()

    async def async_pause(self, choice: PauseChoice = PAUSE_UNTIL_RESUMED) -> AutoSnapshot:
        """Pause automatic execution for this charger, and keep calculating.

        A paused charger still publishes proposals but applies none. Only a plan Auto installed is
        stopped and cleared. The executor resolves `choice` to an instant and raises
        `AutoSettingsError("invalid_pause")` before storing anything if it cannot.
        """
        if self._executor is not None:
            await self._executor.async_pause(choice)
        snapshot = await self._calculate(self._store.settings(self._entry_id))
        self._publish(snapshot, force=True)
        return self.snapshot()

    async def async_resume(self) -> AutoSnapshot:
        """Resume execution: recalculate, and let the boundary apply the latest proposal."""
        if self._executor is not None:
            await self._executor.async_resume()
        return await self.async_recalculate()

    async def async_manual_action(
        self, action: str, choice: PauseChoice | None = None
    ) -> ControlDecision:
        """The admission boundary for a card's immediate action: admit, effect, then reconcile.

        The executor decides and performs under its own lock, so two requests admitted against one
        state cannot both take effect. This layer then recalculates (stop) or reconciles (resume);
        a failure there is reported as `AutoControlCommitted`, never as a refusal and never by
        restoring a cleared pause. A manual `start` reconciles nothing on purpose: the next Auto
        event may take the charge over.
        """
        if self._executor is None:
            raise AutoControlRefused(
                EXECUTION_ACTION_UNAVAILABLE, "this charger has no execution boundary"
            )
        decision = await self._executor.async_manual_action(action, choice)
        try:
            if action == ACTION_STOP:
                snapshot = await self._calculate(self._store.settings(self._entry_id))
                self._publish(snapshot, force=True)
            elif action == ACTION_RESUME:
                await self.async_recalculate()
        except AutoControlError:
            raise
        except Exception as err:  # noqa: BLE001 - the effect is committed; report it, never undo it
            _LOGGER.error(
                "A manual %s committed but its reconcile failed (%s)", action, type(err).__name__
            )
            await self._executor.async_note_reconcile_failed()
            raise AutoControlCommitted(EXECUTION_RECONCILE_FAILED) from None
        return decision

    async def async_settings_seeded(self) -> AutoSnapshot:
        """First-run defaults were just stored: subscribe to their area and calculate again."""
        if self._shutdown:
            return self.snapshot()
        await self._reconcile(self._store.settings(self._entry_id))
        return self.snapshot()

    async def async_recalculate(self) -> AutoSnapshot:
        """Recalculate from the settings and prices on hand.

        A shut-down preview returns its final snapshot instead of a proposal nobody can act on.
        """
        if self._shutdown:
            return self.snapshot()
        settings = self._store.settings(self._entry_id)
        self._publish(await self._calculate(settings))
        return self.snapshot()

    async def async_note_execution_change(self) -> AutoSnapshot:
        """Execution changed the facts the snapshot reports: publish again, without recalculating.

        Only the execution facts are re-read (prices have not moved), which also covers a change at a
        window boundary where no price notification arrives. Publishes only when something changed.
        """
        current = self._snapshot
        if current is None:
            return self.snapshot()
        execution, applied_identity, pending_identity, latest = self._execution_facts()
        if (execution, applied_identity, pending_identity) == (
            current.execution,
            current.applied_identity,
            current.pending_identity,
        ):
            return current
        updated = replace(
            current,
            attempt=latest,
            execution=execution,
            applied_identity=applied_identity,
            pending_identity=pending_identity,
        )
        self._publish(updated, force=True)
        return self.snapshot()

    async def _reconcile(self, settings: AutoSettings) -> None:
        """Match the subscription to the settings, then recalculate."""
        self._generation += 1
        if settings.area_id:
            await self._subscribe_for(settings)
        else:
            self._drop_subscription()
        await self._reconcile_observation(settings)
        self._publish(await self._calculate(settings))

    async def _reconcile_observation(self, settings: AutoSettings) -> None:
        """Move the entry's display subscription to the area these settings name.

        The graph belongs to the market whatever the strategy; see `market_observation`.
        """
        if self._observation is None:
            return
        await self._observation.async_reconcile(settings)

    async def _subscribe_for(self, settings: AutoSettings) -> None:
        """Subscribe once for this entry's area, or move the subscription atomically."""
        if self._shutdown or settings.area_id is None:
            return
        if self._subscribed_area == settings.area_id:
            return
        self._drop_subscription()
        generation = self._generation
        owner = f"{OWNER_PREFIX}{self._entry_id}"

        @callback
        def on_area_snapshot(_area_snapshot: Any) -> None:
            # The manager calls listeners synchronously; the calculation is a coroutine, so make it a task.
            self._hass.async_create_task(self._on_area_changed(generation))

        self._unsubscribe = await self._manager.async_subscribe(
            owner_id=owner, area_id=settings.area_id, listener=on_area_snapshot
        )
        self._subscribed_area = settings.area_id

    def _drop_subscription(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = None
        self._subscribed_area = None

    async def _on_area_changed(self, generation: int) -> None:
        """A shared price snapshot changed: recalculate, unless this callback is stale.

        Also settles an expired pause whose appointment never fired (restart, busy scheduler), so the
        recalculation applies through the ordinary path. Idempotent.
        """
        if self._shutdown or generation != self._generation:
            return
        if self._executor is not None:
            await self._executor.async_settle_pause()
        self._publish(await self._calculate(self._store.settings(self._entry_id)))

    async def _calculate(self, settings: AutoSettings) -> AutoSnapshot:
        """One calculation; every outcome, executable or not, reaches the execution boundary.

        The attempt is claimed first (a newer one makes this inert wherever it lands). A result that
        cannot be installed is still handed over so it can supersede a change waiting for a window
        boundary.
        """
        if self._shutdown:
            # Shutdown is terminal: no calculation, and therefore nothing that could apply.
            return await self._state_only("planning_unavailable", "shutdown")
        self._attempt = (
            self._executor.begin_attempt() if self._executor is not None else self._attempt + 1
        )
        attempt = self._attempt
        snapshot = await self._compute(settings, attempt)
        await self._apply(settings, snapshot, attempt)
        return self._republish_execution(snapshot)

    async def _compute(self, settings: AutoSettings, attempt: int) -> AutoSnapshot:
        """Settings and prices in, one snapshot out.

        Order follows dependency: authority, inputs Auto cannot guess, prices, plan. Every failure
        becomes a stable state and reason. Nothing here reaches a charger; applying is `_apply`.
        """
        calculated_at = self._now()
        # Any earlier appointment is void: this calculation re-arms one only if it needs it.
        self._cancel_wake()
        if settings.strategy != STRATEGY_HYBRID:
            # Cleared for every non-hybrid strategy on every path below, so a charger not on `hybrid`
            # reads as "nothing observed" (`hybrid_execution.clear_hybrid_state`).
            hybrid_execution.clear_hybrid_state(self._hass, self._entry_id)
        if settings.strategy == STRATEGY_SOLAR:
            # The price planner stands down for `solar`: no price check, no plan, no persisted proposal.
            # `_apply` still runs, and `AutoExecutor.async_reconcile` clears an Auto-owned plan under its
            # own lock.
            #
            # Price documents are still read, for the snapshot's metadata only: otherwise `price_identity`
            # would be `None` and a later switch back to `cheapest` would be judged against a stale
            # identity by `AutoExecutor._prices_still_live` and held back.
            area_snapshot = None if not settings.area_id else self._manager.area_snapshot(settings.area_id)
            entry = None if area_snapshot is None else area_snapshot.catalogue
            # `solar_execution_unavailable` only for a site that structurally cannot run solar
            # (`site_supports_solar`); otherwise a `SolarExecutionCoordinator` is live for this charger.
            solar_reason: AutoReason = (
                "solar_running" if site_supports_solar(self._hass, self._entry_id) else "solar_execution_unavailable"
            )
            return self._fresh_snapshot(
                settings,
                "planning_unavailable",
                solar_reason,
                calculated_at,
                entry=entry,
                price_snapshot=area_snapshot,
            )
        missing = settings.missing_for_auto()
        if missing:
            return self._fresh_snapshot(settings, "incomplete_settings", "settings_missing", calculated_at, missing=missing)
        assert settings.area_id is not None  # `missing_for_auto` proved it

        area_snapshot = self._manager.area_snapshot(settings.area_id)
        if area_snapshot is None or area_snapshot.catalogue is None:
            return self._fresh_snapshot(settings, "waiting_for_prices", "no_prices_yet", calculated_at)
        entry = area_snapshot.catalogue

        fiscal = self._fiscal_for(settings, entry)
        if fiscal is None:
            return self._fresh_snapshot(
                settings,
                "incomplete_settings",
                "fiscal_value_missing",
                calculated_at,
                entry=entry,
                price_snapshot=area_snapshot,
            )

        resolved = await self._energy_for(settings, calculated_at, entry, area_snapshot)
        if isinstance(resolved, AutoSnapshot):
            return resolved
        energy_kwh = resolved.kwh
        delivered_energy_trustworthy = resolved.delivered_energy_trustworthy

        if energy_kwh <= 0:
            # A manual need fully delivered: answered like `target_soc`'s `already_at_target`, since
            # `calculate_plan` refuses a non-positive `requested_kwh`. A normal state, not a failure.
            if settings.strategy == STRATEGY_HYBRID:
                hybrid_execution.record_satisfied_state(self._hass, self._entry_id)
            return self._fresh_snapshot(
                settings, "nothing_to_charge", "already_at_target", calculated_at,
                entry=entry, price_snapshot=area_snapshot,
            )

        gated = self._price_data_gate(settings, area_snapshot, calculated_at, entry)
        if gated is not None:
            return gated

        documents = tuple(
            snapshot.document
            for snapshot in (area_snapshot.today_snapshot, area_snapshot.tomorrow_snapshot)
            if snapshot.document is not None
        )
        if not documents:  # pragma: no cover - the gate covers every documented day state
            return self._fresh_snapshot(
                settings, "waiting_for_prices", "no_prices_yet", calculated_at,
                entry=entry, price_snapshot=area_snapshot,
            )

        if settings.strategy == STRATEGY_HYBRID:
            # Plan `grid_kwh` instead of the full need, over the same inputs the cheapest planner uses;
            # `energy_kwh` is the remaining need `_energy_for` produced, never a second accounting.
            hybrid_outcome = await hybrid_execution.async_plan_hybrid(
                self._hass,
                self._entry_id,
                settings,
                documents=documents,
                tz=entry.tz,
                currency=entry.currency,
                fiscal=fiscal,
                now=calculated_at,
                remaining_need_kwh=energy_kwh,
                delivered_energy_trustworthy=delivered_energy_trustworthy,
            )
            plan_window_active = (
                False if self._executor is None else self._executor.controller.plan_window_active_now
            )
            hybrid_execution.record_hybrid_state(
                self._hass, self._entry_id, hybrid_outcome, plan_window_active=plan_window_active
            )
            energy_kwh = hybrid_outcome.result.grid_kwh

        plan_request = PlanRequest(
            area_id=entry.id,
            timezone=entry.tz,
            currency=entry.currency,
            major_unit=entry.major_unit,
            minor_unit=entry.minor_unit,
            documents=documents,
            now=calculated_at,
            phases=settings.phases if settings.phases is not None else 3,
            amps=settings.amps if settings.amps is not None else 0,
            requested_kwh=energy_kwh,
            consumption_kwh_per_10km=self._consumption_for(settings),
            fiscal=fiscal,
            max_periods=settings.max_periods,
            departure=settings.departure if settings.departure_enabled else None,
        )
        wait_facts: dict[str, Any] = {}
        try:
            result = calculate_plan(plan_request)
            if result.reason == "insufficient_price_horizon":
                waited = self._plan_while_prices_are_missing(
                    plan_request, result, entry, calculated_at
                )
                if waited is not None:
                    result, wait_facts, early = waited
                    if early is not None:
                        return self._fresh_snapshot(
                            settings, early[0], early[1], calculated_at,
                            entry=entry, price_snapshot=area_snapshot, proposal=result, **wait_facts,
                        )
        except PlannerInputError as err:
            # Validated inputs: anything the planner still refuses is a named state, never a crash.
            return self._fresh_snapshot(
                settings, "planning_unavailable", self._map_planner_code(err.code), calculated_at,
                entry=entry, price_snapshot=area_snapshot, error_code=err.code,
            )
        except Exception as err:  # noqa: BLE001 - unexpected, and never fatal to the entry
            _LOGGER.warning("Auto preview calculation failed: %s", type(err).__name__)
            return self._fresh_snapshot(
                settings, "error", "unexpected_failure", calculated_at,
                entry=entry, price_snapshot=area_snapshot,
            )

        if not result.has_plan:
            return self._fresh_snapshot(
                settings, "planning_unavailable", self._map_planner_code(result.reason or ""), calculated_at,
                entry=entry, price_snapshot=area_snapshot, proposal=result,
            )
        state: AutoState = "proposal_unpriced" if result.unpriced else "proposal_ready"
        reason: AutoReason = "unpriced" if result.unpriced else "ready"
        if wait_facts.get("price_wait_action") == "buy_now":
            reason = "buying_before_publication"
        elif wait_facts.get("price_wait_action") == "guarantee":
            reason = "charging_without_prices"
        snapshot = self._fresh_snapshot(
            settings, state, reason, calculated_at, entry=entry, price_snapshot=area_snapshot,
            proposal=result, attempt=attempt, **wait_facts,
        )
        if not await self._persist_proposal(settings, snapshot):
            # A proposal whose summary could not be saved is shown with a stable code and installed by
            # nobody. The code is the fact; the exception text is not.
            failed = replace(snapshot, last_error_code="proposal_not_saved")
            self._publish(failed, force=True)
            return failed
        return snapshot

    def _plan_while_prices_are_missing(
        self,
        request: PlanRequest,
        refusal: PlanResult,
        entry: AreaEntry,
        calculated_at: datetime,
    ) -> tuple[PlanResult, dict[str, Any], tuple[AutoState, AutoReason] | None] | None:
        """The window needs prices nobody has published yet: wait, buy what cannot wait, or charge.

        `None` when the price gap is not the problem. Otherwise `(result, facts, early)`: `early` is a
        `(state, reason)` for the wait (no plan), or `None` when `result` is a plan to install.
        The decision is `price_wait.decide`; this feeds it and arms the wake-up.
        """
        gap = price_gap(request)
        if gap is None:
            return None
        zone = dt_util.get_time_zone(entry.tz)
        missing_day = gap.missing_from.astimezone(zone).date()
        publication_at = price_wait.expected_publication_at(missing_day)
        max_kw = power_kw(request.amps, request.phases)
        decision = price_wait.decide(
            now=calculated_at,
            deadline=gap.deadline,
            need_kwh=request.requested_kwh,
            max_charge_kw=max_kw,
            known=tuple(
                price_wait.KnownInterval(
                    start=slot.start,
                    end=slot.start + timedelta(minutes=15),
                    price=slot.local_major_per_kwh,
                )
                for slot in gap.known
            ),
            publication_at=publication_at,
        )
        # A replan is also owed when the guarantee falls due (the latest safe start less one slot).
        self._arm_wake(decision.act_by)
        facts: dict[str, Any] = {
            "price_wait_action": {"wait": "waiting", "buy_now": "buy_now", "guarantee": "guarantee"}[
                decision.action
            ],
            "publication_at": decision.publication_at,
            "must_buy_kwh": decision.must_buy_kwh,
        }
        if decision.action == "wait":
            return (refusal, facts, ("waiting_for_publication", "publication_pending"))
        if decision.action == "buy_now":
            bought = calculate_plan(
                replace(request, requested_kwh=decision.must_buy_kwh, window_end=decision.window_end)
            )
            if bought.has_plan:
                return (bought, facts, None)
            # The cheapest known slots could not even hold the purchase (period limit or a hole):
            # falling back to charging now keeps the deadline, which is the one promise.
            facts["price_wait_action"] = "guarantee"
            facts["must_buy_kwh"] = request.requested_kwh
        return (plan_unpriced(request), facts, None)

    def _arm_wake(self, when: datetime) -> None:
        """Replan at `when` (the latest safe start), replacing any earlier appointment."""
        self._cancel_wake()
        if self._shutdown:
            return
        generation = self._generation
        target = when.astimezone(timezone.utc) + timedelta(seconds=1)
        if target <= self._now():
            return  # already due: this very calculation is the one that acts on it

        @callback
        def wake(_now: datetime) -> None:
            # A plain callback, with the calculation as a task, as in `_subscribe_for`.
            self._wake_cancel = None
            self._hass.async_create_task(self._on_area_changed(generation))

        self._wake_cancel = self._manager.schedule_at(target, wake)

    def _cancel_wake(self) -> None:
        if self._wake_cancel is not None:
            self._wake_cancel()
            self._wake_cancel = None

    def _republish_execution(self, snapshot: AutoSnapshot) -> AutoSnapshot:
        """Publish again when execution changed the facts this snapshot was built with.

        The snapshot is built before the proposal reaches the boundary, so installing (or failing to)
        changes its execution facts. `applied` is recomputed here: it says whether this proposal is
        the plan in force per `materially_applied`, not identity equality, so an equivalent
        recalculation is not reported as merely proposed. Published only when something changed.
        """
        execution, applied_identity, pending_identity, _latest = self._execution_facts()
        application = application_for(self._store.settings(self._entry_id), snapshot)
        proposal_identity = None if application is None else application.identity
        applied = (
            self._executor is not None
            and self._executor.materially_applied(application)
        )
        if (execution, applied_identity, pending_identity, applied, proposal_identity) == (
            snapshot.execution,
            snapshot.applied_identity,
            snapshot.pending_identity,
            snapshot.applied,
            snapshot.proposal_identity,
        ):
            return snapshot
        updated = replace(
            snapshot,
            execution=execution,
            applied_identity=applied_identity,
            pending_identity=pending_identity,
            applied=applied,
            proposal_identity=proposal_identity,
        )
        self._publish(updated)
        return updated

    def _price_data_gate(
        self, settings: AutoSettings, area_snapshot: Any, calculated_at: datetime, entry: AreaEntry
    ) -> AutoSnapshot | None:
        """Whether the prices on hand may be used at all, before anything is calculated.

        * `invalid` today: nothing to calculate from, `price_data_invalid`.
        * `stale` or `unavailable`: a proposal calculated in this process is preserved as it stands.
          Otherwise: `no_published_prices` when a valid index does not list today; `price_data_stale`
          after a completed attempt or when stale; `waiting_for_prices` when nothing was ever asked.
        * `loading` with no document: `waiting_for_prices`.

        Tomorrow's state has no say: the planner names a missing horizon itself.
        """
        today_state = area_snapshot.today_state
        if today_state == "invalid":
            return self._fresh_snapshot(
                settings, "planning_unavailable", "price_data_invalid", calculated_at,
                entry=entry, price_snapshot=area_snapshot, error_code="invalid_contract",
            )
        if today_state in ("stale", "unavailable"):
            preserved = self._preserved_snapshot(settings)
            if preserved is not None:
                return preserved
            if today_state == "unavailable" and area_snapshot.today_authority == "not_listed":
                # A valid index says today does not exist: a fact about the relay, not a wait.
                return self._fresh_snapshot(
                    settings, "planning_unavailable", "no_published_prices", calculated_at,
                    entry=entry, price_snapshot=area_snapshot,
                )
            if today_state == "stale" or area_snapshot.today_snapshot.attempt_error is not None:
                # Stale, or asked and not answered: there is no usable data now.
                return self._fresh_snapshot(
                    settings, "price_data_stale", "price_data_stale", calculated_at,
                    entry=entry, price_snapshot=area_snapshot,
                )
            # No completed attempt and nothing held: waiting for first prices, not stale.
            return self._fresh_snapshot(
                settings, "waiting_for_prices", "no_prices_yet", calculated_at,
                entry=entry, price_snapshot=area_snapshot,
            )
        if today_state == "loading" and area_snapshot.today_snapshot.document is None:
            return self._fresh_snapshot(
                settings, "waiting_for_prices", "no_prices_yet", calculated_at,
                entry=entry, price_snapshot=area_snapshot,
            )
        return None

    def _preserved_snapshot(self, settings: AutoSettings) -> AutoSnapshot | None:
        """The last proposal this process calculated, kept as it was, or `None`.

        A summary restored from the store is never returned. State becomes `price_data_stale` while the
        proposal, price identity and calculation time stay unchanged, so stale data never looks fresh.
        """
        previous = self._snapshot
        if previous is None or previous.proposal is None or not previous.in_process:
            return None
        execution, applied_identity, pending_identity, _latest = self._execution_facts()
        return replace(
            previous,
            state="price_data_stale",
            reason="price_data_stale",
            settings_revision=settings.revision,
            generation=self._generation,
            missing=(),
            # Attempt and execution facts are current, so a reader sees the charger's state beside the kept proposal.
            attempt=self._attempt,
            execution=execution,
            applied_identity=applied_identity,
            pending_identity=pending_identity,
            last_error_code=None,
        )

    @staticmethod
    def _map_planner_code(code: str) -> AutoReason:
        mapping: dict[str, AutoReason] = {
            "no_published_prices": "no_published_prices",
            "insufficient_price_horizon": "insufficient_price_horizon",
            "deadline_too_short": "deadline_too_short",
            "missing_fx_rate": "missing_fx_rate",
        }
        return mapping.get(code, "unexpected_failure")

    @staticmethod
    def _fiscal_for(settings: AutoSettings, entry: AreaEntry) -> FiscalChoice | None:
        """What the fiscal figures resolve to, or `None` when one cannot be resolved.

        Per component: an explicit override wins, else the area's catalogue suggestion, else the
        setting is incomplete. A catalogue value of zero is a value; an absent one is not.
        """
        overrides = settings.override_for(entry.id)
        sentinel = object()

        def resolve(override: Any, suggestion: float | None) -> Any:
            if not override.enabled:
                return None
            if override.value is not None:
                return override.value
            return suggestion if suggestion is not None else sentinel

        vat = resolve(overrides.vat, entry.vat_percent)
        tax = resolve(overrides.tax, entry.suggested_tax)
        transfer = resolve(overrides.transfer, entry.suggested_grid_fee)
        if sentinel in (vat, tax, transfer):
            return None
        return FiscalChoice(
            tax_enabled=overrides.tax.enabled,
            tax_minor_per_kwh=tax,
            transfer_enabled=overrides.transfer.enabled,
            transfer_minor_per_kwh=transfer,
            vat_enabled=overrides.vat.enabled,
            vat_percent=vat,
        ).validated()

    def _consumption_for(self, settings: AutoSettings) -> float:
        """kWh per 10 km for the distance estimate: the planned vehicle's, else the default."""
        if self._consumption_reader is not None:
            reported = self._consumption_reader(settings.target.vehicle_id or "")
            if reported is not None and reported > 0:
                return reported
        return DEFAULT_CONSUMPTION_KWH_PER_10KM

    async def _energy_for(
        self, settings: AutoSettings, calculated_at: datetime, entry: AreaEntry, price_snapshot: Any
    ) -> _EnergyResolution | AutoSnapshot:
        """The energy the plan should deliver: the manual figure (less energy already delivered toward
        the current departure, when trustworthy) or what a target needs from live state of charge."""
        if settings.driver != DRIVER_TARGET_SOC:
            return await self._manual_kwh_remaining(settings, calculated_at, entry)
        facts = None if self._vehicle_reader is None else self._vehicle_reader(settings.target.vehicle_id or "")
        live_soc = None if facts is None else facts.soc_percent
        if live_soc is None:
            return self._fresh_snapshot(
                settings, "incomplete_settings", "target_soc_unknown", calculated_at,
                missing=("live_soc",), entry=entry, price_snapshot=price_snapshot,
            )
        capacity = None
        if facts is not None and facts.reported_capacity_kwh is not None:
            capacity = facts.reported_capacity_kwh
        # Energy the wall must deliver: battery need divided by charging efficiency
        # (`soc_estimate.CHARGE_EFFICIENCY`).
        reason, wall_kwh = target_need_kwh(
            soc_percent=live_soc,
            capacity_kwh=capacity,
            target_percent=settings.target.target_percent,
            vehicle_max_percent=None if facts is None else facts.max_percent,
        )
        if reason == "unknown_capacity":
            return self._fresh_snapshot(
                settings, "incomplete_settings", "target_capacity_unknown", calculated_at,
                missing=("capacity",), entry=entry, price_snapshot=price_snapshot,
            )
        if reason == "already_at_target":
            # The car needs nothing, so `async_plan_hybrid` is never reached: record the `satisfied`
            # result it would produce so the solar coordinator sees the need is gone.
            if settings.strategy == STRATEGY_HYBRID:
                hybrid_execution.record_satisfied_state(self._hass, self._entry_id)
            return self._fresh_snapshot(
                settings, "nothing_to_charge", "already_at_target", calculated_at,
                entry=entry, price_snapshot=price_snapshot,
            )
        kwh = wall_kwh if wall_kwh is not None else settings.requested_kwh
        # Always trustworthy: live state of charge already reflects everything delivered.
        return _EnergyResolution(kwh=kwh, delivered_energy_trustworthy=True)

    def _departure_key(self, settings: AutoSettings, calculated_at: datetime, entry: AreaEntry) -> str:
        """A stable name for which departure occurrence this is, for `_manual_kwh_remaining`'s reset rule.

        The key changes when the departure changes or its occurrence passes. It reuses
        `planner.resolve_departure` anchored on `calculated_at`, independent of price data; a
        one-slot disagreement near a boundary is immaterial for an epoch marker. With no deadline the
        key is a fixed epoch.
        """
        if not settings.departure_enabled:
            return "no_deadline"
        resolved = resolve_departure(calculated_at, entry.tz, settings.departure, calculated_at)
        return resolved.isoformat()

    def _energy_register_entity_id(self) -> str | None:
        """The entity this charger's cumulative energy register is read from, or `None`.

        Resolved once by `ChargingController.energy_register_entity_id`; this module never reaches a
        charger's protocol directly. No executor means no register.
        """
        if self._executor is None:
            return None
        return self._executor.controller.energy_register_entity_id

    def _read_energy_register(self) -> float | None:
        """The charger's cumulative energy register in kWh, or `None` when not configured, not
        readable, or not a numeric energy meter with `state_class: total_increasing` (never guessed).
        """
        return read_energy_register_kwh(self._hass, self._energy_register_entity_id())

    async def _manual_kwh_remaining(
        self, settings: AutoSettings, calculated_at: datetime, entry: AreaEntry
    ) -> _EnergyResolution:
        """`manual_kwh`'s remaining need: `requested_kwh` less energy delivered toward the current
        departure occurrence since a baseline was recorded for it.

        The baseline (`AutoSettingsStore.energy_baseline`) is a register reading captured once per
        departure occurrence and persisted, so a restart does not re-buy delivered energy; `cheapest`
        and `hybrid` share this accounting. When no real progress is visible (no register, unavailable,
        or a reading below the baseline, i.e. a meter reset) the full request is returned with
        `delivered_energy_trustworthy=False`. A new baseline is captured as soon as a reading exists,
        but the capturing call subtracts nothing.
        """
        departure_key = self._departure_key(settings, calculated_at, entry)
        stored = self._store.energy_baseline(self._entry_id)
        current_reading = self._read_energy_register()

        if stored is None or stored.departure_key != departure_key:
            # A fresh epoch: the current reading (or None) is the new starting point.
            await self._store.async_update(
                self._entry_id,
                energy_baseline=EnergyBaseline(register_kwh=current_reading, departure_key=departure_key),
            )
            return _EnergyResolution(
                kwh=settings.requested_kwh, delivered_energy_trustworthy=current_reading is not None
            )

        if current_reading is None:
            return _EnergyResolution(kwh=settings.requested_kwh, delivered_energy_trustworthy=False)

        if stored.register_kwh is None or current_reading < stored.register_kwh:
            # No baseline for this epoch, or the register went backwards (a meter reset): re-baseline
            # so tracking resumes next call, without guessing what was delivered before now.
            await self._store.async_update(
                self._entry_id,
                energy_baseline=EnergyBaseline(register_kwh=current_reading, departure_key=departure_key),
            )
            return _EnergyResolution(kwh=settings.requested_kwh, delivered_energy_trustworthy=False)

        delivered = current_reading - stored.register_kwh
        remaining = max(0.0, settings.requested_kwh - delivered)
        return _EnergyResolution(kwh=remaining, delivered_energy_trustworthy=True)

    def _execution_facts(self) -> tuple[str, str | None, str | None, int]:
        """What the charger is doing, and which application is installed.

        Without an executor: `not_applied`, nothing installed, nothing pending.
        """
        if self._executor is None:
            return (EXECUTION_NOT_APPLIED, None, None, self._attempt)
        return (
            self._executor.execution_state(),
            None if self._executor.applied is None else self._executor.applied.identity,
            None if self._executor.pending is None else self._executor.pending.identity,
            self._executor.attempt,
        )

    def _fresh_snapshot(
        self,
        settings: AutoSettings,
        state: AutoState,
        reason: AutoReason,
        calculated_at: datetime | None = None,
        *,
        entry: AreaEntry | None = None,
        price_snapshot: Any = None,
        proposal: PlanResult | None = None,
        missing: tuple[str, ...] = (),
        error_code: str | None = None,
        attempt: int | None = None,
        price_wait_action: str | None = None,
        publication_at: datetime | None = None,
        must_buy_kwh: float | None = None,
    ) -> AutoSnapshot:
        """Build one immutable snapshot from the settings and whatever is known."""
        execution, applied_identity, pending_identity, latest_attempt = self._execution_facts()
        area_snapshot = price_snapshot
        index_revision = None if area_snapshot is None else area_snapshot.index_revision
        fetched_at = None if area_snapshot is None else area_snapshot.fetched_at
        today = None if area_snapshot is None else area_snapshot.today.isoformat()
        tomorrow = None if area_snapshot is None else area_snapshot.tomorrow.isoformat()
        identity = None
        if area_snapshot is not None:
            stamp = None if fetched_at is None else fetched_at.isoformat()
            identity = f"{today}/{tomorrow}|{index_revision or '-'}|{stamp or '-'}"
        return AutoSnapshot(
            state=state,
            reason=reason,
            settings_revision=settings.revision,
            generation=self._generation,
            calculated_at=calculated_at,
            area_id=entry.id if entry is not None else settings.area_id,
            currency=None if entry is None else entry.currency,
            major_unit=None if entry is None else entry.major_unit,
            minor_unit=None if entry is None else entry.minor_unit,
            timezone=None if entry is None else entry.tz,
            missing=missing,
            price_state=None if area_snapshot is None else area_snapshot.state,
            price_identity=identity,
            today=today,
            tomorrow=tomorrow,
            proposal=proposal,
            priced_slots=0 if proposal is None else proposal.priced_slots,
            unpriced_slots=0 if proposal is None else proposal.unpriced_slots,
            unpriced=False if proposal is None else proposal.unpriced,
            applied=False,
            in_process=proposal is not None,
            attempt=self._attempt if attempt is None else attempt,
            execution=execution,
            applied_identity=applied_identity,
            pending_identity=pending_identity,
            last_error_code=error_code,
            price_wait=price_wait_action,
            publication_at=publication_at,
            must_buy_kwh=must_buy_kwh,
        )

    async def _state_only(self, state: AutoState, reason: AutoReason) -> AutoSnapshot:
        """A snapshot for a state that needs no calculation.

        Carries the latest attempt rather than a calculation's own, describing the world as it is now.
        """
        execution, _applied, _pending, latest_attempt = self._execution_facts()
        return self._fresh_snapshot(
            self._store.settings(self._entry_id),
            state,
            reason,
            self._now(),
            attempt=latest_attempt,
        )

    def _publish(self, snapshot: AutoSnapshot, *, force: bool = False) -> None:
        """Store the snapshot, and tell listeners when it means something new.

        A snapshot from an older attempt than the boundary's latest is dropped. `force` is for states
        not about a calculation (a refusal, a shutdown).
        """
        if not force and self._executor is not None and not self._executor.current(snapshot.attempt):
            return
        self._snapshot = snapshot
        key = snapshot.meaningful_key()
        if not force and key == self._last_key:
            return
        self._last_key = key
        for listener in list(self._listeners):
            self._deliver(listener, snapshot)

    @staticmethod
    def _deliver(listener: Callable[[AutoSnapshot], None], snapshot: AutoSnapshot) -> None:
        """One listener failing is logged and must not stop the others."""
        try:
            listener(snapshot)
        except Exception as err:  # noqa: BLE001 - one listener must not stop the rest
            _LOGGER.warning("An Auto listener failed: %s", type(err).__name__)

    async def _apply(self, settings: AutoSettings, snapshot: AutoSnapshot, attempt: int) -> None:
        """Hand one usable proposal to the execution boundary, if the calculation still counts.

        The boundary re-checks attempt, settings, pause and live price identity inside its own lock.
        A failure in execution never breaks the preview.
        """
        if self._executor is None or not self._executor.current(attempt):
            return
        try:
            await self._executor.async_reconcile(settings, snapshot, attempt=attempt)
        except Exception as err:  # noqa: BLE001 - execution never breaks the preview
            _LOGGER.warning("An Auto application attempt failed: %s", type(err).__name__)

    async def _persist_proposal(self, settings: AutoSettings, snapshot: AutoSnapshot) -> bool:
        """Persist the summary of a proposal, for restart diagnostics only.

        Counts, timestamps, money and period starts; never intervals, documents or a live SoC. On
        reload it is historical and `applied` stays `False`.

        Returns whether the summary is durably stored (`True` when there was nothing to store). The
        failure is caught here because this runs in callbacks and tasks, and no exception text is logged.
        """
        proposal = snapshot.proposal
        if proposal is None or snapshot.calculated_at is None:
            return True
        stored = StoredProposal(
            calculated_at=snapshot.calculated_at,
            state=snapshot.state,
            reason=snapshot.reason,
            settings_revision=settings.revision,
            area_id=snapshot.area_id or "",
            slots_needed=proposal.slots_needed,
            priced_slots=proposal.priced_slots,
            unpriced_slots=proposal.unpriced_slots,
            delivered_kwh=proposal.delivered_kwh,
            estimated_cost=proposal.estimated_cost,
            distance_mil=proposal.distance_mil,
            period_starts=tuple(start.isoformat() for start, _ in proposal.periods),
            period_ends=tuple(end.isoformat() for _, end in proposal.periods),
            price_identity=snapshot.price_identity,
            unpriced=proposal.unpriced,
        )
        try:
            await self._store.async_update(self._entry_id, proposal=stored)
        except Exception as err:  # noqa: BLE001 - becomes a stable, redacted code
            _LOGGER.warning("Storing an Auto proposal summary failed: %s", type(err).__name__)
            return False
        return True


def fiscal_choice_for(settings: AutoSettings, entry: AreaEntry) -> FiscalChoice | None:
    """What the fiscal figures resolve to for one area, or `None` when one cannot resolve.

    Exposes `AutoPlannerController._fiscal_for` so anything showing a price (the dashboard contract)
    prices it exactly as the plan did.
    """
    return AutoPlannerController._fiscal_for(settings, entry)


async def async_remove_auto_state(hass: HomeAssistant, entry_id: str) -> None:
    """An entry was deleted for good: remove its own Auto settings and proposal."""
    store = domain_data(hass).auto_store
    if store is not None:
        await store.async_remove(entry_id)
