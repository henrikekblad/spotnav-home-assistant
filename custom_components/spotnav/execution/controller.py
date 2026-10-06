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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any, AsyncContextManager, Final, Literal, Protocol

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback, Event, EventStateChangedData, HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_time_interval,
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_CHARGE_CONTROL,
    CONF_CORE_OWNERSHIP,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT_NONE,
    CONF_CURRENT_LIMIT,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_ENERGY_REGISTER_NONE,
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
from .target_stop import (
    charge_ceiling_percent,
    charges_to_vehicle_limit,
    decide_target_stop,
    SocReading,
)
from . import top_off
from .window_hold import HOLD, OVERRIDE, WindowHold
from ..core import events as core_events
from ..core.session import ChargeSession, OWNER_CHARGER_SELF, OWNER_NONE, SessionError
from . import ownership_shadow as ownership_shadow_module
from .ownership_shadow import (
    CommandOutcome,
    FIELD_OWNER,
    legacy_intent,
    legacy_owner,
    OwnershipShadow,
    ShadowToken,
)


_LOGGER = logging.getLogger(__name__)

#: How often a charge is looked at for the phases it uses.
PHASE_SAMPLE_INTERVAL = timedelta(seconds=30)
STORE_VERSION = 1
#: The store key of the core's own session record (`ChargeSession.to_store`), kept beside today's keys while the core
#: drives (`CONF_CORE_OWNERSHIP`); today's keys are still written too.
SESSION_STORE_KEY = "charge_session"
#: A change of the core's session that no save of today's keys carried is saved by the first decision at least this
#: long after it (or at shutdown), once: never at every report. No timer of its own: a restart's timers are not moved.
#: A change of the owner or the person intent is not debounced: it is saved at once (`_persist_session`).
SESSION_SAVE_DELAY_S = 30.0
#: The store key of which is newer, the core's record or today's keys beside it, as two marks on one counter
#: (`session`, `keys`): written with them while the core drives, so a restart never lets the older one win.
SESSION_ORDER_KEY = "charge_session_order"

#: How long an accepted Start counts as the cause of the charge that follows it (the charge session record).
START_CAUSE_TTL_S = 300.0

# Token for hybrid window-end handoff logs (see `_async_end_callback`).
HYBRID_LOG_TOKEN = "HYBRID"

#: Who owns a charge a plan window's end leaves alone (`_window_end_spared_owner`): a person's Start, a start
#: with no cause (Charge now on a charger without Auto) and the sun's. Never `plan_window`, nor a charge the
#: charger began by itself (no origin), which the window's end stops as it always did.
WINDOW_END_SPARED_ORIGINS: Final = frozenset({"manual", "other", "solar"})

#: What a connection handler is told (`set_connection_handler`): a vehicle was plugged in, or unplugged.
CONNECTION_PLUGGED_IN = "plugged_in"
CONNECTION_UNPLUGGED = "unplugged"

# Stable codes an installation failure is reported with; the message beside each is for the log.
EXECUTION_STORAGE_FAILED = "storage_failed"
EXECUTION_RESCHEDULE_FAILED = "reschedule_failed"
EXECUTION_ROLLBACK_FAILED = "rollback_failed"
#: A stop the charger's control did not execute (it was unavailable): nothing of the charge is forgotten.
EXECUTION_STOP_NOT_EXECUTED = "stop_not_executed"


#: What an automatic decision is about to do, as the execution boundary's gate is asked
#: (`AutomaticGate`): start a charge (a window, a plug-in, the claim of a charge as the plan's), stop one
#: (a window's end, the hold, a stray charge, a top-off, a target), or resume one load balancing paused.
AUTOMATIC_START: Final = "start"
AUTOMATIC_STOP: Final = "stop"
AUTOMATIC_BALANCING_RESUME: Final = "balancing_resume"
#: Load balancing resuming a charge a person started (a Start under any pause): theirs, so it goes on as
#: a manual Start's does, unless a person's Stop holds the charger off.
AUTOMATIC_PERSON_RESUME: Final = "person_resume"

#: How long (seconds) after an automatic stop the charger's control did not execute, while the charger is
#: seen charging, the decision is taken again (besides the next report of the charger's state).
STOP_RETRY_S: Final = 30.0
#: Under a person's Stop, a charge the charger begins by itself is stopped at most once per this many
#: seconds, and at most `PERSON_HOLD_MAX_STOPS` times in any `PERSON_HOLD_WINDOW_S` of the plug-in (every stop
#: sent counts, taken or not): then SpotNav gives up and says so (`ChargingController.ignores_person_stop`)
#: rather than cycle the charger or send a stop per report.
PERSON_HOLD_STOP_GAP_S: Final = 30.0
PERSON_HOLD_MAX_STOPS: Final = 3
PERSON_HOLD_WINDOW_S: Final = 600.0
#: A person's charge load balancing stopped for safety (not its below-floor pause) is resumed by the
#: regulator no sooner than this many seconds after the last such stop, so a fuse that keeps needing the stop
#: never cycles it.
SAFETY_RESUME_GAP_S: Final = 300.0


class AutomaticGate(Protocol):
    """The execution boundary's answer to "may an automatic decision act now" (`AutoExecutor`).

    `automatic_turn` holds the boundary's own lock for as long as the decision acts, so a person's
    Start, Stop or pause cannot land between the answer and the command; `automatic_allowed` is the same
    answer read without it, for a decision already inside the boundary (a restore, an install).
    `holds_charger_off` says a person's Stop pauses Auto: any charge the charger begins then is stopped.
    """

    def automatic_turn(self, kind: str) -> AsyncContextManager[bool]: ...

    def automatic_allowed(self, kind: str) -> bool: ...

    def holds_charger_off(self) -> bool: ...


class ChargingExecutionError(HomeAssistantError, RuntimeError):
    """An installation step that failed, named by a stable code.

    Payload validation raises `ValueError` (a 400). This type covers steps after a plan is accepted
    (persisting, arming, rolling back), so a caller can tell "refused" from "accepted but the charger
    could not be brought to it". A Home Assistant error translated by its code, so a button or a service
    call shows the person a sentence.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message, translation_domain=DOMAIN, translation_key=code)
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

#: How long a charge still running after a stop of ours is taken as that stop on its way, not as a charge
#: the charger began by itself (`ChargingController.self_started_charge`).
STOP_ACK_S: Final = 120.0
#: How long a stop sent is taken as the one on its way for any other off decision (`_stop_settling`): another path
#: deciding the same (a re-arm right after the last window's end, the regulator's next pass) sends nothing more, as
#: a charger that has ended the transaction rejects a second stop. Shorter than the retry of a stop that did not go
#: out (`STOP_RETRY_S`) and the person's hold's gap (`PERSON_HOLD_STOP_GAP_S`), so neither changes.
STOP_SETTLE_S: Final = 15.0

#: Why a top-off ends (`_async_end_top_off`): the car stopped drawing by itself, its deadline came, the
#: car was unplugged, or the charge was turned off by other means.
TOP_OFF_FULL = "full"
TOP_OFF_DEADLINE = "deadline"
TOP_OFF_UNPLUGGED = "unplugged"
TOP_OFF_OFF = "off"


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
    # A manual amount at least the room left in the battery: the car ends the charge when it is full,
    # so neither the delivered energy nor an estimate ends it first.
    to_vehicle_limit: bool = False
    # The departure this plan is for (an ISO instant), when it has one: a top-off past the last window
    # never runs beyond it (`top_off.py`).
    departure: str | None = None
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
#: Why a write was held: a stop of the charge was on its way.
REGULATED_STOPPING = "stop_in_flight"
#: The controller has shut down: nothing more is sent to its charger.
REGULATED_SHUT_DOWN = "shut_down"


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


#: The core's owners as today's two owner fields (`charge_origin`, `plan_charge`): nobody's, and the charger's own
#: charge, are neither.
_CORE_OWNER_ORIGIN: Final = {
    "plan": ("plan_window", True),
    "top_off": ("plan_window", True),
    "person": ("manual", False),
    "solar": ("solar", False),
    "charge_now": ("other", False),
}


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
        vehicle_limit_reader: Callable[[str | None], float | None] | None = None,
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
        # The execution boundary every automatic decision asks first (`set_automatic_gate`); `None`
        # (a controller without one) lets every decision act.
        self._automatic_gate: AutomaticGate | None = None
        # What the site lets a start give the car (`set_start_cap`); `None` means no cap applies.
        self._start_cap: Callable[[], float | None] | None = None
        # Where a capped start says how much it took, so another start on the site in the same tick is given
        # only what is left (`set_start_cap`); `None` when nothing is reserved.
        self._start_reserve: Callable[[float | None], None] | None = None
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
        # Whether this controller saw that plug-in itself (a change from known unplugged), and when it first
        # knew of a car connected without seeing it arrive (the first connection known after a restart, or
        # one that became known later): what `plugged_in_since` and `plugged_in_for_count` answer from.
        self._plug_in_seen = False
        self._first_known_connected_at: datetime | None = None
        # The connection went unknown while a car was known connected (`_observe_connection`).
        self._connection_gap = False
        # Who is told about a plug-in or an unplug (`set_connection_handler`); it answers whether it
        # takes care of starting an open window itself (Auto replans first).
        self._connection_handler: Callable[[str], bool] | None = None
        # Who hears of a plug-in, an unplug, or the first connection known after a restart, with the
        # connection before it (`set_connection_observer`): the execution boundary, whose manual pause ends
        # with the plug-in session.
        self._connection_observer: Callable[[bool | None, bool], None] | None = None
        # When the car last ended a person's charge by itself (`async_note_car_ended`): no window of the plan
        # starts it again in this plug-in (the car is full) unless its need grew.
        # Persisted, so a restart does not start it either; a plug-in or an unplug forgets it.
        self._car_ended_at: datetime | None = None
        # The car's state of charge then, and which car was read (`None` when it could not be read): a later
        # window starts again only once it has dropped by the hysteresis the sun's rules use (`SOC_DROP_PCT`).
        self._car_ended_soc: float | None = None
        self._car_ended_vehicle: str | None = None
        # A stop of a charge the charger began by itself under a person's Stop is on its way
        # (`_observe_person_hold`): one at a time.
        self._person_hold_stop_pending = False
        # When the stops of such charges went out in this plug-in (taken or not), when the last was tried, and
        # whether they were given up (`PERSON_HOLD_MAX_STOPS` in `PERSON_HOLD_WINDOW_S`): at most one per
        # `PERSON_HOLD_STOP_GAP_S`, never a stop per report. A report of the charger off changes nothing; a
        # plug-in, an unplug, the person's Stop again or the end of their Stop starts afresh.
        self._person_hold_stop_times: list[datetime] = []
        self._person_hold_tried_at: datetime | None = None
        self._person_hold_gave_up = False
        self._person_hold_retry_cancel: Callable[[], None] | None = None
        # Counts plug-in sessions that ended (an unplug, a first report of no car): a command's failure branch
        # that would put back what was held for a session compares it, so an unplug while the command was on
        # its way wins (`_session_unchanged`).
        self._session_generation = 0
        # The charge load balancing holds was stopped for safety (`_regulated_stop`, any code but its pause),
        # and when it last was (`SAFETY_RESUME_GAP_S`). Both belong to the plug-in session: an unplug ends them.
        self._held_for_safety = False
        self._safety_stopped_at: datetime | None = None
        # The appointment at which an automatic stop the charger's control did not execute is decided again
        # (`_automatic_stop_locked`), or `None`.
        self._stop_retry_cancel: Callable[[], None] | None = None
        # A person's Stop as an older release stored it (`person_stopped`), read once at the restore for
        # the execution boundary to take over as its manual pause (`take_legacy_person_stop`).
        self._legacy_person_stopped = False
        # What the record last saved of the facts below that change outside a plan's own saves (a balancing
        # pause and the charge it held, the hold's session, who started the charge, the adapter's memory):
        # a change is saved soon (`_save_memory_soon`), so a restart does not forget it.
        self._saved_memory: tuple[Any, ...] | None = None
        # Solar's battery-credit back-off (`site/solar_surplus.py`): until when a charging battery is not
        # counted, and how long the next back-off is. Kept here, and persisted, so neither a restart nor a
        # rebuilt solar controller forgets it.
        self._credit_backoff_until: datetime | None = None
        self._credit_backoff_next_s: float | None = None
        # Solar's wait after a charge the car ended by itself (`car_stopped`, `vehicle_full`): a plain record
        # the solar coordinator writes and reads (`SolarExecutionCoordinator._keep_ended`), persisted for the
        # same reason as the back-off above. `None` when there is nothing to keep.
        self._solar_car_ended: dict[str, Any] | None = None
        # Whether `async_initialize` has restored the saved state: until then what this controller says
        # about a charge (its origin, a person's Stop) is not yet known.
        self._restored = False
        # When this controller last sent a stop, until the charger is seen to stop: a charge still
        # running then is one being stopped, not one the charger began by itself.
        self._stop_sent_at: datetime | None = None
        # When the last stop went out, while it settles (`_stop_settling`): cleared by a start of ours sent after it
        # and by the charger seen charging again after it was seen not to. `_seen_charging` is the last report.
        self._stop_settle_since: datetime | None = None
        self._seen_charging: bool | None = None
        # Whether the last `_stop_locked` sent nothing because a stop of ours still settled.
        self._stop_settled = False
        # The plan whose end was last left to the car (`async_end_plan_need_met`), so that is said once per plan.
        self._left_to_car: ChargingPlan | None = None
        # A stop command on its way to the charger: the regulator writes no current meanwhile, since on
        # some chargers (Easee) a current written while the stop lands lifts it again.
        self._stop_in_flight = False
        # Shut down (`async_shutdown`): a regulator step that still holds this controller (the entry unloaded
        # while the step waited) sends the charger nothing more.
        self._shut_down = False
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
        # A person's "none" (`CONF_ENERGY_REGISTER_NONE`) switches the automatic lookup off.
        self.energy_register_entity_id: str | None = config.get(CONF_ENERGY_REGISTER_ENTITY) or None
        if (
            self.energy_register_entity_id is None
            and self.ocpp_target is not None
            and not config.get(CONF_ENERGY_REGISTER_NONE)
        ):
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
        # The vehicle's own charge limit (`SocReader.vehicle_max_percent`), or `None` when unknown.
        self._vehicle_limit_reader = vehicle_limit_reader
        # The record of the last target stop, persisted with the plan (see `_async_save`).
        # Written only by the target-stop path.
        self._target_stop: dict[str, Any] | None = None
        # True from when a reached target is about to stop the charge until the stop completes.
        self._target_stopping = False
        # The last time a charge ended because its plan was done (`completion_record`). In memory only:
        # a restart is not a completion.
        self._completion: dict[str, Any] | None = None
        # While the car finishes a charge to its own limit past the plan's last window (`top_off.py`):
        # the latest it may run to (persisted, so a restart resumes or ends it), since when the car has
        # drawn nothing (in memory: a restart looks afresh), and the top-off's own timers.
        self._top_off_until: datetime | None = None
        self._top_off_idle_since: datetime | None = None
        self._top_off_cancels: list[Callable[[], None]] = []
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
        # The charge-ownership core in shadow mode (`ownership_shadow.py`): fed beside the code here at every change
        # of who owns the charge or which person intent holds, compared, never acting. What a report or a timer
        # decided (`_shadow_note`), the car-ended facts the last decision read, and the sun's part of the hold guard.
        self._shadow_decisions: list[str] | None = None
        self._shadow_car_facts: tuple[bool, bool] = (False, False)
        self._shadow_unobserved = False
        self._solar_hold_probe: Callable[[], bool] | None = None
        # Step 2, behind `CONF_CORE_OWNERSHIP` (off unless set): the core decides, today's code acts on its verdicts
        # and takes its owner back (`_take_core_owner`). The verdicts a report or a timer acts on, while it runs.
        drives = config.get(CONF_CORE_OWNERSHIP)
        self._core_drives = bool(ownership_shadow_module.CORE_OWNERSHIP_DEFAULT if drives is None else drives)
        self._core_report_verdict: frozenset[tuple[str, str]] | None = None
        # The manual pause the core decided for the plug-in or unplug being told (`core_connection_manual`).
        self._core_connection_manual: Any = ...
        self._core_hold_verdict: frozenset[tuple[str, str]] | None = None
        # The core's session as last saved (`SESSION_STORE_KEY`), and the debounced save of a change (`_persist_session`).
        self._session_saved: ChargeSession | None = None
        # The whole record as last written or read back, which a save of the session alone writes again beside it.
        self._record_saved: dict[str, Any] | None = None
        self._session_dirty_since: datetime | None = None
        # Which is newer, the core's session or today's ownership keys (`SESSION_ORDER_KEY`): one counter, moved when
        # the core decides and when today's keys are seen changed; the marks of each as they were last written.
        self._order = 0
        self._session_as_of = 0
        self._keys_as_of = 0
        self._keys_seen: tuple[Any, ...] | None = None
        self._session_written_as_of = 0
        self._shadow = OwnershipShadow(
            self._shadow_session,
            drives=self._core_drives,
            writer=self._take_core_owner,
            persist=self._persist_session,
        )
        self._shadow.today_fields = lambda: f"origin={self._charge_origin} plan_charge={self._plan_charge}"

    async def async_initialize(self) -> None:
        """Restore a saved schedule and requested-current memory, and resume."""
        async with self._lock:
            await self._initialize_locked()

    async def _initialize_locked(self) -> None:
        """The restore itself, with the operation lock held.

        The target-stop record is restored too: it explains why a finished charge ended, and a restart must
        not erase it.
        """
        try:
            await self._restore_locked()
        finally:
            self._restored = True

    @property
    def restored(self) -> bool:
        """Whether the saved state has been restored (`async_initialize`)."""
        return self._restored

    async def _restore_locked(self) -> None:
        saved = await self._store.async_load()
        self._record_saved = dict(saved) if isinstance(saved, dict) else None
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
            self._legacy_person_stopped = saved.get("person_stopped") is True
            raw_car_ended = saved.get("car_ended_at")
            parsed_car_ended = dt_util.parse_datetime(raw_car_ended) if isinstance(raw_car_ended, str) else None
            self._car_ended_at = (
                parsed_car_ended if parsed_car_ended is not None and parsed_car_ended.tzinfo is not None else None
            )
            ended_soc = saved.get("car_ended_soc")
            self._car_ended_soc = (
                float(ended_soc)
                if self._car_ended_at is not None
                and isinstance(ended_soc, (int, float))
                and not isinstance(ended_soc, bool)
                else None
            )
            ended_vehicle = saved.get("car_ended_vehicle")
            self._car_ended_vehicle = ended_vehicle if isinstance(ended_vehicle, str) else None
            backoff = saved.get("solar_credit_backoff")
            if isinstance(backoff, dict):
                until = backoff.get("until")
                parsed_until = dt_util.parse_datetime(until) if isinstance(until, str) else None
                self._credit_backoff_until = (
                    parsed_until if parsed_until is not None and parsed_until.tzinfo is not None else None
                )
                next_s = backoff.get("next_s")
                self._credit_backoff_next_s = (
                    float(next_s) if isinstance(next_s, (int, float)) and not isinstance(next_s, bool) and next_s > 0
                    else None
                )
            car_ended = saved.get("solar_car_ended")
            # Storage is untrusted: the coordinator decodes each field again; only the shape is checked here.
            self._solar_car_ended = dict(car_ended) if isinstance(car_ended, dict) else None
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
            raw_top_off = saved.get("top_off_until")
            parsed_top_off = dt_util.parse_datetime(raw_top_off) if isinstance(raw_top_off, str) else None
            self._top_off_until = (
                parsed_top_off if parsed_top_off is not None and parsed_top_off.tzinfo is not None else None
            )
            balancing = saved.get("balancing_pause")
            if isinstance(balancing, dict):
                origin = balancing.get("origin")
                self._paused_by_balancing = True
                self._paused_charge = (
                    origin if isinstance(origin, str) else None,
                    balancing.get("plan_charge") is True,
                )
            hold = saved.get("hold")
            if isinstance(hold, dict):
                self._hold.held = hold.get("held") is True
                self._hold.overridden = hold.get("overridden") is True
            self.adapter.restore_memory(saved.get("adapter_memory"))
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
        self._saved_memory = self._memory_signature()
        stored, migrate = self._stored_session(saved)
        keys_newer = self._restore_order(saved) if stored is not None else False
        # The core's session is what was read back: its own record when it drives and one was read, else today's;
        # today's keys where they are newer than the record (written after its last change).
        self._shadow.restart(
            core_events.Restart(
                legacy_person_stop=self._legacy_person_stopped and self._automatic_gate is not None
            ),
            stored,
            keys_newer=keys_newer,
        )
        if stored is not None:
            self._session_saved = stored
        if migrate:
            # Read from today's keys once: the core's own record is written now, beside them.
            await self._async_save_session()
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
    def use_found_energy_register(self, entity_id: str) -> None:
        """A lifetime register found after setup (`energy_register.async_watch_for_register`) is this
        charger's register from now on. Whatever counts with it starts from its first reading: a session
        or a requested-energy count begun without a register is never credited with energy from before.
        """
        if self.energy_register_entity_id == entity_id:
            return
        self.energy_register_entity_id = entity_id
        self.adapter.energy_entity_id = entity_id
        if self.plan is not None and self.plan.target_soc_percent is not None:
            # The stopping estimate moves with the register: watch it beside the reading.
            self._async_arm_target_listener()

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
        if self._control_on or self.top_off_until is not None:
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
        solar), Auto is not paused (a person's Start or Stop pauses it), the car has not ended the charge
        in this window, load balancing has not paused the charge, the target is not being stopped for, and
        the vehicle is not known to be unplugged.
        """
        return (
            self.plan_window_active_now
            and not self._target_stopping
            and not self._paused_by_balancing
            and not self._hold_blocked()
            and self._automatic_permitted(AUTOMATIC_START)
            and not self._car_ended_holds_window()
            and self.adapter.vehicle_connected() is not False
        )

    @property
    def completion_record(self) -> dict[str, Any] | None:
        """The last charge that ended because its plan was done, or `None`: `at` (ISO instant),
        `reason` (`target` reached, requested `energy` delivered, `plan_done`: the plan's last window, or
        the top-off after it, ended while it charged, or `vehicle_full`: the car stopped drawing by itself
        during the top-off) and, for a target, `target_soc_percent` and `soc_percent`.
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
        self._observe_charging_again()
        self._maybe_resend_current(event)
        self._maybe_write_after_start(event)
        self._observe_connection()
        self._top_off_tick()
        report = self._shadow_report_begin()
        try:
            changed = self._observe_hold()
            if self._charge_origin is not None and self._control_observation is False and not self.start_pending:
                # Seen off with no Start of ours on its way: the charge is nobody's any more, so a later one
                # the charger begins by itself does not inherit its owner (`_plan_charge` goes with it).
                self._charge_origin = None
                changed = True
            self._observe_person_hold()
            if self._charge_progress.evaluate() or changed:
                self._notify()
        finally:
            self._shadow_report_end(report)

    def _observe_charging_again(self) -> None:
        """A charger seen charging after it was seen not to: a stop sent before is no longer the one on its way."""
        charging = self.charging
        if charging and self._seen_charging is False:
            self._stop_settle_since = None
        self._seen_charging = charging

    def _stop_settling(self) -> bool:
        """Whether a stop of ours went out less than `STOP_SETTLE_S` ago and nothing has started the charge since
        (a start of ours, the charger seen charging again): another stop now is the same off decision."""
        since = self._stop_settle_since
        if since is None:
            return False
        elapsed = (dt_util.utcnow() - since).total_seconds()
        return 0 <= elapsed < STOP_SETTLE_S

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
            if self._known_connected is True:
                # Nobody watches the connection now (a charger offline, a cloud outage): a car may leave and
                # another arrive unseen. The plug-in stays known for everything else, but not as one watched
                # throughout (`plugged_in_since`), and a car connected after it counts as one found connected
                # (`plugged_in_for_count`), as after a restart.
                self._plug_in_seen = False
                self._first_known_connected_at = None
                self._connection_gap = True
            return
        previous = self._known_connected
        if self._connection_gap:
            self._connection_gap = False
            if connected and previous is True:
                self._first_known_connected_at = dt_util.utcnow()
        if previous == connected:
            return
        token = self._shadow.begin(
            early=core_events.PlugIn(previous=previous) if connected else core_events.Unplug(previous=previous)
        )
        try:
            self._connection_changed(previous, connected)
        finally:
            self._shadow.end(
                token,
                core_events.PlugIn(previous=previous) if connected else core_events.Unplug(previous=previous),
                fields=(FIELD_OWNER,),
                defer_intent=True,
            )

    def _connection_changed(self, previous: bool | None, connected: bool) -> None:
        """`_observe_connection` once the charger stated a connection other than the last one."""
        self._known_connected = connected
        if connected and previous is None and self._first_known_connected_at is None:
            self._first_known_connected_at = dt_util.utcnow()
        elif not connected:
            self._first_known_connected_at = None
        if connected is False or previous is False:
            self._session_generation += 1
            # A safety stop's hold belongs to the plug-in it was made in.
            self._held_for_safety = False
            self._safety_stopped_at = None
        if self._paused_by_balancing and (connected is False or previous is False):
            # The plug-in session a balancing pause held a charge for is over (an unplug, or the first
            # connection after a restart says no car): nothing is left for the regulator to resume, and a
            # new plug-in is not that charge either.
            self._paused_by_balancing = False
            self._paused_charge = None
            self._save_memory_soon()
        self._tell_connection_observer(previous, connected)
        if previous is None:
            return
        # The stops under a person's Stop belong to the plug-in they were sent in.
        self._reset_person_hold()
        event = CONNECTION_PLUGGED_IN if connected else CONNECTION_UNPLUGGED
        # What the car ended belongs to the plug-in it ended in.
        had_car_ended = self._car_ended_at is not None
        self._car_ended_at = None
        self._car_ended_soc = None
        if connected:
            self._plugged_in_at = dt_util.utcnow()
            self._plug_in_seen = True
        if connected or had_car_ended:
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
            self._async_spawn(self._async_plug_in_start(), "the start at plug-in")

    async def _async_save_connection(self) -> None:
        async with self._lock:
            await self._async_save_quietly()

    def set_connection_observer(self, observer: Callable[[bool | None, bool], None] | None) -> None:
        """Set (or clear) who hears every change of the connection the charger reports, with the one
        before it: a plug-in (`False` -> `True`), an unplug, or the first known after a restart (`None`
        before). Synchronous: it schedules what it does."""
        self._connection_observer = observer

    def _tell_connection_observer(self, previous: bool | None, connected: bool) -> None:
        observer = self._connection_observer
        if observer is None:
            return
        if self._core_drives and self._shadow.alone(own=1):
            # Decided when the connection's feed began. Not while another feed is open (a person's Stop whose pause is
            # being written): the connection is decided after it, and the boundary reads the core's session then.
            self._core_connection_manual = self._shadow.session.manual
        try:
            observer(previous, connected)
        except Exception:  # noqa: BLE001 - a failing observer must not stop the observation
            _LOGGER.debug("Connection observer failed", exc_info=True)
        finally:
            self._core_connection_manual = ...

    @property
    def core_connection_manual(self) -> Any:
        """While a plug-in or an unplug is told and the core drives: the manual pause the core decided for it
        (`None`: none); `...` otherwise."""
        return self._core_connection_manual

    def set_connection_handler(self, handler: Callable[[str], bool] | None) -> None:
        """Set (or clear) who hears of a plug-in or an unplug (`CONNECTION_*`). It returns whether it
        starts an open window itself after replanning; otherwise this controller does, at once.
        """
        self._connection_handler = handler

    @property
    def plugged_in_at(self) -> datetime | None:
        """When a vehicle was last seen plugged in, or `None` when no plug-in has been seen."""
        return self._plugged_in_at

    @property
    def plugged_in_since(self) -> datetime | None:
        """Since when this controller has itself seen the car plugged in, without a gap: the plug-in it saw
        (a change from known unplugged) while the car is known connected still; `None` otherwise (a plug-in
        from before a restart, a reload or a stretch where the connection was not known, whose car may have
        been swapped meanwhile, or no car)."""
        if self._plug_in_seen and self._known_connected is True:
            return self._plugged_in_at
        return None

    @property
    def plugged_in_for_count(self) -> datetime | None:
        """The plug-in a car's charges are counted from while it is connected: the one this controller saw,
        else the first moment it knew of a car connected (a car found plugged in after a restart counts as a
        new plug-in, since another may have left meanwhile); `None` with no car known connected."""
        if self._known_connected is not True:
            return None
        return self.plugged_in_since or self._first_known_connected_at

    @property
    def known_connected(self) -> bool | None:
        """The last connection the charger reported in so many words (`None` until it said one since this
        controller started): what a status that now says nothing (a blip, a `Ready`) last said."""
        return self._known_connected

    def _car_ended_holds_window(self) -> bool:
        """Whether the car ended a person's charge by itself in this plug-in (`async_note_car_ended`) and the
        window open now is not to start it again.

        Only a car known full (`_car_ended_known_full`) keeps every later window of the plug-in from starting
        it, unless its state of charge has dropped since by the hysteresis the sun's rules use
        (`SOC_DROP_PCT`): the need grew. A car that only stopped drawing (its own timer, a preconditioning
        pause, a fault, or a state of charge nobody can read) skips just the window already open then; a
        later window charges it as planned."""
        ended_at = self._car_ended_at
        if ended_at is None or self.plan is None:
            return False
        try:
            windows = self.plan.windows
        except ValueError:
            return False
        now = dt_util.utcnow()
        if not any(start <= now < end for start, end in windows):
            return False
        if self._car_ended_known_full():
            grew = self._car_ended_need_grew()
            self._shadow_car_facts = (True, grew)
            return not grew
        self._shadow_car_facts = (False, False)
        return any(start <= now < end and start <= ended_at for start, end in windows)

    def _car_ended_known_full(self) -> bool:
        """Whether the car that ended a person's charge was full for the plan in force: its state of charge
        was read (not estimated) then and is at or above the plan's target, or the car's own limit when the
        plan charges to it."""
        ended_soc = self._car_ended_soc
        plan = self.plan
        if ended_soc is None or plan is None:
            return False
        if (
            self._car_ended_vehicle is not None
            and plan.vehicle_id is not None
            and self._car_ended_vehicle != plan.vehicle_id
        ):
            return False
        if self.charges_to_vehicle_limit():
            return ended_soc >= charge_ceiling_percent(self.vehicle_limit_percent(plan.vehicle_id))
        if plan.target_soc_percent is None:
            return False
        return ended_soc >= plan.target_soc_percent

    def _car_ended_need_grew(self) -> bool:
        """Whether the car's state of charge has dropped by `SOC_DROP_PCT` since it ended a person's charge."""
        from .solar_execution import SOC_DROP_PCT

        ended_soc = self._car_ended_soc
        if ended_soc is None or self._soc_reader is None:
            return False
        try:
            reading = self._soc_reader(self._car_ended_vehicle)
        except Exception:  # noqa: BLE001 - an unreadable car says nothing grew
            return False
        soc = None if reading is None else reading.soc_percent
        return soc is not None and soc <= ended_soc - SOC_DROP_PCT

    async def async_note_car_ended(self, vehicle_id: str | None = None) -> None:
        """The car ended a person's charge by itself (it is full, or stopped drawing): no window of the plan
        starts it again in this plug-in unless its need grew (`_car_ended_holds_window`), and the sun's
        rules wait afresh, as after a charge they ran that the car ended (`_seed_ended`): tried again after the
        first retry from now, at once after a new plug-in. `vehicle_id` is the car whose state of charge is
        read for it."""
        from ..site.solar_surplus import SolarConfig

        async with self._lock:
            now = self._car_ended_at = dt_util.utcnow()
            reading = None
            if self._soc_reader is not None:
                try:
                    reading = self._soc_reader(vehicle_id)
                except Exception:  # noqa: BLE001 - an unreadable car is a state of charge not known
                    reading = None
            # Only a read state of charge says whether the car is full (`_car_ended_known_full`), never an
            # estimate carried forward.
            self._car_ended_soc = (
                None if reading is None or getattr(reading, "estimated", False) else reading.soc_percent
            )
            self._car_ended_vehicle = vehicle_id
            # A record kept from an earlier charge (its retry long past) must not let the sun start the car the
            # watch just found full: the wait starts now.
            retry_s = SolarConfig().ended_retry_s
            self._solar_car_ended = {
                "cause": "car_stopped",
                "retry_at": (now + timedelta(seconds=retry_s)).isoformat(),
                "next_retry_s": retry_s * 2.0,
                "context": {
                    "plugged_in_at": None if self._plugged_in_at is None else self._plugged_in_at.isoformat(),
                    "soc_percent": self._car_ended_soc,
                    "limit_percent": None,
                    "target_percent": None,
                },
            }
            await self._async_save_quietly()

    def take_legacy_person_stop(self) -> bool:
        """A person's Stop an older release stored (`person_stopped`), once: the execution boundary takes it
        over as its manual pause, and it is never stored here again."""
        legacy = self._legacy_person_stopped
        self._legacy_person_stopped = False
        return legacy

    async def async_rearm(self) -> None:
        """Arm the plan in force again, as a restore does: a window open now starts through its veto and the
        execution boundary's gate, and a charge outside every window is decided as any re-arm decides it.
        For a pause that ended while its plan stayed (its stop had failed): a window timer that fired while
        it held started nothing."""
        async with self._lock:
            if self.plan is not None:
                await self._reschedule_locked()

    async def async_save_record(self) -> None:
        """Save this charger's record as it stands (a key an older release stored is dropped by it)."""
        async with self._lock:
            await self._async_save_quietly()

    async def async_drop_plan(self) -> None:
        """Drop Auto's plan without touching the charger: a person's Start paused Auto, and a plan of a paused
        Auto is not kept (its window ends would stop what the person started). Its top-off goes with it."""
        async with self._lock:
            if self.plan is None:
                return
            token = self._shadow.begin()
            try:
                self.plan = None
                self._plan_charge = False
                self._clear_top_off()
                self._cancel_timers()
                self._async_disarm_target_listener()
                self._async_disarm_probe_listener()
                await self._async_save_quietly()
                self._notify()
            finally:
                self._shadow.end(token, core_events.PlanDropped())

    async def async_hand_charge_to_sun(self) -> None:
        """The strategy left the plan for the sun while a charge runs that the sun's rules keep (the execution
        boundary decided it, under its lock, in its strategy change's feed): the plan is cleared with no stop, and
        the running charge is the sun's from now, as if it had started it. Its next evaluation modulates it."""
        async with self._lock:
            self.plan = None
            self._plan_charge = False
            self._charge_origin = "solar"
            self._hold.spotnav_started()
            self._clear_top_off()
            self._cancel_timers()
            self._async_disarm_target_listener()
            self._async_disarm_probe_listener()
            _LOGGER.info("SpotNav charger %s: the strategy changed; the sun takes the running charge over", self.entry_id)
            await self._async_save_quietly()
            self._notify()

    def _open_window_end(self) -> datetime | None:
        """The end of the plan's window open now (a top-off's deadline while one runs), or `None`."""
        plan = self.plan
        if plan is None:
            return None
        if self.top_off_until is not None:
            return self.top_off_until
        try:
            windows = plan.windows
        except ValueError:
            return None
        now = dt_util.utcnow()
        return next((end for start, end in windows if start <= now < end), None)

    async def async_start_on_plug_in(self) -> bool:
        """A vehicle was plugged in: start the installed plan's window that is open now, as its start
        would have. Returns whether a start was sent.

        Nothing starts while something else owns the charger (Auto paused, which a person's Start or Stop
        does, solar), after the car ended a charge in this plug-in, when the charge control already runs, or
        when the plan's target is reached; a start is capped by load balancing as every start is
        (`_start_locked`).
        """
        async with self._lock:
            return await self._plug_in_start_locked()

    async def _async_plug_in_start(self) -> bool:
        """The plug-in start this controller makes itself (no connection handler takes care of it), as an
        automatic decision (`_automatic`)."""
        async with self._automatic(AUTOMATIC_START) as allowed:
            return allowed and await self._plug_in_start_locked()

    async def _plug_in_start_locked(self) -> bool:
        token = self._shadow.begin()
        self._shadow_car_facts = (False, False)
        facts = self._shadow_facts(
            lambda: {
                "window_open": self._open_window_end() is not None,
                "target_stopping": self._target_stopping,
                "top_off": self.top_off_until is not None,
                **self._shadow_start_facts(),
            }
        )
        note: dict[str, Any] = {"legacy": [], "outcome": None, "target": False}
        try:
            verdict = self._core_window_start_verdict(core_events.TRIGGER_PLUG_IN, facts)
            if verdict is not None:
                return await self._core_plug_in_start(note, verdict)
            return await self._plug_in_start_body(note)
        finally:
            self._shadow_end_event(
                token,
                facts,
                lambda: core_events.WindowStart(
                    trigger=core_events.TRIGGER_PLUG_IN,
                    target_reached=note["target"],
                    **facts,
                    **self._shadow_take_car_facts(),
                ),
                legacy=note["legacy"],
                outcome=note["outcome"],
            )

    async def _core_plug_in_start(self, note: dict[str, Any], verdict: frozenset[tuple[str, str]]) -> bool:
        """The start at plug-in as the core decided it (`CONF_CORE_OWNERSHIP`): a claim, a start after the target's
        own check, or nothing."""
        if ("start", "claim") in verdict:
            if await self._claim_locked():
                note["legacy"].append("start")
                note["outcome"] = CommandOutcome(True)
            return False
        if ("start", "plan_window") not in verdict:
            return False
        if await self._enforce_target_locked():
            note["target"] = True
            return False
        _LOGGER.info("SpotNav charger %s: plugged in inside a planned window, starting", self.entry_id)
        note["legacy"].append("start")
        note["outcome"] = CommandOutcome(False)
        executed = await self._start_locked(cause="plan_window")
        note["outcome"] = self._shadow_start_outcome(executed)
        return executed

    def _core_window_start_verdict(
        self, trigger: str, facts: dict[str, Any] | None
    ) -> frozenset[tuple[str, str]] | None:
        """When the core drives: its verdict on a window start, from the facts and the car-ended rule's (R3)."""
        if not self._core_drives or facts is None:
            return None
        try:
            self._car_ended_holds_window()
            known_full, need_grew = self._shadow_car_facts
            event = core_events.WindowStart(
                trigger=trigger, **facts, car_ended_known_full=known_full, need_grew=need_grew
            )
        except Exception:  # noqa: BLE001 - today's decision stands
            return None
        return self._shadow.verdict(event)

    async def _plug_in_start_body(self, note: dict[str, Any]) -> bool:
        if self._open_window_end() is None or self._target_stopping:
            return False
        if not self._automatic_permitted(AUTOMATIC_START):
            return False
        if self.top_off_until is not None:
            # Past the last window nothing starts: a top-off only lets a running charge finish.
            return False
        if self._hold_blocked() or self._car_ended_holds_window():
            return False
        if self._control_on:
            # The charger started by itself at plug-in: the charge is the plan's (its stops apply).
            if await self._claim_window_charge_locked():
                note["legacy"].append("start")
                note["outcome"] = CommandOutcome(True)
            return False
        if self.adapter.vehicle_connected() is False or self.start_pending:
            # A Start (the replanned plan's own window start) is on its way.
            return False
        if self._paused_by_balancing:
            # Load balancing stopped this charge: its regulator resumes it, with its own margin and dwell.
            return False
        if await self._enforce_target_locked():
            note["target"] = True
            return False
        _LOGGER.info("SpotNav charger %s: plugged in inside a planned window, starting", self.entry_id)
        note["legacy"].append("start")
        note["outcome"] = CommandOutcome(False)
        executed = await self._start_locked(cause="plan_window")
        note["outcome"] = self._shadow_start_outcome(executed)
        return executed

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
            if self.charges_to_vehicle_limit():
                # The car ends this charge itself when it is full: no count or estimate of ours ends it. Said
                # once per plan: every calculation while the need reads met asks again.
                if self._left_to_car is not self.plan:
                    self._left_to_car = self.plan
                    _LOGGER.info(
                        "SpotNav charger %s: the plan charges to the car's own limit, leaving its end to the car",
                        self.entry_id,
                    )
                return False
            if self.plan.target_soc_percent is not None:
                # The target's own stop, with its record, or nothing.
                return await self._enforce_target_locked()
            token = self._shadow.begin()
            facts = self._shadow_facts(
                lambda: {
                    "control_on": self._control_on,
                    "handed_off": self._shadow_handed_off(),
                    **self._shadow_hold_facts(),
                }
            )
            legacy: list[str] = []
            outcome: CommandOutcome | None = None
            try:
                handed_off = self._end_window_guard is not None and self._end_window_guard()
                stop = self._control_on and self._plan_charge and not self._hold_blocked() and not handed_off
                verdict = None if facts is None else self._shadow.verdict(core_events.NeedMet(**facts))
                if verdict is not None:
                    stop = self._shadow.choose("need_met", stop, ("stop", "need_met") in verdict)
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
                    legacy.append("stop")
                    outcome = CommandOutcome(False)
                    await self._stop_locked(clear_schedule=True)
                    outcome = self._shadow_stop_outcome(True)
                    return True
                self.plan = None
                self._cancel_timers()
                self._async_disarm_target_listener()
                self._async_disarm_probe_listener()
                await self._async_save()
                self._notify()
                return True
            finally:
                self._shadow_end_event(token, facts, lambda: core_events.NeedMet(**facts), legacy=legacy, outcome=outcome)

    def set_automatic_gate(self, gate: AutomaticGate | None) -> None:
        """Set (or clear) the execution boundary every automatic decision of this controller asks first."""
        self._automatic_gate = gate

    def _automatic_permitted(self, kind: str) -> bool:
        """Whether the boundary lets an automatic decision of this kind act now, read without its lock (the
        caller is already inside it, or nothing else can run: a restore)."""
        gate = self._automatic_gate
        if gate is None:
            return True
        if self._legacy_person_stopped and kind != AUTOMATIC_STOP:
            # A person's Stop an older release stored, not yet the boundary's manual pause
            # (`take_legacy_person_stop`): it holds as that pause will, from the restore's own re-arm on.
            return False
        return gate.automatic_allowed(kind)

    def _held_off_by_person(self) -> bool:
        """Whether a person's Stop pauses Auto (`AutomaticGate.holds_charger_off`, or one an older release
        stored that the boundary has not taken over yet): any charge the charger begins is stopped."""
        gate = self._automatic_gate
        if gate is None:
            return False
        if self._legacy_person_stopped:
            return True
        holds = getattr(gate, "holds_charger_off", None)
        return bool(holds()) if holds is not None else False

    @callback
    def note_person_hold(self) -> None:
        """Look now whether a charge runs that a person's Stop holds off (`_observe_person_hold`)."""
        self._observe_person_hold_timer()

    def _observe_person_hold(self) -> None:
        """While a person's Stop pauses Auto, a charge the charger begins by itself (a free-charging OCPP
        charger, an Easee without authorization, at plug-in or at any time) is stopped at once, whatever the
        plan's windows say. Never a Start on its way: the person's own Start replaces their Stop, decided
        again under the boundary's lock.

        At most one stop per `PERSON_HOLD_STOP_GAP_S` (a charger whose status lags says `on` at every report
        after a stop it took), and at most `PERSON_HOLD_MAX_STOPS` in any `PERSON_HOLD_WINDOW_S`, whether the
        charger took them or not (one that takes each stop and begins again is cycled no more than that): then
        no more, and the status says the charger does not take SpotNav's stop until the person acts or the car
        is unplugged. The one gate for these stops, the stop's own retry included (`_async_retry_stop`)."""
        if not self._held_off_by_person():
            self._reset_person_hold()
            return
        verdict = self._core_hold_verdict
        if verdict is not None:
            self._core_person_hold(verdict)
            return
        if self._person_hold_stop_pending or self._control_observation is not True or self.start_pending:
            return
        if self._person_hold_gave_up:
            return
        tried_at = self._person_hold_tried_at
        if tried_at is not None:
            wait_s = PERSON_HOLD_STOP_GAP_S - (dt_util.utcnow() - tried_at).total_seconds()
            if wait_s > 0:
                # Looked at again when the gap is over, even if the charger reports nothing new by then.
                if self._person_hold_retry_cancel is None:
                    self._person_hold_retry_cancel = async_call_later(
                        self.hass, wait_s, self._person_hold_retry_callback
                    )
                return
        now = dt_util.utcnow()
        recent = [sent for sent in self._person_hold_stop_times if (now - sent).total_seconds() < PERSON_HOLD_WINDOW_S]
        self._person_hold_stop_times = recent
        if len(recent) >= PERSON_HOLD_MAX_STOPS:
            self._shadow_note("notify")
            self._person_hold_gave_up = True
            _LOGGER.warning(
                "SpotNav charger %s: the charger began charging again after %s stops within %s minutes under a "
                "person's Stop; no more are sent until the person acts or the car is unplugged",
                self.entry_id,
                len(recent),
                int(PERSON_HOLD_WINDOW_S / 60),
            )
            self._notify()
            return
        self._shadow_note("stop")
        self._person_hold_stop_pending = True
        self._async_spawn(self._async_person_hold_stop(), "the stop under a person's Stop")

    def _core_person_hold(self, verdict: frozenset[tuple[str, str]]) -> None:
        """C7 as the core decided it (`CONF_CORE_OWNERSHIP`): the give-up, a stop, or a look again after the gap."""
        if ("notify", "charger_ignores_stop") in verdict:
            self._shadow_note("notify")
            self._person_hold_gave_up = True
            _LOGGER.warning(
                "SpotNav charger %s: the charger began charging again after %s stops within %s minutes under a "
                "person's Stop; no more are sent until the person acts or the car is unplugged",
                self.entry_id,
                PERSON_HOLD_MAX_STOPS,
                int(PERSON_HOLD_WINDOW_S / 60),
            )
            self._notify()
            return
        if ("stop", "person_hold") in verdict:
            self._shadow_note("stop")
            self._person_hold_stop_pending = True
            self._async_spawn(self._async_person_hold_stop(), "the stop under a person's Stop")
            return
        tried_at = self._person_hold_tried_at
        if (
            tried_at is None
            or self._person_hold_stop_pending
            or self._person_hold_gave_up
            or self._control_observation is not True
            or self.start_pending
        ):
            return
        wait_s = PERSON_HOLD_STOP_GAP_S - (dt_util.utcnow() - tried_at).total_seconds()
        if wait_s > 0 and self._person_hold_retry_cancel is None:
            # Looked at again when the gap is over, even if the charger reports nothing new by then.
            self._person_hold_retry_cancel = async_call_later(self.hass, wait_s, self._person_hold_retry_callback)

    @callback
    def _person_hold_retry_callback(self, _now: datetime) -> None:
        self._person_hold_retry_cancel = None
        self._observe_person_hold_timer()

    def _cancel_person_hold_retry(self) -> None:
        cancel = self._person_hold_retry_cancel
        self._person_hold_retry_cancel = None
        if cancel is not None:
            cancel()

    def reset_person_hold(self) -> None:
        """A person's Stop again (`AutoExecutor`): they acted, so the stops under it start afresh."""
        self._reset_person_hold()

    def _reset_person_hold(self) -> None:
        """A new plug-in session, the person acting, or no person's Stop any more: the stops start afresh."""
        gave_up = self._person_hold_gave_up
        self._person_hold_stop_times = []
        self._person_hold_tried_at = None
        self._person_hold_gave_up = False
        self._cancel_person_hold_retry()
        if gave_up:
            self._notify()

    @property
    def ignores_person_stop(self) -> bool:
        """Under a person's Stop the charger kept charging, or began again, after `PERSON_HOLD_MAX_STOPS` stops
        within `PERSON_HOLD_WINDOW_S`: SpotNav sends no more, and the status says so."""
        return self._person_hold_gave_up and self._held_off_by_person()

    async def _async_person_hold_stop(self) -> None:
        """The stop under a person's Stop a report or a timer decided, decided again under the boundary's lock
        (`_recheck`): a person's Start may have replaced their Stop meanwhile."""
        try:
            async with self._automatic(AUTOMATIC_STOP) as allowed:
                await self._recheck(
                    core_events.RECHECK_PERSON_HOLD,
                    lambda: allowed and self._held_off_by_person() and not self.start_pending and self._control_on,
                    self._person_hold_stop_locked,
                )
        finally:
            self._person_hold_stop_pending = False

    async def _person_hold_stop_locked(self) -> bool:
        """One stop of a charge a person's Stop holds off, counted (`_observe_person_hold`). Runs with the
        operation lock held."""
        _LOGGER.info(
            "SpotNav charger %s: a charge began while a person's Stop pauses Auto; stopping it", self.entry_id
        )
        tried_at = self._person_hold_tried_at = dt_util.utcnow()
        # Every attempt counts, taken or not: a command that raised may still have stopped the charger, and
        # one that keeps failing must end in the give-up and its notification, not in endless retries.
        self._person_hold_stop_times.append(tried_at)
        stopped = await self._automatic_stop_locked("the stop under a person's Stop")
        if self._stop_settled and tried_at in self._person_hold_stop_times:
            # Nothing went out (a stop of ours still settles): no attempt the charger could have ignored.
            self._person_hold_stop_times.remove(tried_at)
        return stopped

    @asynccontextmanager
    async def _automatic(self, kind: str) -> AsyncIterator[bool]:
        """One automatic decision (a timer's, a report's, the regulator's): the boundary's lock first, then
        this controller's, and whether it may act, decided at the moment it acts. A pause stored meanwhile
        is seen, and nothing a person does can land between the answer and the command.
        """
        gate = self._automatic_gate
        if gate is None:
            async with self._lock:
                yield True
            return
        async with gate.automatic_turn(kind) as allowed:
            async with self._lock:
                if not allowed:
                    _LOGGER.debug("SpotNav charger %s: an automatic %s is held back by a pause", self.entry_id, kind)
                yield allowed

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
        verdict = self._core_report_verdict
        if verdict is not None:
            # The core decides what the report starts (`CONF_CORE_OWNERSHIP`); each task checks its facts again.
            self._shadow.choose(
                "hold", decision == HOLD and self._automatic_permitted(AUTOMATIC_STOP), ("stop", "hold") in verdict
            )
            if ("stop", "hold") in verdict:
                self._shadow_note("stop")
                self._async_spawn(self._async_hold_stop(), "the hold's stop")
            elif ("start", "claim") in verdict:
                self._shadow_note("start")
                self.hass.async_create_task(self._async_claim_window_charge())
            elif self._plan_charge and self._control_observation is False:
                self.hass.async_create_task(self._async_forget_plan_charge())
            elif ("stop", "stray") in verdict:
                self._shadow_note("stop")
                self._async_spawn(self._async_stray_stop(), "the stray charge's stop")
        elif decision == HOLD:
            self._shadow_note_gated("stop", AUTOMATIC_STOP)
            self._async_spawn(self._async_hold_stop(), "the hold's stop")
        elif self._window_charge_unclaimed():
            self._shadow_note_gated("start", AUTOMATIC_START)
            self.hass.async_create_task(self._async_claim_window_charge())
        elif self._plan_charge:
            if self._control_observation is False:
                self.hass.async_create_task(self._async_forget_plan_charge())
            elif self._plan_charge_strays():
                self._shadow_note_gated("stop", AUTOMATIC_STOP)
                self._async_spawn(self._async_stray_stop(), "the stray charge's stop")
        return decision in (HOLD, OVERRIDE) or before != (hold.held, hold.overridden, connected)

    def _window_charge_unclaimed(self) -> bool:
        """Whether a charge runs inside an open window of the plan that nobody started: the charger began
        it by itself (at plug-in, say). It is the plan's, so the plan's stops (its window's end, a met
        need) apply to it; never a person's, solar's, or one something else owns, or after the car ended a
        charge in this plug-in.
        """
        return (
            self._control_observation is True
            and not self._plan_charge
            and self._charge_origin is None
            and self.plan_window_active_now
            and not self._hold_blocked()
            and not self._car_ended_holds_window()
        )

    async def _async_claim_window_charge(self) -> None:
        """The claim of a window charge a report decided, decided again under the boundary's lock (`_recheck`)."""
        async with self._automatic(AUTOMATIC_START) as allowed:
            await self._recheck(
                core_events.RECHECK_CLAIM,
                lambda: allowed and self._window_charge_unclaimed() and self._automatic_permitted(AUTOMATIC_START),
                self._claim_locked,
            )

    async def _claim_window_charge_locked(self) -> bool:
        if not self._window_charge_unclaimed() or not self._automatic_permitted(AUTOMATIC_START):
            return False
        return await self._claim_locked()

    async def _claim_locked(self) -> bool:
        """The claim itself: the charge running is the plan's."""
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
        async with self._automatic(AUTOMATIC_STOP) as allowed:
            await self._recheck(
                core_events.RECHECK_HOLD,
                lambda: allowed
                and self._next_window_start() is not None
                and not self._hold_blocked()
                and not self._hold.owned
                and self._control_on,
                lambda: self._automatic_stop_locked("the hold's stop"),
            )

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
        if self.top_off_until is not None:
            # The car finishes its charge past the last window: the top-off ends it, not this stop.
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
        async with self._automatic(AUTOMATIC_STOP) as allowed:
            await self._recheck(
                core_events.RECHECK_STRAY,
                lambda: allowed and self._plan_charge_strays(),
                lambda: self._automatic_stop_locked("the stray charge's stop"),
            )

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
        # A new guard: its sun's part is told again (`set_solar_hold_probe`), or the shadow reads it whole.
        self._solar_hold_probe = None

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
    def reports_plug_in(self) -> bool:
        """Whether the charger says when a car is plugged in (a status entity), so a plug-in can end things."""
        return self.connection()[1] is not None

    @property
    def solar_credit_backoff(self) -> tuple[datetime | None, float | None]:
        """Solar's battery-credit back-off: until when a charging battery is not counted, and the next
        back-off's length (`None` for the default)."""
        return self._credit_backoff_until, self._credit_backoff_next_s

    async def async_set_solar_credit_backoff(self, until: datetime | None, next_s: float | None) -> None:
        """Record solar's battery-credit back-off, saved when it changes."""
        if not self._restored or (until, next_s) == (self._credit_backoff_until, self._credit_backoff_next_s):
            return
        async with self._lock:
            self._credit_backoff_until, self._credit_backoff_next_s = until, next_s
            await self._async_save_quietly()

    @property
    def solar_car_ended(self) -> dict[str, Any] | None:
        """Solar's wait after a charge the car ended by itself, as the solar coordinator recorded it."""
        return None if self._solar_car_ended is None else dict(self._solar_car_ended)

    async def async_set_solar_car_ended(self, record: dict[str, Any] | None) -> None:
        """Record solar's wait after a charge the car ended (plain JSON values), saved when it changes."""
        if not self._restored or record == self._solar_car_ended:
            return
        async with self._lock:
            self._solar_car_ended = None if record is None else dict(record)
            await self._async_save_quietly()

    def self_started_charge(self) -> bool:
        """Whether a charge runs that the charger began by itself (at plug-in, say): nobody here started
        it, it is no plan window's, no Start is on its way, and it is not a person's (one started again
        after a hold). A person's Stop pauses Auto, and nothing automatic decides a charge then."""
        if not self.charging:
            self._stop_sent_at = None
            return False
        stop_sent_at = self._stop_sent_at
        if stop_sent_at is not None and (dt_util.utcnow() - stop_sent_at).total_seconds() < STOP_ACK_S:
            # A stop of ours the charger has not answered yet.
            return False
        return (
            self._charge_origin is None
            and not self._plan_charge
            and not self.start_pending
            and not self._hold.overridden
        )

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
        """Write the requested current now that the session exists; a refusal is not retried. Under the
        operation lock, so it never lands inside a stop."""
        async with self._lock:
            amps = self._requested_current_a
            if amps is None or not self.adapter.capabilities.set_current:
                return
            await self._async_assign_current_outcome(amps, reason=WRITE_SESSION_START)

    async def _async_resend_current(self) -> None:
        """Send the last requested current again, the limit having been cleared by the charger. Under the
        operation lock, so it never lands inside a stop."""
        async with self._lock:
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
        first = self.adapter.vehicle_connected()
        token = self._shadow.begin(
            early=None
            if first is None
            else core_events.PlugIn(previous=None)
            if first
            else core_events.Unplug(previous=None)
        )
        self._last_connected = first
        self._known_connected = self._last_connected
        if first:
            # Found connected at start: not a plug-in this controller saw.
            self._first_known_connected_at = dt_util.utcnow()
        if self._known_connected is not None:
            # The first connection known since the restart: a car gone meanwhile ended its plug-in.
            try:
                self._tell_connection_observer(None, self._known_connected)
            finally:
                self._shadow.end(
                    token,
                    core_events.PlugIn(previous=None)
                    if self._known_connected
                    else core_events.Unplug(previous=None),
                    fields=(FIELD_OWNER,),
                    defer_intent=True,
                )
        else:
            self._shadow.cancel(token)

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
        token = self._shadow.begin()
        window_open = False
        try:
            # A new plan replaces the one a top-off finished: its windows decide from here.
            self._clear_top_off()
            self.plan = plan
            # The plan this record described is being replaced.
            self._target_stop = None
            window_open = self.plan_window_active_now
            if not window_open:
                # A new plan with no window open now ends the wish a balancing pause interrupted.
                self._paused_by_balancing = False
        finally:
            self._shadow.end(token, core_events.PlanInstalled(window_open=window_open))
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
            if self._shadow.depth:
                await self._stop_locked(clear_schedule=True)
                return
            await self._shadow_direct_stop(self._stop_locked(clear_schedule=True), True)

    async def async_follow_schedule(self) -> None:
        """Immediately restore the charger state required by the saved plan."""
        async with self._lock:
            await self._follow_locked()

    async def _follow_locked(self) -> None:
        """The follow itself. Runs with the operation lock held."""
        if self.plan is None:
            raise HomeAssistantError("No charging schedule is active")
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
            if self._shadow.depth:
                # Inside a feed of the execution boundary's (a person's Start, the sun's): that feed is the event.
                return await self._start_locked(amps, manual=manual, cause=cause)
            token = self._shadow.begin()
            outcome = CommandOutcome(False)
            try:
                executed = await self._start_locked(amps, manual=manual, cause=cause)
                outcome = self._shadow_start_outcome(executed)
                return executed
            finally:
                self._shadow.end(
                    token, core_events.DirectStart(manual=manual, cause=cause), legacy=("start",), outcome=outcome
                )

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

    def set_start_cap(
        self,
        cap: Callable[[], float | None] | None,
        *,
        reserve: Callable[[float | None], None] | None = None,
    ) -> None:
        """Set (or clear) what a start may give the car: the site's allowance in amps while active
        control is on and the measurements are usable, else `None` (no cap). `reserve` is told what a
        capped start took (`None` when it did not go out), so the site gives a second start in the same
        tick only what is left."""
        self._start_cap = cap
        self._start_reserve = reserve if cap is not None else None

    def _reserve_start(self, amps: float | None) -> None:
        reserve = self._start_reserve
        if reserve is None:
            return
        try:
            reserve(amps)
        except Exception:  # noqa: BLE001 - a failing reservation must not block a start; the regulator still follows
            _LOGGER.debug("Start reservation failed", exc_info=True)

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

    @property
    def paused_charge_origin(self) -> str | None:
        """What the charge load balancing holds back was (`manual` for a person's), or `None`."""
        return None if self._paused_charge is None else self._paused_charge[0]

    def forget_balancing_pause(self) -> None:
        """The wish to charge is gone (Auto paused by a person, say): balancing's pause is not a charge
        to resume any more."""
        self._paused_by_balancing = False
        self._save_memory_soon()

    async def async_battery_probe_start(self, amps: int, *, capped: bool = False) -> bool:
        """Resume a charge that load balancing paused, at `amps`, as a battery probe.

        Only while `paused_by_balancing`. The charge restarts at `amps` (the car's minimum), but the
        request on record is kept: the plan's current stays what the car is climbing to. Takes the
        operation lock, after the execution boundary's gate (`_automatic`). `False` when nothing was started.
        """
        async with self._automatic(AUTOMATIC_BALANCING_RESUME):
            token = self._shadow.begin()
            charging = self._shadow_facts(lambda: {"charging": self.charging})
            note: dict[str, Any] = {"legacy": [], "outcome": None}
            try:
                return await self._battery_probe_start_locked(amps, capped, note)
            finally:
                self._shadow_end_event(
                    token,
                    charging,
                    lambda: core_events.BalancingResume(**charging),
                    legacy=note["legacy"],
                    outcome=note["outcome"],
                )

    async def _battery_probe_start_locked(self, amps: int, capped: bool, note: dict[str, Any]) -> bool:
        """`async_battery_probe_start` itself, with the boundary's lock and the operation lock held."""
        origin, plan_charge = self._paused_charge or (None, False)
        verdict = self._shadow.verdict(core_events.BalancingResume(charging=self.charging))
        if verdict is not None:
            # The core decides who may resume and when (the gate, a safety stop's gap).
            if self._shut_down or ("start", "balancing_resume") not in verdict:
                return False
        else:
            # A charge a person started is theirs under any pause (one they chose for a span too); decided
            # here, with the boundary's lock held, by what the paused charge was.
            allowed = self._automatic_permitted(
                AUTOMATIC_PERSON_RESUME if origin == "manual" else AUTOMATIC_BALANCING_RESUME
            )
            if not allowed or self._shut_down or not self._paused_by_balancing or self.charging:
                return False
            safety = self._held_for_safety
            if safety and self._safety_stopped_at is not None:
                since = (dt_util.utcnow() - self._safety_stopped_at).total_seconds()
                if 0 <= since < SAFETY_RESUME_GAP_S:
                    # Stopped for safety less than the gap ago: held until the gap is over.
                    return False
        kept = self._requested_current_a
        session = self._session_generation
        # The charge balancing paused goes on as what it was: the plan's, a person's or the sun's.
        note["legacy"].append("start")
        note["outcome"] = CommandOutcome(False)
        try:
            executed = await self._start_locked(
                amps, capped=capped, cause=None if origin in (None, "manual") else origin
            )
            note["outcome"] = CommandOutcome(executed)
        except BaseException:
            # The command failed outright: the charge is still the one balancing holds back, unless the
            # car was unplugged meanwhile.
            if self._session_generation == session:
                self._remember_paused_charge(origin, plan_charge)
            raise
        if not executed:
            # Still no room: still the same charge waiting, unless the car was unplugged meanwhile.
            if self._session_generation == session:
                self._remember_paused_charge(origin, plan_charge)
            else:
                self._paused_by_balancing = False
                self._paused_charge = None
            return False
        self._held_for_safety = False
        # A charge the charger began by itself stays its own (no origin), not a start with no cause.
        if (self._charge_origin, self._plan_charge) != (origin, plan_charge):
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
        if manual and self._car_ended_at is not None:
            # A person decided to charge: what the car ended before is theirs to overrule.
            self._car_ended_at = None
            self._car_ended_soc = None
            await self._async_save_quietly()
        if manual and self._target_stop is not None:
            self._target_stop = None
            await self._async_save()
        explicit_amps = amps if amps is not None else (self.plan.amps if self.plan is not None else None)
        if explicit_amps is None and manual:
            # A person's Start with no current named and no plan (a paused Auto keeps none): the last current
            # asked for, so the start is still capped by load balancing as every start is.
            explicit_amps = self._requested_current_a
        reserved = False
        if explicit_amps is not None:
            self._validate_amps(explicit_amps)
            self._requested_current_a = explicit_amps
            too_low: float | None = None
            try:
                # Under `_assign_lock` from the allowance to the write: a regulator write lowering this charger
                # that is on its way lands first, and the start never sends a current decided before it.
                async with self._assign_lock:
                    # With active control on, a start never gives the car more than the site allows now:
                    # `min(request, allowance)`, and no start at all below the floor (the request stays on
                    # record, so the regulator resumes the charge when headroom returns).
                    allowance = self._start_allowance_a() if capped else None
                    if allowance is not None and allowance < DEFAULT_MIN_CURRENT_A:
                        too_low = allowance
                    else:
                        if allowance is not None:
                            explicit_amps = min(explicit_amps, int(allowance))
                            # Taken now, before the next await: a start beside this one in the same tick on the
                            # same site reads the allowance less this.
                            self._reserve_start(explicit_amps)
                            reserved = True
                        if self.current_control == CURRENT_CONTROL_CHANGE_CONFIGURATION:
                            await self._assign_current_locked(
                                explicit_amps, verify=False, reason=WRITE_SESSION_START
                            )
                        elif self._writes_current_at_start and not self.adapter.policy.ignored_while_paused:
                            outcome = await self._assign_current_locked(
                                explicit_amps, verify=False, reason=WRITE_SESSION_START
                            )
                            # A session-bound number is not there before the session: write when it appears.
                            self._start_write_pending = (
                                outcome == ASSIGN_TARGET_UNAVAILABLE and self.adapter.policy.session_bound
                            )
                if too_low is None:
                    await self._async_save()
            except BaseException:
                # Nothing went out: the site's margin is not on its way to this charger.
                if reserved:
                    self._reserve_start(None)
                raise
            if too_low is not None:
                # The charge that was asked for waits for headroom as what it is (the plan's, a person's, the
                # sun's); the regulator's resume gives it back that origin.
                self._remember_paused_charge(
                    "manual" if manual else cause or "other", cause == "plan_window" and not manual
                )
                _LOGGER.info(
                    "SpotNav charger %s: not started, the site allows %.1fA (below the %sA floor)",
                    self.entry_id,
                    too_low,
                    DEFAULT_MIN_CURRENT_A,
                )
                await self._async_save()
                self._notify()
                return False
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
                if executed:
                    # A start went out after any stop before it: a stop from now on is a new decision.
                    self._stop_settle_since = None
            except BaseException:
                # The command failed outright: whatever it was to begin is not ours, nor anybody's.
                if reserved:
                    self._reserve_start(None)
                self._start_cause = was_cause
                self._start_sent_at = None
                self._start_write_pending = False
                self._hold.owned = was_owned
                self._plan_charge = was_plan_charge
                self._charge_origin = was_origin
                raise
            if not executed:
                # The command never went out: nothing is awaiting an answer, and nothing may say so.
                if reserved:
                    self._reserve_start(None)
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
        if reason == WRITE_REGULATOR and self._stop_in_flight:
            # A stop holds `_assign_lock` while its command is on its way: the regulator's write never waits
            # for it, it is simply not sent (`_assign_current_locked` checks again once the lock is held).
            return REGULATED_STOPPING
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
        if reason == WRITE_REGULATOR and self._shut_down:
            return REGULATED_SHUT_DOWN
        if reason == WRITE_REGULATOR and self._stop_in_flight:
            # The regulator writes without the operation lock: a stop on its way (or one that began while this
            # write waited for `_assign_lock`) is not lifted by a current sent under it.
            return REGULATED_STOPPING
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
        inside a plan replacement; the write itself does not (see below).
        """
        if self._shut_down:
            return RegulatedWrite(REGULATED_HELD, REGULATED_SHUT_DOWN, False)
        if amps < DEFAULT_MIN_CURRENT_A:
            # Below the floor no valid pilot current exists, for OCPP as for any charger: the only
            # way to give the car less is to stop it (OCPP sends nothing for such a value).
            return await self._regulated_stop("pause")
        # Never under the operation lock: a must-lower write waits for no Start or Stop on its way (a slow cloud
        # command). What it must not do is lift a pause, so the state is read again, with no wait in between,
        # right before the send (`_assign_current_locked`; Easee also between its two sends).
        outcome = await self._async_assign_current_outcome(amps, reason=WRITE_REGULATOR)
        if outcome == REGULATED_STOPPING:
            # A stop is on its way: it gives the car less than any current would, and a current written
            # while it lands would lift it on a charger that pauses by its limit.
            return RegulatedWrite(REGULATED_HELD, REGULATED_STOPPING, False)
        if outcome == REGULATED_SHUT_DOWN:
            return RegulatedWrite(REGULATED_HELD, REGULATED_SHUT_DOWN, False)
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
        if self._shut_down:
            return RegulatedWrite(REGULATED_HELD, REGULATED_SHUT_DOWN, False)
        _LOGGER.warning(
            "SpotNav charger %s: stopping the charge (%s%s); the current cannot be lowered in time",
            self.entry_id,
            code,
            "" if cause is None else f": {cause}",
        )
        try:
            async with self._lock:
                if self._shut_down:
                    # Shut down while this stop waited for the lock: nothing more goes to the charger.
                    return RegulatedWrite(REGULATED_HELD, REGULATED_SHUT_DOWN, False)
                # Only a charge that was running can be one balancing interrupted; a pause written to a
                # charger somebody already stopped must not make it look wanted. Read under the operation
                # lock: a person's Stop that lands first is never remembered as a charge to resume.
                was_on = self._control_on
                paused_charge = (self._charge_origin, self._plan_charge)
                # A charge balancing already holds back (paused by an earlier pass, the charger not seen off yet or
                # already off): a repeat pause is the same pause, and that charge stays the one to resume.
                held = (self._paused_by_balancing, self._paused_charge)
                # A charge nobody owns while a stop of ours is unanswered is that stopped charge, its owner already
                # cleared (the window's end, a new plan): not one the charger began by itself, nor still wanted.
                stop_sent_at = self._stop_sent_at
                owner_cleared = (
                    paused_charge == (None, False)
                    and stop_sent_at is not None
                    and (dt_util.utcnow() - stop_sent_at).total_seconds() < STOP_ACK_S
                )
                session = self._session_generation
                token = self._shadow.begin()
                outcome = CommandOutcome(False)
                try:
                    # A balancing stop: a top-off running past the last window goes on, paused like any charge.
                    # A safety stop is never taken for a pause on its way (`_stop_settling`): the fuse needs it now.
                    await self._stop_request_locked(balancing=True, urgent=code != "pause")
                    outcome = self._shadow_stop_outcome(True)
                    # Set after the stop (which clears it): this stop is the balancing pause itself. A safety stop
                    # of a person's charge is remembered the same way, so the regulator gives it back when there
                    # is room. Never for a plug-in that ended while the stop was on its way, nor for a charge whose
                    # owner was cleared (`owner_cleared`): a repeat pause after the window's end or a new plan.
                    if held[0] and self._session_generation == session:
                        self._paused_by_balancing, self._paused_charge = held
                    elif (
                        was_on
                        and (code == "pause" or paused_charge[0] == "manual")
                        and not owner_cleared
                        and self._session_generation == session
                    ):
                        self._held_for_safety = code != "pause"
                        if self._held_for_safety:
                            self._safety_stopped_at = dt_util.utcnow()
                        self._remember_paused_charge(*paused_charge)
                        self._save_memory_soon()
                finally:
                    self._shadow.end(
                        token, core_events.BalancingPause(code=code, was_on=was_on), legacy=("stop",), outcome=outcome
                    )
        except Exception:  # noqa: BLE001 - reported, and the next pass tries again
            _LOGGER.exception("SpotNav charger %s: the safety stop failed", self.entry_id)
            return RegulatedWrite(REGULATED_HELD, "stop_failed", False)
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

    async def async_stop(
        self, *, clear_schedule: bool = False, balancing: bool = False, urgent: bool = True
    ) -> None:
        """Stop charging, optionally removing the saved schedule. Takes the operation lock and
        delegates to `_stop_locked`. A person's Stop is the execution boundary's: it pauses Auto for the
        plug-in session and clears the plan (`AutoExecutor`).

        Any stop but load balancing's pause (`balancing`) ends a top-off, and with it the plan whose
        windows are all past: a person's Stop, a pause, solar standing it down. A stop through here goes out
        even while another settles (`urgent`, a person's): an automatic decision passes `urgent=False`.
        """
        async with self._lock:
            request = self._stop_request_locked(clear_schedule=clear_schedule, balancing=balancing, urgent=urgent)
            if self._shadow.depth:
                await request
                return
            await self._shadow_direct_stop(request, clear_schedule)

    async def _stop_request_locked(
        self, *, clear_schedule: bool = False, balancing: bool = False, urgent: bool = False
    ) -> None:
        """`async_stop` itself, with the operation lock held."""
        if self._top_off_until is not None and not balancing:
            # The plan ends with its top-off.
            _LOGGER.info("SpotNav charger %s: the top-off is stopped", self.entry_id)
            clear_schedule = True
        await self._stop_locked(clear_schedule=clear_schedule, urgent=urgent)

    async def _stop_locked(self, *, clear_schedule: bool = False, urgent: bool = False) -> None:
        """The stop itself, with the operation lock held: stop charging, optionally removing the saved
        schedule.

        Says nothing about a target stop: `_async_target_stop` writes that record before calling this, so
        the window-end stop and an explicit cancel record nothing. Clearing the plan drops the state-change
        subscription, since a gone plan enforces nothing. A stop of an automatic decision while another
        settles (`_stop_settling`) sends nothing (`_stop_settled` says so); an `urgent` one (a safety stop, a
        person's Stop) always goes out.
        """
        was_owned = self._hold.owned
        was_balancing = (self._paused_by_balancing, self._paused_charge)
        session = self._session_generation
        self._hold.spotnav_stopped()
        self._paused_by_balancing = False
        self._paused_charge = None
        # What cannot be read (a restart before the charger's entities exist) says nothing about
        # whether the charge is still running, so the flag survives until it can be seen.
        plan_charge = self._plan_charge
        origin = self._charge_origin
        # For the ownership shadow: a stop that sends nothing because the control says nothing keeps the owner.
        self._shadow_unobserved = not self._stop_needed and self._control_observation is None
        self._stop_settled = False
        if self._stop_needed and not urgent and self._stop_settling():
            # A stop of ours is on its way and the charger has not answered it: this off decision is that stop's.
            # Sent again it meets no transaction, and the charger rejects it.
            _LOGGER.debug(
                "SpotNav charger %s: a stop is already on its way; not sent again", self.entry_id
            )
            self._stop_settled = True
            self._cancel_stop_retry()
            self._plan_charge = False
            self._charge_origin = None
        elif self._stop_needed:
            was_sent_at = self._stop_sent_at
            self._stop_sent_at = dt_util.utcnow()
            self._stop_in_flight = True
            failure: Exception | None = None
            try:
                # Under `_assign_lock`: a current write already on its way lands before the stop goes out, so it
                # cannot lift the stop after it (Easee pauses by its limit). The stop waits for that one write
                # at most: every regulator write behind it sees `_stop_in_flight` and sends nothing.
                async with self._assign_lock:
                    executed = await self.adapter.async_stop()
            except Exception as err:  # noqa: BLE001 - a command that failed outright did not execute either
                failure = err
                executed = False
            finally:
                self._stop_in_flight = False
            if executed is False:
                # The command never went out (the control is unavailable): nothing of the charge is
                # forgotten, neither who owns it nor the plan, so the retries that apply to a running
                # charge (a pause's retry, the stray-charge stop) still find it.
                self._stop_sent_at = was_sent_at
                self._hold.owned = was_owned
                if self._session_generation == session:
                    # Not after an unplug while the command was on its way: that plug-in's pause is over.
                    self._paused_by_balancing, self._paused_charge = was_balancing
                _LOGGER.warning(
                    "SpotNav charger %s: the stop was not executed (the charge control is unavailable)",
                    self.entry_id,
                )
                self._notify()
                raise ChargingExecutionError(
                    EXECUTION_STOP_NOT_EXECUTED, "the stop command was not executed"
                ) from failure
            self._cancel_stop_retry()
            self._stop_settle_since = self._stop_sent_at
            self._seen_charging = self.charging
            self._plan_charge = False
            self._charge_origin = None
        elif self._control_observation is not None:
            self._plan_charge = False
            self._charge_origin = None
        # Whatever start was on its way is over: its share of the site's margin is free for another.
        self._reserve_start(None)
        if (
            (plan_charge and not self._plan_charge) or origin != self._charge_origin
        ) and not clear_schedule:
            await self._async_save_quietly()
        if clear_schedule:
            self._plan_charge = False
            self._charge_origin = None
            self.plan = None
            self._clear_top_off()
            self._cancel_timers()
            self._async_disarm_target_listener()
            # The probe's subscription follows the plan: with no plan nothing may be watched.
            self._async_disarm_probe_listener()
            await self._async_save()
        self._notify()

    async def _automatic_stop_locked(
        self, what: str, *, clear_schedule: bool = False, request: bool = False
    ) -> bool:
        """An automatic decision's stop (a re-arm, a window's end, the hold, a stray charge, a top-off, a
        target, a person's Stop holding the charger off), with the operation lock held. Returns whether it
        went out.

        A stop the charger's control did not execute is never raised to an automatic path: what owns the
        charge and the plan are kept (`_stop_locked`), a warning is logged, and while the charger is seen
        charging the decision is taken again in `STOP_RETRY_S` (and at its next report). A charger whose
        control cannot be read and that is not seen charging needs no stop. A stop a person asked for is
        theirs to hear about (`stop_not_executed`), never through here.
        """
        try:
            if request:
                await self._stop_request_locked(clear_schedule=clear_schedule)
            else:
                await self._stop_locked(clear_schedule=clear_schedule)
        except ChargingExecutionError as err:
            if err.code != EXECUTION_STOP_NOT_EXECUTED:
                raise
            if self.charging or self._control_observation is True:
                _LOGGER.warning(
                    "SpotNav charger %s: %s was not executed; trying again in %ss",
                    self.entry_id,
                    what,
                    int(STOP_RETRY_S),
                )
                self._arm_stop_retry()
            else:
                _LOGGER.info(
                    "SpotNav charger %s: %s was not executed; the charger is not seen charging, so nothing is "
                    "left to stop",
                    self.entry_id,
                    what,
                )
            return False
        return True

    def _arm_stop_retry(self) -> None:
        if self._stop_retry_cancel is None:
            self._stop_retry_cancel = async_call_later(self.hass, STOP_RETRY_S, self._async_stop_retry_callback)

    def _cancel_stop_retry(self) -> None:
        cancel = self._stop_retry_cancel
        self._stop_retry_cancel = None
        if cancel is not None:
            cancel()

    @callback
    def _async_stop_retry_callback(self, _now: datetime) -> None:
        self._stop_retry_cancel = None
        self._async_spawn(self._async_retry_stop(), "the stop's retry")

    async def _async_retry_stop(self) -> None:
        """Take an automatic stop's decision again, now: a person's Stop still holding the charger off, or the
        plan re-armed as a restore arms it (outside every window a charge of the plan's is stopped, a target
        reached is stopped, past the last window a plan charge that strays is stopped)."""
        person_hold = False
        async with self._automatic(AUTOMATIC_STOP) as allowed:
            if not allowed:
                return
            if self._held_off_by_person():
                person_hold = True
            elif self.plan is not None:
                await self._reschedule_locked()
        if person_hold:
            # Through the one gate of the stops under a person's Stop: the gap, one on its way at a time, the
            # give-up (`_observe_person_hold`), never a second stop beside one the gap's own timer sends.
            self._observe_person_hold_timer()

    async def async_shutdown(self) -> None:
        """Cancel local callbacks without changing the charger."""
        # Before the lock: a regulator write or resume waiting behind it sends nothing once it gets it.
        self._shut_down = True
        async with self._lock:
            await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        """The shutdown itself. Runs with the operation lock held."""
        self._cancel_timers()
        # A change of the core's session still waiting for its debounced save, or decided while this shutdown waited
        # for the lock (`_shut_down` is set before it, and no decision saves once it is).
        if self._cancel_session_save() or self._core_drives:
            await self._async_save_session()
        self._cancel_stop_retry()
        self._cancel_person_hold_retry()
        # A top-off's deadline stays stored: the next start resumes it or ends it.
        self._cancel_top_off_timers()
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
    def charge_control_on(self) -> bool:
        """Whether a Start is in effect (`_control_on`): charging, or the charge control enabled."""
        return self._control_on

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
            token = self._shadow.begin()
            self._charge_origin = None
            self._shadow.end(token, core_events.ChargerReportedOff(notified=True))
        self._save_memory_soon()
        # A copy: a listener may end its own watch (a task Home Assistant starts eagerly runs up to its first
        # wait inside this loop).
        for listener in list(self._listeners):
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

    def _memory_signature(self) -> tuple[Any, ...]:
        """The facts `_save_memory_soon` keeps saved, as one comparable value."""
        return (
            self._paused_by_balancing,
            self._paused_charge,
            self._hold.held,
            self._hold.overridden,
            self._charge_origin,
            self._plan_charge,
            tuple(sorted(self.adapter.memory().items())),
        )

    def _save_memory_soon(self) -> None:
        """Save the record soon when one of the facts a restart must not forget changed outside a save of
        its own (a balancing pause, the hold, a charge seen ending by itself, an Easee pause of ours)."""
        if not self._restored or self._memory_signature() == self._saved_memory:
            return
        self.hass.async_create_task(self._async_save_memory())

    async def _async_save_memory(self) -> None:
        async with self._lock:
            if self._memory_signature() != self._saved_memory:
                await self._async_save_quietly()

    async def _async_save(self) -> None:
        signature = self._memory_signature()
        record = self._store_record()
        session = self._session_record()
        if session is not None:
            record[SESSION_STORE_KEY] = session[1]
            self._note_keys()
            record[SESSION_ORDER_KEY] = {"session": self._session_as_of, "keys": self._keys_as_of}
        elif self._core_drives and self._session_saved is not None:
            # Unreadable now: the record saved last is kept, not dropped.
            record[SESSION_STORE_KEY] = self._session_saved.to_store()
            self._note_keys()
            record[SESSION_ORDER_KEY] = {"session": self._session_written_as_of, "keys": self._keys_as_of}
        self._record_saved = record
        await self._store.async_save(record)
        self._saved_memory = signature
        if session is not None:
            self._session_saved = session[0]
            self._session_written_as_of = record[SESSION_ORDER_KEY]["session"]
            self._cancel_session_save()

    def _store_record(self) -> dict[str, Any]:
        """Today's keys, as saved."""
        return {
            "plan": asdict(self.plan) if self.plan else None,
            "requested_current_a": self._requested_current_a,
            "plan_charge": self._plan_charge,
            "charge_origin": self._charge_origin,
            "plugged_in_at": None if self._plugged_in_at is None else self._plugged_in_at.isoformat(),
            # Written only by the target-stop path. A window ending normally and an explicit cancel
            # record nothing here (`async_stop` does not touch this key).
            "target_stop": self._target_stop,
            # The deadline of a top-off that runs past the last window, else `None`.
            "top_off_until": None if self._top_off_until is None else self._top_off_until.isoformat(),
            # When the car last ended a person's charge by itself, in this plug-in.
            "car_ended_at": None if self._car_ended_at is None else self._car_ended_at.isoformat(),
            "car_ended_soc": self._car_ended_soc,
            "car_ended_vehicle": self._car_ended_vehicle,
            # A charge load balancing paused, and what it was, so its regulator resumes it after a restart.
            "balancing_pause": None
            if not self._paused_by_balancing
            else {
                "origin": None if self._paused_charge is None else self._paused_charge[0],
                "plan_charge": bool(self._paused_charge is not None and self._paused_charge[1]),
            },
            # The hold's plug-in session: held, and a person's override of it.
            "hold": {"held": self._hold.held, "overridden": self._hold.overridden},
            # What the start/stop path remembers (an Easee pause of ours).
            "adapter_memory": self.adapter.memory(),
            "solar_credit_backoff": {
                "until": None if self._credit_backoff_until is None else self._credit_backoff_until.isoformat(),
                "next_s": self._credit_backoff_next_s,
            },
            "solar_car_ended": self._solar_car_ended,
            # The pilot-floor probe's record; written only through its host interface, absent until
            # the probe has started.
            STORE_KEY: self._probe_record,
        }

    # ------------------------------------------------------------------ the core's stored session

    def _session_record(self) -> tuple[ChargeSession, dict[str, Any]] | None:
        """When the core drives: its session as a restart reads it back, and as stored. `None` when it does not
        drive (nothing of it is written then), or the session could not be read (the last record saved stays)."""
        if not self._core_drives:
            return None
        try:
            stored = self._shadow.session.stored()
            return stored, stored.to_store()
        except Exception:  # noqa: BLE001 - the record is a safeguard, never a reason to fail a save
            _LOGGER.debug("SpotNav charger %s: the charge session could not be stored", self.entry_id, exc_info=True)
            return None

    def _stored_session(self, saved: dict[str, Any] | None) -> tuple[ChargeSession | None, bool]:
        """When the core drives: its session as stored (`None`: none, or none it can read), and whether today's keys
        are to be migrated into its record (one is missing, or of a version it does not know). Off, `(None, False)`:
        today's restore exactly."""
        if not self._core_drives:
            return None, False
        raw = saved.get(SESSION_STORE_KEY) if saved else None
        if raw is None:
            return None, bool(saved)
        try:
            return ChargeSession.from_store(raw), False
        except SessionError as err:
            _LOGGER.info(
                "SpotNav charger %s: the stored charge session is not readable (%s); it is read from the older "
                "keys again",
                self.entry_id,
                err,
            )
            return None, True

    def _persist_session(self, session: ChargeSession) -> None:
        """The core decided (`OwnershipShadow`, only when it drives). A session whose stored record changed is saved
        by the first decision at least `SESSION_SAVE_DELAY_S` after the change (with whatever else changed by then),
        by a save of today's keys, or at shutdown: a report that changes nothing stored writes nothing, and many
        changes in a row are one save. A change of the owner or of the person intent is saved at once (a task, off
        the decision's own path): Home Assistant's stop unloads no entry, so nothing later may be counted on to save
        it."""
        # The session is as of this decision, which lined it up with today's keys as they are now.
        self._note_keys()
        self._order += 1
        self._session_as_of = self._order
        if not self._restored or self._shut_down:
            return
        stored = session.stored()
        if stored == self._session_saved:
            self._session_dirty_since = None
            return
        if self._ownership_changed(stored):
            self._session_dirty_since = None
            self.hass.async_create_task(self._async_save_session())
            return
        now = dt_util.utcnow()
        since = self._session_dirty_since
        if since is None:
            self._session_dirty_since = now
        elif (now - since).total_seconds() >= SESSION_SAVE_DELAY_S:
            self._session_dirty_since = None
            self.hass.async_create_task(self._async_save_session())

    def _ownership_changed(self, stored: ChargeSession) -> bool:
        """Whether `stored` names another owner or another person intent than the record saved last. The charger's own
        charge and nobody's are one at a restart (it is seen again then), so a charger reporting on and off is no
        such change."""
        saved = self._session_saved if self._session_saved is not None else ChargeSession()
        if (stored.manual, stored.span_pause) != (saved.manual, saved.span_pause):
            return True
        unseen = {OWNER_NONE, OWNER_CHARGER_SELF}
        return stored.owner != saved.owner and not {stored.owner, saved.owner} <= unseen

    def _keys_signature(self) -> tuple[Any, ...]:
        """Today's keys the core's session is lined up from (`_shadow_session`), as one comparable value."""
        return (
            self._charge_origin,
            self._plan_charge,
            self._top_off_until,
            self._car_ended_at,
            self._paused_by_balancing,
            self._paused_charge,
            self._hold.held,
            self._hold.overridden,
        )

    def _note_keys(self) -> None:
        """Today's ownership keys changed since last looked at: they are newer than every decision before now."""
        signature = self._keys_signature()
        if signature != self._keys_seen:
            self._keys_seen = signature
            self._order += 1
            self._keys_as_of = self._order

    def _restore_order(self, saved: dict[str, Any] | None) -> bool:
        """The marks of the record and today's keys read back (`SESSION_ORDER_KEY`), and whether today's keys are the
        newer: written after the record's last change. A record with no marks (an earlier build) is the newer, as
        before them."""
        raw = saved.get(SESSION_ORDER_KEY) if saved else None
        marks = (raw.get("session"), raw.get("keys")) if isinstance(raw, dict) else (None, None)
        if not all(isinstance(mark, int) and not isinstance(mark, bool) and mark >= 0 for mark in marks):
            marks = (0, 0)
        self._session_as_of = self._session_written_as_of = marks[0]
        self._keys_as_of = marks[1]
        self._order = max(marks)
        self._keys_seen = self._keys_signature()
        return marks[1] > marks[0]

    async def async_flush_session(self) -> None:
        """Home Assistant stops (`__init__._async_stop`): it unloads no entry, so `async_shutdown` never runs. A change of
        the core's session still waiting for its debounced save is saved now."""
        self._cancel_session_save()
        await self._async_save_session()

    def _cancel_session_save(self) -> bool:
        """Forget a change of the core's session still waiting to be saved; whether one was waiting."""
        waiting = self._session_dirty_since is not None
        self._session_dirty_since = None
        return waiting

    async def _async_save_session(self) -> None:
        """Write the core's session record alone, when it changed: today's keys go as they were last written or read
        back (each of today's own saves writes them, with the record as it is then). Prepared at once and never
        behind the operation lock, so it delays no decision; a save of today's keys after it writes both again."""
        session = self._session_record()
        if session is None or session[0] == self._session_saved:
            return
        base = self._record_saved if self._record_saved is not None else self._store_record()
        # Today's keys go as last written, so as of the mark they were written with (none: the oldest).
        order = base.get(SESSION_ORDER_KEY)
        keys_as_of = order.get("keys") if isinstance(order, dict) else None
        if not isinstance(keys_as_of, int) or isinstance(keys_as_of, bool):
            keys_as_of = 0
        session_as_of = self._session_as_of
        record = {
            **base,
            SESSION_STORE_KEY: session[1],
            SESSION_ORDER_KEY: {"session": session_as_of, "keys": keys_as_of},
        }
        self._record_saved = record
        try:
            await self._store.async_save(record)
        except Exception as err:  # noqa: BLE001 - the record is a safeguard, never a reason to fail
            _LOGGER.warning("Saving the SpotNav charge session failed: %s", type(err).__name__)
            return
        self._session_saved = session[0]
        self._session_written_as_of = session_as_of

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
            token = self._shadow.begin()
            facts = self._shadow_facts(
                lambda: {
                    "control_on": self._control_observation is True,
                    "handed_off": self._shadow_handed_off(),
                    **self._shadow_hold_facts(),
                }
            )
            legacy: list[str] = []
            outcome: CommandOutcome | None = None
            resumes = False
            try:
                if self._resume_top_off(now):
                    # The car is finishing its charge past the last window (a restart, a follow): the
                    # top-off's own timers end it.
                    resumes = True
                    return
                # A plan whose windows are all past is history, kept so the execution layer can report it
                # as `complete` (a reload must not claim Auto never ran); nothing is armed for it.
                self._async_disarm_target_listener()
                self._async_disarm_probe_listener()
                # A plan charge of ours still running past the last window is stopped (never a person's).
                verdict = (
                    None
                    if facts is None
                    else self._shadow.verdict(core_events.Rearm(past_last=True, top_off_resumes=False, **facts))
                )
                strays = self._plan_charge_strays() and self._automatic_permitted(AUTOMATIC_STOP)
                if strays if verdict is None else self._shadow.choose("rearm_past_last", strays, ("stop", "stray") in verdict):
                    legacy.append("stop")
                    outcome = CommandOutcome(False)
                    stopped = await self._automatic_stop_locked(
                        "the stop of a plan charge past the plan's last window"
                    )
                    outcome = self._shadow_stop_outcome(stopped)
                return
            finally:
                self._shadow_end_event(
                    token,
                    facts,
                    lambda: core_events.Rearm(past_last=True, top_off_resumes=resumes, **facts),
                    legacy=legacy,
                    outcome=outcome,
                )
        active = any(start <= now < end for start, end in windows)
        if active and self._car_ended_holds_window():
            # The car ended a person's charge in this plug-in: the window open then is not started again,
            # nor any later one while the car is known full and its need has not grown.
            pass
        elif active:
            # A window already open is a window start like any other and goes through the veto, so
            # a restart mid-window cannot switch a charger on for a car already at target. Only a plan the
            # target's stop ended arms nothing: one whose stop was not executed stays, and its windows'
            # timers are armed as ever.
            if await self._start_window_locked(trigger=core_events.TRIGGER_REARM) and self.plan is None:
                return
        else:
            # Re-arming outside a window stops a charge that runs; a person who starts it again
            # after that is respected, as after the hold (`window_hold.py`). Only the plan's own charge is
            # the plan's to stop: one a window's end spares (a person's Start, a Charge-now start, the sun's,
            # one load balancing holds back for them), or one something else owns (a pause, solar, the
            # hybrid hand-off), goes on.
            token = self._shadow.begin()
            facts = self._shadow_facts(
                lambda: {
                    "charging": self.charging,
                    "handed_off": self._shadow_handed_off(),
                    **self._shadow_hold_facts(),
                }
            )
            legacy = []
            outcome = None
            try:
                spared = (
                    self._charge_origin in WINDOW_END_SPARED_ORIGINS
                    or self._window_end_spared_owner() is not None
                    # A person's override of the hold (kept across a restart) is theirs, as after the hold.
                    or (self._hold.overridden and self._hold.held)
                    or not self._automatic_permitted(AUTOMATIC_STOP)
                    or self._hold_blocked()
                    or (self._end_window_guard is not None and self._end_window_guard())
                )
                verdict = None if facts is None else self._shadow.verdict(core_events.Rearm(past_last=False, **facts))
                if not spared if verdict is None else self._shadow.choose("rearm", not spared, ("stop", "rearm") in verdict):
                    was_charging = self.charging
                    legacy.append("stop")
                    outcome = CommandOutcome(False)
                    stopped = await self._automatic_stop_locked("the stop outside the plan's windows")
                    outcome = self._shadow_stop_outcome(stopped)
                    if stopped and was_charging:
                        self._hold.held_now()
                        await self._async_save_quietly()
            finally:
                self._shadow_end_event(
                    token, facts, lambda: core_events.Rearm(past_last=False, **facts), legacy=legacy, outcome=outcome
                )
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
        land beside a plan being replaced. An automatic decision (`_automatic`): a pause stored meanwhile
        keeps it from starting anything.
        """
        async with self._automatic(AUTOMATIC_START) as allowed:
            return allowed and await self._start_window_locked(trigger=core_events.TRIGGER_TIMER)

    async def _start_window_locked(self, *, trigger: str = core_events.TRIGGER_TIMER) -> bool:
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
        token = self._shadow.begin()
        self._shadow_car_facts = (False, False)
        facts = self._shadow_facts(self._shadow_start_facts)
        legacy: list[str] = []
        outcome: CommandOutcome | None = None
        try:
            verdict = self._core_window_start_verdict(trigger, facts)
            if verdict is not None:
                start = self._shadow.choose(
                    "window_start",
                    self._automatic_permitted(AUTOMATIC_START) and not self._car_ended_holds_window(),
                    ("start", "plan_window") in verdict,
                )
                if not start:
                    if not self._automatic_permitted(AUTOMATIC_START):
                        _LOGGER.info(
                            "SpotNav charger %s: a window opens while Auto is paused; nothing is started", self.entry_id
                        )
                    return False
            elif not self._automatic_permitted(AUTOMATIC_START):
                # A pause holds Auto's execution (one whose stop failed leaves its plan here): no window of
                # it starts, whoever asks (a timer, a restart's re-arm).
                _LOGGER.info(
                    "SpotNav charger %s: a window opens while Auto is paused; nothing is started", self.entry_id
                )
                return False
            elif self._car_ended_holds_window():
                return False
            legacy.append("start")
            outcome = CommandOutcome(False)
            executed = await self._start_locked(cause="plan_window")
            outcome = self._shadow_start_outcome(executed)
            return False
        finally:
            self._shadow_end_event(
                token,
                facts,
                lambda: core_events.WindowStart(trigger=trigger, **facts, **self._shadow_take_car_facts()),
                legacy=legacy,
                outcome=outcome,
            )

    async def _async_enforce_target(self) -> bool:
        """Make the target decision for the plan in force, and act on it. Takes the operation lock
        and delegates; readings arrive from a state-change listener that could otherwise land
        mid plan-replacement.
        """
        async with self._automatic(AUTOMATIC_STOP) as allowed:
            return allowed and await self._enforce_target_locked(gated=True)

    async def _enforce_target_locked(self, *, gated: bool = False) -> bool:
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
            target_soc_percent=self.plan.target_soc_percent,
            reading=reading,
            to_vehicle_limit=self.charges_to_vehicle_limit(),
        )
        if not decision.stop:
            return False
        # Set before the first await and always cleared: a raising service call must not leave
        # the controller refusing to enforce a later plan.
        self._target_stopping = True
        try:
            await self._target_stop_locked(reading, gated=gated)
        finally:
            self._target_stopping = False
        return True

    async def _target_stop_locked(self, reading: SocReading | None, *, gated: bool = False) -> None:
        """Stop for a reached target and record why before ending the plan. The lock is held."""
        token = self._shadow.begin()
        outcome = CommandOutcome(False)
        try:
            outcome = self._shadow_stop_outcome(await self._target_stop_body(reading))
        finally:
            self._shadow.end(token, core_events.TargetReached(gated=gated), legacy=("stop",), outcome=outcome)

    async def _target_stop_body(self, reading: SocReading | None) -> bool:
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
        return await self._automatic_stop_locked("the target's stop", clear_schedule=True)

    def target_reading(self) -> SocReading | None:
        """The live reading for the plan's vehicle, or `None` with no plan or no way to read a
        vehicle. Read fresh on every call.
        """
        if self._soc_reader is None or self.plan is None:
            return None
        return self._soc_reader(self.plan.vehicle_id)

    def vehicle_limit_percent(self, vehicle_id: str | None) -> float | None:
        """The vehicle's own charge limit, or `None` when unknown or nothing reads it."""
        if self._vehicle_limit_reader is None:
            return None
        return self._vehicle_limit_reader(vehicle_id)

    def charges_to_vehicle_limit(self) -> bool:
        """Whether the plan in force charges to the car's own limit, so the car, not SpotNav, ends it: a
        target at or above that limit (or 100 % when it is unknown), or a manual amount Auto capped at the
        room left in the battery (`ChargingPlan.to_vehicle_limit`)."""
        plan = self.plan
        if plan is None:
            return False
        if plan.target_soc_percent is None:
            return plan.to_vehicle_limit
        return charges_to_vehicle_limit(plan.target_soc_percent, self.vehicle_limit_percent(plan.vehicle_id))

    @property
    def charging_to_vehicle_limit(self) -> bool:
        """A Start is in effect for a plan that charges to the car's own limit: the car ends it."""
        return self.charges_to_vehicle_limit() and self._control_on

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
        self._async_spawn(self._async_enforce_target(), "the target's stop")



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
        being on for another reason) and answers `False` for a plan whose stored instants cannot be read. A
        top-off past the last window (`top_off_until`) is the plan's too: nothing else takes the charger
        while it runs.
        """
        plan = self.plan
        if plan is None:
            return False
        if self.top_off_until is not None:
            return True
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
        """A window ended and another follows: stop the plan's charge (never one a person or the sun owns,
        `_window_end_spared_owner`) but keep the schedule, unless
        `end_window_guard` says the sun can carry the charge past this boundary (see
        `set_end_window_guard`), in which case nothing happens and charging continues, or the next plan
        takes the charge over at this boundary (`_successor_continues`).
        """
        if self._end_window_guard is not None and self._end_window_guard():
            _LOGGER.debug(
                "%s charger %s: window ended with sun available, handing off to solar control",
                HYBRID_LOG_TOKEN,
                self.entry_id,
            )
            self._shadow.feed(core_events.WindowEnd(handed_off=True))
            return
        if self._successor_continues():
            self._shadow.feed(core_events.WindowEnd(continued=True))
            self._async_spawn(self._async_hand_over(final=False), "the window end's hand-over")
            return
        self._window_end()

    def _window_end(self) -> None:
        self._async_spawn(self._async_window_end_stop(clear_schedule=False), "the window end's stop")

    def _successor_continues(self) -> bool:
        """Whether the execution boundary holds the next plan, waiting for this window boundary, with a window
        open now (`AutoExecutor.successor_continues`): a best-effort plan ending at its departure while the next
        departure's plan begins then. Its install takes the charge over, so this boundary sends no stop and
        records no end: no contactor cycle, one charge. Either order of the two works: a plan installed first
        cancels this end's timer and its re-arm keeps the charge (`_reschedule_locked`)."""
        probe = getattr(self._automatic_gate, "successor_continues", None)
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - an unreadable boundary hands nothing over: the end as ever
            return False

    async def _async_hand_over(self, *, final: bool) -> None:
        """The window boundary handed to the next plan (`_successor_continues`): it is installed now. When it is
        not after all (its proposal went stale meanwhile), the boundary ends the charge as it always did."""
        plan = self.plan
        apply = getattr(self._automatic_gate, "async_apply_pending", None)
        if apply is not None:
            await apply()
        if self.plan is not plan or self._shut_down:
            return
        _LOGGER.debug("SpotNav charger %s: the next plan was not installed at the window's end", self.entry_id)
        if final:
            self._final_window_end()
        else:
            self._window_end()

    @callback
    def _async_final_end_callback(self, _now: datetime) -> None:
        """The last window ended: stop the plan's charge (a person's or the sun's goes on,
        `_window_end_spared_owner`) and clear the schedule, unless `end_window_guard` says the
        sun can carry the charge past this boundary; then neither happens and solar mode owns the
        charger (see `plan_window_active_now`, `False` once every window is past). A plan that charges
        to the car's own limit with the car still drawing is not stopped either: the car finishes it in
        a top-off (`top_off.py`, `_async_begin_top_off`), nor one the next plan takes over at this instant
        (`_successor_continues`).
        """
        if self._end_window_guard is not None and self._end_window_guard():
            _LOGGER.debug(
                "%s charger %s: final window ended with sun available, handing off to solar control",
                HYBRID_LOG_TOKEN,
                self.entry_id,
            )
            self._shadow.feed(core_events.FinalWindowEnd(handed_off=True))
            return
        if self._successor_continues():
            # The next plan begins now and takes the charge over: no stop, no top-off, and no end recorded.
            self._shadow.feed(core_events.FinalWindowEnd(continued=True))
            self._async_spawn(self._async_hand_over(final=True), "the last window end's hand-over")
            return
        self._final_window_end()

    def _final_window_end(self) -> None:
        if self._window_end_spared_owner() is not None:
            # Not the plan's charge: the plan ends with its last window, the charge goes on (decided again
            # under the lock).
            self._async_spawn(self._async_window_end_stop(clear_schedule=True), "the last window's end")
            return
        if self._top_off_wanted() is not None:
            # A car still drawing on a charge to its own limit finishes it: the top-off decides again
            # under the lock, and ends the plan as this would have when it may not run.
            self._async_spawn(self._async_begin_top_off(), "the last window's end")
            return
        if self.plan is not None and self._control_on:
            # A charge still running at the last window's end: the plan is done.
            self._record_completion("plan_done", target_soc_percent=self.plan.target_soc_percent)
        self._async_spawn(self._async_window_end_stop(clear_schedule=True), "the last window end's stop")

    async def _async_window_end_stop(self, *, clear_schedule: bool) -> None:
        """A window's end stops the plan's charge, as an automatic decision (`_automatic`)."""
        async with self._automatic(AUTOMATIC_STOP) as allowed:
            event = core_events.FinalWindowEnd() if clear_schedule else core_events.WindowEnd()
            token = self._shadow.begin()
            legacy: list[str] = []
            outcome: CommandOutcome | None = None
            try:
                verdict = self._shadow.verdict(event)
                spared = self._window_end_spared_owner()
                stop = allowed and spared is None
                if verdict is not None:
                    stop = self._shadow.choose(
                        "window_end", stop, ("stop", "final_window_end" if clear_schedule else "window_end") in verdict
                    )
                if stop:
                    legacy.append("stop")
                    outcome = CommandOutcome(False)
                    stopped = await self._automatic_stop_locked(
                        "the window end's stop", clear_schedule=clear_schedule, request=True
                    )
                    outcome = self._shadow_stop_outcome(stopped)
                elif allowed:
                    await self._window_end_spare_locked(spared, clear_schedule=clear_schedule)
            finally:
                self._shadow.end(token, event, legacy=legacy, outcome=outcome)

    def _window_end_spared_owner(self) -> str | None:
        """Who owns the charge a plan window's end leaves alone, else `None` (the end stops it).

        A window's end stops only the plan's own charge: the window's, or one the charger began by itself
        inside it (claimed, or not yet). A charge a person started (a Start, Charge now), or the sun, goes
        on, and so does one load balancing holds back for them: the regulator still gives it back. Read
        under the operation lock by the stop itself; the timer's callback reads it too, to choose between
        the stop and the top-off. The core decides the same (`core.ownership.window_end_spared`).
        """
        if self._plan_charge:
            return None
        origin = self._charge_origin
        if self._control_observation is False and not self.start_pending:
            # Seen off with no Start on its way: an origin left from a charge that ended owns nothing.
            origin = None
        if origin is None and self._paused_by_balancing and self._paused_charge is not None:
            origin, plan_charge = self._paused_charge
            if plan_charge:
                return None
        return origin if origin in WINDOW_END_SPARED_ORIGINS else None

    async def _window_end_spare_locked(self, owner: str | None, *, clear_schedule: bool) -> None:
        """A window's end that leaves the charge running, with the operation lock held: it is not the plan's
        (`owner`, or the core's owner when the core drives), and the last window's end ends the plan."""
        _LOGGER.info(
            "SpotNav charger %s: a planned window ended; the charge is not the plan's (%s), so it goes on%s",
            self.entry_id,
            owner or self._shadow.session.owner,
            " and the plan ends" if clear_schedule else "",
        )
        if clear_schedule:
            await self._end_plan_locked()

    async def _end_plan_locked(self) -> None:
        """Clear the plan, its top-off and its timers without touching the charger, as a stop that clears
        the schedule clears them (`_stop_locked`)."""
        self._plan_charge = False
        self.plan = None
        self._clear_top_off()
        self._cancel_timers()
        self._async_disarm_target_listener()
        self._async_disarm_probe_listener()
        await self._async_save()
        self._notify()

    # ------------------------------------------------------------------ the top-off (`top_off.py`)

    @property
    def top_off_until(self) -> datetime | None:
        """While the car finishes a charge to its own limit past the plan's last window, the latest the
        top-off may run to (`top_off.deadline`); else `None`."""
        if self._top_off_until is None or self.plan is None:
            return None
        return self._top_off_until

    def _plan_departure(self) -> datetime | None:
        """The departure the plan is for, or `None` when it has none (or it cannot be read)."""
        plan = self.plan
        if plan is None or not plan.departure:
            return None
        try:
            return _parse_datetime(plan.departure)
        except ValueError:
            return None

    def car_drawing(self) -> bool | None:
        """Whether the car draws current now (`top_off.car_drawing`), `None` when nothing can tell."""
        return self._car_drawing()

    def _car_drawing(self) -> bool | None:
        """Whether the car draws current now, from the facts the progress observation reads."""
        facts = self.charge_progress_facts()
        return top_off.car_drawing(
            connector_status=facts.connector_status,
            current_a=facts.current_import_a,
            power_mode=facts.power_mode,
            power_w=facts.power_w,
            idle_power_w=facts.idle_power_w,
        )

    def _top_off_wanted(self) -> datetime | None:
        """The deadline of the top-off the plan's last window ending calls for, else `None`.

        Only for a plan that charges to the car's own limit, with its charge still on and the plan's (not a
        person's or the sun's, `_window_end_spared_owner`), nothing else owning the charger (a pause,
        solar), load balancing not pausing it, a car not known to be gone,
        and the car still drawing by what the charger measures (a control that is merely on is no
        evidence). Never past the departure: a deadline already reached is none.
        """
        plan = self.plan
        if plan is None or not self.charges_to_vehicle_limit():
            return None
        try:
            window_end = plan.windows[-1][1]
        except ValueError:
            return None
        if not self._control_on or self._paused_by_balancing or self._hold_blocked():
            return None
        if self._window_end_spared_owner() is not None:
            return None
        if self.adapter.vehicle_connected() is False or self._car_drawing() is not True:
            return None
        until = top_off.deadline(window_end, self._plan_departure())
        if until <= dt_util.utcnow():
            return None
        return until

    async def _async_begin_top_off(self) -> None:
        """The plan's last window ended with the car still drawing on a charge to its own limit: keep the
        charge on until the car stops by itself or the deadline. Decided again under the lock; when it
        may no longer run, the plan ends as its last window's end ends it."""
        async with self._automatic(AUTOMATIC_STOP) as allowed:
            plan = self.plan
            if not allowed:
                self._shadow.feed(core_events.FinalWindowEnd())
                return
            if plan is None or self._top_off_until is not None:
                return
            try:
                window_end = plan.windows[-1][1]
            except ValueError:
                return
            if dt_util.utcnow() < window_end:
                # Replaced meanwhile by a plan with a window still ahead: its own timers decide.
                return
            token = self._shadow.begin()
            until: datetime | None = None
            legacy: list[str] = []
            outcome: CommandOutcome | None = None
            try:
                until = self._top_off_wanted()
                spared = self._window_end_spared_owner()
                stop = until is None and spared is None
                verdict = self._shadow.verdict(core_events.FinalWindowEnd(top_off_wanted=until is not None))
                if verdict is not None:
                    # The core decides: the last window's stop, a top-off, or a charge it leaves running.
                    stop = self._shadow.choose("final_window_end", stop, ("stop", "final_window_end") in verdict)
                    if stop:
                        until = None
                if until is None:
                    if self._control_on:
                        self._record_completion("plan_done", target_soc_percent=plan.target_soc_percent)
                    if not stop:
                        # Not the plan's charge: it goes on, and the plan ends.
                        await self._window_end_spare_locked(spared, clear_schedule=True)
                        return
                    legacy.append("stop")
                    outcome = CommandOutcome(False)
                    stopped = await self._automatic_stop_locked("the last window end's stop", clear_schedule=True)
                    outcome = self._shadow_stop_outcome(stopped)
                    return
                self._top_off_until = until
                self._top_off_idle_since = None
                _LOGGER.info(
                    "SpotNav charger %s: the last window ended with the car still drawing; letting it finish "
                    "until it stops by itself, at most until %s",
                    self.entry_id,
                    until.isoformat(),
                )
                await self._async_save_quietly()
                self._arm_top_off_timers()
                self._notify()
            finally:
                self._shadow.end(
                    token,
                    core_events.FinalWindowEnd(top_off_wanted=until is not None),
                    legacy=legacy,
                    outcome=outcome,
                )

    def _resume_top_off(self, now: datetime) -> bool:
        """At a re-arm with every window past (a restart, a follow): go on with a stored top-off whose
        deadline is still ahead. One that is over is forgotten, and the plan's charge is stopped as any
        charge that strays past the last window is. Whether one goes on."""
        until = self._top_off_until
        if until is None:
            return False
        if self.plan is None or now >= until or self._hold_blocked():
            self._clear_top_off()
            return False
        # A restart looks afresh: the car must stop drawing for the whole idle time again.
        self._top_off_idle_since = None
        self._arm_top_off_timers()
        _LOGGER.info(
            "SpotNav charger %s: going on with the top-off, at most until %s", self.entry_id, until.isoformat()
        )
        return True

    def _arm_top_off_timers(self) -> None:
        """The top-off's deadline, and a regular look (a car at 0 A reports nothing new)."""
        self._cancel_top_off_timers()
        until = self._top_off_until
        if until is None:
            return
        self._top_off_cancels.append(
            async_track_point_in_utc_time(self.hass, self._async_top_off_deadline_callback, until)
        )
        self._top_off_cancels.append(
            async_track_time_interval(
                self.hass, self._async_top_off_interval, top_off.CHECK_INTERVAL, cancel_on_shutdown=True
            )
        )

    def _cancel_top_off_timers(self) -> None:
        for cancel in self._top_off_cancels:
            cancel()
        self._top_off_cancels.clear()

    def _clear_top_off(self) -> None:
        """Forget a top-off (its record is saved with the plan's next save)."""
        self._cancel_top_off_timers()
        self._top_off_until = None
        self._top_off_idle_since = None

    @callback
    def _async_top_off_deadline_callback(self, _now: datetime) -> None:
        self._async_spawn(self._async_end_top_off(TOP_OFF_DEADLINE), "the top-off\'s end")

    @callback
    def _async_top_off_interval(self, _now: datetime) -> None:
        self._top_off_tick()

    @callback
    def _top_off_tick(self) -> None:
        """One look at a running top-off: past its deadline, the car gone, the charge off by other means,
        or the car drawing nothing for `top_off.IDLE_S` in a row ends it. A charge load balancing paused
        is not the car's choice, and a car whose drawing cannot be read is not counted as idle."""
        until = self.top_off_until
        if until is None:
            return
        now = dt_util.utcnow()
        if now >= until:
            self._async_spawn(self._async_end_top_off(TOP_OFF_DEADLINE), "the top-off\'s end")
            return
        if self.adapter.vehicle_connected() is False:
            self._async_spawn(self._async_end_top_off(TOP_OFF_UNPLUGGED), "the top-off\'s end")
            return
        if self._paused_by_balancing:
            self._top_off_idle_since = None
            return
        if self._control_observation is False:
            self._async_spawn(self._async_end_top_off(TOP_OFF_OFF), "the top-off\'s end")
            return
        if self._car_drawing() is not False:
            self._top_off_idle_since = None
            return
        since = self._top_off_idle_since
        if since is None:
            self._top_off_idle_since = now
            return
        if (now - since).total_seconds() >= top_off.IDLE_S:
            self._async_spawn(self._async_end_top_off(TOP_OFF_FULL), "the top-off\'s end")

    async def _async_end_top_off(self, reason: str) -> None:
        """End a top-off, decided again under the lock, and the plan with it.

        `full` (the car stopped drawing by itself) is a completion of its own; the deadline is the plan's
        ordinary end (`plan_done` while the charge is on); the car gone or the charge already off records
        nothing.
        """
        async with self._automatic(AUTOMATIC_STOP) as allowed:
            until = self._top_off_until
            plan = self.plan
            if until is None or plan is None:
                return
            token = self._shadow.begin()
            note: dict[str, Any] = {"legacy": [], "outcome": None, "valid": False}
            try:
                await self._end_top_off_locked(reason, allowed, until, plan, note)
            finally:
                self._shadow.end(
                    token,
                    core_events.TopOffEnd(reason=reason, valid=note["valid"]),
                    legacy=note["legacy"],
                    outcome=note["outcome"],
                )

    async def _end_top_off_locked(
        self, reason: str, allowed: bool, until: datetime, plan: ChargingPlan, note: dict[str, Any]
    ) -> None:
        """`_async_end_top_off` itself, with the boundary's lock and the operation lock held."""
        if not allowed and not self._core_drives:
            return
        now = dt_util.utcnow()
        if reason == TOP_OFF_DEADLINE:
            valid = now >= until
        elif reason == TOP_OFF_UNPLUGGED:
            valid = self.adapter.vehicle_connected() is False
        elif reason == TOP_OFF_OFF:
            valid = self._control_observation is False and not self._paused_by_balancing
        else:
            since = self._top_off_idle_since
            valid = (
                not self._paused_by_balancing
                and since is not None
                and (now - since).total_seconds() >= top_off.IDLE_S
                and self._car_drawing() is False
            )
        note["valid"] = valid
        verdict = self._shadow.verdict(core_events.TopOffEnd(reason=reason, valid=valid))
        if verdict is None:
            if not (allowed and valid):
                return
        elif not self._shadow.choose("top_off_end", allowed and valid, ("stop", "top_off_end") in verdict):
            return
        _LOGGER.info("SpotNav charger %s: the top-off ends (%s)", self.entry_id, reason)
        if reason == TOP_OFF_FULL:
            self._record_completion("vehicle_full", target_soc_percent=plan.target_soc_percent)
        elif reason == TOP_OFF_DEADLINE and self._control_on:
            self._record_completion("plan_done", target_soc_percent=plan.target_soc_percent)
        note["legacy"].append("stop")
        note["outcome"] = CommandOutcome(False)
        stopped = await self._automatic_stop_locked("the top-off's stop", clear_schedule=True)
        note["outcome"] = self._shadow_stop_outcome(stopped)

    # ------------------------------------------------------------------ the ownership core's shadow

    @property
    def ownership_shadow(self) -> OwnershipShadow:
        """The charge-ownership core in shadow mode (`ownership_shadow.py`): diagnostics read it, the execution
        boundary feeds it."""
        return self._shadow

    def set_solar_hold_probe(self, probe: Callable[[], bool] | None) -> None:
        """The sun's part of the hold guard (`set_hold_guard`), told apart for the ownership shadow, which decides
        the pause's part itself. Set after the guard."""
        self._solar_hold_probe = probe

    def _take_core_owner(self, session: ChargeSession) -> bool:
        """When the core drives: a charge the core says nobody here owns clears today's two owner fields, so
        everything that reads them (the re-arm, the stray stop, a claim, the sun's take-over, a balancing pause)
        follows it. One-way: an owner is never written back. Today's code sets its fields where it starts or claims
        a charge, and clears `plan_charge` on its own when it sees the charger off (`_async_forget_plan_charge`);
        setting it back to the core's owner after every feed would undo that at every report. Whether they
        changed."""
        if session.owner in _CORE_OWNER_ORIGIN:
            return False
        if (self._charge_origin, self._plan_charge) == (None, False):
            return False
        self._charge_origin, self._plan_charge = None, False
        self._save_memory_soon()
        return True

    def _shadow_note(self, kind: str) -> None:
        """A report or a timer decided a command (`start`, `stop`, `notify`): kept for the shadow's comparison."""
        decisions = self._shadow_decisions
        if decisions is not None:
            decisions.append(kind)

    def _shadow_note_gated(self, kind: str, gate: str) -> None:
        """A report spawned a decision its task takes again through the gate: noted only when the gate lets it act
        now (what the task will find, unless something lands between)."""
        if self._shadow_decisions is None:
            return
        try:
            permitted = self._automatic_permitted(gate)
        except Exception:  # noqa: BLE001 - the shadow never raises into the real path
            return
        if permitted:
            self._shadow_decisions.append(kind)

    def _shadow_start_pending(self) -> bool:
        """`start_pending` without its side effect (it forgets an answered start)."""
        sent_at = self._start_sent_at
        if sent_at is None or self.charging or self.adapter.held_by_charger():
            return False
        return (dt_util.utcnow() - sent_at).total_seconds() < START_ACK_TIMEOUT_S

    def _shadow_stop_recent(self) -> bool:
        """A stop of ours the charger has not answered yet (`self_started_charge`), read without its side effect."""
        sent_at = self._stop_sent_at
        return sent_at is not None and (dt_util.utcnow() - sent_at).total_seconds() < STOP_ACK_S

    def _shadow_handed_off(self) -> bool:
        return self._end_window_guard is not None and bool(self._end_window_guard())

    def _shadow_hold_facts(self) -> dict[str, bool]:
        """What else may own the charger, split as the core decides it: the sun's phase, and whether the plan is
        Auto's (a pause holds the hold only for an Auto plan). With no probe for the sun's part the guard is read
        whole, as the sun's."""
        guard = self._hold_guard
        if guard is None:
            return {"solar_holds": False, "plan_auto_owned": False}
        probe = self._solar_hold_probe
        if probe is None:
            return {"solar_holds": bool(guard()), "plan_auto_owned": False}
        plan = self.plan
        return {"solar_holds": bool(probe()), "plan_auto_owned": plan is not None and plan.auto_owned}

    def _shadow_windows(self) -> tuple[datetime | None, bool]:
        """The start of the plan's window open now (`None`: none), and whether one is ahead outside every window."""
        plan = self.plan
        if plan is None:
            return None, False
        try:
            windows = plan.windows
        except ValueError:
            return None, False
        now = dt_util.utcnow()
        open_start = next((start for start, end in windows if start <= now < end), None)
        return open_start, self._next_window_start() is not None

    def _shadow_start_facts(self) -> dict[str, Any]:
        open_start, _ahead = self._shadow_windows()
        return {
            "open_window_start": open_start,
            "control_on": self._control_on,
            "connected": self.adapter.vehicle_connected(),
            "start_pending": self._shadow_start_pending(),
            **self._shadow_hold_facts(),
        }

    def _shadow_take_car_facts(self) -> dict[str, bool]:
        known_full, need_grew = self._shadow_car_facts
        self._shadow_car_facts = (False, False)
        return {"car_ended_known_full": known_full, "need_grew": need_grew}

    def _shadow_facts(self, read: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        """Facts for an event, read without ever raising into the real path (`None`: they could not be read, and
        the event is not fed)."""
        try:
            return read()
        except Exception:  # noqa: BLE001 - the shadow never raises into the real path
            _LOGGER.debug("SpotNav ownership shadow: facts unreadable", exc_info=True)
            return None

    def _shadow_end_event(
        self,
        token: ShadowToken,
        facts: dict[str, Any] | None,
        build: Callable[[], core_events.Event],
        *,
        legacy: Any = (),
        outcome: CommandOutcome | None = None,
    ) -> None:
        if facts is None:
            self._shadow.cancel(token)
            return
        try:
            event = build()
        except Exception:  # noqa: BLE001 - the shadow never raises into the real path
            _LOGGER.debug("SpotNav ownership shadow: event unbuildable", exc_info=True)
            self._shadow.cancel(token)
            return
        self._shadow.end(token, event, legacy=legacy, outcome=outcome)

    def _shadow_stop_outcome(self, executed: bool) -> CommandOutcome:
        """What became of a stop: it went out (or the charger was known stopped), or not; one that sent nothing
        because the charge control said nothing is `unobserved`."""
        return CommandOutcome(executed, unobserved=executed and self._shadow_unobserved)

    def _shadow_start_outcome(self, executed: bool) -> CommandOutcome:
        return CommandOutcome(executed, balancing_held=not executed and self._paused_by_balancing)

    async def _recheck(
        self, what: str, today: Callable[[], bool], act: Callable[[], Awaitable[bool]]
    ) -> bool:
        """A background task's decision taken again under the boundary's lock and the operation lock: the core
        decides on its session lined up now (`core_events.Recheck`), and when it drives that verdict sends the
        command, else today's rule (`today`) does. What the command did comes back to the core as its
        `CommandResult`. Returns whether it went out."""
        token = self._shadow.begin()
        facts = self._shadow_facts(lambda: self._recheck_facts(what))
        command = "start" if what == core_events.RECHECK_CLAIM else "stop"
        legacy: list[str] = []
        outcome: CommandOutcome | None = None
        try:
            due = today()
            if facts is not None and not self._core_drives:
                # The car-ended rule's answer as today's own rule read it (nothing when it short-circuited first).
                facts.update(self._shadow_take_car_facts())
            verdict = None if facts is None else self._shadow.verdict(core_events.Recheck(what=what, **facts))
            if verdict is not None:
                due = self._shadow.choose(f"recheck_{what}", due, (command, what) in verdict)
            if not due:
                return False
            legacy.append(command)
            outcome = CommandOutcome(False)
            executed = await act()
            outcome = self._shadow_start_outcome(executed) if command == "start" else self._shadow_stop_outcome(executed)
            return executed
        finally:
            self._shadow_end_event(
                token, facts, lambda: core_events.Recheck(what=what, **facts), legacy=legacy, outcome=outcome
            )

    def _recheck_facts(self, what: str) -> dict[str, Any]:
        """The facts a background task's rule reads, read again under the lock (`core_events.Recheck`).

        The car-ended rule's facts (only the claim's decision reads them) are read here only when the core drives:
        reading them may read the car's state of charge, which moves and saves its anchor. With the option off they
        are what today's rule read itself, taken after it (`_recheck`), so the shadow reads nothing of the car."""
        open_start, ahead = self._shadow_windows()
        self._shadow_car_facts = (False, False)
        if self._core_drives and what == core_events.RECHECK_CLAIM:
            self._car_ended_holds_window()
        known_full, need_grew = self._shadow_car_facts
        self._shadow_car_facts = (False, False)
        commanded = what in (core_events.RECHECK_HOLD, core_events.RECHECK_PERSON_HOLD)
        return {
            "control_on": self._control_on if commanded else self._control_observation is True,
            "window_ahead_outside": ahead,
            "window_open": self.plan_window_active_now,
            "in_window": open_start is not None,
            "plan_present": self.plan is not None,
            "open_window_start": open_start,
            "car_ended_known_full": known_full,
            "need_grew": need_grew,
            "handed_off": self._shadow_handed_off(),
            "top_off": self.top_off_until is not None,
            "start_pending": self._shadow_start_pending(),
            "owned": self._hold.owned,
            **self._shadow_hold_facts(),
        }

    async def _shadow_direct_stop(self, stop: Any, clear_schedule: bool) -> None:
        token = self._shadow.begin()
        outcome = CommandOutcome(False)
        try:
            await stop
            outcome = self._shadow_stop_outcome(True)
        finally:
            self._shadow.end(
                token, core_events.DirectStop(clear_schedule=clear_schedule), legacy=("stop",), outcome=outcome
            )

    def _shadow_report_begin(self) -> tuple[ShadowToken, core_events.Event | None, list[str] | None]:
        """Before a report's decisions: the facts as the report found them, and a list for what it decides."""
        token = self._shadow.begin()
        event: core_events.Event | None = None
        try:
            observed = self._control_observation
            connected = self.adapter.vehicle_connected()
            if observed is False:
                event = core_events.ChargerReportedOff(
                    start_pending=self._shadow_start_pending(), connected=connected
                )
            elif observed is True:
                open_start, ahead = self._shadow_windows()
                event = core_events.ChargerReportedOn(
                    charging=self.charging,
                    was_on=self._hold._on,  # noqa: SLF001 - the hold's own memory, read only
                    connected=connected,
                    window_ahead_outside=ahead,
                    window_open=self.plan_window_active_now,
                    in_window=open_start is not None,
                    plan_present=self.plan is not None,
                    open_window_start=open_start,
                    handed_off=self._shadow_handed_off(),
                    start_pending=self._shadow_start_pending(),
                    stop_recent=self._shadow_stop_recent(),
                    owned=self._hold.owned,
                    **self._shadow_hold_facts(),
                )
        except Exception:  # noqa: BLE001 - the shadow never raises into the real path
            _LOGGER.debug("SpotNav ownership shadow: report facts unreadable", exc_info=True)
            event = None
        previous = self._shadow_decisions
        self._shadow_decisions = []
        self._shadow_car_facts = (False, False)
        if self._core_drives and event is not None:
            try:
                if isinstance(event, core_events.ChargerReportedOn):
                    self._car_ended_holds_window()
                    known_full, need_grew = self._shadow_car_facts
                    event = replace(event, car_ended_known_full=known_full, need_grew=need_grew)
                self._core_report_verdict = self._core_hold_verdict = self._shadow.verdict(event)
            except Exception:  # noqa: BLE001 - today's decision stands
                self._core_report_verdict = self._core_hold_verdict = None
        return token, event, previous

    def _shadow_report_end(self, report: tuple[ShadowToken, core_events.Event | None, list[str] | None]) -> None:
        token, event, previous = report
        decisions = self._shadow_decisions or []
        self._shadow_decisions = previous
        self._core_report_verdict = self._core_hold_verdict = None
        if event is None:
            self._shadow.cancel(token)
            return
        if isinstance(event, core_events.ChargerReportedOn) and not self._core_drives:
            try:
                event = replace(event, **self._shadow_take_car_facts())
            except Exception:  # noqa: BLE001
                self._shadow.cancel(token)
                return
        self._shadow.end(token, event, legacy=decisions)

    def _observe_person_hold_timer(self) -> None:
        """`_observe_person_hold` outside a report (its retry, a restore, a stop's retry), fed as a timer."""
        token = self._shadow.begin()
        facts = self._shadow_facts(
            lambda: {"control_on": self._control_observation is True, "start_pending": self._shadow_start_pending()}
        )
        previous = self._shadow_decisions
        self._shadow_decisions = []
        if facts is not None:
            self._core_hold_verdict = self._shadow.verdict(core_events.Timer(**facts))
        try:
            self._observe_person_hold()
        finally:
            self._core_hold_verdict = None
            decisions = self._shadow_decisions or []
            self._shadow_decisions = previous
            self._shadow_end_event(token, facts, lambda: core_events.Timer(**facts), legacy=decisions)

    def _shadow_session(self) -> ChargeSession:
        """Today's ownership and person intent, read as the core's session (`ownership_shadow.legacy_session`)."""
        gate = self._automatic_gate
        pause = None
        if gate is not None:
            try:
                pause = getattr(gate, "pause_intent", None)
            except Exception:  # noqa: BLE001 - an unreadable pause is read as none
                pause = None
        manual, span = legacy_intent(pause, gate is not None and self._legacy_person_stopped)
        top_off = self.top_off_until is not None
        paused = self._paused_charge
        paused_origin: str | None = None
        if paused is not None and paused[0] is not None:
            owner = legacy_owner(origin=paused[0], top_off=top_off, charging=False, start_pending=False, stop_recent=False)
            paused_origin = None if owner == "none" else owner
        return ChargeSession(
            plugged=self._known_connected,
            owner=legacy_owner(
                origin=self._charge_origin,
                top_off=top_off,
                charging=self.charging,
                start_pending=self._shadow_start_pending(),
                stop_recent=self._shadow_stop_recent(),
            ),
            manual=manual,
            span_pause=span,
            held=self._hold.held,
            overridden=self._hold.overridden,
            car_ended_at=self._car_ended_at,
            balancing_paused=self._paused_by_balancing,
            paused_origin=paused_origin,
            held_for_safety=self._held_for_safety,
            safety_stopped_at=self._safety_stopped_at,
            hold_stop_times=tuple(self._person_hold_stop_times),
            hold_tried_at=self._person_hold_tried_at,
            hold_gave_up=self._person_hold_gave_up,
            hold_stop_pending=self._person_hold_stop_pending,
        )

    def _async_spawn(self, work: Any, what: str) -> None:
        """Run one decision a timer or a report made, as a task whose failure is logged, not raised into
        Home Assistant's loop: a stop the charger did not execute keeps the charge as it was, and the
        next report or timer decides again."""
        self.hass.async_create_task(self._async_logged(work, what))

    async def _async_logged(self, work: Any, what: str) -> None:
        try:
            await work
        except Exception as err:  # noqa: BLE001 - logged; the charge stays as it was
            _LOGGER.warning("SpotNav charger %s: %s failed: %s", self.entry_id, what, type(err).__name__)

    def _cancel_timers(self) -> None:
        for cancel in self._timer_cancels:
            cancel()
        self._timer_cancels.clear()
