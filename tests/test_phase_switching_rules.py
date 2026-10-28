"""Phase switching's pure rules: what the charging controller keeps per plug-in (`execution/phase_switching.py`), and
the settle read by the car-ended watch, the phase learning, the Automatic phase tracking and the solar controller's
start verification."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from custom_components.spotnav.execution.phase_switching import CONFIRM_S, PhaseSwitchState, SETTLE_S
from custom_components.spotnav.execution.plan_car_ended import PLAN_CAR_IDLE_S, PlanChargeWatch
from custom_components.spotnav.planning.phases import MIN_SAMPLES, PhaseObserver
from custom_components.spotnav.site.auto_phase import AUTO_PHASE_SETTLE_S, AutoPhaseTracker
from custom_components.spotnav.site.solar_surplus import SolarConfig, SolarController, SolarObservation

T0 = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
THREE = ("L1", "L2", "L3")


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# ------------------------------------------------------------------------------------------------ the state


def test_a_switch_settles_for_settle_s_and_counts_against_the_budget() -> None:
    state = PhaseSwitchState()
    state.sent(1, T0, counted=True)
    assert state.settling(_at(SETTLE_S - 1)) and not state.settling(_at(SETTLE_S))
    assert (state.switches, state.last_switch_at) == (1, T0)
    assert state.switched_phases is None, "one phase counts once the read-back shows it"


def test_setting_three_back_is_not_counted_and_is_three_at_once() -> None:
    state = PhaseSwitchState(switched_phases=1, switches=2)
    state.sent(3, T0, counted=False)
    assert state.switches == 2
    assert state.switched_phases is None


def test_the_read_back_confirms_or_the_switch_is_given_up() -> None:
    state = PhaseSwitchState()
    state.sent(1, T0, counted=True)
    assert state.confirm(3, _at(CONFIRM_S - 1)) is None
    assert state.confirm(1, _at(CONFIRM_S - 1)) == "confirmed"
    assert state.switched_phases == 1 and state.confirming is None

    state = PhaseSwitchState()
    state.sent(1, T0, counted=True)
    assert state.confirm(None, _at(CONFIRM_S)) == "given_up"
    assert state.given_up and state.given_up_count == 1
    assert state.switched_phases is None, "unknown is three"

    state = PhaseSwitchState(switched_phases=1)
    state.sent(3, T0, counted=True)
    assert state.confirm(1, _at(CONFIRM_S)) == "given_up"
    assert state.switched_phases == 1, "on whatever phases the read-back shows"


def test_the_read_back_wins_over_what_is_kept_but_not_over_a_switch_on_its_way() -> None:
    state = PhaseSwitchState(switched_phases=1)
    assert state.reconcile(None) is False and state.switched_phases == 1
    assert state.reconcile(3) is True and state.switched_phases is None
    state.sent(1, T0, counted=True)
    assert state.reconcile(3) is False


def test_a_new_plug_in_refills_the_budget_and_keeps_what_the_charger_is_set_to() -> None:
    state = PhaseSwitchState(switched_phases=1, switches=3, last_switch_at=T0, given_up=True)
    state.new_plug_in()
    assert (state.switches, state.last_switch_at, state.given_up) == (0, None, False)
    assert state.switched_phases == 1


def test_the_stored_record_round_trips_and_is_absent_while_there_is_nothing_to_keep() -> None:
    assert PhaseSwitchState().to_store() is None
    state = PhaseSwitchState(switched_phases=1, switches=2, last_switch_at=T0, given_up=True)
    back = PhaseSwitchState.from_store(state.to_store())
    assert (back.switched_phases, back.switches, back.last_switch_at, back.given_up) == (1, 2, T0, True)
    # Storage is untrusted.
    junk = PhaseSwitchState.from_store({"switched_phases": "1", "switches": True, "last_switch_at": "x", "given_up": 1})
    assert (junk.switched_phases, junk.switches, junk.last_switch_at, junk.given_up) == (None, 0, None, False)
    assert PhaseSwitchState.from_store([1]).switched_phases is None


# ---------------------------------------------------------------------------------------- the settle's readers


def test_the_plan_watch_does_not_take_a_settling_switch_for_the_car_ending_the_charge() -> None:
    watch = PlanChargeWatch()
    facts = {
        "plan_charge": True,
        "control_on": True,
        "connector_status": "SuspendedEV",
        "held": False,
        "balancing_paused": False,
        "start_pending": False,
    }
    assert not watch.observe(now=T0, drawing=True, **facts)  # type: ignore[arg-type]
    for second in range(0, int(PLAN_CAR_IDLE_S) + 60, 30):
        assert not watch.observe(now=_at(second), drawing=False, phase_switch_settling=True, **facts)  # type: ignore[arg-type]
    # Settled, the clock starts from there.
    assert not watch.observe(now=_at(400), drawing=False, **facts)  # type: ignore[arg-type]
    assert watch.observe(now=_at(400 + PLAN_CAR_IDLE_S), drawing=False, **facts)  # type: ignore[arg-type]


def test_a_switched_charge_is_dropped_from_the_phase_learning() -> None:
    observer = PhaseObserver()
    for _ in range(MIN_SAMPLES + 2):
        assert observer.sample(True, (13.0, 0.0, 0.0), switched=True) is None
    assert observer.sample(False, None) is None, "nothing learned from a forced one-phase charge"
    for _ in range(MIN_SAMPLES + 2):
        observer.sample(True, (13.0, 0.0, 0.0))
    assert observer.sample(False, None) == 1, "the same charge not switched is a one-phase one"


def test_no_new_automatic_phase_while_a_switch_settles() -> None:
    tracker = AutoPhaseTracker()
    tracker.observe("L1", 0.0)
    assert tracker.observe("L1", AUTO_PHASE_SETTLE_S) == "L1"
    assert tracker.observe("L2", AUTO_PHASE_SETTLE_S + 1, settling=True) == "L1"
    assert tracker.observe(None, AUTO_PHASE_SETTLE_S + 2, settling=True) == "L1"
    assert tracker.observe("L2", 100.0, settling=True) == "L1"
    # Settled: the new phase must hold on its own.
    assert tracker.observe("L2", 101.0) is None
    assert tracker.observe("L2", 101.0 + AUTO_PHASE_SETTLE_S) == "L2"


def _credited(now: float, battery_w: float, car_a: float, *, settling: bool = False) -> SolarObservation:
    return SolarObservation(
        now=now,
        signed_grid_w={p: 0.0 for p in THREE},
        voltage_v={p: 230.0 for p in THREE},
        car_delivered_a={p: car_a for p in THREE},
        battery_w=battery_w,
        car_phases=THREE,
        battery_configured=True,
        phase_switch_settling=settling,
    )


def test_a_credited_start_is_not_judged_while_a_switch_settles() -> None:
    """A battery-credited start whose car shows nothing while a phase switch settles is not a false credit."""
    six_a_w = 6 * 3 * 230.0
    solar = SolarController(SolarConfig(verify_s=120.0, min_off_s=0.0))
    solar.observe(_credited(0.0, 8000.0, 0.0))
    assert solar.observe(_credited(120.0, 8000.0, 0.0)).action == "start"
    settling = [solar.observe(_credited(t, -six_a_w, 6.0, settling=True)) for t in (150.0, 170.0, 200.0)]
    assert all(v.action != "stop" for v in settling)
    # Not settling, the same reading is the false credit it is.
    verdict = solar.observe(replace(_credited(210.0, -six_a_w, 6.0), phase_switch_settling=False))
    assert (verdict.action, verdict.reason) == ("stop", "battery_credit_false")
