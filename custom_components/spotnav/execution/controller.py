"""Persistent charging schedule and charger control.

* `ChargingController` owns one charger: its plan, the timers that follow it, and every command
  sent to the charger.
* `_lock` serializes every public entry point (install, start, stop, follow, restore, window
  callbacks); `*_locked` methods assume it is held and must never re-acquire it. `_assign_lock` is
  taken inside it, for the whole read-modify-write of `AssignedCurrent`, so no two writers interleave.
* A plan is validated, persisted, then adopted in memory and armed, in that order; a failure at any
  step restores the previous state.
* A window boundary only starts or stops charging. It never extends or moves a window, and a
  target-state-of-charge stop is terminal for the plan.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback, Event, EventStateChangedData, HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_time_interval,
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT_NONE,
    CONF_CURRENT_LIMIT,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_IDLE_POWER_W,
    CONF_MODE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CONF_OCPP_TARGET_UNRESOLVED,
    CONF_POWER_ENTITY,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    DEFAULT_IDLE_POWER_W,
    DEFAULT_MIN_CURRENT_A,
    DOMAIN,
    MAX_SCHEDULE_PERIODS,
    MODE_OCPP,
)
from ..vehicles.ocpp_identity import (
    discover_controls,
    energy_register_entity_for,
    OcppConnectorTarget,
    OcppCurrentControls,
    resolve_target,
)
from .charger_entities import charger_is_disabled
from .power_energy import integrated_energy_unique_id, read_power_w
from .chargers.adapter import build_adapter, ChargerAdapter
from .chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_NO_TARGET,
    ASSIGN_PROBE_IN_FLIGHT,
    ASSIGN_READ_FAILED,
    ASSIGN_TARGET_UNAVAILABLE,
    ASSIGN_UNCONFIRMED,
    ASSIGN_UNSUPPORTED,
    IN_EFFECT_OUTCOMES,
    WRITE_REGULATOR,
    WRITE_RESEND,
    WRITE_RESTORE,
    WRITE_SESSION_START,
)
from .chargers.ocpp import assigned_amps_for_connector, OcppAssignedCurrent, rewrite_assigned_current
from .charge_progress import (
    ChargeProgress,
    ChargeProgressFacts,
    ChargeProgressObserver,
    connector_current,
    connector_status,
    START_ACK_TIMEOUT_S,
)
from .pilot_floor_probe import connector_entity_id, PilotFloorProbe, PROBE_TOKEN, STORE_KEY
from .target_stop import decide_target_stop, SocReading
from .window_hold import HOLD, OVERRIDE, WindowHold


_LOGGER = logging.getLogger(__name__)

#: How often a charge is looked at for the phases it uses.
PHASE_SAMPLE_INTERVAL = timedelta(seconds=30)
STORE_VERSION = 1

#: How long an accepted Start counts as the cause of the charge that follows it (the charge session record).
START_CAUSE_TTL_S = 300.0

# Token for hybrid window-end handoff logs (see `_async_end_callback`).
HYBRID_LOG_TOKEN = "HYBRID"

#: What a connection handler is told (`set_connection_handler`): a vehicle was plugged in, or unplugged.
CONNECTION_PLUGGED_IN = "plugged_in"
CONNECTION_UNPLUGGED = "unplugged"

# Stable codes an installation failure is reported with; the message beside each is for the log.
EXECUTION_STORAGE_FAILED = "storage_failed"
EXECUTION_RESCHEDULE_FAILED = "reschedule_failed"
EXECUTION_ROLLBACK_FAILED = "rollback_failed"


class ChargingExecutionError(RuntimeError):
    """An installation step that failed, named by a stable code.

    Payload validation raises `ValueError` (a 400). This type covers steps after a plan is accepted
    (persisting, arming, rolling back), so a caller can tell "refused" from "accepted but the charger
    could not be brought to it".
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

# Parsing entity ids to find a connector lives in `ocpp_identity`: every OCPP path here
# is given an `OcppConnectorTarget` and none re-derives one from a current-slider entity id.


def _stored_ocpp_target(config: dict[str, Any]) -> OcppConnectorTarget | None:
    """The connector target the config entry stores, or `None` when none is usable.

    Trusted only in the exact shape this integration writes (non-empty charge point id, positive
    integer connector); a half-written target is treated as absent, since a guessed connector cannot be
    told from a correct one.
    """
    charge_point_id = config.get(CONF_OCPP_CHARGE_POINT_ID)
    connector_id = config.get(CONF_OCPP_CONNECTOR_ID)
    if not isinstance(charge_point_id, str) or not charge_point_id:
        return None
    if isinstance(connector_id, bool) or not isinstance(connector_id, int):
        return None
    if connector_id < 1:
        return None
    return OcppConnectorTarget(charge_point_id, connector_id)


# Absolute safety bounds for any current treated as valid, independent of a current-limit entity's
# own min/max (which may narrow but never widen them).
ABSOLUTE_MIN_AMPS = 1
ABSOLUTE_MAX_AMPS = 80

# Unit strings accepted for a current-limit entity. A missing or unknown unit is never
# assumed to be amperes (same rule as `site_capacity.classify_current`).
_AMPERE_UNITS = ("a", "amp", "amps", "ampere", "amperes")


@dataclass(slots=True)
class ChargingPlan:
    """A charging period received from SpotNav."""

    start: str
    end: str
    amps: int
    phases: int = 3
    power_kw: float | None = None
    energy_kwh: float | None = None
    price_area: str | None = None
    unpriced: bool = False
    periods: list[dict[str, str]] | None = None
    # The target state of charge this charge must stop at, and which vehicle to read it
    # from. Both optional; absent means "no target": the charge ends with its window.
    target_soc_percent: float | None = None
    vehicle_id: str | None = None
    # What Auto built this plan from: identity, settings revision and price identity.
    # Internal metadata, never read out of a payload.
    auto_identity: str | None = None
    auto_settings_revision: int | None = None
    auto_price_identity: str | None = None

    @property
    def auto_owned(self) -> bool:
        """Whether Auto built this plan, from the identity it stamped on it."""
        return bool(self.auto_identity)

    @property
    def start_time(self) -> datetime:
        return _parse_datetime(self.start)

    @property
    def end_time(self) -> datetime:
        return _parse_datetime(self.end)

    @property
    def windows(self) -> list[tuple[datetime, datetime]]:
        if not self.periods:
            return [(self.start_time, self.end_time)]
        return [(_parse_datetime(item["start"]), _parse_datetime(item["end"])) for item in self.periods]


def _is_auto_identity(value: Any) -> bool:
    """A lowercase 32-hex application identity, exactly as this release writes it."""
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def stored_plan(raw: Any) -> ChargingPlan | None:
    """A stored plan record as a plan, or `None` when it may not be trusted as one.

    Stored JSON is untrusted and the record carries ownership, so the policy is strict: a record whose
    metadata is partial, contradictory, unknown or mistyped is discarded, not repaired, because wrongly
    adopting one makes Auto defend a schedule it does not own. Shape and safety are checked too (ordered
    periods, each ending after it starts, timezone-aware instants, a whole-number current within
    bounds), so an invalid record never reaches timer arming. An expired plan is accepted: it is
    history, reported as `complete`.
    """
    if not isinstance(raw, dict):
        return None
    identity = raw.get("auto_identity")
    revision = raw.get("auto_settings_revision")
    price_identity = raw.get("auto_price_identity")
    if not _is_auto_identity(identity):
        return None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return None
    if not isinstance(price_identity, str) or not price_identity or len(price_identity) > 512:
        return None
    try:
        plan = ChargingPlan(**raw)
    except TypeError:
        # Unknown keys: a shape this release did not write.
        return None
    try:
        windows = plan.windows
    except ValueError:
        return None
    if not windows or len(windows) > MAX_SCHEDULE_PERIODS:
        return None
    previous_end: datetime | None = None
    for start, end in windows:
        if end <= start:
            return None
        if previous_end is not None and start < previous_end:
            return None
        previous_end = end
    if isinstance(plan.amps, bool) or not isinstance(plan.amps, int):
        return None
    if not ABSOLUTE_MIN_AMPS <= plan.amps <= ABSOLUTE_MAX_AMPS:
        return None
    if plan.phases not in (1, 3):
        return None
    return plan


def _parse_datetime(value: str) -> datetime:
    parsed = dt_util.parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        raise ValueError("Timestamp must include a time zone")
    return parsed


def _safe_float(value: Any) -> float | None:
    """`value` as a finite float, or `None`: missing, non-numeric, `NaN`/`inf`, a string that
    raises `OverflowError`, or a `bool` (never a current reading).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _validate_current_limit_state(state: State | None) -> int | None:
    """A current-limit entity's live state as validated whole amps, or `None`.

    The single source of truth for reading this entity. Accepted only when the entity exists and is not
    `unknown`/`unavailable`/empty, its unit is an explicit ampere unit, the state is a finite whole
    number (a fractional reading is rejected, never rounded), its parseable `min`/`max` attributes are
    respected, and the value is within `[ABSOLUTE_MIN_AMPS, ABSOLUTE_MAX_AMPS]`.
    """
    if state is None:
        return None
    if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, "", None):
        return None
    unit = state.attributes.get("unit_of_measurement")
    if not unit or str(unit).strip().lower() not in _AMPERE_UNITS:
        return None
    value = _safe_float(state.state)
    if value is None:
        return None
    if value != int(value):
        return None
    amps = int(value)
    minimum = _safe_float(state.attributes.get("min"))
    maximum = _safe_float(state.attributes.get("max"))
    if minimum is not None and amps < minimum:
        return None
    if maximum is not None and amps > maximum:
        return None
    if not ABSOLUTE_MIN_AMPS <= amps <= ABSOLUTE_MAX_AMPS:
        return None
    return amps


#: The lowest current the card offers and a charger's range starts at (the J1772 pilot
#: floor), the fallback ceiling when the charger states none, and where a ceiling came from.
CURRENT_RANGE_MIN_A = 6
CURRENT_RANGE_DEFAULT_MAX_A = 32
CURRENT_RANGE_SOURCE_CURRENT_LIMIT = "current_limit"
CURRENT_RANGE_SOURCE_SESSION_LIMIT = "session_limit"
CURRENT_RANGE_SOURCE_STATION_MAXIMUM = "station_maximum"
CURRENT_RANGE_SOURCE_DEFAULT = "default"


def current_range_dict(max_a: int, source: str) -> dict[str, Any]:
    """The dashboard's `current_range` block: `{min_a, max_a, source}`."""
    return {"min_a": CURRENT_RANGE_MIN_A, "max_a": max_a, "source": source}


def _charger_maximum_from_state(state: State | None) -> int | None:
    """The whole-amp ceiling a current entity states in its `max` attribute, or `None`.

    Same discipline as `_validate_current_limit_state` for the range (explicit ampere unit, finite,
    fractional maximum floored). The entity's value is not judged: an idle or unavailable entity still
    describes what it can be set to.
    """
    if state is None:
        return None
    unit = state.attributes.get("unit_of_measurement")
    if not unit or str(unit).strip().lower() not in _AMPERE_UNITS:
        return None
    maximum = _safe_float(state.attributes.get("max"))
    if maximum is None:
        return None
    amps = int(math.floor(maximum))
    if not CURRENT_RANGE_MIN_A <= amps <= ABSOLUTE_MAX_AMPS:
        return None
    return amps


def _validate_stored_amps(value: Any) -> int | None:
    """A value read back from storage as validated whole amps, or `None`."""
    value = _safe_float(value)
    if value is None or value != int(value):
        return None
    amps = int(value)
    if not ABSOLUTE_MIN_AMPS <= amps <= ABSOLUTE_MAX_AMPS:
        return None
    return amps


@dataclass(frozen=True, slots=True)
class ResolvedCurrent:
    """The best available answer to "what current is this charger about to use", with provenance.

    `source == "setpoint"` means the value came from the current-limit entity's live state, not from
    anything SpotNav requested; callers must never report it as an explicit request.
    """

    amps: int | None
    source: Literal["requested", "setpoint", "unknown"]


RESTORE_NOT_NEEDED = "not_needed"
RESTORE_RESTORED = "restored"
RESTORE_FAILED = "failed"
RESTORE_NO_AUTHORITATIVE_CURRENT = "no_authoritative_current"


#: What became of one regulator write (`ChargingController.async_apply_regulated_current`).
REGULATED_WROTE = "wrote"
#: Refused by the adapter's policy, nothing sent, and the fuse did not need it.
REGULATED_HELD = "held"
#: The fuse needed a lower current the adapter could not write in time: the charge was stopped.
REGULATED_STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class RegulatedWrite:
    """The answer to one regulator write: what was done and why.

    `outcome` is `wrote`, `held` or `stopped`; `code` is the adapter's stable outcome code (or
    `safety_stop`/`pause`); `written` says whether the current went out, so the caller can keep its
    damping state honest about what is really on the charger.
    """

    outcome: str
    code: str
    written: bool


@dataclass(frozen=True, slots=True)
class CurrentRestore:
    """One charger's answer to `ChargingController.async_restore_current`."""

    outcome: str
    code: str | None
    from_a: int | None
    to_a: int | None


class ChargingController:
    """Control one Home Assistant charger from SpotNav commands.

    A plan may carry a target state of charge, so the charge must also stop when the car reaches it
    (decision in `execution/target_stop.py`). The reading arrives as an injected callable; a controller
    built without one enforces nothing.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: dict[str, Any],
        *,
        soc_reader: Callable[[str | None], SocReading | None] | None = None,
        end_window_guard: Callable[[], bool] | None = None,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        # Hybrid mode's one hook into window-end handling (see `set_end_window_guard` and
        # `_async_end_callback`); `None` is inert.
        self._end_window_guard = end_window_guard
        # What the hold of a charge that starts by itself outside a window remembers
        # (`window_hold.py`), and the one hook that tells it something else owns the charger.
        self._hold = WindowHold()
        self._hold_guard: Callable[[], bool] | None = None
        # What the site lets a start give the car (`set_start_cap`); `None` means no cap applies.
        self._start_cap: Callable[[], float | None] | None = None
        self._last_connected: bool | None = None
        # The last connection the charger reported in so many words (`None` until it said one): only a
        # change between two known states is a plug-in or an unplug, never a status that comes back after
        # a restart or a blip through `unavailable`.
        self._known_connected: bool | None = None
        # The charge clock (`charging_seconds`): seconds counted while charging, and since when it runs.
        self._charge_clock_s = 0.0
        self._charge_clock_since: datetime | None = None
        # What a charge load balancing paused was (its origin, and whether it was the plan's), so the
        # regulator's resume gives it back (`async_battery_probe_start`).
        self._paused_charge: tuple[str | None, bool] | None = None
        #: When a vehicle was last seen plugged in (known unplugged, then known plugged in). Persisted: a
        #: charge counted per plug-in must not start counting again after a restart.
        self._plugged_in_at: datetime | None = None
        # Who is told about a plug-in or an unplug (`set_connection_handler`); it answers whether it
        # takes care of starting an open window itself (Auto replans first).
        self._connection_handler: Callable[[str], bool] | None = None
        # The end of the window a person stopped the charge in: until then a plug-in or a new plan
        # does not start that window again. In memory only, as a person's Stop has always been.
        self._person_stop_until: datetime | None = None
        # Whether the charge that runs was started by a plan window of ours (not a person's Start, not
        # solar). Persisted: a restart outside every window must still know the charge is ours.
        self._plan_charge = False
        #: Who started the charge that is running (`manual`, `solar`, `plan_window`, `other`), for the
        #: grid-charging signal; `None` while unknown (a charger that started by itself).
        self._charge_origin: str | None = None
        self.charge_control: str = config[CONF_CHARGE_CONTROL]
        # "None" in the card: no current entity, no current control and no automatic session-limit lookup.
        self.current_limit_none: bool = bool(config.get(CONF_CURRENT_LIMIT_NONE))
        self.current_limit: str | None = (
            None if self.current_limit_none else config.get(CONF_CURRENT_LIMIT) or None
        )
        # How this charger's current may be set. Absent or empty records the request without
        # applying it; the explicit opt-in is the only value that changes behaviour (see const.py).
        self.current_control: str = "" if self.current_limit_none else config.get(CONF_CURRENT_CONTROL) or ""
        # OCPP control identity: which connector of which charge point every OCPP write is about.
        self.ocpp_target: OcppConnectorTarget | None = None
        self.ocpp_target_source = ""
        # Only OCPP entries (mode OCPP or the OCPP-only control strategy) need an identity; a
        # generic charger keeps its `current_limit` model.
        if config.get(CONF_MODE) == MODE_OCPP or self.current_control == (
            CURRENT_CONTROL_CHANGE_CONFIGURATION
        ):
            stored = _stored_ocpp_target(config)
            if stored is not None:
                self.ocpp_target, self.ocpp_target_source = stored, "stored"
            elif config.get(CONF_OCPP_TARGET_UNRESOLVED):
                self.ocpp_target_source = "unresolved"
            else:
                resolution = resolve_target(hass, charge_control=self.charge_control)
                self.ocpp_target = resolution.target
                self.ocpp_target_source = resolution.source or "unresolved"
        # `manual_kwh` delivered-energy accounting: the stored override if any, otherwise the same
        # connector's cumulative `Energy.Active.Import.Register` sensor, found through the
        # registries and the entity's key (`ocpp_identity.energy_register_entity_for`), never by
        # constructing an entity id.
        self.energy_register_entity_id: str | None = config.get(CONF_ENERGY_REGISTER_ENTITY) or None
        if self.energy_register_entity_id is None and self.ocpp_target is not None:
            self.energy_register_entity_id = energy_register_entity_for(hass, self.ocpp_target)
        # A charger behind a smart plug: its power sensor. With no energy register of its own, SpotNav's
        # integrated-energy sensor (`sensor.py`) stands in for one once it exists.
        self.power_entity_id: str | None = config.get(CONF_POWER_ENTITY) or None
        self.idle_power_w: float = float(config.get(CONF_IDLE_POWER_W) or DEFAULT_IDLE_POWER_W)
        self.energy_from_power: bool = (
            self.energy_register_entity_id is None and self.power_entity_id is not None
        )
        if self.energy_from_power:
            self.energy_register_entity_id = er.async_get(hass).async_get_entity_id(
                "sensor", DOMAIN, integrated_energy_unique_id(entry_id)
            )
        # How this charger is started, stopped, read and given a current (`chargers/adapter.py`):
        # the OCPP `ChangeConfiguration` path and the generic switch are two of its parts. The
        # target is read through a getter, so the OCPP part always sees the controller's own.
        self.adapter: ChargerAdapter = build_adapter(
            hass,
            config,
            ocpp_target=lambda: self.ocpp_target,
            energy_entity_id=self.energy_register_entity_id,
        )
        self.plan: ChargingPlan | None = None
        # How this controller reads a vehicle's state of charge, or `None` when it enforces no
        # target.
        self._soc_reader = soc_reader
        # The record of the last target stop, persisted with the plan (see `_async_save`).
        # Written only by the target-stop path.
        self._target_stop: dict[str, Any] | None = None
        # True from when a reached target is about to stop the charge until the stop completes.
        self._target_stopping = False
        # The last time a charge ended because its plan was done (`completion_record`). In memory only:
        # a restart is not a completion.
        self._completion: dict[str, Any] | None = None
        # The one state-change subscription, alive while a plan with a target is (see
        # `_async_reschedule`).
        self._state_listener_cancel: Callable[[], None] | None = None
        # The last amps SpotNav explicitly asked for; distinct from `plan.amps` (a manual start
        # with no schedule sets it too) and from `setpoint_current_a` (the entity's live value,
        # which can drift).
        self._requested_current_a: int | None = None
        # Whether the last thing done to the charge control was the regulator's pause (a stop for
        # want of headroom for the minimum current): any other stop or start clears it. Only the
        # battery probe reads it (`site/battery_probe.py`), to resume a charge balancing interrupted.
        self._paused_by_balancing = False
        # When a Start this controller accepted was sent to the charge control while the charger
        # has not reported it on yet, else `None`.
        self._start_sent_at: datetime | None = None
        # Who asked for the Start that was last accepted and when, for the charge session record.
        self._start_cause: tuple[str, datetime] | None = None
        #: A session start found its current's target unavailable (a number that exists only while
        #: a session runs): written once that target reports.
        self._start_write_pending = False
        # The vehicle-side observation (`charge_progress.py`): one passive value plus the grace
        # timer. Built with this controller as its host, hence a protocol rather than an import.
        self._charge_progress = ChargeProgressObserver(self)
        # The one state-change subscription the observation needs, armed for this controller's
        # life (see `_async_arm_progress_listener`).
        self._progress_listener_cancel: Callable[[], None] | None = None
        # What a running charge shows about the phases it uses (`planning/phases.py`), sampled on a timer.
        # (imported here: `planning/phases.py` reaches this module through `planning/first_run.py`)
        from ..planning.phases import PhaseObserver

        self._phase_observer = PhaseObserver()
        self._phase_timer_cancel: Callable[[], None] | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}"
        )
        # The one-off pilot-floor probe (`pilot_floor_probe.py`), built with this controller as
        # its host (a protocol, so the dependency runs one way).
        self._probe = PilotFloorProbe(self)
        # The probe's persisted state, restored in `async_initialize`.
        self._probe_record: dict[str, Any] = {}
        # The probe's state-change subscription, alive while a plan is (see `_async_reschedule`);
        # "has the car been drawing for a minute" is only answerable from that sensor's reports.
        self._probe_listener_cancel: Callable[[], None] | None = None
        self._timer_cancels: list[Callable[[], None]] = []
        self._listeners: set[Callable[[], None]] = set()
        # Who wants to know what Home Assistant reports for the charge control, as opposed to what
        # this integration asked for.
        self._charge_state_listeners: set[Callable[[], None]] = set()
        self._charge_state_cancel: Callable[[], None] | None = None
        # The one operation lock for the plan, its timers and its persisted record.
        self._lock = asyncio.Lock()
        # Serialises every `AssignedCurrent` read-modify-write (the site regulator's, a Start's and
        # the active-control restore's).
        self._assign_lock = asyncio.Lock()

    async def async_initialize(self) -> None:
        """Restore a saved schedule and requested-current memory, and resume."""
        async with self._lock:
            await self._initialize_locked()

    async def _initialize_locked(self) -> None:
        """The restore itself, with the operation lock held.

        The target-stop record is restored too: it explains why a finished charge ended, and a restart must
        not erase it.
        """
        saved = await self._store.async_load()
        if saved:
            if saved.get("plan"):
                # Never a plain `ChargingPlan(**record)`: the record carries ownership, so it goes
                # through the strict reader (`stored_plan`).
                stored = stored_plan(saved["plan"])
                if stored is None:
                    _LOGGER.warning("Discarding invalid saved SpotNav charging plan")
                else:
                    self.plan = stored
            self._plan_charge = saved.get("plan_charge") is True
            origin = saved.get("charge_origin")
            self._charge_origin = origin if isinstance(origin, str) else None
            plugged = saved.get("plugged_in_at")
            parsed = dt_util.parse_datetime(plugged) if isinstance(plugged, str) else None
            self._plugged_in_at = parsed if parsed is not None and parsed.tzinfo is not None else None
            raw_requested = saved.get("requested_current_a")
            validated_requested = _validate_stored_amps(raw_requested)
            if raw_requested is not None and validated_requested is None:
                _LOGGER.warning(
                    "Discarding invalid saved SpotNav requested current: %r", raw_requested
                )
            self._requested_current_a = validated_requested
            raw_target_stop = saved.get("target_stop")
            if isinstance(raw_target_stop, dict):
                self._target_stop = raw_target_stop
            elif raw_target_stop is not None:
                # Storage is untrusted: a record of another shape is dropped, not reported as fact.
                _LOGGER.warning(
                    "Discarding invalid saved SpotNav target stop record: %r", raw_target_stop
                )
            raw_probe = saved.get(STORE_KEY)
            if isinstance(raw_probe, dict):
                self._probe_record = raw_probe
            # A probe that started and did not finish leaves the charger where its last step put
            # it.
            if self._probe_record.get("started") and not self._probe_record.get("completed"):
                _LOGGER.error(
                    "%s started but never finished, so %s may still be left at %r rather "
                    "than %r; set it back by hand",
                    PROBE_TOKEN,
                    self._probe_record.get("devid"),
                    self._probe_record.get("last_written"),
                    self._probe_record.get("remembered"),
                )
        await self._reschedule_locked()
        # Armed for the controller's whole life, not only while a plan is: a manual Start has no
        # plan, and a scheduled one is observed from acceptance.
        self._async_arm_progress_listener()

    @property
    def requested_current_a(self) -> int | None:
        """The last amps SpotNav explicitly asked this charger to use (manual start, schedule
        start callback, or, when nothing else is known, the current-limit entity's live value; see
        `async_start`). `None` means unknown and is never presented as a measured current.
        """
        return self._requested_current_a

    @property
    def setpoint_current_a(self) -> int | None:
        """The current-limit entity's configured value, read live: independent of what SpotNav
        last asked for and never a measured charging current. `None` if no entity is configured or
        its state is unusable (see `_validate_current_limit_state`).
        """
        return self._current_limit_entity_value()

    @callback
    def set_integrated_energy_entity(self, entity_id: str) -> None:
        """The integrated-energy sensor was added: it is this charger's energy register, unless the
        person set one (then it is only an extra sensor and nothing reads it as the register).
        """
        if not self.energy_from_power:
            return
        self.energy_register_entity_id = entity_id
        self.adapter.energy_entity_id = entity_id

    @property
    def charge_progress(self) -> ChargeProgress:
        """What is known about whether the vehicle is taking the charge (`charge_progress.py`).

        Recomputed from current facts at every observation point, never derived by a caller.
        """
        return self._charge_progress.progress

    @property
    def charge_expected_now(self) -> bool:
        """Whether SpotNav expects this charger to be charging now: the charge control reports
        itself on (an accepted manual or scheduled Start), or the installed plan covers this
        instant. A plan whose stored instants cannot be read answers `False` rather than raising.
        """
        if self._control_on:
            return True
        plan = self.plan
        if plan is None:
            return False
        try:
            windows = plan.windows
        except ValueError:
            return False
        now = dt_util.utcnow()
        return any(start <= now < end for start, end in windows)

    @property
    def plan_expects_charge(self) -> bool:
        """Whether the installed plan expects this charger to be charging now, so a charge that is not
        running is a surprise: a window of the plan is open, nothing else owns the charger (a pause,
        solar), a person has not stopped this window, load balancing has not paused the charge, the
        target is not being stopped for, and the vehicle is not known to be unplugged.
        """
        return (
            self.plan_window_active_now
            and not self._target_stopping
            and not self._paused_by_balancing
            and not self._hold_blocked()
            and not self._person_stopped_now()
            and self.adapter.vehicle_connected() is not False
        )

    @property
    def completion_record(self) -> dict[str, Any] | None:
        """The last charge that ended because its plan was done, or `None`: `at` (ISO instant),
        `reason` (`target` reached, requested `energy` delivered, or `plan_done`: the plan's last window
        ended while it charged) and, for a target, `target_soc_percent` and `soc_percent`.
        """
        return self._completion

    def _record_completion(self, reason: str, **facts: Any) -> None:
        self._completion = {"at": dt_util.utcnow().isoformat(), "reason": reason, **facts}

    @property
    def start_pending(self) -> bool:
        """Whether an accepted Start is still awaiting the charger's acknowledgement.

        Bounded: after `START_ACK_TIMEOUT_S` it is `False` again, since an unanswered command differs from
        a started charge the car is not taking. Reading clears the record once the charge control reports
        itself on.
        """
        sent_at = self._start_sent_at
        if sent_at is None:
            return False
        if self.charging or self.adapter.held_by_charger():
            # Answered: charging, or the charger says its own scheduler holds the charge.
            self._start_sent_at = None
            return False
        return (dt_util.utcnow() - sent_at).total_seconds() < START_ACK_TIMEOUT_S

    @property
    def held_by_charger(self) -> bool:
        """Whether the charger's own scheduler or load balancer holds the charge."""
        return self.adapter.held_by_charger()

    def connection(self) -> tuple[str, str | None]:
        """The charger's connection state and the entity it came from (`charger_connection.py`)."""
        return self.adapter.connection()

    @property
    def states_unreported(self) -> bool:
        """Whether the charger has not yet reported anything usable: its status sensor (when one is
        configured) is missing, unavailable or unknown, and so is its charge control. Right after a
        restart this is every charger, and the card then says it is starting up rather than "off".
        """
        if self.adapter.status_readable():
            return False
        state = self.hass.states.get(self.charge_control)
        return state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)

    @property
    def charger_disabled(self) -> bool:
        """Whether the charger's own enable switch is off (Easee's `is_enabled`): it cannot start while
        it is, and SpotNav never writes that switch.
        """
        return charger_is_disabled(self.hass, self.charge_control, self.adapter.platform)

    @property
    def charge_progress_subject(self) -> str:
        """The observation's subject: this connector, plan and charge control."""
        target = self.ocpp_target
        connected = "none" if target is None else f"{target.devid}:{target.connector_id}"
        plan = self.plan
        identity = "none" if plan is None else f"{plan.start}|{plan.end}|{plan.amps}"
        return f"{self.entry_id}|{self.charge_control}|{connected}|{identity}"

    def charge_progress_facts(self) -> ChargeProgressFacts:
        """One captured instant for the vehicle-side observation (`charge_progress.py`).

        Synchronous, read-only, and the only place those entity states are read: the connector's status
        sensor and current-import main state for the connector the target names (never the station-wide
        ceiling), this controller's expectation, and whether a Start is unanswered.
        """
        target = self.ocpp_target
        status: str | None = None
        current: float | None = None
        if target is None and not self.adapter.is_ocpp:
            # A charger that is not OCPP: the adapter's status sensor and measured current.
            status = self.adapter.progress_status()
            current = self.adapter.measured_current_a()
        if target is not None:
            states = self.hass.states
            status = connector_status(
                states.get(
                    connector_entity_id(target.devid, target.connector_id, "status_connector")
                )
            )
            current = connector_current(
                states.get(
                    connector_entity_id(target.devid, target.connector_id, "current_import")
                )
            )
        # A charger with a power sensor and no status sensor is judged by its power.
        power_mode = (
            target is None
            and not self.adapter.is_ocpp
            and not self.adapter.status_entity_id
            and self.power_entity_id is not None
        )
        return ChargeProgressFacts(
            expected=self.charge_expected_now,
            start_pending=self.start_pending,
            connector_status=status,
            current_import_a=current,
            subject=self.charge_progress_subject,
            power_mode=power_mode,
            power_w=read_power_w(self.hass, self.power_entity_id) if power_mode else None,
            idle_power_w=self.idle_power_w,
        )

    @callback
    def charge_progress_changed(self) -> None:
        """The observation's grace period expired: tell the readers."""
        self._notify()

    @callback
    def _async_progress_state_changed(self, event: Any = None) -> None:
        """A watched fact reported: decide the diagnostic again, and tell readers if it moved.

        A charger that forgets its current limit on plug-in or reboot is told it again.
        """
        self._tick_charge_clock()
        self._maybe_resend_current(event)
        self._maybe_write_after_start(event)
        self._observe_connection()
        changed = self._observe_hold()
        if self._charge_progress.evaluate() or changed:
            self._notify()

    def _remember_paused_charge(self, origin: str | None, plan_charge: bool) -> None:
        """Load balancing holds a charge back (paused it, or refused its start below the floor): remember
        what it was, so the regulator's resume (`async_battery_probe_start`) gives it back that origin."""
        self._paused_by_balancing = True
        self._paused_charge = (origin, plan_charge)

    def _charge_clock_running(self) -> bool:
        """Whether the charge clock runs: the charger charges or its control is on, or this cannot be
        told (an unreadable control, a charger with no status and a control that says nothing, such as a
        button pair). Lenient on purpose: the clock only bounds what an energy register may count."""
        if self._control_on or self._control_observation is None:
            return True
        return not self.adapter.status_readable() and self.adapter.enabled_state() is None

    def _tick_charge_clock(self) -> None:
        """Advance the charge clock: the seconds the charge control has been on (or the charger charging)
        since this controller started, observed at every report and every command."""
        now = dt_util.utcnow()
        since = self._charge_clock_since
        if since is not None and now > since:
            self._charge_clock_s += (now - since).total_seconds()
        self._charge_clock_since = now if self._charge_clock_running() else None

    def charging_seconds(self) -> float:
        """How long the charger has been, or may have been, charging since this controller started
        (`_charge_clock_running`).
        Only differences mean anything: what an energy register may have counted between two readings
        is bounded by the charging time between them (`planning/auto_controller.advance_register`)."""
        self._tick_charge_clock()
        return self._charge_clock_s

    def _observe_connection(self) -> None:
        """Notice a plug-in or an unplug: a change between two connection states the charger reported.

        A plug-in is remembered (`plugged_in_at`) and handed to the connection handler, which replans; a
        plug-in inside an open window of the installed plan starts that window now unless the handler
        takes care of it (`async_start_on_plug_in`).
        """
        connected = self.adapter.vehicle_connected()
        if connected is None:
            return
        previous = self._known_connected
        self._known_connected = connected
        if previous is None or previous == connected:
            return
        event = CONNECTION_PLUGGED_IN if connected else CONNECTION_UNPLUGGED
        if connected:
            self._plugged_in_at = dt_util.utcnow()
            self.hass.async_create_task(self._async_save_connection())
        _LOGGER.debug("SpotNav charger %s: vehicle %s", self.entry_id, event)
        handled = False
        handler = self._connection_handler
        if handler is not None:
            try:
                handled = bool(handler(event))
            except Exception:  # noqa: BLE001 - a failing handler must not stop the observation
                _LOGGER.debug("Connection handler failed", exc_info=True)
        if connected and not handled:
            self.hass.async_create_task(self.async_start_on_plug_in())

    async def _async_save_connection(self) -> None:
        async with self._lock:
            await self._async_save_quietly()

    def set_connection_handler(self, handler: Callable[[str], bool] | None) -> None:
        """Set (or clear) who hears of a plug-in or an unplug (`CONNECTION_*`). It returns whether it
        starts an open window itself after replanning; otherwise this controller does, at once.
        """
        self._connection_handler = handler

    @property
    def plugged_in_at(self) -> datetime | None:
        """When a vehicle was last seen plugged in, or `None` when no plug-in has been seen."""
        return self._plugged_in_at

    def _person_stopped_now(self) -> bool:
        until = self._person_stop_until
        if until is None:
            return False
        if dt_util.utcnow() >= until:
            self._person_stop_until = None
            return False
        return True

    def _open_window_end(self) -> datetime | None:
        """The end of the plan's window open now, or `None`."""
        plan = self.plan
        if plan is None:
            return None
        try:
            windows = plan.windows
        except ValueError:
            return None
        now = dt_util.utcnow()
        return next((end for start, end in windows if start <= now < end), None)

    async def async_start_on_plug_in(self) -> bool:
        """A vehicle was plugged in: start the installed plan's window that is open now, as its start
        would have. Returns whether a start was sent.

        Nothing starts while something else owns the charger (Auto paused, solar), after a person
        stopped the charge in this window, when the charge control already runs, or when the plan's
        target is reached; a start is capped by load balancing as every start is (`_start_locked`).
        """
        async with self._lock:
            return await self._plug_in_start_locked()

    async def _plug_in_start_locked(self) -> bool:
        if self._open_window_end() is None or self._target_stopping:
            return False
        if self._hold_blocked() or self._person_stopped_now():
            return False
        if self._control_on:
            # The charger started by itself at plug-in: the charge is the plan's (its stops apply).
            await self._claim_window_charge_locked()
            return False
        if self.adapter.vehicle_connected() is False or self.start_pending:
            # A Start (the replanned plan's own window start) is on its way.
            return False
        if self._paused_by_balancing:
            # Load balancing stopped this charge: its regulator resumes it, with its own margin and dwell.
            return False
        if await self._enforce_target_locked():
            return False
        _LOGGER.info("SpotNav charger %s: plugged in inside a planned window, starting", self.entry_id)
        return await self._start_locked(cause="plan_window")

    async def async_end_plan_need_met(self) -> bool:
        """The need is met before the plan ran out: stop the charge the plan started and clear the
        windows still ahead. A person's Start, a solar charge, a charge something else owns (a pause,
        solar, the hybrid hand-off) and one that started by itself go on. Returns whether a plan was
        cleared.

        A plan with a target ends only on the target stop's own evidence (`decide_target_stop`: a reading
        at the target, or an estimate past it by its margin), never on the planner's estimate alone.
        """
        async with self._lock:
            if self.plan is None:
                return False
            if self.plan.target_soc_percent is not None:
                # The target's own stop, with its record, or nothing.
                return await self._enforce_target_locked()
            handed_off = self._end_window_guard is not None and self._end_window_guard()
            stop = self._control_on and self._plan_charge and not self._hold_blocked() and not handed_off
            _LOGGER.info(
                "SpotNav charger %s: the need is met, clearing the plan%s",
                self.entry_id,
                " and stopping the charge" if stop else "",
            )
            if stop:
                self._record_completion(
                    "target" if self.plan.target_soc_percent is not None else "energy",
                    target_soc_percent=self.plan.target_soc_percent,
                )
                await self._stop_locked(clear_schedule=True)
                return True
            self.plan = None
            self._cancel_timers()
            self._async_disarm_target_listener()
            self._async_disarm_probe_listener()
            await self._async_save()
            self._notify()
            return True

    def _hold_blocked(self) -> bool:
        """Whether something else owns the charger (a pause, solar execution): nothing is held then."""
        return self._hold_guard is not None and self._hold_guard()

    def _next_window_start(self) -> datetime | None:
        """The start of the next window while the time is outside every window of the plan, else
        `None`: no plan, a plan whose windows are all past, or a window open now.
        """
        plan = self.plan
        if plan is None:
            return None
        try:
            windows = plan.windows
        except ValueError:
            return None
        now = dt_util.utcnow()
        if any(start <= now < end for start, end in windows):
            return None
        return min((start for start, _ in windows if start > now), default=None)

    def _observe_hold(self) -> bool:
        """Decide, once per report, whether a charge that started by itself outside a window is
        stopped (`window_hold.py`). Returns whether what the status says moved.
        """
        hold = self._hold
        connected = self.adapter.vehicle_connected()
        before = (hold.held, hold.overridden, self._last_connected)
        self._last_connected = connected
        decision = hold.observe(
            control_on=self._control_observation,
            connected=connected,
            gap=self._next_window_start() is not None and not self._hold_blocked(),
        )
        if decision == HOLD:
            self.hass.async_create_task(self._async_hold_stop())
        elif self._window_charge_unclaimed():
            self.hass.async_create_task(self._async_claim_window_charge())
        elif self._plan_charge:
            if self._control_observation is False:
                self.hass.async_create_task(self._async_forget_plan_charge())
            elif self._plan_charge_strays():
                self.hass.async_create_task(self._async_stray_stop())
        return decision in (HOLD, OVERRIDE) or before != (hold.held, hold.overridden, connected)

    def _window_charge_unclaimed(self) -> bool:
        """Whether a charge runs inside an open window of the plan that nobody started: the charger began
        it by itself (at plug-in, say). It is the plan's, so the plan's stops (its window's end, a met
        need) apply to it; never a person's, solar's, or one something else owns, or after a person's
        Stop in this window.
        """
        return (
            self._control_observation is True
            and not self._plan_charge
            and self._charge_origin is None
            and self.plan_window_active_now
            and not self._hold_blocked()
            and not self._person_stopped_now()
        )

    async def _async_claim_window_charge(self) -> None:
        async with self._lock:
            await self._claim_window_charge_locked()

    async def _claim_window_charge_locked(self) -> bool:
        if not self._window_charge_unclaimed():
            return False
        self._plan_charge = True
        self._charge_origin = "plan_window"
        self._hold.spotnav_started()
        _LOGGER.info("SpotNav charger %s: a charge began by itself in a planned window; it is the plan's", self.entry_id)
        await self._async_save_quietly()
        self._notify()
        return True

    async def _async_hold_stop(self) -> None:
        """The one stop of a charge that started by itself outside a window: the ordinary stop,
        decided again under the lock, since a window may have opened meanwhile.
        """
        async with self._lock:
            if (
                self._next_window_start() is None
                or self._hold_blocked()
                or self._hold.owned
                or not self._control_on
            ):
                return
            await self._stop_locked(clear_schedule=False)

    def _plan_charge_strays(self) -> bool:
        """Whether a charge a plan window of ours started runs while the time is outside every window
        of the installed plan (replaced, restarted, or handed off and not taken back): it is ours, so it
        is ours to stop. Never while something else owns the charger (a pause, solar) or the hybrid
        hand-off carries it, and never for a charge a person started.
        """
        plan = self.plan
        if plan is None or not self._plan_charge or self._control_observation is not True:
            return False
        if self._hold_blocked() or (self._end_window_guard is not None and self._end_window_guard()):
            return False
        try:
            windows = plan.windows
        except ValueError:
            return False
        now = dt_util.utcnow()
        return not any(start <= now < end for start, end in windows)

    async def _async_stray_stop(self) -> None:
        """The ordinary stop of a plan charge that runs outside every window, decided again under the
        lock: a plan may have been installed, or the charge ended, meanwhile.
        """
        async with self._lock:
            if self._plan_charge_strays():
                await self._stop_locked(clear_schedule=False)

    async def _async_forget_plan_charge(self) -> None:
        """The charger was seen off: nothing of ours runs, so a later start is not ours."""
        async with self._lock:
            if self._plan_charge and self._control_observation is False:
                self._plan_charge = False
                await self._async_save_quietly()

    async def _async_save_quietly(self) -> None:
        try:
            await self._async_save()
        except Exception as err:  # noqa: BLE001 - the flag is a safeguard, never a reason to fail
            _LOGGER.warning("Saving the SpotNav plan-charge flag failed: %s", type(err).__name__)

    def set_hold_guard(self, guard: Callable[[], bool] | None) -> None:
        """Set (or clear with `None`) the guard that says something else owns the charger (Auto
        paused by the person, solar or hybrid execution running it). A setter for the reason
        `set_end_window_guard` is one: the answer comes from objects built after this controller.
        """
        self._hold_guard = guard

    @property
    def hold_until(self) -> datetime | None:
        """While a charge is held back for a window still ahead, that window's start: the plan has
        one ahead, nothing else owns the charger, SpotNav has not started a charge, it is not
        charging and a vehicle is there (reported
        plugged in, or the hold itself stopped a charge it started). Else `None`.
        """
        start = self._next_window_start()
        if (
            start is None
            or self._hold_blocked()
            or self._hold.owned
            or self.charging
            or self._hold.overridden
        ):
            return None
        if self._hold.held or self.adapter.vehicle_connected() is True:
            return start
        return None

    @property
    def hold_overridden(self) -> bool:
        """Whether a person started the charge again after the hold, and it is allowed to go on."""
        return self._hold.overridden and self.charging

    def _maybe_resend_current(self, event: Any) -> None:
        current = self.adapter.current
        needs_resend = getattr(current, "needs_resend", None)
        if needs_resend is None or event is None or not self.adapter.capabilities.set_current:
            return
        data = getattr(event, "data", None) or {}
        if data.get("entity_id") != self.adapter.status_entity_id:
            return
        old, new = data.get("old_state"), data.get("new_state")
        if needs_resend(old.state if old is not None else None, new.state if new is not None else None):
            self.hass.async_create_task(self._async_resend_current())

    def _maybe_write_after_start(self, event: Any) -> None:
        """A current that found its number unavailable at the start is written once the number is
        there (OCPP's session limit exists only while a transaction runs).
        """
        if not self._start_write_pending or event is None:
            return
        data = getattr(event, "data", None) or {}
        if data.get("entity_id") not in self.adapter.current.retry_entity_ids():
            return
        new = data.get("new_state")
        if new is None or new.state == STATE_UNAVAILABLE:
            return
        self._start_write_pending = False
        self.hass.async_create_task(self._async_write_after_start())

    async def _async_write_after_start(self) -> None:
        """Write the requested current now that the session exists; a refusal is not retried."""
        amps = self._requested_current_a
        if amps is None or not self.adapter.capabilities.set_current:
            return
        await self._async_assign_current_outcome(amps, reason=WRITE_SESSION_START)

    async def _async_resend_current(self) -> None:
        """Send the last requested current again, the limit having been cleared by the charger."""
        amps = self._requested_current_a
        if amps is None:
            return
        await self._async_assign_current_outcome(amps, reason=WRITE_RESEND)

    def _watched_control_entities(self) -> list[str]:
        """The charge control and whatever else reports the charging state, each once."""
        entity_ids = [self.charge_control]
        for entity_id in self.adapter.state_entity_ids():
            if entity_id not in entity_ids:
                entity_ids.append(entity_id)
        return entity_ids

    def _async_arm_progress_listener(self) -> None:
        """Watch the facts the vehicle-side observation depends on, for this controller's life.

        Passive: no writes, no service calls. A charger with no unique connector target still watches its
        charge control; missing connector facts are reported as `unknown`.
        """
        if self._progress_listener_cancel is not None:
            return
        entity_ids = self._watched_control_entities()
        target = self.ocpp_target
        if target is not None:
            entity_ids.append(
                connector_entity_id(target.devid, target.connector_id, "status_connector")
            )
            entity_ids.append(
                connector_entity_id(target.devid, target.connector_id, "current_import")
            )
        entity_ids.extend(
            entity_id
            for entity_id in (
                *self.adapter.current_entity_ids,
                *self.adapter.current.retry_entity_ids(),
                *((self.power_entity_id,) if self.power_entity_id else ()),
            )
            if entity_id not in entity_ids
        )
        self._progress_listener_cancel = async_track_state_change_event(
            self.hass, entity_ids, self._async_progress_state_changed
        )
        self._phase_timer_cancel = async_track_time_interval(
            self.hass, self._async_sample_phases, PHASE_SAMPLE_INTERVAL, cancel_on_shutdown=True
        )
        # One evaluation now, so the value is honest before anything reads it. A predicate that
        # already holds begins a new grace period, the only honest thing a restart can do.
        self._charge_progress.evaluate()
        # A charge that already runs is not "seen" later: only a start after this point is.
        self._hold.baseline(self._control_observation)
        self._last_connected = self.adapter.vehicle_connected()
        self._known_connected = self._last_connected

    def charge_phase_currents(self) -> tuple[float | None, ...] | None:
        """The charger's measured current per phase: its own three current entities, else its site's
        measurement of it; `None` when neither tells the phases apart.
        """
        own = self.adapter.measured_phase_currents_a()
        if own is not None:
            return own
        from ..planning.first_run import site_for_charger
        from ..runtime import site_controller_for

        site = site_for_charger(self.hass, self.entry_id)
        controller = None if site is None else site_controller_for(self.hass, site.entry_id)
        measured = None if controller is None else controller.charger_measured_current(self.entry_id)
        if measured is None:
            return None
        return tuple(
            value.value if value.problem is None else None for value in (measured.l1, measured.l2, measured.l3)
        )

    @callback
    def _async_sample_phases(self, _now: datetime | None = None) -> None:
        """One look at which phases carry the charge; a finished charge is taken into what is learned."""
        from ..planning.phases import async_record_charge_phases

        observed = self._phase_observer.sample(self.charging, self.charge_phase_currents())
        if observed is not None:
            self.hass.async_create_task(async_record_charge_phases(self.hass, self.entry_id, observed))

    def _async_disarm_progress_listener(self) -> None:
        """Drop the observation's subscription, if one is held."""
        if self._progress_listener_cancel is not None:
            self._progress_listener_cancel()
            self._progress_listener_cancel = None
        if self._phase_timer_cancel is not None:
            self._phase_timer_cancel()
            self._phase_timer_cancel = None

    def resolve_current(self) -> ResolvedCurrent:
        """The best available answer to "what current will this charger use": the last amps
        SpotNav requested if known, else the current-limit entity's usable live setpoint, else
        unknown. See `ResolvedCurrent` for why `source` must be kept.
        """
        if self._requested_current_a is not None:
            return ResolvedCurrent(self._requested_current_a, "requested")
        setpoint = self._current_limit_entity_value()
        if setpoint is not None:
            return ResolvedCurrent(setpoint, "setpoint")
        return ResolvedCurrent(None, "unknown")

    def validate_plan(self, plan: ChargingPlan) -> ChargingPlan:
        """The shared plan rules for a plan however built. Pure: nothing stored, timed or notified."""
        windows = plan.windows
        if not 1 <= len(windows) <= MAX_SCHEDULE_PERIODS:
            # `periods=[]` on a typed plan means no window list, which `windows` reads as the single
            # `start`/`end`; a count outside the bound is refused whatever the shape.
            raise ValueError(
                f"Schedule must contain between one and {MAX_SCHEDULE_PERIODS} periods"
            )
        previous_end: datetime | None = None
        for start, end in windows:
            if end <= start:
                raise ValueError("Period end must be after its start")
            if previous_end is not None and start < previous_end:
                raise ValueError("Charging periods must be ordered and must not overlap")
            previous_end = end
        if plan.end_time <= dt_util.utcnow():
            raise ValueError("End time must be in the future")
        if plan.end_time > dt_util.utcnow() + timedelta(days=7):
            raise ValueError("End time must be within seven days")
        self._validate_amps(plan.amps)
        return plan

    async def async_install(self, plan: ChargingPlan) -> None:
        """Store and activate an already validated plan exactly once, or change nothing.

        Order matters: check the live current bounds, persist, then adopt in memory and arm.

        * a validation failure changes nothing;
        * a storage failure restores the previous in-memory plan and target record, so no timer, listener
          or service call happens;
        * a failure while arming rolls back to the previous plan, in storage and memory, because timers that
          do not match the plan would charge at hours nobody asked for; a rollback that itself fails has its
          own stable code.

        Returns `None` on success, else a `ChargingExecutionError`. Nothing is notified on failure. Held
        under the operation lock.
        """
        async with self._lock:
            await self._install_locked(plan)

    async def _install_locked(self, plan: ChargingPlan) -> None:
        """The installation itself. Runs with the operation lock held."""
        # Re-run on purpose: this method is also reachable directly, and the cost is one entity read.
        self._validate_amps(plan.amps)
        previous_plan = self.plan
        previous_target_stop = self._target_stop
        self.plan = plan
        # The plan this record described is being replaced.
        self._target_stop = None
        if not self.plan_window_active_now:
            # A new plan with no window open now ends the wish a balancing pause interrupted.
            self._paused_by_balancing = False
        try:
            await self._async_save()
        except Exception as err:
            self.plan = previous_plan
            self._target_stop = previous_target_stop
            raise ChargingExecutionError(
                EXECUTION_STORAGE_FAILED, "the charging plan could not be stored"
            ) from err
        try:
            await self._reschedule_locked()
            # A schedule that arrives already satisfied ends at once rather than booking unusable
            # quarter-hours.
            await self._enforce_target_locked()
        except Exception as err:
            await self._rollback_install(previous_plan, previous_target_stop, err)
            if isinstance(err, ChargingExecutionError):
                raise
            raise ChargingExecutionError(
                EXECUTION_RESCHEDULE_FAILED, "the charging plan could not be armed"
            ) from err
        self._notify()

    async def _rollback_install(
        self,
        plan: ChargingPlan | None,
        target_stop: dict[str, Any] | None,
        cause: BaseException,
    ) -> None:
        """Put the previous plan back, in memory and storage. Best effort, and named."""
        self._cancel_timers()
        self.plan = plan
        self._target_stop = target_stop
        try:
            await self._async_save()
            await self._reschedule_locked()
        except Exception as err:  # noqa: BLE001 - reported as a rollback failure, not raised raw
            _LOGGER.error(
                "Rolling back a failed SpotNav installation also failed: %s",
                type(err).__name__,
            )
            raise ChargingExecutionError(
                EXECUTION_ROLLBACK_FAILED,
                "the previous charging plan could not be restored",
            ) from cause

    async def async_cancel(self) -> None:
        """Stop charging and remove the active schedule. One operation, one lock."""
        async with self._lock:
            await self._stop_locked(clear_schedule=True)

    async def async_follow_schedule(self) -> None:
        """Immediately restore the charger state required by the saved plan."""
        async with self._lock:
            await self._follow_locked()

    async def _follow_locked(self) -> None:
        """The follow itself. Runs with the operation lock held."""
        if self.plan is None:
            raise HomeAssistantError("No charging schedule is active")
        # A person asking to follow the plan again ends their own Stop of the window open now.
        self._person_stop_until = None
        await self._reschedule_locked()

    async def async_start(
        self, amps: int | None = None, *, manual: bool = False, cause: str | None = None
    ) -> bool:
        """Start charging: a window opening, a manual button, or a webhook `start` action.
        Takes the operation lock and delegates to `_start_locked`. `False` when the start command
        was not executed (the charge control is unavailable), so no caller may claim a start.
        `cause` is who asked, for the charge session record (`START_CAUSE_*`); a manual start says so.
        """
        async with self._lock:
            return await self._start_locked(amps, manual=manual, cause=cause)

    def consume_start_cause(self) -> str | None:
        """Who started the charge that is now running, once: the last accepted Start's cause if it was
        sent within `START_CAUSE_TTL_S`, else `None` (the charger started by itself or something else
        started it). Read by the session recorder (`sessions/recorder.py`).
        """
        hint = self._start_cause
        self._start_cause = None
        if hint is None or (dt_util.utcnow() - hint[1]).total_seconds() > START_CAUSE_TTL_S:
            return None
        return hint[0]

    def set_start_cap(self, cap: Callable[[], float | None] | None) -> None:
        """Set (or clear) what a start may give the car: the site's allowance in amps while active
        control is on and the measurements are usable, else `None` (no cap)."""
        self._start_cap = cap

    def _start_allowance_a(self) -> float | None:
        cap = self._start_cap
        if cap is None:
            return None
        try:
            return cap()
        except Exception:  # noqa: BLE001 - a failing cap must not block a start; the regulator still follows
            _LOGGER.debug("Start cap failed", exc_info=True)
            return None

    @property
    def paused_by_balancing(self) -> bool:
        """Whether the charger is stopped because load balancing paused it, and nothing has stopped
        or started it since (a person's Stop, a window's end, a target stop all clear it)."""
        return self._paused_by_balancing and not self.charging

    def forget_balancing_pause(self) -> None:
        """The wish to charge is gone (Auto paused by a person, say): balancing's pause is not a charge
        to resume any more."""
        self._paused_by_balancing = False

    async def async_battery_probe_start(self, amps: int, *, capped: bool = False) -> bool:
        """Resume a charge that load balancing paused, at `amps`, as a battery probe.

        Only while `paused_by_balancing`. The charge restarts at `amps` (the car's minimum), but the
        request on record is kept: the plan's current stays what the car is climbing to. Takes the
        operation lock. `False` when nothing was started.
        """
        async with self._lock:
            if not self._paused_by_balancing or self.charging:
                return False
            kept = self._requested_current_a
            origin, plan_charge = self._paused_charge or (None, False)
            # The charge balancing paused goes on as what it was: the plan's, a person's or the sun's.
            executed = await self._start_locked(
                amps, capped=capped, cause=None if origin in (None, "manual") else origin
            )
            if not executed:
                # Still no room: still the same charge waiting.
                self._remember_paused_charge(origin, plan_charge)
                return False
            if origin is not None or plan_charge:
                self._charge_origin, self._plan_charge = origin, plan_charge
                if self._start_cause is not None and origin is not None:
                    self._start_cause = (origin, self._start_cause[1])
                self._paused_charge = None
                await self._async_save_quietly()
            if kept is not None and self._requested_current_a != kept:
                self._requested_current_a = kept
                await self._async_save()
                self._notify()
            return True

    async def _start_locked(
        self,
        amps: int | None = None,
        *,
        manual: bool = False,
        cause: str | None = None,
        capped: bool = True,
    ) -> bool:
        """The start itself, with the operation lock held: record the requested current (if known) and
        start charging.

        * `manual` marks a person deciding to charge. It clears any recorded target stop and is not vetoed
          by a reading at or above the target: with no plan there is nothing to enforce. The veto lives in
          `_async_start_window`, which only the scheduled path uses.
        * `requested_current_a` is set only from an explicit request (the `amps` argument, or the active
          plan's amps). Otherwise it is preserved across a stop and never backfilled from the entity's live
          setpoint; with nothing known it stays `None`, never 0.
        * The request is recorded, not applied, unless this charger opted in to `ChangeConfiguration`
          (`CONF_CURRENT_CONTROL`). By default the current-limit entity is never written, since that would
          send a persistent charging profile. The recorded request is what the dashboard reports and what
          the load balancer computes against.
        """
        self._paused_by_balancing = False
        # Whatever balancing paused before is over: this start is a charge of its own.
        self._paused_charge = None
        if manual:
            self._person_stop_until = None
        if manual and self._target_stop is not None:
            self._target_stop = None
            await self._async_save()
        explicit_amps = amps if amps is not None else (self.plan.amps if self.plan is not None else None)
        if explicit_amps is not None:
            self._validate_amps(explicit_amps)
            self._requested_current_a = explicit_amps
            # With active control on, a start never gives the car more than the site allows now:
            # `min(request, allowance)`, and no start at all below the floor (the request stays on
            # record, so the regulator resumes the charge when headroom returns).
            allowance = self._start_allowance_a() if capped else None
            if allowance is not None:
                if allowance < DEFAULT_MIN_CURRENT_A:
                    # The charge that was asked for waits for headroom as what it is (the plan's, a
                    # person's, the sun's); the regulator's resume gives it back that origin.
                    self._remember_paused_charge(
                        "manual" if manual else cause or "other", cause == "plan_window" and not manual
                    )
                    _LOGGER.info(
                        "SpotNav charger %s: not started, the site allows %.1fA (below the %sA floor)",
                        self.entry_id,
                        allowance,
                        DEFAULT_MIN_CURRENT_A,
                    )
                    await self._async_save()
                    self._notify()
                    return False
                explicit_amps = min(explicit_amps, int(allowance))
            if self.current_control == CURRENT_CONTROL_CHANGE_CONFIGURATION:
                await self._async_assign_current(explicit_amps)
            elif self._writes_current_at_start and not self.adapter.policy.ignored_while_paused:
                outcome = await self._async_assign_current_outcome(
                    explicit_amps, reason=WRITE_SESSION_START
                )
                # A session-bound number is not there before the session: write when it appears.
                self._start_write_pending = (
                    outcome == ASSIGN_TARGET_UNAVAILABLE and self.adapter.policy.session_bound
                )
            await self._async_save()
        executed = True
        # From here the charge is SpotNav's, whoever asked (a window, a manual Start, a webhook,
        # solar or hybrid execution): the hold of a charge that starts by itself leaves it alone.
        was_owned = self._hold.owned
        was_plan_charge = self._plan_charge
        self._hold.spotnav_started()
        # A plan window's start is a plan charge; a person's, solar's or a webhook's is not, and
        # stays outside the stop of a charge that strays from a replaced plan.
        self._plan_charge = cause == "plan_window" and not manual
        origin = "manual" if manual else cause or "other"
        origin_changed = origin != self._charge_origin
        was_origin = self._charge_origin
        self._charge_origin = origin
        if not self._control_on:
            # Recorded before the first await: an accepted Start the charger has not answered is not
            # a failure, and the observation must not blame the car (see `charge_progress.py`).
            sent_at = self._start_sent_at = dt_util.utcnow()
            # Recorded before the command: a charger that reports charging before the command returns
            # opens its session record at once, and the record must know who started it.
            was_cause = self._start_cause
            self._start_cause = (("manual" if manual else cause or "other"), sent_at)
            try:
                executed = await self.adapter.async_start(explicit_amps)
            except BaseException:
                # The command failed outright: whatever it was to begin is not ours, nor anybody's.
                self._start_cause = was_cause
                self._start_sent_at = None
                self._start_write_pending = False
                self._hold.owned = was_owned
                self._plan_charge = was_plan_charge
                self._charge_origin = was_origin
                raise
            if not executed:
                # The command never went out: nothing is awaiting an answer, and nothing may say so.
                self._start_cause = was_cause
                self._start_sent_at = None
                self._start_write_pending = False
                self._hold.owned = was_owned
                self._plan_charge = was_plan_charge
                self._charge_origin = was_origin
            elif (
                explicit_amps is not None
                and self._writes_current_at_start
                and self.adapter.policy.ignored_while_paused
            ):
                # A charger that only stores a value while paused takes it once it is running.
                await self._async_assign_current_outcome(explicit_amps, reason=WRITE_SESSION_START)
        if executed and (self._plan_charge != was_plan_charge or origin_changed):
            await self._async_save_quietly()
        self._notify()
        return executed

    async def async_set_requested_current(self, amps: int) -> None:
        """Record a new requested current. Writes nothing to the charger, ever.

        The modulation path for solar: solar decides how much the car should want, never how much it gets
        now. Site capacity's damped, yield-stepped write reads `requested_current_a` on its next pass and
        applies it, capped by the fuse. `async_start` would write at once and undamped, bypassing every
        deadband, dwell and yield-stepping protection. It never toggles the charge control switch.

        Takes the operation lock: a modulation must not land mid plan-replacement or stop.
        """
        async with self._lock:
            await self._set_requested_current_locked(amps)

    async def _set_requested_current_locked(self, amps: int) -> None:
        """The record itself. Runs with the operation lock held."""
        self._validate_amps(amps)
        self._requested_current_a = amps
        await self._async_save()
        self._notify()

    async def _async_assign_current(self, amps: int) -> None:
        """Assign this current to the charger, best-effort, discarding the outcome.

        The site regulator's and `async_start`'s entry point; see `_async_assign_current_outcome`.
        """
        await self._async_assign_current_outcome(amps)

    async def _async_assign_current_outcome(
        self, amps: int, *, verify: bool = False, reason: str = WRITE_SESSION_START
    ) -> str:
        """Ask the charger's adapter to assign this current, or say why not.

        Returns `ASSIGN_ASSIGNED` when the write was sent (with `verify`, read back and found to carry
        `amps`), else a stable code naming what stopped it. This is the one place a charger's assigned
        current is written by regular control. It holds `_assign_lock` for the whole read-modify-write, so
        the regulator's writes, a Start's and the active-control restore never interleave.

        `ChangeConfiguration` on `AssignedCurrent` modulates a running charge with no charging profile and
        no reboot; it runs only for a charger that opted in, and there is no fallback to
        `number.set_value`. Best effort: the request is recorded by the caller either way, and every way
        this cannot proceed logs one warning naming no entity and writes nothing.
        """
        async with self._assign_lock:
            return await self._assign_current_locked(amps, verify=verify, reason=reason)

    async def _assign_current_locked(self, amps: int, *, verify: bool, reason: str) -> str:
        """The write itself. Runs with `_assign_lock` held."""
        if self._probe.in_flight:
            # The pilot-floor probe is stepping this connector down and reading the response.
            _LOGGER.debug(
                "Not assigning %sA: the pilot-floor probe is stepping the current down", amps
            )
            return ASSIGN_PROBE_IN_FLIGHT
        # Everything below is the adapter's: for an OCPP charger the very same read, rewrite and
        # write of `AssignedCurrent` it has always been (`OcppAssignedCurrent`); for any other, a
        # write under the platform's policy.
        return await self.adapter.async_set_current(amps, reason=reason, verify=verify)

    async def async_apply_regulated_current(self, amps: int, *, must_lower: bool) -> RegulatedWrite:
        """The regulator's one write to this charger, through the same `_assign_lock`.

        For an OCPP charger exactly what it has always been: `_async_assign_current`, best effort,
        reported as written. For any other adapter the write goes through the platform's policy and
        can be refused; a refusal is then judged against the fuse:

        * a proposal below the 6 A floor is a pause: the charge is stopped (OCPP keeps its old
          behaviour of sending nothing);
        * a refusal when `must_lower` (an overload, or a reduction in the protection band) and the
          proposal is below what the charger is set to, or what it is set to is unknown: the charge
          is **stopped**, since nothing slower than a stop can be trusted to protect the fuse;
        * any other refusal just holds; the damper's state is kept at what is really on the
          charger and the next pass tries again.

        A stop is never refused by a write policy. It takes the operation lock, so it cannot land
        inside a plan replacement.
        """
        if amps < DEFAULT_MIN_CURRENT_A:
            # Below the floor no valid pilot current exists, for OCPP as for any charger: the only
            # way to give the car less is to stop it (OCPP sends nothing for such a value).
            return await self._regulated_stop("pause")
        outcome = await self._async_assign_current_outcome(amps, reason=WRITE_REGULATOR)
        if outcome in IN_EFFECT_OUTCOMES:
            return RegulatedWrite(REGULATED_WROTE, outcome, True)
        if not must_lower:
            return RegulatedWrite(REGULATED_HELD, outcome, False)
        applied = self.adapter.current.setpoint_a()
        if applied is None:
            applied = self._requested_current_a
        if applied is not None and amps >= applied:
            # Already at or below what the fuse needs: nothing to lower.
            return RegulatedWrite(REGULATED_HELD, outcome, False)
        return await self._regulated_stop("safety_stop", cause=outcome)

    async def _regulated_stop(self, code: str, *, cause: str | None = None) -> RegulatedWrite:
        _LOGGER.warning(
            "SpotNav charger %s: stopping the charge (%s%s); the current cannot be lowered in time",
            self.entry_id,
            code,
            "" if cause is None else f": {cause}",
        )
        # Only a charge that was running can be one balancing interrupted; a pause written to a
        # charger somebody already stopped must not make it look wanted.
        was_on = self._control_on
        paused_charge = (self._charge_origin, self._plan_charge)
        try:
            await self.async_stop()
        except Exception:  # noqa: BLE001 - reported, and the next pass tries again
            _LOGGER.exception("SpotNav charger %s: the safety stop failed", self.entry_id)
            return RegulatedWrite(REGULATED_HELD, "stop_failed", False)
        # Set after the stop (which clears it): this stop is the balancing pause itself.
        if code == "pause" and was_on:
            self._remember_paused_charge(*paused_charge)
        return RegulatedWrite(REGULATED_STOPPED, code, False)

    async def async_restore_current(self, *, lowered_by_balancing: bool) -> CurrentRestore:
        """Put this charger back on its authoritative current, and say truthfully how it went.

        Only called by the off transition of active load balancing. Same operation lock as every public
        entry point; its one write goes through `_async_assign_current_outcome` under `_assign_lock`.

        The authoritative current is `resolve_current()` (what SpotNav last asked for, else the validated
        setpoint). What the charger is really at is read from the charger, never assumed from what the
        regulator remembers (a restart forgets it; the charger does not).

        * the charger already carries it: `not_needed`;
        * it carries something higher and balancing never wrote to it: `not_needed`, not ours to change;
        * otherwise it is written and read back; only a read-back that carries it is `restored`. A refused
          or unconfirmed write is `failed` with a stable code.
        """
        async with self._lock:
            return await self._restore_current_locked(lowered_by_balancing=lowered_by_balancing)

    async def _restore_current_locked(self, *, lowered_by_balancing: bool) -> CurrentRestore:
        """The restore itself. Runs with the operation lock held."""
        target = self.resolve_current().amps
        if not self.adapter.is_ocpp:
            return await self._restore_adapter_current_locked(
                target, lowered_by_balancing=lowered_by_balancing
            )
        if self.ocpp_target is None:
            return CurrentRestore(
                RESTORE_NOT_NEEDED if not lowered_by_balancing else RESTORE_FAILED,
                None if not lowered_by_balancing else ASSIGN_NO_TARGET,
                None,
                target,
            )
        read = await self._async_read_assigned_current()
        actual = None if read is None else assigned_amps_for_connector(read[2], read[1])
        if target is None:
            # Nothing authoritative to return to: a failure the person must see if balancing may have
            # lowered this charger; otherwise nothing to restore.
            return CurrentRestore(
                RESTORE_FAILED if lowered_by_balancing else RESTORE_NOT_NEEDED,
                RESTORE_NO_AUTHORITATIVE_CURRENT if lowered_by_balancing else None,
                actual,
                None,
            )
        if actual is not None and actual == target:
            return CurrentRestore(RESTORE_NOT_NEEDED, None, actual, target)
        if actual is not None and actual > target and not lowered_by_balancing:
            return CurrentRestore(RESTORE_NOT_NEEDED, None, actual, target)
        if actual is None and not lowered_by_balancing:
            # Nothing says balancing wrote here and the charger cannot be read: unproven either way,
            # and writing to a charger we never lowered is not ours to do. Say so, do not claim.
            return CurrentRestore(RESTORE_FAILED, ASSIGN_READ_FAILED, None, target)
        outcome = await self._async_assign_current_outcome(int(target), verify=True)
        if outcome == ASSIGN_ASSIGNED:
            return CurrentRestore(RESTORE_RESTORED, None, actual, target)
        return CurrentRestore(RESTORE_FAILED, outcome, actual, target)

    async def _restore_adapter_current_locked(
        self, target: int | None, *, lowered_by_balancing: bool
    ) -> CurrentRestore:
        """The restore for a charger whose current is not OCPP's: same outcomes, read from the
        adapter's own setpoint. A rate-limited or refused write is `failed` with its code.
        """
        if not self.adapter.capabilities.set_current:
            return CurrentRestore(
                RESTORE_NOT_NEEDED if not lowered_by_balancing else RESTORE_FAILED,
                None if not lowered_by_balancing else ASSIGN_UNSUPPORTED,
                None,
                target,
            )
        actual = self.adapter.current.setpoint_a()
        if target is None:
            return CurrentRestore(
                RESTORE_FAILED if lowered_by_balancing else RESTORE_NOT_NEEDED,
                RESTORE_NO_AUTHORITATIVE_CURRENT if lowered_by_balancing else None,
                actual,
                None,
            )
        if actual is not None and actual == target:
            return CurrentRestore(RESTORE_NOT_NEEDED, None, actual, target)
        if actual is not None and actual > target and not lowered_by_balancing:
            return CurrentRestore(RESTORE_NOT_NEEDED, None, actual, target)
        if actual is None and not lowered_by_balancing:
            return CurrentRestore(RESTORE_FAILED, ASSIGN_READ_FAILED, None, target)
        outcome = await self._async_assign_current_outcome(
            int(target), verify=True, reason=WRITE_RESTORE
        )
        if outcome in IN_EFFECT_OUTCOMES and outcome != ASSIGN_UNCONFIRMED:
            return CurrentRestore(RESTORE_RESTORED, None, actual, target)
        return CurrentRestore(RESTORE_FAILED, outcome, actual, target)

    async def _async_read_assigned_current(self) -> tuple[str, int, str] | None:
        """Read the charger's `AssignedCurrent` slot (OCPP only)."""
        current = self.adapter.current
        if not isinstance(current, OcppAssignedCurrent):
            return None
        return await current.async_read_assigned_current()

    async def _async_write_assigned_current(self, devid: str, value: str) -> bool:
        """Write the slot verbatim, reporting whether it went. Writes nothing else."""
        current = self.adapter.current
        if not isinstance(current, OcppAssignedCurrent):
            return False
        return await current.async_write_assigned_current(devid, value)

    def probe_records(self) -> dict[str, Any]:
        """The probe's persisted record."""
        return dict(self._probe_record)

    async def probe_save(self, record: dict[str, Any]) -> None:
        """Persist the probe's record through this controller's own store."""
        async with self._lock:
            await self._probe_save_locked(record)

    async def _probe_save_locked(self, record: dict[str, Any]) -> None:
        """The probe's own save. Runs with the operation lock held."""
        self._probe_record = dict(record)
        await self._async_save()

    async def probe_read_assigned_current(self) -> tuple[str, int, str] | None:
        return await self._async_read_assigned_current()

    async def probe_write_amps(self, devid: str, connector: int, amps: int) -> str | None:
        """Read, rewrite for this connector and write, returning the value written."""
        read = await self._async_read_assigned_current()
        if read is None:
            return None
        try:
            value = rewrite_assigned_current(read[2], connector, amps)
        except Exception:
            _LOGGER.warning("%s could not rewrite AssignedCurrent for %sA", PROBE_TOKEN, amps)
            return None
        if not await self._async_write_assigned_current(devid, value):
            return None
        return value

    async def probe_write_assigned_current(self, devid: str, value: str) -> bool:
        """Write a slot string back exactly as it was read."""
        return await self._async_write_assigned_current(devid, value)
        _LOGGER.debug("Assigned the charger's current through ChangeConfiguration")

    def _current_limit_entity_value(self) -> int | None:
        """The charger's live current setpoint, fully validated; read only, never written back.

        The single call site for reading this entity, so `setpoint_current_a` and `resolve_current` agree
        on validity. Two entities can answer, in order: the configured current-limit entity, then the OCPP
        session limit of this charger's target. The second is a diagnostic reading, not an actuator, and is
        often `unavailable` outside a transaction (unknown, not zero).
        """
        if self.current_limit:
            value = _validate_current_limit_state(self.hass.states.get(self.current_limit))
            if value is not None:
                return value
        controls = self.ocpp_controls
        if self.current_limit_none or controls is None or controls.session_limit_entity is None:
            return None
        return _validate_current_limit_state(
            self.hass.states.get(controls.session_limit_entity)
        )

    def current_range(self) -> dict[str, Any]:
        """The current this charger can be asked for, `{min_a, max_a, source}`.

        The ceiling is the lowest `max` stated by the configured current-limit number, the OCPP connector's
        session limit and the OCPP station maximum: each is a ceiling, so the smallest binds. `source` names
        the entity that supplied it. If none states one, the everyday 32 A; never above `ABSOLUTE_MAX_AMPS`.
        """
        candidates: list[tuple[str | None, str]] = [
            (self.current_limit or None, CURRENT_RANGE_SOURCE_CURRENT_LIMIT)
        ]
        controls = self.ocpp_controls
        if controls is not None:
            if not self.current_limit_none:
                candidates.append((controls.session_limit_entity, CURRENT_RANGE_SOURCE_SESSION_LIMIT))
            candidates.append((controls.station_maximum_entity, CURRENT_RANGE_SOURCE_STATION_MAXIMUM))
        lowest: tuple[int, str] | None = None
        for entity_id, source in candidates:
            if not entity_id:
                continue
            maximum = _charger_maximum_from_state(self.hass.states.get(entity_id))
            if maximum is not None and (lowest is None or maximum < lowest[0]):
                lowest = (maximum, source)
        if lowest is not None:
            return current_range_dict(*lowest)
        return current_range_dict(CURRENT_RANGE_DEFAULT_MAX_A, CURRENT_RANGE_SOURCE_DEFAULT)

    @property
    def ocpp_controls(self) -> OcppCurrentControls | None:
        """The OCPP roles around this charger's target, or `None` when it has no unique target.

        Discovered from the registry on each read rather than copied into the config entry. Side-effect
        free; it also finds a session limit whose state is `unavailable`.
        """
        if self.ocpp_target is None:
            return None
        return discover_controls(self.hass, self.ocpp_target)

    def ocpp_control_facts(self) -> dict[str, Any]:
        """The OCPP identity and role facts, for diagnostics.

        Facts and role booleans rather than entity id strings: redaction works per key, and an entity id
        string would smuggle the charge point's id past it.
        """
        controls = self.ocpp_controls
        session_entity = controls.session_limit_entity if controls else None
        session_state = self.hass.states.get(session_entity) if session_entity else None
        return {
            "target": None
            if self.ocpp_target is None
            else {
                "charge_point_id": self.ocpp_target.charge_point_id,
                "connector_id": self.ocpp_target.connector_id,
            },
            "target_source": self.ocpp_target_source,
            "station_maximum_present": bool(controls and controls.station_maximum_entity),
            "session_limit_present": bool(session_entity),
            "session_limit_state": None if session_state is None else str(session_state.state),
        }

    async def async_stop(self, *, clear_schedule: bool = False, person: bool = False) -> None:
        """Stop charging, optionally removing the saved schedule. Takes the operation lock and
        delegates to `_stop_locked`. `person` marks a person's Stop: inside an open window it keeps a
        plug-in or a new plan from starting that window again (`async_start_on_plug_in`).
        """
        async with self._lock:
            if person:
                self._person_stop_until = self._open_window_end()
            await self._stop_locked(clear_schedule=clear_schedule)

    async def _stop_locked(self, *, clear_schedule: bool = False) -> None:
        """The stop itself, with the operation lock held: stop charging, optionally removing the saved
        schedule.

        Says nothing about a target stop: `_async_target_stop` writes that record before calling this, so
        the window-end stop and an explicit cancel record nothing. Clearing the plan drops the state-change
        subscription, since a gone plan enforces nothing.
        """
        self._hold.spotnav_stopped()
        self._paused_by_balancing = False
        self._paused_charge = None
        # What cannot be read (a restart before the charger's entities exist) says nothing about
        # whether the charge is still running, so the flag survives until it can be seen.
        plan_charge = self._plan_charge
        origin = self._charge_origin
        if self._stop_needed:
            await self.adapter.async_stop()
            self._plan_charge = False
            self._charge_origin = None
        elif self._control_observation is not None:
            self._plan_charge = False
            self._charge_origin = None
        if (
            (plan_charge and not self._plan_charge) or origin != self._charge_origin
        ) and not clear_schedule:
            await self._async_save_quietly()
        if clear_schedule:
            self._plan_charge = False
            self._charge_origin = None
            self.plan = None
            self._cancel_timers()
            self._async_disarm_target_listener()
            # The probe's subscription follows the plan: with no plan nothing may be watched.
            self._async_disarm_probe_listener()
            await self._async_save()
        self._notify()

    async def async_shutdown(self) -> None:
        """Cancel local callbacks without changing the charger."""
        async with self._lock:
            await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        """The shutdown itself. Runs with the operation lock held."""
        self._cancel_timers()
        # State-change subscriptions are local callbacks too: nothing may be decided or called
        # once this controller is gone.
        self._async_disarm_target_listener()
        self._async_disarm_probe_listener()
        self._async_disarm_charge_state_listener()
        self._async_disarm_progress_listener()
        self.adapter.cancel_pending_reads()
        # Terminal and silent: an unexpired grace period must not produce an advisory about a
        # charger nobody is observing.
        self._charge_progress.shutdown()

    @property
    def charge_origin(self) -> str | None:
        """Who started the running charge (`manual`, `solar`, `plan_window`, `other`), or `None`."""
        return self._charge_origin

    @property
    def charging(self) -> bool:
        """Whether the charger is charging: its status sensor when one is configured and readable,
        else the charge control (the switch is on), exactly as before for a charger without one.
        """
        return self.adapter.charging_state()

    @property
    def _control_on(self) -> bool:
        """Whether a Start is in effect: charging, or the charge control enabled as commanded.

        A charger that reports what it is doing separately from whether it is enabled (its switch
        means not paused, its status says waiting for the car) must not be started again, and
        must still be stopped while enabled. For a plain switch the two are one fact.
        """
        return self.charging or self.adapter.enabled_state() is True

    @property
    def _control_observation(self) -> bool | None:
        """`_control_on` as an observation: `None` while the charge control cannot be read
        (missing, unavailable or unknown) and no status says the charger is charging, so a charge
        already running is never mistaken for one that just started (`window_hold.py`).
        """
        if not self.charging:
            state = self.hass.states.get(self.charge_control)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return None
        return self._control_on

    @property
    def _stop_needed(self) -> bool:
        """Whether a Stop has anything to do: charging, or not known to be disabled. A button pair
        cannot say, so it is always pressed; a plain switch is turned off only while it is on.
        """
        return self.charging or self.adapter.enabled_state() is not False

    @property
    def _writes_current_at_start(self) -> bool:
        """Whether a session start writes the current through a non-OCPP path: the person opted in
        and the adapter can set one. OCPP's own opt-in is handled beside it, unchanged.
        """
        return (
            self.current_control != CURRENT_CONTROL_CHANGE_CONFIGURATION
            and self.adapter.capabilities.set_current
        )

    @property
    def is_commandable(self) -> bool:
        """Whether active control may act on this charger: OCPP's opt-in as ever, or an opted-in
        adapter that can set a current. A flash-stored one counts: the regulator cannot write it, but
        it can stop the charge when the fuse needs a lower current than that setting allows.
        """
        if self.current_control == CURRENT_CONTROL_CHANGE_CONFIGURATION:
            return True
        return self.adapter.capabilities.set_current

    def add_charge_state_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Be told when Home Assistant's state for the charge control changes.

        One subscription exists while at least one listener does, dropped with the last or at shutdown.
        """
        self._charge_state_listeners.add(listener)
        if self._charge_state_cancel is None:
            self._charge_state_cancel = async_track_state_change_event(
                self.hass, self._watched_control_entities(), self._on_charge_state_reported
            )
        cancelled = False

        def remove() -> None:
            nonlocal cancelled
            if cancelled:
                return
            cancelled = True
            self._charge_state_listeners.discard(listener)
            if not self._charge_state_listeners and self._charge_state_cancel is not None:
                cancel = self._charge_state_cancel
                self._charge_state_cancel = None
                cancel()

        return remove

    @callback
    def _on_charge_state_reported(self, _event: Any = None) -> None:
        """The charge control reported something: every listener is told once."""
        for listener in list(self._charge_state_listeners):
            listener()

    def _async_disarm_charge_state_listener(self) -> None:
        """Drop the charge-control subscription, if one is held, at shutdown."""
        cancel = self._charge_state_cancel
        self._charge_state_cancel = None
        self._charge_state_listeners.clear()
        if cancel is not None:
            cancel()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _notify(self) -> None:
        """Tell every reader, deciding the vehicle-side observation once more first.

        Every path that changes this charger's state calls this, so the observation is evaluated once per
        observation point instead of by a polling loop.
        """
        self._tick_charge_clock()
        self._charge_progress.evaluate()
        if (
            self._charge_origin is not None
            and not self.charging
            and self._control_observation is False
            and not self.start_pending
        ):
            # The charge ended by itself: its origin must not label the next one. A Start the charger has
            # not answered yet keeps its origin, or the charge it begins would look like nobody's.
            self._charge_origin = None
        for listener in self._listeners:
            listener()

    def _validate_amps(self, amps: int) -> None:
        if amps < ABSOLUTE_MIN_AMPS or amps > ABSOLUTE_MAX_AMPS:
            raise ValueError("Charging current is outside the supported range")
        if not self.current_limit:
            return
        state = self.hass.states.get(self.current_limit)
        if state is None:
            return
        # A non-numeric min/max attribute is ignored; the absolute bounds still apply.
        minimum = _safe_float(state.attributes.get("min"))
        maximum = _safe_float(state.attributes.get("max"))
        if minimum is not None and amps < minimum:
            raise ValueError(f"Charging current must be at least {minimum:g} A")
        if maximum is not None and amps > maximum:
            raise ValueError(f"Charging current must be at most {maximum:g} A")

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "plan": asdict(self.plan) if self.plan else None,
                "requested_current_a": self._requested_current_a,
                "plan_charge": self._plan_charge,
                "charge_origin": self._charge_origin,
                "plugged_in_at": None if self._plugged_in_at is None else self._plugged_in_at.isoformat(),
                # Written only by the target-stop path. A window ending normally and an explicit cancel
                # record nothing here (`async_stop` does not touch this key).
                "target_stop": self._target_stop,
                # The pilot-floor probe's record; written only through its host interface, absent until
                # the probe has started.
                STORE_KEY: self._probe_record,
            }
        )

    async def _reschedule_locked(self) -> None:
        """Arm the plan's timers, or clear a plan that is over. The lock is held.

        Private because every caller already holds the operation lock (`async_install`,
        `_rollback_install`, `async_initialize`, `_follow_locked`); re-acquiring would deadlock.
        """
        self._cancel_timers()
        if self.plan is None:
            self._async_disarm_target_listener()
            self._async_disarm_probe_listener()
            return
        # The probe's listener follows a plan, not a target: the probe runs during any real
        # charge, and its persisted record stops a second run.
        self._async_arm_probe_listener()
        # Armed before the windows are decided so a plan with a target is watched from the moment
        # it exists, including across a restart (`async_initialize` resumes through this call).
        self._async_arm_target_listener()
        now = dt_util.utcnow()
        windows = self.plan.windows
        if now >= windows[-1][1]:
            # A plan whose windows are all past is history, kept so the execution layer can report it
            # as `complete` (a reload must not claim Auto never ran); nothing is armed for it.
            self._async_disarm_target_listener()
            self._async_disarm_probe_listener()
            # A plan charge of ours still running past the last window is stopped (never a person's).
            if self._plan_charge_strays():
                await self._stop_locked(clear_schedule=False)
            return
        active = any(start <= now < end for start, end in windows)
        if active and self._person_stopped_now():
            # A person stopped the charge in the window open now: a new plan waits for its next window.
            pass
        elif active:
            # A window already open is a window start like any other and goes through the veto, so
            # a restart mid-window cannot switch a charger on for a car already at target.
            if await self._start_window_locked():
                return
        else:
            # Re-arming outside a window stops a charge that runs; a person who starts it again
            # after that is respected, as after the hold (`window_hold.py`). A charge a person or the sun
            # started, or one something else owns (a pause, solar, the hybrid hand-off), is not the plan's
            # to stop.
            spared = (
                self._charge_origin in ("manual", "solar")
                or self._hold_blocked()
                or (self._end_window_guard is not None and self._end_window_guard())
            )
            if not spared:
                was_charging = self.charging
                await self._stop_locked(clear_schedule=False)
                if was_charging:
                    self._hold.held_now()
        for index, (start, end) in enumerate(windows):
            if start > now:
                self._timer_cancels.append(async_track_point_in_utc_time(
                    self.hass, self._async_start_callback, start
                ))
            if end > now:
                # Two methods rather than one taking a flag: binding the flag needs a lambda or
                # partial, which is not marked `@callback`, so Home Assistant runs it in a worker
                # thread where `async_create_task` raises, `async_stop` never runs and the charge
                # control stays on.
                final = index == len(windows) - 1
                self._timer_cancels.append(async_track_point_in_utc_time(
                    self.hass,
                    self._async_final_end_callback if final else self._async_end_callback,
                    end,
                ))
    async def _async_start_window(self) -> bool:
        """Open a window: start its charge, or report that the target ended the plan.

        Takes the operation lock and delegates; a window timer fires into this, and its decision must not
        land beside a plan being replaced.
        """
        async with self._lock:
            return await self._start_window_locked()

    async def _start_window_locked(self) -> bool:
        """The window opening itself, with the operation lock held.

        The veto and the start are one step, so no window starts without the decision. A veto only decides
        not to start: it never moves, adds or extends a window and never touches a current. Returns whether
        the target ended the plan instead of starting anything, so a caller arming timers knows there is no
        plan left to arm.
        """
        # The plug-in session a hold belonged to ends where the next window starts.
        self._hold.end_session()
        if self._target_stopping:
            # A stop is in flight and the plan stays live until its turn_off is awaited: starting now
            # would race it. Report the charge as over so no caller arms timers or starts it.
            return True
        if await self._enforce_target_locked():
            return True
        await self._start_locked(cause="plan_window")
        return False

    async def _async_enforce_target(self) -> bool:
        """Make the target decision for the plan in force, and act on it. Takes the operation lock
        and delegates; readings arrive from a state-change listener that could otherwise land
        mid plan-replacement.
        """
        async with self._lock:
            return await self._enforce_target_locked()

    async def _enforce_target_locked(self) -> bool:
        """The target decision itself, with the operation lock held.

        The one place the decision is taken, so the evaluation points (a schedule arriving, a window
        opening, a reading changing) cannot drift apart. Returns whether it stopped the charge; does nothing
        with no plan or a plan without a target.

        A stop already on its way owns the decision: `async_stop` clears the plan only after its
        `switch.turn_off` is awaited, so an in-flight flag prevents a second reading from stopping the same
        charge again. Check and set are synchronous, so two enforcements cannot interleave.
        """
        if self.plan is None or self.plan.target_soc_percent is None:
            return False
        if self._target_stopping:
            return False
        reading = self.target_reading()
        decision = decide_target_stop(
            target_soc_percent=self.plan.target_soc_percent, reading=reading
        )
        if not decision.stop:
            return False
        # Set before the first await and always cleared: a raising service call must not leave
        # the controller refusing to enforce a later plan.
        self._target_stopping = True
        try:
            await self._target_stop_locked(reading)
        finally:
            self._target_stopping = False
        return True

    async def _target_stop_locked(self, reading: SocReading | None) -> None:
        """Stop for a reached target and record why before ending the plan. The lock is held."""
        self._target_stop = {
            "at": dt_util.utcnow().isoformat(),
            "soc_percent": reading.soc_percent if reading else None,
            "target_soc_percent": self.plan.target_soc_percent if self.plan else None,
            "source": reading.source if reading else None,
            "vehicle_id": reading.vehicle_id if reading else None,
            "reading_age_s": reading.age_s if reading else None,
            # Which evidence ended the charge: a fresh reading, or the delivered-energy estimate
            # (target plus margin) when no fresh reading arrived.
            "estimated": bool(reading.estimated) if reading else False,
            "basis": "estimate" if reading is not None and reading.estimated else "reading",
        }
        _LOGGER.info(
            "SpotNav charger %s: target %s%% reached on %s (%s%%, %s), stopping",
            self.entry_id,
            self.plan.target_soc_percent if self.plan else None,
            "an estimate from delivered energy" if self._target_stop["estimated"] else "a reading",
            reading.soc_percent if reading else None,
            reading.source if reading else None,
        )
        self._record_completion(
            "target",
            target_soc_percent=self._target_stop["target_soc_percent"],
            soc_percent=self._target_stop["soc_percent"],
        )
        await self._stop_locked(clear_schedule=True)

    def target_reading(self) -> SocReading | None:
        """The live reading for the plan's vehicle, or `None` with no plan or no way to read a
        vehicle. Read fresh on every call.
        """
        if self._soc_reader is None or self.plan is None:
            return None
        return self._soc_reader(self.plan.vehicle_id)

    @property
    def target_stop_record(self) -> dict[str, Any] | None:
        """The recorded target stop, or `None`."""
        return self._target_stop


    def _async_arm_target_listener(self) -> None:
        """Watch the plan's vehicle for as long as a target is enforced.

        Remade whenever the plan changes, so there is one subscription. Armed even when the current reading
        is unusable (a sleeping car's entity reports nothing, and watching it is how waking is noticed);
        with no entity to watch nothing is armed and the reason is reported as `no_source`.
        """
        self._async_disarm_target_listener()
        if self.plan is None or self.plan.target_soc_percent is None:
            return
        reading = self.target_reading()
        # The charger's energy register is watched with the reading: between two readings of a
        # cloud-polled car it is the only thing that moves, and it drives the stopping estimate
        # (`soc_estimate`).
        entity_ids = [
            entity_id
            for entity_id in (
                reading.entity_id if reading is not None else None,
                self.energy_register_entity_id if self._soc_reader is not None else None,
            )
            if entity_id
        ]
        if not entity_ids:
            return
        self._state_listener_cancel = async_track_state_change_event(
            self.hass, entity_ids, self._async_target_state_changed
        )

    def _async_disarm_target_listener(self) -> None:
        """Drop the state-change subscription, if one is held."""
        if self._state_listener_cancel is not None:
            self._state_listener_cancel()
            self._state_listener_cancel = None

    def _async_arm_probe_listener(self) -> None:
        """Watch the connector's current-import sensor while a plan is active.

        Only its reports can answer the probe's gate (drawing at or above the floor for a minute), so the
        probe is fed from here with nothing polling. A charger that never opted in to `ChangeConfiguration`,
        or whose current-limit entity is missing or not per-connector, gets no listener.
        """
        if self._probe_listener_cancel is not None:
            return
        if self.current_control != CURRENT_CONTROL_CHANGE_CONFIGURATION:
            return
        target = self.ocpp_target
        if target is None:
            return
        self._probe_listener_cancel = async_track_state_change_event(
            self.hass,
            [connector_entity_id(target.devid, target.connector_id, "current_import")],
            self._async_probe_import_changed,
        )

    def _async_disarm_probe_listener(self) -> None:
        """Drop the probe's subscription, if one is held."""
        if self._probe_listener_cancel is not None:
            self._probe_listener_cancel()
            self._probe_listener_cancel = None

    @callback
    def _async_probe_import_changed(self, event: Event[EventStateChangedData]) -> None:
        """Feed the probe's floor clock, and start it once the gate opens."""
        new_state = event.data.get("new_state")
        self._probe.note_current_import(
            _safe_float(new_state.state) if new_state is not None else None
        )
        if self._probe.refusal() is None:
            # A task, not an await: this is a state-change callback and the probe holds the current
            # down for a hundred seconds.
            self.hass.async_create_task(self._probe.async_maybe_run())

    @callback
    def _async_target_state_changed(self, _event: Event[EventStateChangedData]) -> None:
        """A new reading arrived while a target is being enforced.

        Fires whether or not a window is open (a car charged elsewhere makes a later plan moot). It may only
        stop something: it never starts a charger or changes a current, and does nothing once the plan is
        gone, which makes a target stop terminal.
        """
        if self.plan is None or self.plan.target_soc_percent is None:
            return
        self.hass.async_create_task(self._async_enforce_target())



    def set_end_window_guard(self, guard: Callable[[], bool] | None) -> None:
        """Set (or clear with `None`) the window-end guard (see `_async_end_callback`).

        A setter because the guard's answer (is this charger's solar coordinator `on` or `disarming`)
        depends on a coordinator built after this controller during the same setup.
        """
        self._end_window_guard = guard

    @property
    def plan_window_active_now(self) -> bool:
        """Whether `self.plan` has a window covering this instant: the read-only half of hybrid
        arbitration (while a plan window is active, the solar coordinator issues no start, stop or
        set_current).

        Unlike `charge_expected_now` it ignores `self.charging` (a fact about the plan, not about the switch
        being on for another reason) and answers `False` for a plan whose stored instants cannot be read.
        """
        plan = self.plan
        if plan is None:
            return False
        try:
            windows = plan.windows
        except ValueError:
            return False
        now = dt_util.utcnow()
        return any(start <= now < end for start, end in windows)

    @callback
    def _async_start_callback(self, _now: datetime) -> None:
        self.hass.async_create_task(self._async_start_window())

    @callback
    def _async_end_callback(self, _now: datetime) -> None:
        """A window ended and another follows: stop but keep the schedule, unless
        `end_window_guard` says the sun can carry the charge past this boundary (see
        `set_end_window_guard`), in which case nothing happens and charging continues.
        """
        if self._end_window_guard is not None and self._end_window_guard():
            _LOGGER.debug(
                "%s charger %s: window ended with sun available, handing off to solar control",
                HYBRID_LOG_TOKEN,
                self.entry_id,
            )
            return
        self.hass.async_create_task(self.async_stop(clear_schedule=False))

    @callback
    def _async_final_end_callback(self, _now: datetime) -> None:
        """The last window ended: stop and clear the schedule, unless `end_window_guard` says the
        sun can carry the charge past this boundary; then neither happens and solar mode owns the
        charger (see `plan_window_active_now`, `False` once every window is past).
        """
        if self._end_window_guard is not None and self._end_window_guard():
            _LOGGER.debug(
                "%s charger %s: final window ended with sun available, handing off to solar control",
                HYBRID_LOG_TOKEN,
                self.entry_id,
            )
            return
        if self.plan is not None and self._control_on:
            # A charge still running at the last window's end: the plan is done.
            self._record_completion("plan_done", target_soc_percent=self.plan.target_soc_percent)
        self.hass.async_create_task(self.async_stop(clear_schedule=True))

    def _cancel_timers(self) -> None:
        for cancel in self._timer_cancels:
            cancel()
        self._timer_cancels.clear()
