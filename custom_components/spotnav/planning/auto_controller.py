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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Final, Literal

from homeassistant.core import callback, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import history_wait, price_wait
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
from ..execution.controller import CONNECTION_PLUGGED_IN
from ..sessions.model import STARTED_HYBRID, STARTED_MANUAL, STARTED_SOLAR
from ..sessions.recorder import RESET_TOLERANCE_KWH
from ..vehicles.soc_estimate import (
    read_energy_register_kwh,
    REGISTER_TOLERANCE_KWH,
    SocReader,
    target_need_kwh,
)
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
from .grid_voltage import voltage_between_phases_v
from .phases import effective_phases
from .planner import (
    calculate_plan,
    FiscalChoice,
    local_instant,
    MAX_DEPARTURE_DAYS_AHEAD,
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

#: A plug-in or unplug is planned for once its status has settled this long (a connector passes
#: through a few values on the way).
CONNECTION_DEBOUNCE_S: Final = 5.0

#: What `note_connection` is told when a charger that cannot report a plug-in begins a new charge.
CHARGE_BEGUN: Final = "charge_begun"

#: A register that falls to this or below starts again from zero (one that counts per plug-in): what it
#: shows is what it counted since. The session recorder reads registers the same way.
SESSION_REGISTER_ZERO_KWH: Final = RESET_TOLERANCE_KWH

#: A reading below the last one counts as a register that started again only once it has held for this
#: many readings over this long: a lifetime register reads 0 for a moment while its charger reboots.
DROP_HOLD_READINGS: Final = 2
DROP_HOLD_S: Final = 120.0

#: This soon after a plug-in, a register that falls to about zero is one that counts per plug-in, and
#: counts again from zero at once.
PLUG_IN_RESTART_S: Final = 900.0

#: Slack on top of what the charger can deliver between two readings: a reading that climbs further is
#: not believed (one false high reading must not end a plan).
JUMP_MARGIN_KWH: Final = 0.5

#: The current a rise is judged against at the least: 63 A on three phases (about 43 kW).
CEILING_A: Final = 63


@dataclass(frozen=True, slots=True)
class RegisterStep:
    """One reading taken into a baseline: the energy delivered in its epoch, the baseline after it, and
    whether the reading was believed (one that was not leaves the count where it was)."""

    delivered_kwh: float
    baseline: EnergyBaseline
    accepted: bool


def advance_register(
    baseline: EnergyBaseline,
    reading: float,
    now: datetime,
    *,
    max_kw: float,
    plugged_in_at: datetime | None,
) -> RegisterStep:
    """Take one register reading into a baseline's epoch.

    * A reading at or above the last one is believed when it climbed no faster than the charger can
      deliver (`max_kw` since the last change, plus `JUMP_MARGIN_KWH`), or when it is the second reading
      in a row at or above one that climbed too fast (a faster charger, or a meter that caught up). A
      single reading that climbed too fast and then fell back is never believed.
    * A reading that falls back to the one before the last (a rise that did not hold) is the last
      reading proving false: the count goes back to it.
    * Any other drop is a register that started again: at once when it fell to about zero soon after a
      plug-in (one that counts per plug-in), otherwise once the drop has held for `DROP_HOLD_READINGS`
      readings over `DROP_HOLD_S` (until then it is a glitch, such as a lifetime register reading 0 while
      its charger reboots, and nothing moves). What was counted before is carried; the register counts
      from zero when what it shows is no more than could have been delivered since the last reading
      (so the energy delivered while the drop held is counted too), else from its new reading (a meter
      replaced).
    """
    reference = baseline.register_kwh if baseline.register_kwh is not None else reading
    last = baseline.last_register_kwh if baseline.last_register_kwh is not None else reference
    carried = baseline.carried_kwh
    held = carried + max(0.0, last - reference)
    since = baseline.last_register_at or baseline.started_at
    calm = {"pending_drop_kwh": None, "pending_drop_at": None, "pending_drop_count": 0, "rejected_kwh": None}

    def deliverable(rise: float) -> bool:
        if since is None:
            return True
        hours = max(0.0, (now - since).total_seconds()) / 3600.0
        return rise <= max_kw * hours + JUMP_MARGIN_KWH

    def believe(new_reference: float, new_carried: float, previous: float | None) -> RegisterStep:
        updated = replace(
            baseline,
            register_kwh=new_reference,
            carried_kwh=round(new_carried, 6),
            previous_register_kwh=previous,
            last_register_kwh=reading,
            last_register_at=now,
            **calm,
        )
        return RegisterStep(new_carried + max(0.0, reading - new_reference), updated, True)

    if reading >= last - REGISTER_TOLERANCE_KWH:
        if reading <= last:
            return RegisterStep(carried + max(0.0, reading - reference), replace(baseline, register_kwh=reference), True)
        rejected = baseline.rejected_kwh
        if deliverable(reading - last) or (rejected is not None and reading >= rejected - REGISTER_TOLERANCE_KWH):
            return believe(reference, carried, last)
        return RegisterStep(held, replace(baseline, rejected_kwh=reading), False)
    to_zero = reading <= SESSION_REGISTER_ZERO_KWH
    previous = baseline.previous_register_kwh
    if not to_zero and previous is not None and reading >= previous - REGISTER_TOLERANCE_KWH:
        # Back to where it was before the last rise: that rise was false.
        return believe(reference, carried, baseline.previous_register_kwh)
    near_plug_in = (
        plugged_in_at is not None and 0.0 <= (now - plugged_in_at).total_seconds() <= PLUG_IN_RESTART_S
    )
    if not (near_plug_in and to_zero):
        first = baseline.pending_drop_at or now
        count = baseline.pending_drop_count + 1 if baseline.pending_drop_at is not None else 1
        if count < DROP_HOLD_READINGS or (now - first).total_seconds() < DROP_HOLD_S:
            pending = replace(baseline, pending_drop_kwh=reading, pending_drop_at=first, pending_drop_count=count)
            return RegisterStep(held, pending, False)
    from_zero = to_zero or deliverable(reading)
    return believe(0.0 if from_zero else reading, held, None)


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
    #: `waiting_for_publication`: a dated departure, and the weekday history says the hours not yet
    #: published are clearly cheaper than anything published; nothing is planned until they are.
    "waiting_for_history",
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
    #: How a manual need was counted: `register` (the energy register vouches for it), `kept` (the
    #: register cannot be read, the last remainder it vouched for is kept), `sessions` (no register,
    #: counted from the charger's recorded sessions) or `requested` (nothing delivered is known);
    #: `None` for a target, whose live state of charge already says it.
    basis: str | None = None
    #: The energy counted as delivered toward this need, when the basis knows it.
    delivered_kwh: float | None = None


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
    #: The last history weighing for a dated departure (`history_wait.evaluate`), whatever it decided; `None`
    #: when the departure is daily or the plan never needed unpublished hours.
    history: history_wait.HistoryDecision | None = None
    #: Which rule decided a plan that needed unpublished prices: `history` (`history_wait`) or `implicit`
    #: (the daily wait for the publication, `price_wait`); `None` when nothing was unpublished.
    wait_rule: str | None = None
    #: `manual_kwh` only: how the remaining need was counted (`_EnergyResolution.basis`), the need that
    #: remains and the energy already delivered toward it; `None` where not counted.
    energy_basis: str | None = None
    remaining_kwh: float | None = None
    delivered_kwh: float | None = None

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
            None if self.history is None else (self.history.outcome, self.history.percent, self.history.weekday),
            self.wait_rule,
            self.energy_basis,
            None if self.remaining_kwh is None else round(self.remaining_kwh, 1),
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
        # The departure's own appointment (`_arm_departure`), the settling plug-in or unplug
        # (`note_connection`) and the energy register watched while a manual need counts on it.
        self._departure_cancel: Callable[[], None] | None = None
        self._connection_cancel: Callable[[], None] | None = None
        self._plug_in_pending = False
        self._energy_cancel: Callable[[], None] | None = None
        self._energy_watched: str | None = None
        # The count the register watcher keeps between calculations (`_baseline`), and the epoch whose
        # delivered energy was acted on (`_energy_met_done`).
        self._live_baseline: EnergyBaseline | None = None
        self._sessions_cancel: Callable[[], None] | None = None
        self._energy_met_done: str | None = None

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
        self._cancel_departure()
        self._cancel_connection()
        self._drop_energy_watch()
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
        if mutate is not None:
            mutate = self._with_departure_date_guard(mutate)
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

    def _area_zone(self, settings: AutoSettings) -> Any:
        """The zone the departure date is read in: the chosen area's, else the installation's own.

        From the held catalogue, not the subscription: a write that moves the area is judged in the new
        area's zone before anything is subscribed to it.
        """
        entry = None if not settings.area_id else self._manager.catalogue_snapshot().area(settings.area_id)
        zone = None if entry is None else dt_util.get_time_zone(entry.tz)
        return zone if zone is not None else dt_util.get_default_time_zone()

    def _with_departure_date_guard(
        self, mutate: Callable[[AutoSettings], AutoSettings]
    ) -> Callable[[AutoSettings], AutoSettings]:
        """Judge `departure_date` against today in the area's zone, at every write, whichever transport.

        * a date newly set must not be in the past and at most 7 days ahead (`invalid_departure`);
        * a stored date that has gone by and is carried through unchanged is cleared by this write: it
          was already ignored by planning, and the write is the first chance to forget it.
        """

        def guarded(current: AutoSettings) -> AutoSettings:
            updated = mutate(current)
            chosen = updated.departure_date
            if chosen is None:
                return updated
            today = self._now().astimezone(self._area_zone(updated)).date()
            if chosen < today:
                if chosen == current.departure_date:
                    return replace(updated, departure_date=None)
                raise AutoSettingsError("invalid_departure", "departure_date is in the past")
            if chosen > today + timedelta(days=MAX_DEPARTURE_DAYS_AHEAD):
                raise AutoSettingsError(
                    "invalid_departure",
                    f"departure_date is more than {MAX_DEPARTURE_DAYS_AHEAD} days ahead",
                )
            return updated

        return guarded

    def _effective_departure_date(
        self, settings: AutoSettings, entry: AreaEntry, calculated_at: datetime
    ) -> date | None:
        """The date planning honours: the stored one while its deadline is still ahead, else none.

        A date that has gone by (or whose deadline is not after `calculated_at`) is ignored, so the
        departure reads as the daily one, until a write clears it. Also none without a departure.

        Without a chosen date, a daily departure that leaves some weekdays out takes the date of its next
        occurrence on a weekday that is in the set, when that is not simply the next occurrence: the plan
        then runs to that day, exactly as it does to a date the person chose.
        """
        if not settings.departure_enabled:
            return None
        zone = dt_util.get_time_zone(entry.tz)
        if zone is None:
            return None
        chosen = settings.departure_date
        if chosen is None:
            return self._next_departure_weekday(settings, entry.tz, calculated_at)
        deadline = local_instant(chosen, settings.departure, zone)
        if deadline.astimezone(timezone.utc) <= calculated_at.astimezone(timezone.utc):
            return self._next_departure_weekday(settings, entry.tz, calculated_at)
        return chosen

    @staticmethod
    def _next_departure_weekday(settings: AutoSettings, tz: str, calculated_at: datetime) -> date | None:
        """The date of the next departure on an allowed weekday, or `None` when the next occurrence is
        already on one (the ordinary daily departure) or every weekday is allowed."""
        if len(settings.departure_weekdays) >= 7:
            return None
        zone = dt_util.get_time_zone(tz)
        if zone is None:
            return None
        day = resolve_departure(calculated_at, tz, settings.departure, calculated_at).astimezone(zone).date()
        if day.isoweekday() in settings.departure_weekdays:
            return None
        while day.isoweekday() not in settings.departure_weekdays:
            day += timedelta(days=1)
        return day

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
        """`_compute_plan`, with how a manual need was counted stamped on whatever it returns, and the
        departure's own appointment armed (the instant it passes, the next occurrence is planned).
        """
        energy: dict[str, Any] = {}
        snapshot = await self._compute_plan(settings, attempt, energy)
        self._arm_departure(settings)
        self._arm_energy_watch(settings)
        self._arm_charge_watch()
        return replace(snapshot, **energy) if energy else snapshot

    async def _compute_plan(
        self, settings: AutoSettings, attempt: int, energy: dict[str, Any]
    ) -> AutoSnapshot:
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
        if resolved.basis is not None:
            energy.update(
                energy_basis=resolved.basis,
                remaining_kwh=resolved.kwh,
                delivered_kwh=resolved.delivered_kwh,
            )

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
            phases=effective_phases(self._hass, self._entry_id),
            amps=settings.amps if settings.amps is not None else 0,
            requested_kwh=energy_kwh,
            consumption_kwh_per_10km=self._consumption_for(settings),
            fiscal=fiscal,
            max_periods=settings.max_periods,
            departure=settings.departure if settings.departure_enabled else None,
            departure_date=self._effective_departure_date(settings, entry, calculated_at),
            voltage_between_phases_v=voltage_between_phases_v(self._hass, self._entry_id),
        )
        wait_facts: dict[str, Any] = {}
        try:
            result = calculate_plan(plan_request)
            if result.reason == "insufficient_price_horizon":
                waited = None
                if plan_request.departure is not None:
                    waited = await self._plan_departure_by_history(plan_request, result, entry, calculated_at)
                if waited is None:
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
        # The publication that fills the gap is the market day's (a London evening hour is the next Paris file).
        zone = dt_util.get_time_zone(entry.market_tz)
        missing_day = gap.missing_from.astimezone(zone).date()
        publication_at = price_wait.expected_publication_at(missing_day)
        max_kw = power_kw(request.amps, request.phases, request.voltage_between_phases_v)
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
            "wait_rule": "implicit",
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

    async def _plan_departure_by_history(
        self, request: PlanRequest, refusal: PlanResult, entry: AreaEntry, calculated_at: datetime
    ) -> tuple[PlanResult, dict[str, Any], tuple[AutoState, AutoReason] | None] | None:
        """A departure (dated or daily) whose window runs past the last published price.

        A daily departure takes this path too (its deadline tomorrow morning is partly unpriced before the
        afternoon publication), but only with a usable profile: without one it returns `None` and the
        implicit rule (`_plan_while_prices_are_missing`) decides exactly as it always did. A dated departure
        without a profile plans on `known`.

        Same answer shape as `_plan_while_prices_are_missing`, which this falls back to (`None`) whenever the
        published intervals cannot hold the whole need. Otherwise:

        1. `known`: the cheapest placement of the whole need in published intervals.
        2. With a usable history profile, `history_wait.evaluate` weighs it against what the unpublished hours
           usually cost. A clear saving (beyond one standard deviation) argues for waiting, and then
           `price_wait.decide` judges whether that is safe: nothing is planned (`wait`) or only what cannot
           wait is bought (`buy_now`), exactly the daily rule.
        3. Everything else (no profile, no clear saving, waiting not safe) installs `known`. Prices only ever
           add intervals, so the next publication can only improve it, and every publication replans.
        """
        gap = price_gap(request)
        if gap is None:
            return None
        known = calculate_plan(replace(request, window_end=gap.missing_from))
        if not known.has_plan:
            return None
        profile = await self._manager.async_profile(request.area_id)
        decision = history_wait.evaluate(request, gap, known, profile)
        if request.departure_date is None:
            return self._daily_by_history(request, refusal, entry, calculated_at, known, decision)
        facts: dict[str, Any] = {"history": decision, "wait_rule": "history"}
        if decision.outcome != "wait":
            return (known, facts, None)

        # The publication that fills the gap is the market day's (a London evening hour is the next Paris file).
        zone = dt_util.get_time_zone(entry.market_tz)
        missing_day = gap.missing_from.astimezone(zone).date()
        waited = price_wait.decide(
            now=calculated_at,
            deadline=gap.deadline,
            need_kwh=request.requested_kwh,
            max_charge_kw=power_kw(request.amps, request.phases, request.voltage_between_phases_v),
            known=tuple(
                price_wait.KnownInterval(
                    start=slot.start,
                    end=slot.start + timedelta(minutes=15),
                    price=slot.local_major_per_kwh,
                )
                for slot in gap.known
            ),
            publication_at=price_wait.expected_publication_at(missing_day),
        )
        if waited.action == "guarantee":
            # Waiting would miss the deadline: the published plan stands, the price given up is only the hope.
            return (known, {"history": history_wait.unsafe(decision), "wait_rule": "history"}, None)
        # A replan is also owed when waiting stops being safe (the latest safe start less one slot).
        self._arm_wake(waited.act_by)
        facts.update(
            price_wait_action="waiting" if waited.action == "wait" else "buy_now",
            publication_at=waited.publication_at,
            must_buy_kwh=waited.must_buy_kwh,
        )
        if waited.action == "wait":
            return (refusal, facts, ("waiting_for_publication", "waiting_for_history"))
        bought = calculate_plan(
            replace(request, requested_kwh=waited.must_buy_kwh, window_end=waited.window_end)
        )
        if bought.has_plan:
            return (bought, facts, None)
        # The cheapest published slots could not hold even the part that cannot wait: install the whole plan.
        return (known, {"history": history_wait.unsafe(decision), "wait_rule": "history"}, None)

    def _daily_by_history(
        self,
        request: PlanRequest,
        refusal: PlanResult,
        entry: AreaEntry,
        calculated_at: datetime,
        known: PlanResult,
        decision: history_wait.HistoryDecision,
    ) -> tuple[PlanResult, dict[str, Any], tuple[AutoState, AutoReason] | None] | None:
        """A daily departure: the implicit rule stands unless history says something definite.

        Waiting for the publication is nearly free, so history may only (1) explain a wait the implicit rule
        makes anyway (the unpublished hours are clearly cheaper), or (2) end it: the published hours are
        clearly cheaper than the expected unpublished ones, so the published plan is installed. Anything
        else (flat, no profile) returns `None`: the implicit rule, bit for bit.
        """
        if decision.known_cheaper:
            return (known, {"history": decision, "wait_rule": "history"}, None)
        if decision.outcome != "wait":
            return None
        implicit = self._plan_while_prices_are_missing(request, refusal, entry, calculated_at)
        if implicit is None:
            return None
        result, facts, early = implicit
        if early is not None and early[1] == "publication_pending":
            facts = {**facts, "history": decision, "wait_rule": "history"}
            return (result, facts, ("waiting_for_publication", "waiting_for_history"))
        return implicit

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
        application = application_for(
            self._store.settings(self._entry_id), snapshot, effective_phases(self._hass, self._entry_id)
        )
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
        setting is incomplete. A catalogue value of zero is a value; an absent one is not. A component
        the published price already includes (contract v2's `included`) is off whatever is stored:
        the price holds it, so nothing is added for it a second time.
        """
        overrides = settings.override_for(entry.id)
        sentinel = object()

        def resolve(override: Any, suggestion: float | None, included: bool) -> Any:
            if included or not override.enabled:
                return None
            if override.value is not None:
                return override.value
            return suggestion if suggestion is not None else sentinel

        locked = {component: component_included(entry, component) for component in ("vat", "tax", "transfer")}
        vat = resolve(overrides.vat, entry.vat_percent, locked["vat"])
        tax = resolve(overrides.tax, entry.suggested_tax, locked["tax"])
        transfer = resolve(overrides.transfer, entry.suggested_grid_fee, locked["transfer"])
        if sentinel in (vat, tax, transfer):
            return None
        return FiscalChoice(
            tax_enabled=overrides.tax.enabled and not locked["tax"],
            tax_minor_per_kwh=tax,
            transfer_enabled=overrides.transfer.enabled and not locked["transfer"],
            transfer_minor_per_kwh=transfer,
            vat_enabled=overrides.vat.enabled and not locked["vat"],
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
        need is counted per plug-in (`plugin:` and the instant the charger saw the vehicle arrive); a
        charger that has never reported one keeps the fixed epoch.
        """
        if not settings.departure_enabled:
            plugged_in_at = self._plugged_in_at()
            if plugged_in_at is not None:
                return f"plugin:{plugged_in_at.astimezone(timezone.utc).isoformat()}"
            # A charger that cannot report a plug-in: once the need was met, the next charge that begins
            # starts a new count (`charge:` and its start).
            stored = self._baseline()
            if stored is None:
                return "no_deadline"
            begun = None if stored.met_at is None else self._charge_begun_after(stored.met_at)
            if begun is not None:
                return f"charge:{begun.astimezone(timezone.utc).isoformat()}"
            return stored.departure_key if stored.departure_key.startswith(("charge:", "no_deadline")) else "no_deadline"
        instant = self._departure_instant(settings, calculated_at, entry)
        return "no_deadline" if instant is None else instant.isoformat()

    def _departure_instant(
        self, settings: AutoSettings, calculated_at: datetime, entry: AreaEntry
    ) -> datetime | None:
        """The departure the plan is for now (the chosen date's, else the next occurrence), or `None`
        without a departure."""
        if not settings.departure_enabled:
            return None
        dated = self._effective_departure_date(settings, entry, calculated_at)
        if dated is not None:
            zone = dt_util.get_time_zone(entry.tz)
            if zone is None:
                return None
            return local_instant(dated, settings.departure, zone)
        return resolve_departure(calculated_at, entry.tz, settings.departure, calculated_at)

    def _charge_begun_after(self, since: datetime) -> datetime | None:
        """The start of the latest recorded charge that began after `since`, or `None`. A charge a person
        or the sun started is theirs, not a new need: only one the plan or the charger itself started
        counts."""
        store = domain_data(self._hass).session_store
        if store is None:
            return None
        sessions = [*store.closed_raw(self._entry_id)]
        open_session = store.open_raw(self._entry_id)
        if open_session is not None:
            sessions.append(open_session)
        starts = [
            session.start
            for session in sessions
            if session.start > since and session.started_by not in (STARTED_MANUAL, STARTED_SOLAR, STARTED_HYBRID)
        ]
        return max(starts, default=None)

    def _epoch_start(self, departure_key: str, calculated_at: datetime) -> datetime:
        """When a new count began: the plug-in or the charge its key names, else now."""
        for prefix in ("plugin:", "charge:"):
            if departure_key.startswith(prefix):
                parsed = dt_util.parse_datetime(departure_key[len(prefix):])
                if parsed is not None and parsed.tzinfo is not None:
                    return parsed
        return calculated_at

    def _plugged_in_at(self) -> datetime | None:
        """When the charger last saw a vehicle plugged in, or `None` (no executor, or never seen)."""
        if self._executor is None:
            return None
        return self._executor.controller.plugged_in_at

    def _arm_departure(self, settings: AutoSettings) -> None:
        """One appointment at the departure the plan is for: when it passes, the next occurrence is
        planned (and its energy counted afresh) without waiting for the next price."""
        self._cancel_departure()
        if self._shutdown or not settings.departure_enabled or not settings.area_id:
            return
        entry = self._manager.catalogue_snapshot().area(settings.area_id)
        if entry is None:
            return
        try:
            instant = self._departure_instant(settings, self._now(), entry)
        except Exception:  # noqa: BLE001 - an appointment is a convenience, never a failure
            _LOGGER.debug("Resolving the departure appointment failed", exc_info=True)
            return
        if instant is None:
            return
        target = instant.astimezone(timezone.utc) + timedelta(seconds=1)
        generation = self._generation

        @callback
        def departed(_now: datetime) -> None:
            self._departure_cancel = None
            if self._shutdown or generation != self._generation:
                return
            if self._now() < target:
                # Early (a scheduler that fired ahead of time): wait for the instant itself.
                self._departure_cancel = self._manager.schedule_at(target, departed)
                return
            self._hass.async_create_task(self._on_area_changed(generation))

        self._departure_cancel = self._manager.schedule_at(target, departed)

    def _cancel_departure(self) -> None:
        if self._departure_cancel is not None:
            self._departure_cancel()
            self._departure_cancel = None

    @callback
    def note_connection(self, event: str) -> bool:
        """The charger saw a vehicle plugged in or unplugged: plan again shortly (a status that settles
        through a few values is one event), and after a plug-in start the plan's window open now.

        Returns whether the start after a plug-in is taken care of here (an executor that can run it).
        """
        if self._shutdown:
            return False
        self._cancel_connection()
        if event == CONNECTION_PLUGGED_IN:
            self._plug_in_pending = True

        @callback
        def settled(_now: datetime) -> None:
            self._connection_cancel = None
            if self._shutdown:
                return
            plug_in = self._plug_in_pending
            self._plug_in_pending = False
            self._hass.async_create_task(self._async_replan_for_connection(plug_in))

        self._connection_cancel = self._manager.schedule_at(
            self._now() + timedelta(seconds=CONNECTION_DEBOUNCE_S), settled
        )
        return self._executor is not None

    def _cancel_connection(self) -> None:
        if self._connection_cancel is not None:
            self._connection_cancel()
            self._connection_cancel = None

    async def _async_replan_for_connection(self, plug_in: bool) -> None:
        """Plan again for the vehicle that arrived or left; after a plug-in, start an open window."""
        if self._shutdown:
            return
        try:
            if self._executor is not None:
                await self._executor.async_settle_pause()
            self._publish(await self._calculate(self._store.settings(self._entry_id)))
        except Exception as err:  # noqa: BLE001 - a replan is retried by the next event
            _LOGGER.warning("Replanning after a plug-in or unplug failed: %s", type(err).__name__)
        if plug_in and self._executor is not None and not self._shutdown:
            try:
                await self._executor.async_start_on_plug_in()
            except Exception as err:  # noqa: BLE001 - reported; the window's own timers still run
                _LOGGER.warning("Starting a window after a plug-in failed: %s", type(err).__name__)

    def _arm_energy_watch(self, settings: AutoSettings) -> None:
        """Watch the energy register while a `manual_kwh` need is counted against it, so the charge
        stops when the energy is delivered (`_on_energy_reading`), as a target stops at its state of
        charge."""
        entity_id = None if settings.driver == DRIVER_TARGET_SOC else self._energy_register_entity_id()
        if entity_id == self._energy_watched:
            return
        self._drop_energy_watch()
        if entity_id is None or self._shutdown:
            return
        self._energy_watched = entity_id
        self._energy_cancel = async_track_state_change_event(
            self._hass, [entity_id], self._on_energy_reading
        )

    def _drop_energy_watch(self) -> None:
        if self._energy_cancel is not None:
            self._energy_cancel()
        self._energy_cancel = None
        self._energy_watched = None
        if self._sessions_cancel is not None:
            self._sessions_cancel()
        self._sessions_cancel = None

    def _arm_charge_watch(self) -> None:
        """Hear of recorded charges, once: on a charger that cannot report a plug-in, a charge that
        begins after a met need starts a new count without a departure (`_departure_key`)."""
        if self._sessions_cancel is not None or self._shutdown or self._executor is None:
            return
        store = domain_data(self._hass).session_store
        if store is None:
            return
        self._sessions_cancel = store.add_listener(self._entry_id, self._on_sessions_changed)

    @callback
    def _on_sessions_changed(self) -> None:
        if self._shutdown or self._plugged_in_at() is not None:
            return
        settings = self._store.settings(self._entry_id)
        if settings.driver == DRIVER_TARGET_SOC or settings.departure_enabled:
            return
        stored = self._baseline()
        if stored is None or stored.met_at is None:
            return
        begun = self._charge_begun_after(stored.met_at)
        if begun is None or stored.departure_key == f"charge:{begun.astimezone(timezone.utc).isoformat()}":
            return
        self.note_connection(CHARGE_BEGUN)

    @callback
    def _on_energy_reading(self, _event: Any = None) -> None:
        """A new register reading, taken into the count (`advance_register`). When the manual need is
        delivered, and a second believed reading still says so, Auto's plan ends at once
        (`AutoExecutor.async_end_plan_need_met`), whatever state the planner is in, and the planner is
        asked once to say so. Readings after that change nothing until a new count begins.
        """
        if self._shutdown or self._executor is None:
            return
        settings = self._store.settings(self._entry_id)
        if settings.driver == DRIVER_TARGET_SOC:
            return
        stored = self._baseline()
        reading = self._read_energy_register()
        if stored is None or reading is None or stored.register_kwh is None:
            return
        step = self._advance(stored, reading)
        if step.baseline != stored:
            self._live_baseline = step.baseline
            if _structural(stored, step.baseline):
                self._hass.async_create_task(self._save_baseline(step.baseline))
        if not step.accepted or step.delivered_kwh < settings.requested_kwh:
            return
        # A believed reading (one that climbed no faster than the charger can deliver) is enough: a
        # register that reports once an hour must not let the charge run on for another hour.
        key = step.baseline.departure_key
        if self._energy_met_done == key:
            return
        self._energy_met_done = key
        self._hass.async_create_task(self._async_energy_met(step.baseline))

    async def _async_energy_met(self, baseline: EnergyBaseline) -> None:
        """End the plan whose energy is delivered, then plan again once to say so."""
        if baseline.met_at is None:
            met = replace(baseline, met_at=self._now())
            self._live_baseline = met
            await self._save_baseline(met)
        if self._executor is not None and self._executor.applied is not None:
            try:
                await self._executor.async_end_plan_need_met()
            except Exception as err:  # noqa: BLE001 - reported; the next calculation ends it
                _LOGGER.warning("Ending a plan whose energy is delivered failed: %s", type(err).__name__)
        await self.async_recalculate()

    def _baseline(self) -> EnergyBaseline | None:
        """The count in force: the one the register watcher keeps in memory, else the stored one."""
        stored = self._store.energy_baseline(self._entry_id)
        live = self._live_baseline
        if live is not None and stored is not None and live.departure_key == stored.departure_key:
            return live
        return stored

    def _advance(self, baseline: EnergyBaseline, reading: float) -> RegisterStep:
        controller = None if self._executor is None else self._executor.controller
        max_a = 32 if controller is None else int(controller.current_range()["max_a"])
        return advance_register(
            baseline,
            reading,
            self._now(),
            # Generous: a charger's stated range can be the everyday 32 A while it delivers 43 kW.
            max_kw=power_kw(max(max_a, CEILING_A), 3),
            plugged_in_at=None if controller is None else controller.plugged_in_at,
        )

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
        departure occurrence (or, with no departure, the current plug-in) since a baseline was recorded
        for it.

        The baseline (`AutoSettingsStore.energy_baseline`) is a register reading captured once per
        epoch and persisted, so a restart does not re-buy delivered energy; `cheapest` and `hybrid`
        share this accounting. A capturing call subtracts nothing.

        * A register that falls back to about zero counts again from zero (one that counts per plug-in,
          or a reset): what it counted before is carried. One that falls elsewhere (a meter replaced)
          carries what it counted and starts again from its new reading.
        * Without a reading, a partial charge is never bought again in full: the last remainder the
          register vouched for is kept (`kept`); with no register at all, the charger's recorded sessions
          since the epoch began are counted (`sessions`); only with neither is the whole request planned.
          None of these is trustworthy enough for `hybrid` to credit forecast sun.
        """
        requested = settings.requested_kwh
        departure_key = self._departure_key(settings, calculated_at, entry)
        stored = self._baseline()
        current_reading = self._read_energy_register()

        if stored is None or stored.departure_key != departure_key:
            # A fresh epoch: the current reading (or None) is the new starting point.
            self._energy_met_done = None
            await self._save_baseline(
                EnergyBaseline(
                    register_kwh=current_reading,
                    departure_key=departure_key,
                    started_at=self._epoch_start(departure_key, calculated_at),
                    last_register_kwh=current_reading,
                    last_register_at=None if current_reading is None else calculated_at,
                    remaining_kwh=requested if current_reading is not None else None,
                    delivered_kwh=0.0 if current_reading is not None else None,
                )
            )
            return _EnergyResolution(
                kwh=requested,
                delivered_energy_trustworthy=current_reading is not None,
                basis="register" if current_reading is not None else "requested",
                delivered_kwh=0.0 if current_reading is not None else None,
            )

        if current_reading is None:
            return self._unread_remainder(settings, stored, departure_key)

        if stored.register_kwh is None:
            # The register had no reading when this epoch began: count from now. What it vouched for
            # before is kept; anything else delivered meanwhile cannot be told.
            await self._save_baseline(
                replace(
                    stored,
                    register_kwh=current_reading,
                    last_register_kwh=current_reading,
                    last_register_at=calculated_at,
                )
            )
            return self._unread_remainder(settings, stored, departure_key)

        step = self._advance(stored, current_reading)
        delivered = step.delivered_kwh
        remaining = max(0.0, requested - delivered)
        updated = replace(
            step.baseline,
            remaining_kwh=round(remaining, 6),
            delivered_kwh=round(delivered, 6),
            met_at=step.baseline.met_at or (calculated_at if remaining <= 0 else None),
        )
        if updated != stored:
            await self._save_baseline(updated)
        # A meter that fell to a new non-zero reading lost what it counted between its last two
        # readings, and a reading not believed says nothing new: counted, not vouched for.
        replaced_meter = (
            updated.register_kwh != stored.register_kwh and current_reading > SESSION_REGISTER_ZERO_KWH
        )
        return _EnergyResolution(
            kwh=remaining,
            delivered_energy_trustworthy=step.accepted and not replaced_meter,
            basis="register",
            delivered_kwh=delivered,
        )

    async def _save_baseline(self, baseline: EnergyBaseline) -> None:
        self._live_baseline = baseline
        await self._store.async_update(self._entry_id, energy_baseline=baseline)

    def _unread_remainder(
        self, settings: AutoSettings, stored: EnergyBaseline, departure_key: str
    ) -> _EnergyResolution:
        """The need while the register cannot be read: the remainder it last vouched for, else what the
        charger's recorded sessions delivered since the epoch began, else the whole request."""
        requested = settings.requested_kwh
        if stored.delivered_kwh is not None or stored.remaining_kwh is not None:
            # What the register last vouched for was delivered: the request as it stands now, less that.
            if stored.delivered_kwh is not None:
                delivered_kept = stored.delivered_kwh
                remaining = max(0.0, requested - delivered_kept)
            else:  # a record from before the delivered energy was kept
                remaining = min(requested, stored.remaining_kwh or 0.0)
                delivered_kept = max(0.0, requested - remaining)
            return _EnergyResolution(
                kwh=remaining, delivered_energy_trustworthy=False, basis="kept", delivered_kwh=delivered_kept,
            )
        delivered = None
        if not departure_key.startswith("no_deadline") and stored.started_at is not None:
            # A bounded epoch only: counting sessions over a fixed epoch would end the need for good.
            delivered = self._session_energy_since(stored.started_at)
        if delivered is not None and delivered > 0:
            return _EnergyResolution(
                kwh=max(0.0, requested - delivered), delivered_energy_trustworthy=False,
                basis="sessions", delivered_kwh=delivered,
            )
        return _EnergyResolution(kwh=requested, delivered_energy_trustworthy=False, basis="requested")

    def _session_energy_since(self, since: datetime) -> float | None:
        """The energy this charger's recorded sessions delivered since `since` (closed and open), or
        `None` when nothing records sessions."""
        store = domain_data(self._hass).session_store
        if store is None:
            return None
        sessions = [*store.closed_raw(self._entry_id)]
        open_session = store.open_raw(self._entry_id)
        if open_session is not None:
            sessions.append(open_session)
        return sum(
            session.energy_kwh for session in sessions if session.start >= since and session.energy_kwh > 0
        )

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
        history: history_wait.HistoryDecision | None = None,
        wait_rule: str | None = None,
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
            history=history,
            wait_rule=wait_rule,
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


#: The settings' fiscal components and the names contract v2's `included` uses for them.
INCLUDED_NAME: Final = {"vat": "vat", "tax": "tax", "transfer": "grid_fee"}


def component_included(entry: AreaEntry | None, component: str) -> bool:
    """Whether the area's published price already contains a fiscal component (`vat`, `tax`, `transfer`)."""
    return entry is not None and entry.includes(INCLUDED_NAME[component])


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


def _structural(before: EnergyBaseline, after: EnergyBaseline) -> bool:
    """Whether a reading changed more than the last reading itself: a drop pending or accepted, a
    register that started again. Only these are saved between calculations."""
    keep = ("last_register_kwh", "last_register_at", "previous_register_kwh")
    return any(
        getattr(before, name) != getattr(after, name)
        for name in before.__dataclass_fields__
        if name not in keep
    )
