"""The typed manual pause: admission, expiry, restart and storage.

The pause is one persisted intent rather than three booleans, so these tests are about *time*: the
choice is resolved to one absolute instant when it is admitted, expiry is a comparison against the
clock the execution boundary already owns, and an already-expired pause is settled exactly once.
Nothing here sleeps: the session's clock and scheduler are injected, and the one Home Assistant
point-in-time appointment is recorded instead of waited for.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution.auto_execution import (
    EXECUTION_ERROR,
    EXECUTION_PAUSE_CLEAR_FAILED,
    EXECUTION_PAUSE_STOP_FAILED,
    EXECUTION_PAUSED,
    EXECUTION_RECONCILE_FAILED,
    PAUSE_RETRY_DELAY,
    AutoExecutor,
    PendingApplication,
    application_for,
)
from custom_components.spotnav.planning.auto_settings import (
    PAUSE_NEXT_PERIOD,
    PAUSE_UNTIL_RESUMED,
    PAUSE_UNTIL_TOMORROW,
    AutoSettings,
    AutoSettingsError,
    AutoSettingsStore,
    PauseIntent,
)

from .relay import SE4, serve, serve_index
from .harness import Session
from .harness import FlakyStore
from .relay import Clock
from .relay import TODAY, TOMORROW
from .harness import RecordedAppointments


@pytest.fixture(autouse=True)
def frozen_real_clock(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """Pin Home Assistant's own clock to the harness clock for this module.

    The harness injects its clock into the repository, the manager, the preview and the execution
    boundary, but the *controller's* shared plan rules and a pause's `until tomorrow` both compare
    against `dt_util.utcnow()` too. With a fixed harness instant and a real clock that keeps moving,
    a suite passes in the morning and fails in the afternoon; pinning the real clock to the same
    instant makes every test here deterministic without weakening a single assertion.
    """
    now = clock()

    def frozen(*_args: Any, **_kwargs: Any) -> datetime:
        return now

    monkeypatch.setattr(dt_util, "utcnow", frozen)


async def _auto_with_a_plan(session: Session) -> Any:
    """One charger in Auto, with prices served, a plan calculated and installed."""
    serve(session.transport)
    snapshot = await session.set_auto()
    assert session.executor.applied is not None, "the fixture must install a plan first"
    return snapshot


# ------------------------------------------------------------------- admission


async def test_each_choice_resolves_to_one_instant_at_admission(
    session: Session, clock: Clock, pause_appointments: RecordedAppointments
) -> None:
    """The three choices are one intent with three different ends, each resolved once."""
    snapshot = await _auto_with_a_plan(session)
    plan = session.controller.plan
    assert plan is not None and plan.windows, "a plan with periods is what next_period needs"

    # (a) until the next planned period: the plan's own next start, never a guessed horizon.
    await session.preview.async_pause(PAUSE_NEXT_PERIOD)
    paused = session.settings().pause
    assert paused.choice == PAUSE_NEXT_PERIOD
    assert paused.admitted_at == clock.now
    assert paused.expires_at == min(start for start, _ in plan.windows if start > clock.now)
    assert pause_appointments.when == paused.expires_at, "one appointment, at the stored instant"

    # (b) until tomorrow: the next local midnight in the *market's* zone.
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    paused = session.settings().pause
    zone = dt_util.get_time_zone(snapshot.timezone)
    assert zone is not None, "the fixture's area must state a zone for this to mean anything"
    expected = datetime.combine(
        clock.now.astimezone(zone).date() + timedelta(days=1), time(0, 0), tzinfo=zone
    )
    assert paused.choice == PAUSE_UNTIL_TOMORROW
    assert paused.expires_at == expected.astimezone(timezone.utc)
    assert pause_appointments.when == paused.expires_at

    # (c) until I resume: no expiry at all, and therefore no appointment.
    await session.preview.async_pause(PAUSE_UNTIL_RESUMED)
    paused = session.settings().pause
    assert paused.choice == PAUSE_UNTIL_RESUMED
    assert paused.expires_at is None
    assert pause_appointments.when is None and pause_appointments.cancelled >= 2


async def test_a_choice_that_cannot_be_honoured_is_refused_and_changes_nothing(
    session: Session,
) -> None:
    """No plan and no market zone each refuse by code, before anything is stored or stopped."""
    before = session.settings()

    # No plan is running: there is no "next planned period" to pause until.
    with pytest.raises(AutoSettingsError) as refusal:
        await session.preview.async_pause(PAUSE_NEXT_PERIOD)
    assert refusal.value.code == "invalid_pause"

    # No prices and no catalogue: the market zone is unknown, so "tomorrow" has no instant.
    with pytest.raises(AutoSettingsError) as unknown_zone:
        await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    assert unknown_zone.value.code == "invalid_pause"

    assert session.settings() == before, "a refused pause stores nothing"
    assert session.settings().revision == before.revision
    assert len(session.off_calls) == 0, "and it issues no charger command"


# ----------------------------------------------------------------------- retry


def _script_stop(
    monkeypatch: pytest.MonkeyPatch, session: Session, *, failures: int
) -> list[Any]:
    """The physical stop, scripted: the first `failures` attempts raise, the rest succeed.

    Returns the recorded attempts, so a test can count exactly how many real stops were issued --
    the retry contract is about one new stop and nothing else.
    """
    attempts: list[Any] = []
    original = session.controller.async_stop
    remaining = {"failures": failures}

    async def stop(*args: Any, **kwargs: Any) -> Any:
        attempts.append((args, kwargs))
        if remaining["failures"] > 0:
            remaining["failures"] -= 1
            raise RuntimeError("the charger refused")
        return await original(*args, **kwargs)

    monkeypatch.setattr(session.controller, "async_stop", stop)
    return attempts


async def test_a_same_choice_retry_after_a_failed_stop_reuses_the_stored_intent_byte_for_byte(
    session: Session,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    install_spy: list[Any],
    pause_appointments: RecordedAppointments,
) -> None:
    """A retry completes the *stop*; it never re-admits the pause, however much time passed."""
    await _auto_with_a_plan(session)
    installs_before = len(install_spy)
    stops = _script_stop(monkeypatch, session, failures=2)

    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    stored = session.settings().pause
    revision = session.settings().revision
    assert len(stops) == 1
    assert session.executor.last_error == EXECUTION_PAUSE_STOP_FAILED
    assert len(pause_appointments.armed) == 1, "one appointment for one bounded pause"
    assert pause_appointments.cancelled == 0, "a retry cancels and re-arms nothing"
    assert session.executor.applied is not None, "the plan is still installed and must be retried"
    assert session.settings().pause == stored

    # Some time passes -- still inside the pause somebody chose, so the retry must not re-admit it.
    # (The passed-instant cases are `next_period` after its start and `until_tomorrow` after a
    # midnight; both are their own tests, because there the retry's *stop* is the whole point.)
    clock.now = clock.now + timedelta(hours=1)

    original_resolve = session.executor._resolve_pause  # noqa: SLF001 - the audit point
    resolutions: list[Any] = []

    def spy(choice: Any, now: Any) -> Any:
        resolutions.append(choice)
        return original_resolve(choice, now)

    monkeypatch.setattr(session.executor, "_resolve_pause", spy)
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)

    assert resolutions == [], "a same-choice retry never resolves the choice again"
    assert session.settings().pause.as_dict() == stored.as_dict(), "reused byte for byte"
    assert session.settings().revision == revision, "no settings write, no new revision"
    assert len(stops) == 2, "exactly one new stop attempt"
    assert len(install_spy) == installs_before, "and nothing else: no planner, install or service"
    assert session.executor.last_error == EXECUTION_PAUSE_STOP_FAILED
    assert len(pause_appointments.armed) == 1
    assert pause_appointments.cancelled == 0, "a retry cancels and re-arms nothing"

    # The third press finally stops the charger -- still without touching the stored intent.
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    assert len(stops) == 3
    assert session.settings().pause.as_dict() == stored.as_dict(), "the instant never moved"
    assert session.settings().revision == revision
    assert session.executor.applied is None, "the stop cleared Auto's own plan"
    assert session.executor.last_error is None
    assert session.executor.paused is True
    assert resolutions == [], "and even the successful retry never re-resolved the choice"
    assert len(install_spy) == installs_before


async def test_a_next_period_retry_still_stops_after_its_original_start_has_passed(
    session: Session,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    install_spy: list[Any],
    pause_appointments: RecordedAppointments,
) -> None:
    """A retry does not re-read the plan, so a start that has passed cannot refuse it."""
    await _auto_with_a_plan(session)
    plan = session.controller.plan
    assert plan is not None
    installs_before = len(install_spy)
    stops = _script_stop(monkeypatch, session, failures=2)

    await session.preview.async_pause(PAUSE_NEXT_PERIOD)
    stored = session.settings().pause
    assert stored.expires_at is not None
    revision = session.settings().revision

    # Every window of the plan is now in the past: a *fresh* `next_period` resolution would have
    # nothing left to pause until, and the stored instant has passed -- the retry must not care.
    clock.now = max(end for _, end in plan.windows) + timedelta(hours=1)
    await session.preview.async_pause(PAUSE_NEXT_PERIOD)

    assert session.settings().pause == stored
    assert session.settings().revision == revision
    assert len(stops) == 2, "the retry reached the charger without re-resolving anything"
    assert len(pause_appointments.armed) == 1
    assert pause_appointments.cancelled == 0, "neither cancelled nor re-armed"
    assert len(install_spy) == installs_before
    assert session.executor.last_error == EXECUTION_PAUSE_STOP_FAILED


async def test_an_until_tomorrow_retry_never_re_resolves_the_midnight(
    session: Session, clock: Clock, monkeypatch: pytest.MonkeyPatch, pause_appointments: RecordedAppointments
) -> None:
    """The instant somebody chose survives a passed midnight, byte for byte."""
    await _auto_with_a_plan(session)
    stops = _script_stop(monkeypatch, session, failures=2)
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    stored = session.settings().pause
    revision = session.settings().revision
    assert stored.expires_at is not None

    clock.now = stored.expires_at + timedelta(hours=12)
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)

    assert session.settings().pause.as_dict() == stored.as_dict()
    assert session.settings().revision == revision
    assert len(stops) == 2
    assert len(pause_appointments.armed) == 1
    assert pause_appointments.cancelled == 0, "neither cancelled nor re-armed"


# ---------------------------------------------------------------------- expiry


async def test_settle_pause_clears_once_and_reports_whether_it_did(
    session: Session, clock: Clock
) -> None:
    """The helper itself: one guarded write, and a truthful "did I clear one" answer.

    Deliberately narrow -- it proves the clear, not the reconciliation. The production proof that
    an expiry *installs the current proposal* is
    `test_the_expiry_callback_clears_once_and_installs_the_new_live_proposal_once`, which drives
    the registered callback instead of this helper.
    """
    await _auto_with_a_plan(session)
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    expires_at = session.settings().pause.expires_at
    assert expires_at is not None
    revision_paused = session.settings().revision

    # Nothing settles early: the pause stands right up to its own instant.
    assert await session.executor.async_settle_pause() is False
    assert session.settings().pause.admitted is True

    # The instant arrives: one clear, one revision, and no further write for a second caller.
    clock.now = expires_at
    assert await session.executor.async_settle_pause() is True
    assert session.settings().pause == PauseIntent()
    assert session.settings().revision == revision_paused + 1
    assert await session.executor.async_settle_pause() is False
    assert session.settings().revision == revision_paused + 1


async def test_the_expiry_callback_clears_once_and_installs_the_new_live_proposal_once(
    session: Session, clock: Clock, install_spy: list[Any], pause_appointments: RecordedAppointments
) -> None:
    """The real expiry path: one clear, and the *current* proposal installed exactly once.

    The proposal that must run at expiry is the one the live prices and settings produce *then*,
    not the plan that was installed when the pause was admitted. So the test changes the request
    while the pause is in force, drives the registered callback at the stored instant, and asserts
    against the live provider's own application -- never against a plan object captured earlier.
    """
    await _auto_with_a_plan(session)
    installed_at_admission = session.executor.applied
    assert installed_at_admission is not None and installed_at_admission.plan.amps == 10
    await session.preview.async_pause(PAUSE_NEXT_PERIOD)
    intent = session.settings().pause
    expires_at = intent.expires_at
    assert expires_at is not None
    revision_paused = session.settings().revision
    installs_before = len(install_spy)
    assert session.executor.applied is None, "pausing cleared Auto's own plan"

    # While paused, the live proposal changes -- a different current *and* more energy, so the
    # proposal's own periods differ. The preview recalculates and publishes it; execution refuses to
    # install it, which is what a pause means.
    await session.set_auto(amps=16, requested_kwh=40)
    assert session.settings().revision == revision_paused + 1
    assert len(install_spy) == installs_before, "nothing installs while the pause is in force"
    live = session.preview.snapshot()
    assert live is not None
    expected = application_for(session.settings(), live)
    assert expected is not None and expected.plan.amps == 16
    assert expected.plan.windows != installed_at_admission.plan.windows, (
        "the fixture must make the live proposal observably different from the admission-time plan"
    )

    # The real instant: the registered callback fires, and it is the *live* proposal that lands.
    clock.now = expires_at
    await pause_appointments.fire(expires_at)

    assert session.settings().pause == PauseIntent(), "the pause was cleared"
    assert session.settings().revision == revision_paused + 2, "once, and only for the clear"
    assert len(install_spy) == installs_before + 1, "exactly one installation"
    assert install_spy[-1].amps == 16, "the new live proposal, never the admission-time plan"
    assert all(plan.amps == 16 for plan in install_spy[installs_before:])
    assert install_spy[-1].windows == expected.plan.windows, (
        "reconciled from the live snapshot, not from one captured at admission"
    )
    applied = session.executor.applied
    assert applied is not None
    assert applied.identity != installed_at_admission.identity, "never the admission-time plan"
    assert applied.price_identity == live.price_identity, "built from the live price snapshot"
    assert applied.settings_revision == session.settings().revision, "and the current record"
    assert pause_appointments.armed == [], "a settled pause needs no appointment"
    assert session.executor.paused is False
    assert session.executor.last_error is None
    assert session.executor.execution_state() != EXECUTION_PAUSED

    # The stale callback, and an authoritative event after it, do nothing more.
    await pause_appointments.fire(expires_at)
    assert len(install_spy) == installs_before + 1
    assert session.settings().revision == revision_paused + 2
    await session.tick()
    assert session.settings().pause == PauseIntent()
    assert len(install_spy) == installs_before + 1, "no second clear and no second install"


# ------------------------------------------------- the persisted execution gate


async def _expired_with_a_failed_clear(
    session: Session,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    pause_appointments: RecordedAppointments,
    *,
    failures: int = 2,
) -> tuple[Any, dict[str, int]]:
    """The state this micro-round is about: the instant has passed, and the clear never committed.

    Returns the stored intent and the write recorder, so a caller can prove that later events keep
    retrying the clear *and* keep applying nothing. `failures=2` covers the expiry callback and the
    first follow-up event.
    """
    await _auto_with_a_plan(session)
    await session.preview.async_pause(PAUSE_NEXT_PERIOD)
    intent = session.settings().pause
    assert intent.expires_at is not None
    writes = _fail_pause_clears(monkeypatch, session, failures=failures)
    clock.now = intent.expires_at
    await pause_appointments.fire(intent.expires_at)

    assert session.settings().pause.as_dict() == intent.as_dict(), "the intent is still the record"
    assert session.executor.last_error == EXECUTION_PAUSE_CLEAR_FAILED
    assert session.executor.paused is False, "the time condition has ended"
    return intent, writes


async def test_a_price_event_after_a_failed_clear_retries_the_clear_and_applies_nothing(
    session: Session,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    pause_appointments: RecordedAppointments,
    install_spy: list[Any],
) -> None:
    """A publish retries the clear; a still-stored intent keeps every application blocked."""
    intent, writes = await _expired_with_a_failed_clear(
        session, clock, monkeypatch, pause_appointments
    )
    installs_before = len(install_spy)
    off_before, on_before = len(session.off_calls), len(session.on_calls)
    assert application_for(session.settings(), session.preview.snapshot()) is not None, (
        "the proposal is executable: the refusal must come from the persisted gate, not from a "
        "missing application"
    )

    # An authoritative price event: new documents and a fresh index, then the manager's own tick.
    serve(session.transport, flat=True)
    serve_index(session.transport, {SE4: [TODAY, TOMORROW]})
    await session.tick()

    assert writes["attempts"] == 2, "the price event retried the clear through the boundary"
    assert session.settings().pause.as_dict() == intent.as_dict(), "and it failed again"
    assert session.executor.last_error == EXECUTION_PAUSE_CLEAR_FAILED
    # The calculation may run -- the preview stays informed -- but nothing may be applied.
    assert session.preview.snapshot().calculated_at == clock.now
    assert application_for(session.settings(), session.preview.snapshot()) is not None
    assert len(install_spy) == installs_before, "no plan reached the charger"
    assert session.executor.pending is None, "and nothing was queued for a boundary"
    assert (len(session.off_calls), len(session.on_calls)) == (off_before, on_before)


async def test_a_settings_update_while_the_clear_failed_cannot_apply_a_plan(
    session: Session,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    pause_appointments: RecordedAppointments,
    install_spy: list[Any],
) -> None:
    """A committed settings replacement reconciles, and still applies nothing."""
    intent, _writes = await _expired_with_a_failed_clear(
        session, clock, monkeypatch, pause_appointments
    )
    installs_before = len(install_spy)
    off_before, on_before = len(session.off_calls), len(session.on_calls)
    revision_before = session.settings().revision

    await session.set_auto(amps=16)

    assert session.settings().revision == revision_before + 1, "the settings replacement committed"
    assert session.settings().pause.as_dict() == intent.as_dict(), "and the pause is still stored"
    assert len(install_spy) == installs_before, "no Auto plan passed the stored pause"
    assert (len(session.off_calls), len(session.on_calls)) == (off_before, on_before)
    assert session.executor.last_error == EXECUTION_PAUSE_CLEAR_FAILED




async def test_a_waiting_proposal_is_dropped_when_the_pause_is_still_persisted(
    session: Session,
    clock: Clock,
    install_spy: list[Any],
) -> None:
    """A charging-window boundary is not a reason to act on a record that still says stop.

    A valid wait and a stored pause cannot arise through the admission path -- admitting a pause
    drops any waiting change and bumps the revision -- so this seam's own defence is proved by
    building exactly that state: the record says stop, and the wait is otherwise current (same
    attempt, same revision, the live proposal's own identity, and no window charging now).
    """
    await _auto_with_a_plan(session)
    installs_before = len(install_spy)

    stored_pause = PauseIntent(
        choice=PAUSE_UNTIL_RESUMED, admitted_at=clock.now
    ).validated()
    await session.store.async_update(
        session.entry_id, mutate=lambda current: replace(current, pause=stored_pause)
    )
    settings = session.settings()
    application = application_for(settings, session.preview.snapshot())
    assert application is not None, "the wait is a real, executable application"
    assert session.executor.window_charging_now() is False, "and no window is charging now"
    session.executor._pending = PendingApplication(  # noqa: SLF001 - the seam under test
        application=application,
        attempt=session.executor.attempt,
        settings_revision=settings.revision,
        price_identity=application.price_identity,
    )

    # The boundary arrives: the controller notifies, and the wait is released through its own path.
    await session.boundary()
    await session.executor.async_apply_pending()

    assert session.executor.pending is None, "the waiting change was discarded, not installed"
    assert len(install_spy) == installs_before, "and nothing reached the charger"
    assert session.settings().pause.admitted is True, "the record still says stop"


async def test_restore_with_an_expired_intent_whose_clear_fails_installs_nothing(
    session: Session,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    pause_appointments: RecordedAppointments,
    install_spy: list[Any],
) -> None:
    """Setup settles a past-due intent; while the clear fails, setup and the first event apply zero."""
    await _auto_with_a_plan(session)
    await session.preview.async_pause(PAUSE_NEXT_PERIOD)
    intent = session.settings().pause
    assert intent.expires_at is not None
    clock.now = intent.expires_at + timedelta(minutes=1)

    writes = _fail_pause_clears(monkeypatch, session, failures=2)
    installs_before = len(install_spy)
    await session.executor.async_restore_pause()

    assert writes["attempts"] == 1, "setup tried the clear"
    assert session.executor.last_error == EXECUTION_PAUSE_CLEAR_FAILED
    assert len(install_spy) == installs_before, "and installed nothing"
    assert len(pause_appointments.armed) == 1, "exactly one retry appointment"

    # The first price notification after that restore is equally unable to apply anything.
    serve(session.transport, flat=True)
    await session.tick()
    assert writes["attempts"] == 2
    assert len(install_spy) == installs_before
    assert session.settings().pause.as_dict() == intent.as_dict()


async def test_presentation_and_persistence_are_reported_separately_in_the_failure_state(
    session: Session,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    pause_appointments: RecordedAppointments,
) -> None:
    """The clock says the pause elapsed; the record says stop, and the state says so honestly."""
    intent, _writes = await _expired_with_a_failed_clear(
        session, clock, monkeypatch, pause_appointments
    )

    # Presentation: the timed pause is over.
    assert session.executor.paused is False
    assert session.executor.pause_intent.is_active_at(clock.now) is False
    # Persistence: the record still admits it, and says so, unchanged.
    assert session.settings().pause.admitted is True
    assert session.settings().pause == intent
    assert session.settings().execution_paused is True
    # And the state is never a clean resume with a plan behind it.
    assert session.executor.execution_state() == EXECUTION_ERROR
    assert session.executor.last_error == EXECUTION_PAUSE_CLEAR_FAILED
    assert session.executor.applied is None


async def test_the_production_reconcile_path_refuses_an_expired_but_stored_pause(
    session: Session,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    pause_appointments: RecordedAppointments,
    install_spy: list[Any],
) -> None:
    """`_may_apply` is reached through its public caller and refuses under the lock."""
    await _expired_with_a_failed_clear(session, clock, monkeypatch, pause_appointments)
    installs_before = len(install_spy)
    settings = session.settings()
    snapshot = session.preview.snapshot()
    application = application_for(settings, snapshot)
    assert application is not None, "the proposal itself is executable"
    assert application.settings_revision == settings.revision

    installed = await session.executor.async_reconcile(
        settings, snapshot, attempt=session.executor.begin_attempt()
    )

    assert installed is None, "the boundary applied nothing"
    assert len(install_spy) == installs_before
    assert session.executor.execution_state() == EXECUTION_ERROR
    assert session.executor.last_error == EXECUTION_PAUSE_CLEAR_FAILED, "and invented no other code"


# ------------------------------------------------- early callback and recovery



def _fail_pause_clears(
    monkeypatch: pytest.MonkeyPatch, session: Session, *, failures: int = 1
) -> dict[str, int]:
    """Fail the *document write that clears this charger's pause*, and nothing else.

    Deliberately not "the disk is broken": every other settings write -- an admission, a field
    edit, a proposal summary -- passes through the real store, so what the tests exercise is
    exactly "the clear could not be committed" and the boundary's behaviour around it.
    """
    state = {"failures": failures, "attempts": 0}
    store = session.store._store  # noqa: SLF001 - the store's own persistence seam
    original = store.async_save
    entry_id = session.entry_id

    async def save(data: Any) -> Any:
        record = ((data or {}).get("chargers") or {}).get(entry_id) or {}
        pause = (record.get("settings") or {}).get("pause") or {}
        if pause.get("choice") is None and session.store.settings(entry_id).pause.admitted:
            state["attempts"] += 1
            if state["failures"] > 0:
                state["failures"] -= 1
                raise RuntimeError("the disk said no")
        return await original(data)

    monkeypatch.setattr(store, "async_save", save)
    return state


async def test_an_early_callback_re_arms_the_original_instant_and_changes_nothing(
    session: Session, clock: Clock, pause_appointments: RecordedAppointments, install_spy: list[Any]
) -> None:
    """A callback that fires before its instant must not cost the pause its only appointment."""
    await _auto_with_a_plan(session)
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    intent = session.settings().pause
    revision = session.settings().revision
    installs_before = len(install_spy)
    assert intent.expires_at is not None

    # The platform fires early (a scheduling wobble, or a stalled clock).
    await pause_appointments.fire(intent.expires_at - timedelta(minutes=5))

    assert session.settings().pause.as_dict() == intent.as_dict(), "nothing cleared"
    assert session.settings().revision == revision, "and nothing written"
    assert len(install_spy) == installs_before, "and nothing reconciled or installed"
    assert session.executor.last_error is None
    assert len(pause_appointments.armed) == 1, "the pause keeps exactly one appointment"
    assert pause_appointments.when == intent.expires_at, "re-armed for the *original* instant"

    # The real instant then settles and reconciles once, as the production path does.
    clock.now = intent.expires_at
    await pause_appointments.fire(intent.expires_at)
    assert session.settings().pause == PauseIntent()
    assert session.settings().revision == revision + 1
    assert len(install_spy) == installs_before + 1
    assert pause_appointments.armed == []


async def test_a_failed_clear_write_keeps_the_intent_and_arms_one_retry(
    session: Session,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    pause_appointments: RecordedAppointments,
    install_spy: list[Any],
) -> None:
    """An unpersisted clear leaves the record as the truth and guarantees one bounded retry."""
    await _auto_with_a_plan(session)
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    intent = session.settings().pause
    assert intent.expires_at is not None
    revision = session.settings().revision
    installs_before = len(install_spy)

    writes = _fail_pause_clears(monkeypatch, session)
    clock.now = intent.expires_at
    await pause_appointments.fire(intent.expires_at)

    assert writes["attempts"] == 1, "the clear was attempted"
    assert session.settings().pause.as_dict() == intent.as_dict(), "the intent still stands"
    assert session.settings().revision == revision, "and no revision moved"
    assert len(install_spy) == installs_before, "nothing applies on an uncommitted clear"
    assert session.executor.last_error == EXECUTION_PAUSE_CLEAR_FAILED
    assert session.executor.paused is False, "the clock, not the record, decides suspension"
    assert len(pause_appointments.armed) == 1, "exactly one retry appointment"
    assert pause_appointments.when == intent.expires_at + PAUSE_RETRY_DELAY

    # Persistence recovers: the retry clears once and reconciles once.
    clock.now = pause_appointments.when
    await pause_appointments.fire()
    assert writes["attempts"] == 2
    assert session.settings().pause == PauseIntent()
    assert session.settings().revision == revision + 1
    assert len(install_spy) == installs_before + 1
    assert session.executor.last_error is None
    assert pause_appointments.armed == []

    # And nothing clears or installs a second time: a repeat callback has nothing to fire, and a
    # further price event finds an already-clear record and an identical application.
    await pause_appointments.fire()
    assert session.settings().revision == revision + 1
    assert len(install_spy) == installs_before + 1
    serve_index(session.transport, {SE4: [TODAY, TOMORROW]})
    await session.tick()
    assert session.settings().pause == PauseIntent()
    assert session.settings().revision == revision + 1, "no second clear"
    # The index now lists a day whose successor is not published: with no departure the 24-hour
    # window needs it, and part of the 20 kWh cannot wait for it. That one purchase, planned from
    # published prices only, is the only installation this event may add.
    assert session.preview.snapshot().price_wait == "buy_now"
    assert len(install_spy) == installs_before + 2, "the price-wait purchase, and nothing else"
    assert install_spy[-1].energy_kwh < 1.0 and install_spy[-1].unpriced is False
    assert pause_appointments.armed == []


async def test_a_committed_clear_whose_reconcile_fails_reports_it_and_never_restores(
    session: Session,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    pause_appointments: RecordedAppointments,
    install_spy: list[Any],
) -> None:
    """The clear cannot be rolled back; the failure is reported and the next event retries.

    A `next_period` pause is used on purpose: its expiry is inside the published day, so the
    follow-up recalculation below can genuinely reconcile -- the point of the test is the failed
    pass and the ordinary retry after it.
    """
    await _auto_with_a_plan(session)
    await session.preview.async_pause(PAUSE_NEXT_PERIOD)
    intent = session.settings().pause
    assert intent.expires_at is not None
    revision = session.settings().revision
    installs_before = len(install_spy)

    remaining = {"failures": 1}
    original = session.executor.async_reconcile

    async def reconcile(*args: Any, **kwargs: Any) -> Any:
        if remaining["failures"] > 0:
            remaining["failures"] -= 1
            raise RuntimeError("reconciliation blew up")
        return await original(*args, **kwargs)

    monkeypatch.setattr(session.executor, "async_reconcile", reconcile)
    clock.now = intent.expires_at
    await pause_appointments.fire(intent.expires_at)

    assert session.settings().pause == PauseIntent(), "the clear is committed and stands"
    assert session.settings().revision == revision + 1
    assert session.executor.last_error == EXECUTION_RECONCILE_FAILED
    assert session.executor.paused is False, "not 'paused' -- the override is gone"
    assert len(install_spy) == installs_before, "and nothing was applied on the failed attempt"
    assert pause_appointments.armed == [], "a cleared pause is never re-armed"

    # The next ordinary recalculation reconciles through the existing rules, exactly once.
    await session.preview.async_recalculate()
    assert session.settings().pause == PauseIntent()
    assert len(install_spy) == installs_before + 1, "execution resumed through the normal path"
    assert pause_appointments.armed == []


async def test_the_appointment_is_replaced_only_by_a_different_choice_and_cancelled_by_resume(
    session: Session, clock: Clock, pause_appointments: RecordedAppointments
) -> None:
    """One live appointment per executor: a *different* choice replaces it, resume/shutdown cancel.

    The same choice twice is deliberately *not* a replacement -- see
    `test_a_same_choice_retry_after_a_failed_stop_reuses_the_stored_intent_byte_for_byte` -- so it
    is the second, different choice here that must cancel the first appointment and arm its own.
    """
    await _auto_with_a_plan(session)
    await session.preview.async_pause(PAUSE_NEXT_PERIOD)
    first = pause_appointments.when
    assert first is not None and len(pause_appointments.armed) == 1

    # A different explicit choice is a new admission: one write, one cancellation, one new
    # appointment at the new instant.
    clock.now = clock.now + timedelta(hours=6)
    revision = session.settings().revision
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    assert session.settings().revision == revision + 1
    assert len(pause_appointments.armed) == 1, "arming replaces rather than accumulates"
    assert pause_appointments.cancelled == 1, "the appointment it replaced was cancelled"
    assert pause_appointments.when == session.settings().pause.expires_at

    await session.preview.async_resume()
    assert pause_appointments.armed == [], "resuming cancels the appointment it would have ended at"
    assert session.settings().pause == PauseIntent()

    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    assert len(pause_appointments.armed) == 1
    await session.executor.async_shutdown()
    assert pause_appointments.armed == [], "a shut-down boundary leaves no callback alive"


# --------------------------------------------------------------------- restart


async def test_a_restored_charger_re_arms_the_stored_instant_and_settles_an_expired_one(
    session: Session, clock: Clock, pause_appointments: RecordedAppointments
) -> None:
    """The intent is durable, so a restart re-arms it -- and settles one that already ran out."""
    await _auto_with_a_plan(session)
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    stored = session.settings().pause
    assert stored.expires_at is not None
    # A restart reads the same durable record, and the boundary asks to restore itself.
    assert session.store.settings(session.entry_id).pause == stored
    await session.executor.async_restore_pause()
    assert pause_appointments.when == stored.expires_at

    # ...and an expiry that passed while Home Assistant was down settles on the spot.
    clock.now = stored.expires_at + timedelta(seconds=1)
    await session.executor.async_restore_pause()
    assert session.settings().pause == PauseIntent()
    assert pause_appointments.armed == []


async def test_a_restart_at_the_exact_instant_settles_and_reconciles_once(
    session: Session, clock: Clock, pause_appointments: RecordedAppointments, install_spy: list[Any]
) -> None:
    """The "due" case of a restart: the instant has just arrived, so it settles and reconciles."""
    await _auto_with_a_plan(session)
    await session.preview.async_pause(PAUSE_NEXT_PERIOD)
    stored = session.settings().pause
    assert stored.expires_at is not None
    revision = session.settings().revision
    installs_before = len(install_spy)

    clock.now = stored.expires_at
    await session.executor.async_restore_pause()

    assert session.settings().pause == PauseIntent()
    assert session.settings().revision == revision + 1, "one clear, on the dot"
    assert len(install_spy) == installs_before + 1, "and one reconciliation behind it"
    assert pause_appointments.armed == [], "a settled pause is not re-armed"


async def test_resume_then_a_stale_callback_is_inert(
    session: Session, clock: Clock, pause_appointments: RecordedAppointments, install_spy: list[Any]
) -> None:
    """Every appointment a pause owned is cancelled, and one that fires anyway changes nothing."""
    await _auto_with_a_plan(session)
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    stale = pause_appointments.armed[-1]["when"]

    # A resume cancels the appointment it would have ended at, and applies the latest proposal once.
    await session.preview.async_resume()
    assert pause_appointments.armed == []
    assert session.settings().pause == PauseIntent()
    revision = session.settings().revision
    installs_after_resume = len(install_spy)

    # ...and a callback that fires anyway finds nothing to settle and nothing to arm.
    await pause_appointments.fire(stale)
    assert session.settings().pause == PauseIntent()
    assert session.settings().revision == revision, "no write beyond the resume's own"
    assert len(install_spy) == installs_after_resume, "and no adoption of a spent intent"
    assert pause_appointments.armed == []

    # A shutdown is terminal in the same way: nothing it did can be resurrected by a late callback.
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    pending = pause_appointments.armed[-1]["when"]
    await session.executor.async_shutdown()
    assert pause_appointments.armed == []
    await pause_appointments.fire(pending)
    assert pause_appointments.armed == []
    assert len(install_spy) == installs_after_resume


async def test_a_rebuilt_boundary_reads_the_same_pause_and_stops_suspending_at_expiry(
    hass: HomeAssistant, session: Session, clock: Clock
) -> None:
    """Nothing about a pause lives in memory: a rebuilt boundary reads the stored intent."""
    await _auto_with_a_plan(session)
    await session.preview.async_pause(PAUSE_UNTIL_TOMORROW)
    assert session.store is not None
    rebuilt = AutoExecutor(
        hass, session.controller, session.store, live_snapshot=session.preview.snapshot, now=clock
    )
    assert rebuilt.pause_intent == session.settings().pause
    assert rebuilt.paused is True

    expires_at = session.settings().pause.expires_at
    assert expires_at is not None
    clock.now = expires_at + timedelta(seconds=1)
    assert rebuilt.paused is False, "an expired pause stops suspending before it is settled"
    assert await rebuilt.async_settle_pause() is True
    assert rebuilt.paused is False


# --------------------------------------------------------------------- storage


async def test_a_record_with_an_unknown_pause_field_is_refused(
    hass: HomeAssistant, flaky: FlakyStore
) -> None:
    """The stored shape is exact: a misspelled pause field is not a pause."""
    settings = AutoSettings(amps=10, area_id=SE4, phases=1).validated()
    record = settings.as_dict()
    record["pause"] = {"choice": PAUSE_UNTIL_TOMORROW, "expires_at": None, "resume_at": None}
    flaky.payload = {
        "schema": 1,
        "chargers": {"entry-a": {"settings": record, "proposal": None}},
    }
    store = AutoSettingsStore(hass, store=flaky)
    await store.async_load()

    assert store.entry_ids() == ()


@pytest.mark.parametrize(
    "intent",
    [
        PauseIntent(choice="whenever"),
        PauseIntent(choice=PAUSE_UNTIL_TOMORROW),
        PauseIntent(
            choice=PAUSE_UNTIL_RESUMED, expires_at=datetime(2026, 9, 22, tzinfo=timezone.utc)
        ),
        PauseIntent(choice=PAUSE_NEXT_PERIOD, admitted_at=datetime(2026, 9, 22)),
        PauseIntent(choice=None, expires_at=datetime(2026, 9, 22, tzinfo=timezone.utc)),
        PauseIntent(
            choice=PAUSE_NEXT_PERIOD,
            admitted_at=datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 9, 22, 5, 0, tzinfo=timezone.utc),
        ),
    ],
)
def test_an_impossible_pause_is_refused_by_code(intent: PauseIntent) -> None:
    """Every refusal names what cannot be true of a pause, and none of them clamps."""
    with pytest.raises(AutoSettingsError) as refusal:
        intent.validated()
    assert refusal.value.code == "invalid_pause"
