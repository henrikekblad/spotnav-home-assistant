"""Property-based, stateful tests of the charge-ownership core (`core/ownership.py`): random event sequences, with
random results for every command, against the invariants the refactor is for.

* A person's Stop holds until the unplug that ends its plug-in session, the end of the next plug-in for a Stop
  given with no car, their Start, Resume or Follow, or another pause they pick: nothing else starts the charger
  meanwhile.
* At most one owner, and one pause at a time.
* A charge the charger began by itself is stopped at the first reading with no surplus (I4).
* A person's Start is never stopped by anything automatic (only by the person, or load balancing for safety).
* A manual pause of the plug-in session ends at the unplug.
* A restart (serialise, read back, restart) changes nothing about who owns the charge or what the person wants.

The facts an event carries are drawn so that they could come from Home Assistant together: no plan exists under a
person's pause (it is dropped), so no plan-bound event says one is applied then.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import invariant, rule, RuleBasedStateMachine, run_state_machine_as_test

from custom_components.spotnav.core import events as ev
from custom_components.spotnav.core.ownership import decide, Notify, Start, Stop
from custom_components.spotnav.core.session import ChargeSession, OWNERS, SCOPE_PLUG_IN

T0 = datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc)

#: Events by which a person's Stop may end (with the condition checked in `step`).
STOP_ENDS = {"person_start", "resume", "pause_choice", "unplug", "plug_in", "command_result"}
#: Automatic happenings: none may stop a person's Start.
AUTOMATIC = {
    "window_start",
    "window_end",
    "final_window_end",
    "rearm",
    "solar_start",
    "solar_stop",
    "charger_reported_on",
    "charger_reported_off",
    "timer",
    "recheck",
    "top_off_end",
    "need_met",
    "target_reached",
    "balancing_resume",
    "strategy_change",
    "plan_installed",
    "plan_dropped",
    "car_ended",
    "restart",
}
#: Person happenings that may start the charger under their own Stop.
PERSON_STARTS = {"person_start", "direct_start"}

maybe_bool = st.sampled_from([True, False, None])
when = st.one_of(st.none(), st.integers(-7200, 7200).map(lambda s: T0 + timedelta(seconds=s)))


class OwnershipMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.session = ChargeSession()
        self.now = T0
        self.examples_steps = 0

    # ------------------------------------------------------------------ the one step, with its per-step checks

    def step(self, event: ev.Event, results: st.DataObject | None = None) -> None:
        pre = self.session
        session, commands = decide(pre, event, self.now)
        self.check_step(pre, event, session, commands)
        self.session = session
        acted = [command for command in commands if isinstance(command, (Start, Stop))]
        if acted and results is not None:
            command = acted[0]
            executed = results.draw(st.booleans())
            result = ev.CommandResult(
                command=command.kind,
                reason=command.reason,
                executed=executed,
                balancing_held=command.kind == "start" and not executed and results.draw(st.booleans()),
                unobserved=command.kind == "stop" and executed and results.draw(st.booleans()),
            )
            pre = self.session
            session, more = decide(pre, result, self.now)
            self.check_step(pre, result, session, more)
            self.session = session
        self.examples_steps += 1

    def check_step(self, pre: ChargeSession, event: ev.Event, post: ChargeSession, commands: tuple) -> None:
        kinds = [command.kind for command in commands]
        # Total and deterministic: the same input decides the same.
        assert decide(pre, event, self.now) == (post, commands)
        assert commands, "every decision says what to do, if only keep"
        # A person's Stop holds: nothing but the person starts the charger, and only the rules end it.
        if pre.held_off_by_person:
            if event.kind not in PERSON_STARTS:
                assert "start" not in kinds, (event, kinds)
            if not post.held_off_by_person:
                assert event.kind in STOP_ENDS, event
                if event.kind == "unplug":
                    assert pre.manual is not None and pre.manual.scope == SCOPE_PLUG_IN
                if event.kind == "plug_in":
                    assert getattr(event, "previous", None) is False and pre.manual.scope == SCOPE_PLUG_IN
                if event.kind == "command_result":
                    assert event.command == "start" and event.reason == "person"
                if event.kind == "resume":
                    assert event.reason in ("resume", "follow")
        # A person's Start is never stopped by anything automatic.
        if pre.person_started and event.kind in AUTOMATIC:
            assert "stop" not in kinds, (event, kinds)
        # The manual pause of the plug-in session ends at the unplug.
        if event.kind == "unplug" and pre.manual is not None and pre.manual.scope == SCOPE_PLUG_IN:
            assert post.manual is None
        # I4: a charge the charger began by itself is stopped at the first reading with no surplus.
        if (
            isinstance(event, ev.SolarStop)
            and event.take_over
            and event.charging
            and event.strategy_ok
            and not (event.start_pending or event.stop_recent or event.top_off_or_window)
            and pre.owner in ("none", "charger_self")
            and not pre.overridden
            and not pre.paused
        ):
            assert "stop" in kinds
        # C7: under a person's Stop, a charge the charger begins is stopped at once, or SpotNav says it gives up.
        if (
            isinstance(event, ev.ChargerReportedOn)
            and pre.held_off_by_person
            and not event.start_pending
            and not (pre.hold_stop_pending or pre.hold_gave_up)
            and (pre.hold_tried_at is None or (self.now - pre.hold_tried_at).total_seconds() >= 30)
        ):
            assert "stop" in kinds or any(isinstance(command, Notify) for command in commands)

    # ------------------------------------------------------------------ invariants after every step

    @invariant()
    def one_owner_one_pause(self) -> None:
        session = self.session
        assert session.owner in OWNERS
        assert session.manual is None or session.span_pause is None
        assert session.paused_origin is None or session.paused_origin in OWNERS
        assert len(session.hold_stop_times) <= 3 or session.hold_gave_up

    @invariant()
    def the_record_reads_back(self) -> None:
        assert ChargeSession.from_json(self.session.to_json()) == self.session

    # ------------------------------------------------------------------ the world

    @rule(seconds=st.integers(1, 3600))
    def tick(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)

    @rule(previous=maybe_bool)
    def plug_in(self, previous) -> None:
        if previous is True:
            return
        self.step(ev.PlugIn(previous=previous))

    @rule(previous=maybe_bool)
    def unplug(self, previous) -> None:
        if previous is False:
            return
        self.step(ev.Unplug(previous=previous))

    @rule()
    def connection_unknown(self) -> None:
        self.step(ev.ConnectionUnknown())

    @rule(
        data=st.data(),
        trigger=st.sampled_from(["timer", "rearm", "plug_in"]),
        reached=st.booleans(),
        window_start=when,
        known_full=st.booleans(),
        need_grew=st.booleans(),
        control_on=st.booleans(),
        connected=maybe_bool,
        start_pending=st.booleans(),
        top_off=st.booleans(),
        solar=st.booleans(),
    )
    def window_start(
        self, data, trigger, reached, window_start, known_full, need_grew, control_on, connected, start_pending, top_off, solar
    ) -> None:
        self.step(
            ev.WindowStart(
                trigger=trigger,
                target_reached=reached,
                open_window_start=window_start,
                car_ended_known_full=known_full,
                need_grew=need_grew,
                control_on=control_on,
                connected=connected,
                start_pending=start_pending,
                top_off=top_off,
                solar_holds=solar,
            ),
            data,
        )

    @rule(data=st.data(), handed_off=st.booleans())
    def window_end(self, data, handed_off) -> None:
        self.step(ev.WindowEnd(handed_off=handed_off), data)

    @rule(data=st.data(), handed_off=st.booleans(), top_off=st.booleans())
    def final_window_end(self, data, handed_off, top_off) -> None:
        self.step(ev.FinalWindowEnd(handed_off=handed_off, top_off_wanted=top_off), data)

    @rule(
        data=st.data(),
        past_last=st.booleans(),
        resumes=st.booleans(),
        handed_off=st.booleans(),
        control_on=st.booleans(),
        charging=st.booleans(),
        solar=st.booleans(),
        auto=st.booleans(),
    )
    def rearm(self, data, past_last, resumes, handed_off, control_on, charging, solar, auto) -> None:
        self.step(
            ev.Rearm(
                past_last=past_last,
                top_off_resumes=resumes,
                handed_off=handed_off,
                control_on=control_on,
                charging=charging,
                solar_holds=solar,
                plan_auto_owned=auto,
            ),
            data,
        )

    @rule(data=st.data(), connected=maybe_bool)
    def person_start(self, data, connected) -> None:
        self.step(ev.PersonStart(connected=connected), data)

    @rule(data=st.data(), connected=maybe_bool)
    def person_stop(self, data, connected) -> None:
        self.step(ev.PersonStop(connected=connected), data)

    @rule(data=st.data(), manual=st.booleans(), cause=st.sampled_from([None, "solar", "plan_window", "other"]))
    def direct_start(self, data, manual, cause) -> None:
        self.step(ev.DirectStart(manual=manual, cause=cause), data)

    @rule(data=st.data(), clear=st.booleans())
    def direct_stop(self, data, clear) -> None:
        self.step(ev.DirectStop(clear_schedule=clear), data)

    @rule(reason=st.sampled_from(["resume", "follow", "expired"]))
    def resume(self, reason) -> None:
        self.step(ev.Resume(reason=reason))

    @rule(data=st.data(), choice=st.sampled_from(["next_period", "until_tomorrow", "until_resumed"]), applied=st.booleans())
    def pause_choice(self, data, choice, applied) -> None:
        self.step(ev.PauseChoiceMade(choice=choice, plan_applied=applied and not self.session.paused), data)

    @rule(data=st.data(), strategy=st.sampled_from(["solar", "hybrid", "cheapest"]), applied=st.booleans())
    def strategy_change(self, data, strategy, applied) -> None:
        # No Auto plan stays on the charger under a pause (a person's pause drops it, a span pause clears it).
        self.step(ev.StrategyChange(strategy=strategy, plan_applied=applied and not self.session.paused), data)

    @rule(data=st.data(), strategy_ok=st.booleans())
    def solar_start(self, data, strategy_ok) -> None:
        self.step(ev.SolarStart(strategy_ok=strategy_ok), data)

    @rule(
        data=st.data(),
        take_over=st.booleans(),
        strategy_ok=st.booleans(),
        window=st.booleans(),
        charging=st.booleans(),
        start_pending=st.booleans(),
        stop_recent=st.booleans(),
    )
    def solar_stop(self, data, take_over, strategy_ok, window, charging, start_pending, stop_recent) -> None:
        self.step(
            ev.SolarStop(
                take_over=take_over,
                strategy_ok=strategy_ok,
                top_off_or_window=window,
                charging=charging,
                start_pending=start_pending,
                stop_recent=stop_recent,
            ),
            data,
        )

    @rule(
        data=st.data(),
        charging=st.booleans(),
        was_on=maybe_bool,
        connected=maybe_bool,
        ahead=st.booleans(),
        window_open=st.booleans(),
        in_window=st.booleans(),
        plan=st.booleans(),
        window_start=when,
        solar=st.booleans(),
        auto=st.booleans(),
        handed_off=st.booleans(),
        start_pending=st.booleans(),
        stop_recent=st.booleans(),
        owned=maybe_bool,
    )
    def charger_reported_on(
        self, data, charging, was_on, connected, ahead, window_open, in_window, plan, window_start, solar, auto,
        handed_off, start_pending, stop_recent, owned,
    ) -> None:
        self.step(
            ev.ChargerReportedOn(
                charging=charging,
                was_on=was_on,
                connected=connected,
                window_ahead_outside=ahead,
                window_open=window_open,
                in_window=in_window,
                plan_present=plan,
                open_window_start=window_start,
                solar_holds=solar,
                plan_auto_owned=auto,
                handed_off=handed_off,
                start_pending=start_pending,
                stop_recent=stop_recent,
                owned=owned,
            ),
            data,
        )

    @rule(notified=st.booleans(), start_pending=st.booleans(), connected=maybe_bool)
    def charger_reported_off(self, notified, start_pending, connected) -> None:
        self.step(ev.ChargerReportedOff(notified=notified, start_pending=start_pending, connected=connected))

    @rule()
    def car_ended(self) -> None:
        self.step(ev.CarEnded())

    @rule(data=st.data(), code=st.sampled_from(["pause", "safety_stop"]), was_on=st.booleans())
    def balancing_pause(self, data, code, was_on) -> None:
        self.step(ev.BalancingPause(code=code, was_on=was_on), data)

    @rule(data=st.data(), charging=st.booleans())
    def balancing_resume(self, data, charging) -> None:
        self.step(ev.BalancingResume(charging=charging), data)

    @rule(data=st.data(), gated=st.booleans())
    def target_reached(self, data, gated) -> None:
        # Only the reading's own listener decides it with no plan-bound path; a plan does not outlive a pause.
        self.step(ev.TargetReached(gated=gated or self.session.paused), data)

    @rule(data=st.data(), control_on=st.booleans(), handed_off=st.booleans(), solar=st.booleans())
    def need_met(self, data, control_on, handed_off, solar) -> None:
        self.step(ev.NeedMet(control_on=control_on, handed_off=handed_off, solar_holds=solar), data)

    @rule(data=st.data(), valid=st.booleans())
    def top_off_end(self, data, valid) -> None:
        self.step(ev.TopOffEnd(valid=valid), data)

    @rule(window_open=st.booleans())
    def plan_installed(self, window_open) -> None:
        self.step(ev.PlanInstalled(window_open=window_open))

    @rule()
    def plan_dropped(self) -> None:
        self.step(ev.PlanDropped())

    @rule(data=st.data(), control_on=st.booleans(), start_pending=st.booleans())
    def timer(self, data, control_on, start_pending) -> None:
        self.step(ev.Timer(control_on=control_on, start_pending=start_pending), data)

    @rule(
        data=st.data(),
        what=st.sampled_from(list(ev.RECHECKS)),
        control_on=st.booleans(),
        ahead=st.booleans(),
        window_open=st.booleans(),
        in_window=st.booleans(),
        plan_present=st.booleans(),
        start_pending=st.booleans(),
        owned=maybe_bool,
        solar=st.booleans(),
    )
    def recheck(
        self, data, what, control_on, ahead, window_open, in_window, plan_present, start_pending, owned, solar
    ) -> None:
        """A background task decides its command again under the boundary's lock."""
        self.step(
            ev.Recheck(
                what=what,
                control_on=control_on,
                window_ahead_outside=ahead,
                window_open=window_open,
                in_window=in_window,
                plan_present=plan_present and not self.session.paused,
                start_pending=start_pending,
                owned=owned,
                solar_holds=solar,
            ),
            data,
        )

    @rule(legacy=st.booleans())
    def restart(self, legacy) -> None:
        """Persistence is part of the model: what is read back after a restart owns what it owned before."""
        before = self.session
        read_back = ChargeSession.from_json(before.to_json())
        assert read_back == before
        # The record a charger keeps (`to_store`) leaves out only what the restart clears anyway.
        stored = ChargeSession.from_store(before.to_store())
        assert stored == before.stored()
        assert decide(stored, ev.Restart(legacy_person_stop=legacy), self.now) == decide(
            read_back, ev.Restart(legacy_person_stop=legacy), self.now
        )
        session, commands = decide(read_back, ev.Restart(legacy_person_stop=legacy), self.now)
        assert [command.kind for command in commands] == ["keep"]
        for name in ("owner", "held", "overridden", "car_ended_at", "balancing_paused", "paused_origin", "span_pause"):
            assert getattr(session, name) == getattr(before, name), name
        if not (legacy and not before.paused):
            assert session.manual == before.manual
        assert session.plugged is None and session.pending == ()
        self.session = session


# Hypothesis notes a seeded random of the test harness it cannot manage; the machine draws only its own data.
@pytest.mark.filterwarnings("ignore:It looks like `register_random`")
def test_the_ownership_invariants_hold_for_random_event_sequences() -> None:
    run_state_machine_as_test(
        OwnershipMachine,
        settings=settings(
            max_examples=300,
            stateful_step_count=60,
            deadline=None,
            derandomize=True,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
        ),
    )
