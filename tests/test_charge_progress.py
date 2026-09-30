"""The vehicle-side observation: one passive rule, one timer, and nothing else.

The failure mode covered is a connector that accepts Start, has a transaction, and stays
`SuspendedEV` at 0 A. The feature is *observation* only: one captured instant, one pure rule, one
grace period and a value -- never a retry, reset, profile clear or command.

Tests run on a hand-moved clock and a recording scheduler, so none sleeps. The last section runs the
real integration with invented OCPP entities and mocked switch services, so any call to a charger
would be visible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution import charge_progress as progress
from custom_components.spotnav.execution.charge_progress import (
    CHARGING,
    CHARGE_PROGRESS_REASONS,
    CHARGE_PROGRESS_STATES,
    GRACE_PERIOD_S,
    INSTRUCTION_CLEAR,
    INSTRUCTION_PUBLISH,
    INSTRUCTION_START,
    INSTRUCTION_WAIT,
    NOT_OBSERVED,
    REASON_CHARGE_NOT_EXPECTED,
    REASON_CONNECTOR_CHARGING,
    REASON_CONNECTOR_NOT_SUSPENDED_EV,
    REASON_CONNECTOR_STATUS_UNAVAILABLE,
    REASON_CURRENT_FLOWING,
    REASON_CURRENT_IMPORT_UNAVAILABLE,
    REASON_START_PENDING,
    REASON_SUSPENDED_EV_ZERO_CURRENT,
    REASON_SUSPENDED_EV_ZERO_CURRENT_PENDING,
    START_ACK_TIMEOUT_S,
    STATE_NORMAL,
    STATE_UNKNOWN,
    STATE_VEHICLE_NOT_REQUESTING_CURRENT,
    SUSPENDED_EV,
    ChargeProgress,
    ChargeProgressFacts,
    ChargeProgressObserver,
    decide,
    observe,
)
from custom_components.spotnav.const import CURRENT_CONTROL_CHANGE_CONFIGURATION

from .helpers import make_entry, webhook_dashboard

#: The invented charge point and connector every connector-scoped entity below belongs to. §1 of the
#: plan forbids the real installation's identifiers in this repository, so these are made up -- and
#: shaped exactly like the installed OCPP 0.12.0's own entities (`ocpp_identity` reads the same
#: shapes).
CPID = "picasso"
CONNECTOR = 1
STATUS_ENTITY = f"sensor.{CPID}_connector_{CONNECTOR}_status_connector"
CURRENT_ENTITY = f"sensor.{CPID}_connector_{CONNECTOR}_current_import"
CURRENT_LIMIT = f"number.{CPID}_connector_{CONNECTOR}_session_current_limit"

#: The instant every test's own clock starts at.
START = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


class Clock:
    """A clock the test advances by hand, so the grace period costs no wall time."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


class Scheduler:
    """The one timer the observer may arm, recorded rather than waited for.

    Two entry points, because the observer has two ways to reach a timer: the `arm` callable it was
    built with (the unit tests' seam) and Home Assistant's own `async_track_point_in_utc_time`
    (the module-level seam the integration tests patch). Both record here, so "at most one timer"
    can be asserted whichever path a test drives.
    """

    def __init__(self) -> None:
        self.armed: list[dict[str, Any]] = []

    def record(
        self, action: Callable[[datetime], None], when: datetime
    ) -> Callable[[], None]:
        """Record one armed point-in-time callback, and return its cancel handle."""
        entry: dict[str, Any] = {"action": action, "when": when, "cancelled": False}
        self.armed.append(entry)

        def cancel() -> None:
            entry["cancelled"] = True

        return cancel

    def __call__(self, action: Callable[[datetime], None], when: datetime) -> Callable[[], None]:
        return self.record(action, when)

    def for_home_assistant(
        self, _hass: Any, action: Callable[[datetime], None], when: datetime
    ) -> Callable[[], None]:
        """The module-level seam: `async_track_point_in_utc_time(hass, action, when)`."""
        return self.record(action, when)

    @property
    def pending(self) -> list[dict[str, Any]]:
        """Every timer that is still armed, in the order it was armed."""
        return [entry for entry in self.armed if not entry["cancelled"]]

    def fire(self, entry: dict[str, Any] | None = None) -> None:
        """Fire an armed timer, as Home Assistant's own callback would.

        `entry` names a specific one when a test has moved on to a newer observation and needs to
        fire the older timer anyway -- which is exactly the case "an obsolete callback publishes
        nothing" is about. A point-in-time timer is gone the moment it fires, so firing marks it
        consumed: that is what `async_track_point_in_utc_time` does, and recording it keeps "at most
        one timer" an assertion rather than a hope.
        """
        target = self.pending[-1] if entry is None else entry
        target["cancelled"] = True
        target["action"](target["when"])


class Host:
    """A capture that returns whatever the test last set, and counts what it was told."""

    def __init__(self, captured: ChargeProgressFacts) -> None:
        self.facts = captured
        self.changes = 0
        self.hass: Any = None

    def charge_progress_facts(self) -> ChargeProgressFacts:
        return self.facts

    def charge_progress_changed(self) -> None:
        self.changes += 1


def facts(
    *,
    expected: bool = True,
    start_pending: bool = False,
    connector_status: str | None = SUSPENDED_EV,
    current_import_a: float | None = 0.0,
    subject: str = "target-1",
) -> ChargeProgressFacts:
    """One captured instant, in the shape the rule reads."""
    return ChargeProgressFacts(
        expected=expected,
        start_pending=start_pending,
        connector_status=connector_status,
        current_import_a=current_import_a,
        subject=subject,
    )


def observing(
    captured: ChargeProgressFacts | None = None,
) -> tuple[ChargeProgressObserver, Host, Clock, Scheduler]:
    """An observer over one captured instant, with its clock and scheduler in the test's hands."""
    host = Host(captured if captured is not None else facts())
    clock = Clock()
    scheduler = Scheduler()
    observer = ChargeProgressObserver(host, now=clock, arm=scheduler)
    return observer, host, clock, scheduler


def state_of(**kwargs: Any) -> ChargeProgress:
    """`decide` over one captured instant, for the cases that need no lifecycle at all."""
    return decide(facts(**kwargs))


def published(
    observer: ChargeProgressObserver, clock: Clock, scheduler: Scheduler
) -> None:
    """Drive one observation through its grace period, the way the controller's own path does.

    The first evaluation is what begins the observation (the predicate has just become true), the
    clock then stands past the grace period, and the timer fires -- no test ever waits.
    """
    assert observer.evaluate() is True, "the observation begins here"
    clock.advance(GRACE_PERIOD_S)
    scheduler.fire()
    assert observer.progress.state == STATE_VEHICLE_NOT_REQUESTING_CURRENT


def state(value: str, attributes: dict[str, Any] | None = None) -> Any:
    """One plain Home Assistant state, built without a running instance."""
    from homeassistant.core import State

    return State("sensor.invented", value, dict(attributes or {}))


# --------------------------------------------------------------- the frozen vocabulary


def test_the_vocabulary_is_exactly_the_three_states_and_the_nine_reasons() -> None:
    """The wire vocabulary is frozen here, once: a client's decoder is written against this list.

    Both clients switch on these strings, and the card shows the advisory's own code as subdued
    detail, so a rename or an added value is a contract change rather than a refactor.
    """
    assert CHARGE_PROGRESS_STATES == ("normal", "vehicle_not_requesting_current", "unknown")
    assert CHARGE_PROGRESS_REASONS == (
        "charge_not_expected",
        "start_pending",
        "connector_charging",
        "connector_not_suspended_ev",
        "current_flowing",
        "connector_status_unavailable",
        "current_import_unavailable",
        "suspended_ev_zero_current_pending",
        "suspended_ev_zero_current",
    )


def test_the_published_value_has_exactly_three_keys_and_no_identifiers() -> None:
    """`charge_progress` is these three keys, and carries no entity, device or transaction id."""
    advisory = state_of()
    fields = advisory.as_dict()
    assert set(fields) == {"state", "reason", "since"}
    assert fields["since"] is None, "a decision alone claims no observation"

    with_since = ChargeProgress(advisory.state, advisory.reason, START).as_dict()
    assert with_since["since"] == START.isoformat()
    serialized = json.dumps(with_since)
    for private in (CPID, STATUS_ENTITY, CURRENT_ENTITY, "connector_1", "transaction"):
        assert private not in serialized


@pytest.mark.parametrize(
    ("captured", "expected_state", "expected_reason"),
    [
        # A Start that has been accepted and not answered: not a failure, and never the advisory.
        (facts(start_pending=True), STATE_NORMAL, REASON_START_PENDING),
        # Nothing is expected: `SuspendedEV` here is ordinary idle behaviour.
        (
            facts(expected=False, connector_status=SUSPENDED_EV, current_import_a=0.0),
            STATE_NORMAL,
            REASON_CHARGE_NOT_EXPECTED,
        ),
        # The charger says it is charging -- whatever the current reads, including zero under a
        # load-balancing allocation, which is not the vehicle's refusal.
        (
            facts(connector_status=CHARGING, current_import_a=0.0),
            STATE_NORMAL,
            REASON_CONNECTOR_CHARGING,
        ),
        # The EVSE side's own suspended state, which `pilot_floor_probe` cares about and the vehicle
        # does not answer for.
        (
            facts(connector_status="SuspendedEVSE", current_import_a=0.0),
            STATE_NORMAL,
            REASON_CONNECTOR_NOT_SUSPENDED_EV,
        ),
        # Any other status the vocabulary does not name: a false silence, never a wrong alarm.
        (
            facts(connector_status="Available", current_import_a=0.0),
            STATE_NORMAL,
            REASON_CONNECTOR_NOT_SUSPENDED_EV,
        ),
        # Current is flowing: whatever the connector's own status says, there is nothing to report.
        (
            facts(connector_status=SUSPENDED_EV, current_import_a=6.2),
            STATE_NORMAL,
            REASON_CURRENT_FLOWING,
        ),
        (
            facts(connector_status=CHARGING, current_import_a=16.0),
            STATE_NORMAL,
            REASON_CURRENT_FLOWING,
        ),
        # Unobservable, not zero: no status entity, or one that is `unavailable`.
        (facts(connector_status=None), STATE_UNKNOWN, REASON_CONNECTOR_STATUS_UNAVAILABLE),
        # Unobservable, not zero: an `unavailable` current is not an unrequested current.
        (facts(current_import_a=None), STATE_UNKNOWN, REASON_CURRENT_IMPORT_UNAVAILABLE),
        # The one coherent snapshot the advisory is about.
        (
            facts(connector_status=SUSPENDED_EV, current_import_a=0.0),
            STATE_VEHICLE_NOT_REQUESTING_CURRENT,
            REASON_SUSPENDED_EV_ZERO_CURRENT,
        ),
        # A trickle below half an amp is still "not requesting current".
        (
            facts(connector_status=SUSPENDED_EV, current_import_a=0.3),
            STATE_VEHICLE_NOT_REQUESTING_CURRENT,
            REASON_SUSPENDED_EV_ZERO_CURRENT,
        ),
    ],
)
def test_the_rule_answers_every_combination_this_product_has(
    captured: ChargeProgressFacts, expected_state: str, expected_reason: str
) -> None:
    """Every row of the matrix, and the order the reasons are reported in."""
    decision = decide(captured)
    assert (decision.state, decision.reason) == (expected_state, expected_reason)
    assert decision.since is None, "a decision alone never claims an observation"
    assert decision.state in CHARGE_PROGRESS_STATES
    assert decision.reason in CHARGE_PROGRESS_REASONS


def test_a_pending_start_outranks_every_other_reading() -> None:
    """The first condition reported is the first that applies: an unanswered Start hides the rest."""
    assert decide(
        facts(start_pending=True, connector_status=None, current_import_a=None)
    ).reason == REASON_START_PENDING


def test_the_unobservable_reasons_are_reported_in_a_fixed_order() -> None:
    """With both facts missing, the connector's own status is the one named."""
    assert decide(facts(connector_status=None, current_import_a=None)).reason == (
        REASON_CONNECTOR_STATUS_UNAVAILABLE
    )


# ------------------------------------------------------------------ reading the states


def test_the_status_reader_never_reads_anything_but_the_state() -> None:
    """A connector status is its state; an `unavailable` or absent one is not observable."""
    assert progress.connector_status(None) is None
    assert progress.connector_status(state("unavailable")) is None
    assert progress.connector_status(state("unknown")) is None
    assert progress.connector_status(state("  ")) is None
    assert progress.connector_status(state("SuspendedEV")) == SUSPENDED_EV
    assert progress.connector_status(state("SuspendedEV", {"l1": 6.0})) == SUSPENDED_EV


def test_the_current_reader_reads_the_main_state_and_never_the_phase_attributes() -> None:
    """The installation's own behaviour, pinned: stale `L1/L2/L3` beside a main state of `0`.

    Those attributes keep their last non-zero samples after the main state has already gone back to
    zero, so a rule that read one would report current that is not flowing -- and would silence the
    one diagnostic this module exists for.
    """
    stale = state("0", {"l1": 6.1, "l2": 6.0, "l3": 0.0})
    assert progress.connector_current(stale) == 0.0
    assert progress.connector_current(state("6.2", {"l1": 0.0})) == 6.2


@pytest.mark.parametrize(
    "text", ["unavailable", "unknown", "", "not-a-number", "nan", "inf", "-inf"]
)
def test_an_unusable_current_never_becomes_a_number(text: str) -> None:
    """`unavailable`, `unknown` and nonsense are *unobservable* -- never `0.0`, never a value."""
    assert progress.connector_current(state(text)) is None


def test_stale_phases_beside_a_zero_main_state_still_qualify_through_the_readers() -> None:
    """The whole predicate, read through the readers, with the stale attributes present."""
    captured = facts(
        connector_status=progress.connector_status(state("SuspendedEV")),
        current_import_a=progress.connector_current(
            state("0", {"l1": 6.1, "l2": 6.0, "l3": 6.0})
        ),
    )
    assert decide(captured).state == STATE_VEHICLE_NOT_REQUESTING_CURRENT


# ------------------------------------------------------------ the pure timing instruction


def test_the_instruction_starts_waits_and_publishes_on_the_grace_period() -> None:
    """One captured instant, the observation start and `now`: the whole timing rule."""
    start = observe(facts(), None, START)
    assert start.instruction == INSTRUCTION_START
    assert start.since == START
    assert start.progress == ChargeProgress(
        STATE_NORMAL, REASON_SUSPENDED_EV_ZERO_CURRENT_PENDING, START
    )

    almost = observe(facts(), START, START + timedelta(seconds=GRACE_PERIOD_S - 0.5))
    assert almost.instruction == INSTRUCTION_WAIT
    assert almost.since == START, "the observation keeps the instant it began at"
    assert almost.progress.reason == REASON_SUSPENDED_EV_ZERO_CURRENT_PENDING

    due = observe(facts(), START, START + timedelta(seconds=GRACE_PERIOD_S))
    assert due.instruction == INSTRUCTION_PUBLISH
    assert due.since == START
    assert due.progress == ChargeProgress(
        STATE_VEHICLE_NOT_REQUESTING_CURRENT, REASON_SUSPENDED_EV_ZERO_CURRENT, START
    )


def test_a_fact_that_stops_holding_clears_the_observation_and_its_start() -> None:
    """Whatever ends it -- current, status, a pending Start -- the observation is over at once."""
    for captured in (
        facts(current_import_a=6.2),
        facts(connector_status=CHARGING),
        facts(connector_status="SuspendedEVSE"),
        facts(expected=False),
        facts(start_pending=True),
    ):
        cleared = observe(captured, START, START + timedelta(seconds=GRACE_PERIOD_S * 10))
        assert cleared.instruction == INSTRUCTION_CLEAR
        assert cleared.since is None, "a cleared observation carries no start forward"


def test_a_clock_that_has_not_moved_forward_never_publishes_early() -> None:
    """A wall-clock step backwards leaves the observation waiting rather than publishing."""
    early = observe(facts(), START, START - timedelta(seconds=1))
    assert early.instruction == INSTRUCTION_WAIT
    assert early.since == START


# ------------------------------------------------------------------- the observer itself


def test_the_advisory_is_published_once_the_grace_period_has_run_out() -> None:
    """Less than the grace period says nothing; through it, the advisory -- once."""
    observer, host, clock, scheduler = observing()
    assert observer.progress == NOT_OBSERVED

    assert observer.evaluate() is True, "the observation began, so the value moved"
    assert observer.progress.reason == REASON_SUSPENDED_EV_ZERO_CURRENT_PENDING
    assert len(scheduler.pending) == 1, "exactly one timer, armed for the grace period"
    assert scheduler.pending[0]["when"] == START + timedelta(seconds=GRACE_PERIOD_S)
    assert host.changes == 0, "nothing is published to a reader yet"

    clock.advance(GRACE_PERIOD_S - 1)
    assert observer.evaluate() is False, "still waiting: the value did not move"
    assert observer.progress.state == STATE_NORMAL
    assert host.changes == 0

    clock.advance(1)
    scheduler.fire()
    assert observer.progress == ChargeProgress(
        STATE_VEHICLE_NOT_REQUESTING_CURRENT, REASON_SUSPENDED_EV_ZERO_CURRENT, START
    )
    assert host.changes == 1, "the reader is told exactly once"
    assert scheduler.pending == [], "the timer that just fired is no longer armed"
    assert observer.evaluate() is False, "and no second timer was armed for it"
    assert scheduler.pending == []


def test_current_flowing_clears_the_advisory_immediately() -> None:
    """A vehicle that starts drawing is answered on the spot, not after another grace period."""
    observer, host, clock, scheduler = observing()
    published(observer, clock, scheduler)

    host.facts = facts(current_import_a=6.1)
    assert observer.evaluate() is True
    assert observer.progress == ChargeProgress(STATE_NORMAL, REASON_CURRENT_FLOWING)
    assert scheduler.pending == [], "and nothing is left armed behind it"


def test_a_charging_connector_clears_the_advisory_immediately() -> None:
    """`Charging` is the charger's own statement, and it ends the vehicle-side question at once."""
    observer, host, clock, scheduler = observing()
    published(observer, clock, scheduler)

    host.facts = facts(connector_status=CHARGING, current_import_a=0.0)
    assert observer.evaluate() is True
    assert observer.progress == ChargeProgress(STATE_NORMAL, REASON_CONNECTOR_CHARGING)
    assert scheduler.pending == []


def test_suspended_evse_never_produces_the_vehicle_advisory() -> None:
    """The EVSE side's own suspended state: no timer is ever armed for it."""
    observer, _host, clock, scheduler = observing(facts(connector_status="SuspendedEVSE"))
    clock.advance(GRACE_PERIOD_S * 4)

    assert observer.evaluate() is True
    assert observer.progress == ChargeProgress(STATE_NORMAL, REASON_CONNECTOR_NOT_SUSPENDED_EV)
    assert scheduler.armed == [], "nothing to wait for: this is not the vehicle's state"


def test_suspended_ev_outside_an_expected_charge_is_ordinary_idle_behaviour() -> None:
    """No charge expected: `SuspendedEV` is normal, and produces no observation at all."""
    observer, _host, clock, scheduler = observing(facts(expected=False))
    clock.advance(GRACE_PERIOD_S * 4)

    assert observer.evaluate() is True
    assert observer.progress == ChargeProgress(STATE_NORMAL, REASON_CHARGE_NOT_EXPECTED)
    assert scheduler.armed == []


def test_a_pending_start_does_not_warn_and_an_accepted_start_begins_observation() -> None:
    """A Start nobody has answered yet is not a failure; the acknowledgement starts the clock."""
    observer, host, _clock, scheduler = observing(facts(start_pending=True))

    assert observer.evaluate() is True
    assert observer.progress == ChargeProgress(STATE_NORMAL, REASON_START_PENDING)
    assert scheduler.armed == [], "an unanswered command is not observed at all"

    host.facts = facts(start_pending=False)
    assert observer.evaluate() is True
    assert observer.progress.reason == REASON_SUSPENDED_EV_ZERO_CURRENT_PENDING
    assert len(scheduler.pending) == 1, "the accepted Start begins the grace period"


def test_an_unreadable_current_is_unknown_and_never_a_zero() -> None:
    """The whole observer, over an `unavailable` current: `unknown`, and no timer."""
    observer, _host, _clock, scheduler = observing(facts(current_import_a=None))

    assert observer.evaluate() is True
    assert observer.progress == ChargeProgress(STATE_UNKNOWN, REASON_CURRENT_IMPORT_UNAVAILABLE)
    assert scheduler.armed == []


def test_cancelling_the_schedule_clears_the_advisory_and_cancels_the_timer() -> None:
    """A plan that is cancelled, paused or over ends the observation at once."""
    observer, host, _clock, scheduler = observing()
    assert observer.evaluate() is True
    armed = scheduler.pending[-1]

    host.facts = facts(expected=False)
    assert observer.evaluate() is True
    assert observer.progress == ChargeProgress(STATE_NORMAL, REASON_CHARGE_NOT_EXPECTED)
    assert armed["cancelled"] is True, "the grace timer is cancelled, not merely ignored"
    assert scheduler.pending == []


def test_a_changed_subject_makes_the_old_timer_inert() -> None:
    """A new plan identity is a new observation: the old timer can publish nothing for it."""
    observer, host, clock, scheduler = observing()
    assert observer.evaluate() is True
    old = scheduler.pending[-1]
    first_generation = observer.generation

    clock.advance(30)
    host.facts = replace(host.facts, subject="target-2")
    assert observer.evaluate() is True, "the new observation's own start moved the value"
    assert observer.generation == first_generation + 1
    assert old["cancelled"] is True, "the superseded timer is cancelled as well as made inert"
    assert observer.progress.since == START + timedelta(seconds=30)
    assert len(scheduler.pending) == 1, "and exactly one timer belongs to the new subject"

    # Even if the superseded callback still ran -- a cancel that raced the loop -- it publishes
    # nothing at all, because it no longer owns the observation it was armed for.
    before = observer.progress
    clock.advance(GRACE_PERIOD_S)
    scheduler.fire(old)
    assert observer.progress == before
    assert host.changes == 0

    scheduler.fire()
    assert observer.progress == ChargeProgress(
        STATE_VEHICLE_NOT_REQUESTING_CURRENT,
        REASON_SUSPENDED_EV_ZERO_CURRENT,
        START + timedelta(seconds=30),
    )
    assert host.changes == 1


def test_an_unload_during_the_grace_period_publishes_nothing() -> None:
    """Shutdown is terminal: no value, no timer, and no late callback that restores one."""
    observer, host, clock, scheduler = observing()
    assert observer.evaluate() is True
    armed = scheduler.pending[-1]

    observer.shutdown()
    assert host.changes == 0, "an unload publishes nothing"
    assert scheduler.pending == []
    assert observer.evaluate() is False, "and nothing is decided after it either"

    clock.advance(GRACE_PERIOD_S * 4)
    scheduler.fire(armed)
    assert host.changes == 0
    assert observer.progress.state != STATE_VEHICLE_NOT_REQUESTING_CURRENT


def test_a_report_after_the_due_instant_publishes_and_drops_the_timer() -> None:
    """An evaluation that already stands past the grace period publishes, and leaves nothing armed."""
    observer, _host, clock, scheduler = observing()
    assert observer.evaluate() is True

    # No timer fires: a state report arrives after the grace period has already elapsed.
    clock.advance(GRACE_PERIOD_S + 5)
    assert observer.evaluate() is True
    assert observer.progress.state == STATE_VEHICLE_NOT_REQUESTING_CURRENT
    assert scheduler.pending == [], "the timer had nothing left to decide"


def test_two_chargers_never_share_a_timer_or_an_advisory() -> None:
    """Two connectors, two observers: one charger's grace period is not another's."""
    first, first_host, first_clock, first_scheduler = observing(facts(subject="charger-a"))
    second, second_host, _clock, second_scheduler = observing(
        facts(subject="charger-b", connector_status=CHARGING, current_import_a=0.0)
    )

    published(first, first_clock, first_scheduler)

    assert first.progress.state == STATE_VEHICLE_NOT_REQUESTING_CURRENT
    assert second.progress == NOT_OBSERVED, "the other observer ran nothing of its own"
    assert second_scheduler.armed == []
    assert second.evaluate() is True
    assert second.progress == ChargeProgress(STATE_NORMAL, REASON_CONNECTOR_CHARGING)
    assert first_host.changes == 1 and second_host.changes == 0


# ------------------------------------------------------------- the real integration path


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Freeze Home Assistant's clock, as this suite's other time-dependent tests do."""
    frozen = Clock()
    monkeypatch.setattr(dt_util, "utcnow", frozen)
    return frozen


@pytest.fixture
def timers(monkeypatch: pytest.MonkeyPatch) -> Scheduler:
    """Record the grace timer instead of waiting for it.

    The module-level seam rather than an injected argument on purpose: the controller builds the
    observer, so a test that drives the real controller has nothing to inject into.
    """
    scheduler = Scheduler()
    monkeypatch.setattr(progress, "async_track_point_in_utc_time", scheduler.for_home_assistant)
    return scheduler


async def charger_with_connector(hass: HomeAssistant) -> Any:
    """One real charger entry, with the invented OCPP connector roles it is observed through.

    The entry stores its explicit connector target, and a target is what makes the connector's status
    and current-import sensors readable at all.
    """
    hass.states.async_set("switch.charger_a", "off")
    hass.states.async_set(CURRENT_LIMIT, "16", {"min": 6, "max": 16, "unit_of_measurement": "A"})
    entry = make_entry(
        hass,
        entry_id="entry_a",
        charge_control="switch.charger_a",
        current_limit=CURRENT_LIMIT,
        webhook_id="webhook-a",
        title="Charger A",
        current_control=CURRENT_CONTROL_CHANGE_CONFIGURATION,
        ocpp_target=(CPID, CONNECTOR),
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def status_of(client: Any, webhook_id: str = "webhook-a") -> dict[str, Any]:
    """One dashboard answer, asserted to be an answer."""
    return await webhook_dashboard(client, webhook_id)


def assert_never_commanded(calls: list[list[Any]]) -> None:
    """Every charger-facing service this integration can reach is still untouched."""
    assert all(call == [] for call in calls), [call for call in calls if call]


async def test_the_dashboard_publishes_the_advisory_only_after_the_grace_period(
    hass: HomeAssistant, hass_client_no_auth, clock: Clock, timers: Scheduler
) -> None:
    """The whole path: real controller, real webhook, one grace period, and nothing commanded.

    This is the 2026-09-27 shape -- a charge SpotNav expects, a connector suspended at 0 A with
    stale per-phase samples beside it -- and the answer the product must give: not yet, then "the
    vehicle is not requesting current", then nothing again the moment current flows.
    """
    await charger_with_connector(hass)
    # Every service a mistake could reach. The diagnostic must leave all of them empty.
    on_calls = async_mock_service(hass, "switch", "turn_on")
    off_calls = async_mock_service(hass, "switch", "turn_off")
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    get_configuration_calls = async_mock_service(hass, "ocpp", "get_configuration")
    set_value_calls = async_mock_service(hass, "number", "set_value")

    hass.states.async_set(STATUS_ENTITY, SUSPENDED_EV)
    # The installation's own behaviour: the main state is `0` while the phase attributes still
    # carry their last non-zero samples.
    hass.states.async_set(CURRENT_ENTITY, "0", {"l1": 6.1, "l2": 6.0, "l3": 6.0})
    # And the charge is expected, because the charge control says so.
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    pending = await status_of(client)
    assert pending["api_version"] == 1
    assert pending["charge_progress"] == {
        "state": "normal",
        "reason": "suspended_ev_zero_current_pending",
        "since": START.isoformat(),
    }
    assert len(timers.pending) == 1
    assert timers.pending[0]["when"] == START + timedelta(seconds=GRACE_PERIOD_S)

    # Inside the grace period: the same coherent facts, and still nothing to report.
    clock.advance(GRACE_PERIOD_S - 1)
    hass.states.async_set(CURRENT_ENTITY, "0", {"l1": 6.1})
    await hass.async_block_till_done()
    almost = await status_of(client)
    assert almost["charge_progress"]["state"] == "normal"

    clock.advance(1)
    timers.fire()
    await hass.async_block_till_done()

    advisory = await status_of(client)
    assert advisory["charge_progress"] == {
        "state": "vehicle_not_requesting_current",
        "reason": "suspended_ev_zero_current",
        "since": START.isoformat(),
    }

    # Current arrives: the advisory clears on the spot, and no grace period is re-run.
    hass.states.async_set(CURRENT_ENTITY, "6.2")
    await hass.async_block_till_done()
    cleared = await status_of(client)
    assert cleared["charge_progress"] == {
        "state": "normal",
        "reason": "current_flowing",
        "since": None,
    }
    assert timers.pending == []

    assert_never_commanded(
        [on_calls, off_calls, configure_calls, get_configuration_calls, set_value_calls]
    )


async def test_a_start_the_charger_never_answers_is_never_the_vehicle_s_failure(
    hass: HomeAssistant, hass_client_no_auth, clock: Clock, timers: Scheduler
) -> None:
    """A pending Start is not the advisory, and neither is a command nothing ever answered.

    The charger's own acknowledgement is what starts the observation: while a Start is unanswered
    the honest answer is "pending", and once the bounded acknowledgement has expired the honest
    answer is that no charge is expected -- never that the vehicle refused one.
    """
    await charger_with_connector(hass)
    async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set(STATUS_ENTITY, SUSPENDED_EV)
    hass.states.async_set(CURRENT_ENTITY, "0")
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    started = await client.post(
        "/api/webhook/webhook-a", json={"version": 1, "action": "start", "amps": 10}
    )
    assert started.status == 200
    await hass.async_block_till_done()

    pending = await status_of(client)
    assert pending["charge_progress"] == {
        "state": "normal",
        "reason": "start_pending",
        "since": None,
    }
    assert timers.armed == [], "an unanswered command is not observed"

    # The charger never reports itself on, and the bounded acknowledgement runs out. The new status
    # is not the advisory: nothing was asked of a vehicle, because nothing reached the charger.
    clock.advance(START_ACK_TIMEOUT_S + 1)
    hass.states.async_set(STATUS_ENTITY, SUSPENDED_EV, {"reported_again": True})
    await hass.async_block_till_done()

    expired = await status_of(client)
    assert expired["charge_progress"] == {
        "state": "normal",
        "reason": "charge_not_expected",
        "since": None,
    }
    assert timers.armed == []


# --------------------------------------------------------------------- the source audit


async def test_the_real_grace_timer_fires_on_the_event_loop(hass: HomeAssistant) -> None:
    """The production timer path (no injected `arm`) must run its callback on the loop.

    An undecorated function handed to `async_track_point_in_utc_time` is run by Home Assistant in
    a worker thread, where the publish reached `async_write_ha_state` off the loop.
    """
    import threading

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    loop_thread = threading.get_ident()
    seen: list[int] = []

    class ThreadHost(Host):
        def charge_progress_changed(self) -> None:
            seen.append(threading.get_ident())
            super().charge_progress_changed()

    host = ThreadHost(facts())
    host.hass = hass
    start = dt_util.utcnow()
    clock = Clock(start)
    observer = ChargeProgressObserver(host, now=clock)

    observer.evaluate()
    clock.advance(seconds=progress.GRACE_PERIOD_S + 1)
    async_fire_time_changed(hass, start + timedelta(seconds=progress.GRACE_PERIOD_S + 1))
    await hass.async_block_till_done()

    assert host.changes == 1, "the grace timer fired and published"
    assert seen == [loop_thread], "and it did so on the event loop, not in a worker thread"
