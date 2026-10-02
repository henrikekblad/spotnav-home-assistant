"""Whether the vehicle is actually asking for current: one passive, typed observation.

The narrow claim this module may make: *the charge was started, and the vehicle is not requesting
current*. It is not a failed schedule, charger, command or load balancing, so nothing here tries to
fix anything: no retry, reset, profile clear or service call. It reads Home Assistant states,
decides one value, and owns one timer.

Values (shared with both clients): `normal` (nothing to say), `vehicle_not_requesting_current` (the
observation held through its grace period), `unknown` (a charge is expected and cannot be
observed). The advisory is true only of one captured snapshot in which all of these hold:

1. SpotNav expects charging now (the installed plan covers the instant, or the charge control was
   accepted and reports on);
2. no Start is still awaiting the charger's acknowledgement;
3. the connector status is exactly OCPP's `SuspendedEV`;
4. the current-import **main state** is a finite near-zero value.

`L1/L2/L3` attributes are never read: some sensors keep the last non-zero per-phase samples there
after the main state has gone to `0`. `unavailable`, `unknown` and non-numeric states are
unobservable, not zero. `SuspendedEVSE` is the charger's own pilot (see `pilot_floor_probe`), not
the vehicle's failure, and a connector that reports `Charging` is the charger doing as told.

A charger with a power sensor and no status sensor (a dumb charger behind a smart plug) has no
connector status: it is judged by its power instead. The advisory is the same state, with the reason
`power_below_threshold`, once the power has stayed under the idle threshold (100 W unless set) for
`POWER_GRACE_PERIOD_S` (five minutes) while a charge is expected.

The grace timer (`GRACE_PERIOD_S`) is armed when the full predicate first becomes true, cancelled
the instant any fact stops holding or the subject changes, and publishes only if the same
subject/connector/plan still owns it. Nothing is persisted; a restart begins a new grace period.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN as ENTITY_STATE_UNKNOWN
from homeassistant.core import callback, HomeAssistant, State
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util


#: Nothing to say: charging is not expected, current is flowing, or a Start is unanswered.
STATE_NORMAL = "normal"
#: The observation held through its grace period.
STATE_VEHICLE_NOT_REQUESTING_CURRENT = "vehicle_not_requesting_current"
#: A charge is expected and the facts needed to judge it cannot be observed.
STATE_UNKNOWN = "unknown"

#: Every state this contract may carry, so a client decoder and this rule cannot drift.
CHARGE_PROGRESS_STATES = (STATE_NORMAL, STATE_VEHICLE_NOT_REQUESTING_CURRENT, STATE_UNKNOWN)

#: No charge is expected; `SuspendedEV` here is ordinary idle behaviour.
REASON_CHARGE_NOT_EXPECTED = "charge_not_expected"
#: A Start was accepted and the charger has not reported charging yet; not a failure.
REASON_START_PENDING = "start_pending"
#: The connector says it is charging, whatever the current reads (including zero under
#: load balancing).
REASON_CONNECTOR_CHARGING = "connector_charging"
#: Some other connector status (`SuspendedEVSE`, `Available`, `Finishing`, ...).
REASON_CONNECTOR_NOT_SUSPENDED_EV = "connector_not_suspended_ev"
#: The main state is a finite current above the near-zero threshold.
REASON_CURRENT_FLOWING = "current_flowing"
#: The connector's own status cannot be read.
REASON_CONNECTOR_STATUS_UNAVAILABLE = "connector_status_unavailable"
#: The current-import main state cannot be read as a finite number.
REASON_CURRENT_IMPORT_UNAVAILABLE = "current_import_unavailable"
#: The full predicate is true and the grace period has not run out yet.
REASON_SUSPENDED_EV_ZERO_CURRENT_PENDING = "suspended_ev_zero_current_pending"
#: The published advisory: started, `SuspendedEV`, no current, past the grace period.
REASON_SUSPENDED_EV_ZERO_CURRENT = "suspended_ev_zero_current"
#: A charger with a power sensor and no status sensor (a dumb charger behind a smart plug): the
#: power cannot be read.
REASON_POWER_UNAVAILABLE = "power_unavailable"
#: The power is above the idle threshold: the charger is drawing.
REASON_POWER_FLOWING = "power_flowing"
#: The power is below the idle threshold and the grace period has not run out yet.
REASON_POWER_BELOW_THRESHOLD_PENDING = "power_below_threshold_pending"
#: The published advisory: started, power below the threshold for the whole grace period.
REASON_POWER_BELOW_THRESHOLD = "power_below_threshold"

#: Every reason this contract may carry; a reason is always present.
CHARGE_PROGRESS_REASONS = (
    REASON_CHARGE_NOT_EXPECTED,
    REASON_START_PENDING,
    REASON_CONNECTOR_CHARGING,
    REASON_CONNECTOR_NOT_SUSPENDED_EV,
    REASON_CURRENT_FLOWING,
    REASON_CONNECTOR_STATUS_UNAVAILABLE,
    REASON_CURRENT_IMPORT_UNAVAILABLE,
    REASON_SUSPENDED_EV_ZERO_CURRENT_PENDING,
    REASON_SUSPENDED_EV_ZERO_CURRENT,
    REASON_POWER_UNAVAILABLE,
    REASON_POWER_FLOWING,
    REASON_POWER_BELOW_THRESHOLD_PENDING,
    REASON_POWER_BELOW_THRESHOLD,
)

#: OCPP's own spelling of the two statuses this rule names, compared exactly: an
#: unknown status is a false silence, never a wrong alarm.
SUSPENDED_EV = "SuspendedEV"
CHARGING = "Charging"

#: Above this many amperes the main state is a charge in progress; well below the
#: IEC 61851 6 A floor that `pilot_floor_probe` measures against.
ZERO_CURRENT_A = 0.5

#: How long the full predicate must hold before the advisory is published.
GRACE_PERIOD_S = 120.0

#: A power reading is noisier than a connector status, so "not drawing" must hold for five minutes.
POWER_GRACE_PERIOD_S = 300.0

#: How long an accepted Start may go unacknowledged before it stops counting as pending.
START_ACK_TIMEOUT_S = 30.0

#: Timer instructions one snapshot may produce; the rule decides, the observer owns the timer.
INSTRUCTION_CLEAR = "clear"
INSTRUCTION_START = "start"
INSTRUCTION_WAIT = "wait"
INSTRUCTION_PUBLISH = "publish"


@dataclass(frozen=True, slots=True)
class ChargeProgress:
    """One observation's published value: the `charge_progress` object.

    `state` is one of `CHARGE_PROGRESS_STATES` and `reason` one of `CHARGE_PROGRESS_REASONS`. `since` is
    when the current observation began, or `None`; never an entity id, transaction id or payload.
    """

    state: str
    reason: str
    since: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """The dashboard's `charge_progress` block."""
        return {
            "state": self.state,
            "reason": self.reason,
            "since": None if self.since is None else self.since.isoformat(),
        }


#: Reported when there is nothing to observe: no loaded controller.
NOT_OBSERVED = ChargeProgress(STATE_UNKNOWN, REASON_CONNECTOR_STATUS_UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class ChargeProgressFacts:
    """One captured instant, read once, in the shape the rule reads.

    A value rather than getters so a decision can never mix a fact read before an await with one read
    after it. `subject` identifies the observation (connector, plan, charge control) so a timer armed
    for one subject never publishes for another; it is internal and never published.
    """

    expected: bool
    start_pending: bool
    connector_status: str | None
    current_import_a: float | None
    subject: str
    #: A charger with a power sensor and no status sensor is judged by `power_w` against
    #: `idle_power_w` instead of by a connector status; `power_w` is `None` when unreadable.
    power_mode: bool = False
    power_w: float | None = None
    idle_power_w: float = 100.0

    @property
    def grace_period_s(self) -> float:
        return POWER_GRACE_PERIOD_S if self.power_mode else GRACE_PERIOD_S


@dataclass(frozen=True, slots=True)
class Observation:
    """What one instant instructs: an `INSTRUCTION_*`, the value it would publish, and the
    observation start it carries forward (`None` for clear).
    """

    instruction: str
    progress: ChargeProgress
    since: datetime | None


def connector_status(state: State | None) -> str | None:
    """One connector entity's status, or `None` when not observable."""
    if state is None:
        return None
    text = state.state
    if not isinstance(text, str) or text in (STATE_UNAVAILABLE, ENTITY_STATE_UNKNOWN):
        return None
    text = text.strip()
    return text or None


def connector_current(state: State | None) -> float | None:
    """One connector's current-import **main state** as a finite number, or `None`."""
    if state is None:
        return None
    text = state.state
    if not isinstance(text, str) or text in (STATE_UNAVAILABLE, ENTITY_STATE_UNKNOWN):
        return None
    try:
        value = float(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    return value


def decide(facts: ChargeProgressFacts) -> ChargeProgress:
    """The whole rule: one captured instant -> what is known about it."""
    if facts.start_pending:
        return ChargeProgress(STATE_NORMAL, REASON_START_PENDING)
    if not facts.expected:
        return ChargeProgress(STATE_NORMAL, REASON_CHARGE_NOT_EXPECTED)
    if facts.power_mode:
        if facts.power_w is None:
            return ChargeProgress(STATE_UNKNOWN, REASON_POWER_UNAVAILABLE)
        if facts.power_w >= facts.idle_power_w:
            return ChargeProgress(STATE_NORMAL, REASON_POWER_FLOWING)
        return ChargeProgress(STATE_VEHICLE_NOT_REQUESTING_CURRENT, REASON_POWER_BELOW_THRESHOLD)
    if facts.connector_status is None:
        return ChargeProgress(STATE_UNKNOWN, REASON_CONNECTOR_STATUS_UNAVAILABLE)
    if facts.current_import_a is None:
        return ChargeProgress(STATE_UNKNOWN, REASON_CURRENT_IMPORT_UNAVAILABLE)
    if facts.current_import_a > ZERO_CURRENT_A:
        return ChargeProgress(STATE_NORMAL, REASON_CURRENT_FLOWING)
    if facts.connector_status != SUSPENDED_EV:
        return ChargeProgress(
            STATE_NORMAL,
            REASON_CONNECTOR_CHARGING
            if facts.connector_status == CHARGING
            else REASON_CONNECTOR_NOT_SUSPENDED_EV,
        )
    return ChargeProgress(STATE_VEHICLE_NOT_REQUESTING_CURRENT, REASON_SUSPENDED_EV_ZERO_CURRENT)


def observe(
    facts: ChargeProgressFacts, since: datetime | None, now: datetime
) -> Observation:
    """The rule plus its timing: snapshot, observation start and `now` in, instruction out."""
    decision = decide(facts)
    if decision.state != STATE_VEHICLE_NOT_REQUESTING_CURRENT:
        # A fact that stops holding ends the observation and its grace period.
        return Observation(INSTRUCTION_CLEAR, decision, None)
    power = facts.power_mode
    pending_reason = REASON_POWER_BELOW_THRESHOLD_PENDING if power else REASON_SUSPENDED_EV_ZERO_CURRENT_PENDING
    if since is None:
        # The predicate just became true: the grace period starts now.
        pending = ChargeProgress(STATE_NORMAL, pending_reason, now)
        return Observation(INSTRUCTION_START, pending, now)
    if (now - since).total_seconds() >= facts.grace_period_s:
        advisory = ChargeProgress(STATE_VEHICLE_NOT_REQUESTING_CURRENT, decision.reason, since)
        return Observation(INSTRUCTION_PUBLISH, advisory, since)
    pending = ChargeProgress(STATE_NORMAL, pending_reason, since)
    return Observation(INSTRUCTION_WAIT, pending, since)


def _utcnow() -> datetime:
    """Home Assistant's clock, read here so a freeze is one patch."""
    return dt_util.utcnow()


class ChargeProgressHost(Protocol):
    """What the observer needs from its controller."""

    hass: HomeAssistant

    def charge_progress_facts(self) -> ChargeProgressFacts:
        """One captured instant: entity states and controller facts, read once."""

    def charge_progress_changed(self) -> None:
        """The published diagnostic moved: tell whoever reads it."""


class ChargeProgressObserver:
    """Owns the one grace timer, the published value, and the generation it belongs to.

    `evaluate()` captures the facts once, detects a subject change (which makes any old timer inert),
    asks `observe` what to do, and arms, keeps or cancels exactly one timer. It is a callback: no await,
    lock, entity write or service call. A timer publishes nothing if the subject moved, the observation
    was cleared, or `shutdown()` ran; shutdown is silent.
    """

    def __init__(
        self,
        host: ChargeProgressHost,
        *,
        now: Callable[[], datetime] | None = None,
        arm: Callable[[Callable[[datetime], None], datetime], Callable[[], None]] | None = None,
    ) -> None:
        self._host = host
        # Resolved at call time so a test that freezes `dt_util.utcnow` freezes this too.
        self._now = _utcnow if now is None else now
        self._arm = arm if arm is not None else self._arm_point_in_utc_time
        self._generation = 0
        self._subject: str | None = None
        self._since: datetime | None = None
        self._grace_s = GRACE_PERIOD_S
        self._cancel: Callable[[], None] | None = None
        self._current = NOT_OBSERVED
        self._shutdown = False

    def _arm_point_in_utc_time(
        self, action: Callable[[datetime], None], when: datetime
    ) -> Callable[[], None]:
        """The production timer, through Home Assistant's helper."""
        return async_track_point_in_utc_time(self._host.hass, action, when)

    @property
    def progress(self) -> ChargeProgress:
        """The value last published; `NOT_OBSERVED` until the first evaluation."""
        return self._current

    @property
    def generation(self) -> int:
        """How many times the observation's subject has changed."""
        return self._generation

    @callback
    def evaluate(self) -> bool:
        """Decide again from this instant's facts; report whether the value moved.

        Returns `False` after `shutdown()`.
        """
        if self._shutdown:
            return False
        facts = self._host.charge_progress_facts()
        if facts.subject != self._subject:
            # A new connector, plan or charge control: cancel any timer armed for the old one.
            self._generation += 1
            self._subject = facts.subject
            self._since = None
            self._cancel_timer()
        self._grace_s = facts.grace_period_s
        observation = observe(facts, self._since, self._now())
        self._since = observation.since
        if observation.instruction == INSTRUCTION_START:
            self._arm_grace_period()
        elif observation.instruction == INSTRUCTION_WAIT and self._cancel is None:
            # The timer fired but the clock says the grace period has not run out (clock step
            # or an early revalidation): keep exactly one timer, armed for when it is due.
            self._rearm_grace_period(observation.since)
        elif observation.instruction == INSTRUCTION_CLEAR:
            self._cancel_timer()
        elif observation.instruction == INSTRUCTION_PUBLISH:
            # Already past the grace period (a report arrived before the timer fired): drop the
            # timer, it has nothing left to decide.
            self._cancel_timer()
        if observation.progress == self._current:
            return False
        self._current = observation.progress
        return True

    def _arm_grace_period(self) -> None:
        """Arm the one grace timer for the observation that has just started."""
        self._cancel_timer()
        self._rearm_grace_period(self._since)

    def _rearm_grace_period(self, since: datetime | None) -> None:
        if since is None:
            return
        token = self._generation

        @callback
        def fired(_now: datetime) -> None:
            self._grace_period_expired(token)

        self._cancel = self._arm(fired, since + timedelta(seconds=self._grace_s))

    @callback
    def _grace_period_expired(self, token: int) -> None:
        """Only the observation that armed the timer may publish."""
        if self._shutdown or token != self._generation:
            return
        self._cancel = None
        # Revalidate all facts rather than reuse the ones that armed the timer: the vehicle
        # may have begun drawing, or the connector moved to `Charging`, while it waited.
        if self.evaluate():
            self._host.charge_progress_changed()

    def _cancel_timer(self) -> None:
        cancel = self._cancel
        self._cancel = None
        if cancel is not None:
            cancel()

    @callback
    def shutdown(self) -> None:
        """Cancel terminally: nothing may publish once the controller is gone."""
        self._shutdown = True
        self._generation += 1
        self._subject = None
        self._since = None
        self._cancel_timer()
