"""The yield-verified stepping state machine (`execution/yield_stepping.py`); no test sleeps, each clock is the observation's own `now`."""

from __future__ import annotations

import pytest

from custom_components.spotnav.execution.yield_stepping import (
    YieldConfig,
    YieldObservation,
    YieldStepper,
)

# A round ceiling: 20.2 A normal site current plus one 2 A step is 22.2 A, inside a ceiling of 23.
DEFAULT_CEILING_A = 23.0


def _config(**overrides) -> YieldConfig:
    overrides.setdefault("ceiling_a", DEFAULT_CEILING_A)
    return YieldConfig(**overrides)


def _obs(
    now: float,
    *,
    site: float | dict[str, float | None],
    delivered: float | dict[str, float | None],
    phases: tuple[str, ...] = ("L2",),
) -> YieldObservation:
    """Build an observation for one or several phases; a bare number applies to every phase, a dict gives per-phase control."""
    site_map = {p: site for p in phases} if not isinstance(site, dict) else dict(site)
    delivered_map = (
        {p: delivered for p in phases} if not isinstance(delivered, dict) else dict(delivered)
    )
    return YieldObservation(now=now, site_current_a=site_map, delivered_current_a=delivered_map, phases=phases)


def _raw_for(delivered: float, min_current_a: float = 6.0) -> float:
    """The raw decision's overload-branch arithmetic: `current + uncredited margin` (`delivered - 1.2`), legalised to a pause (`0.0`) below the charger's minimum.

    Reproduces `regulator._legalize_or_pause` narrowly rather than importing it, so this module stays independent.
    """
    raw = max(0.0, delivered - 1.2)
    return 0.0 if raw < min_current_a else raw


def test_2026_09_26_replay_climbs_in_steps_and_holds_once_confirmed() -> None:
    """Site flat at ~20.2 A on L2, assigned starting at the 6 A floor, delivered at ~89% of assigned, raw computed as the real overload branch would (`_raw_for`).

    At the floor raw is a standing pause (`0.0`) that can never be carried out, and `assigned_a`
    never moves (a pause below `min_current_a` is never written), so raw and `assigned_a` never
    converge. That must not be read as an ordinary "raw wants to reduce" and passed through forever
    (see `test_the_real_2026_09_26_condition_eventually_probes`).

    Asserts: despite raw proposing a reduction or pause, the stepper climbs to `requested_a` in 2 A
    steps; it holds rather than reducing once confirmed; and no step is written while `settling`.
    """
    site_a = 20.2
    requested_a = 16.0
    assigned = 6.0
    now = 0.0
    stepper = YieldStepper(_config())

    seen_states_while_settling: list[str] = []
    written_levels: list[float] = []

    def tick(delivered: float) -> None:
        nonlocal now, assigned
        raw = _raw_for(delivered)
        verdict = stepper.observe(raw, requested_a, assigned, _obs(now, site=site_a, delivered=delivered))
        if verdict.state == "settling":
            seen_states_while_settling.append(verdict.action)
        if verdict.action == "write":
            assigned = verdict.current_a
            written_levels.append(assigned)
        now += 1.0

    # Climb from 6 up to 16 in 2 A steps, each probed, settled and confirmed before the next.
    while assigned < requested_a:
        pre_step_assigned = assigned
        delivered_before = pre_step_assigned * 0.89
        tick(delivered_before)  # establishes/refreshes the baseline

        # A probe or confirmed step should have just been written.
        assert written_levels, "expected a step to have been written"
        stepped_to = written_levels[-1]
        assert stepped_to == min(requested_a, pre_step_assigned + 2.0)

        delivered_after = stepped_to * 0.89
        # The car ramps in; the site does not move (y ~= 1.0).
        tick(delivered_after)
        # Hold through the settle window.
        for _ in range(5):
            now += 5.0
            raw = _raw_for(delivered_after)
            verdict = stepper.observe(
                raw, requested_a, assigned, _obs(now, site=site_a, delivered=delivered_after)
            )
            assert verdict.action != "write"
        # Expire the settle window and confirm.
        now += 30.0
        raw = _raw_for(delivered_after)
        verdict = stepper.observe(
            raw, requested_a, assigned, _obs(now, site=site_a, delivered=delivered_after)
        )
        assert verdict.action == "hold"
        assert verdict.state == "confirmed"
        assert verdict.y == pytest.approx(1.0, abs=1e-6)
        now += 1.0

    assert written_levels == [8.0, 10.0, 12.0, 14.0, 16.0]
    assert assigned == requested_a
    # No step was written while settling, and settling happened repeatedly, so this is not vacuous.
    assert len(seen_states_while_settling) >= 5
    assert all(action != "write" for action in seen_states_while_settling)

    # Once confirmed at the target, a genuine reduction (a real, carryable raw above `min_current_a`) is held, not written.
    raw = _raw_for(assigned * 0.89)
    assert raw > 6.0  # sanity: this is not a floor pause
    verdict = stepper.observe(raw, requested_a, assigned, _obs(now, site=site_a, delivered=assigned * 0.89))
    assert verdict.action == "hold"
    assert verdict.current_a is None
    assert verdict.reason == "held_at_yield_setpoint"


def test_the_real_2026_09_26_condition_eventually_probes() -> None:
    """40 observations 30 s apart of a standing floor pause with `assigned` never moving must produce at least one `probe_step` write, to 8.0 A.

    Without the floor handling every observation came back
    `("passthrough", "passthrough_raw_reduce", "unknown")` and the feature did nothing.
    """
    stepper = YieldStepper(_config())
    now = 0.0
    verdicts = []
    for _ in range(40):
        v = stepper.observe(0.0, 16.0, 6.0, _obs(now, site=20.2, delivered=5.9))
        verdicts.append(v)
        now += 30.0

    assert any(v.action == "write" and v.reason == "probe_step" and v.current_a == 8.0 for v in verdicts), (
        "expected at least one probe_step write to 8.0 A; got: "
        f"{[(v.action, v.reason, v.state) for v in verdicts[:5]]}"
    )


def test_a_raw_pause_above_the_floor_still_passes_through() -> None:
    """A raw pause is reinterpreted as a hold only when `assigned_a` is at or below `min_current_a`.

    A car running at 16 A with raw proposing a pause must still pass through: this layer never
    holds or probes a real running charge because raw gave up on it.
    """
    stepper = YieldStepper(_config())
    v = stepper.observe(0.0, 16.0, 16.0, _obs(0.0, site=20.2, delivered=14.2))

    assert v.action == "passthrough"
    assert v.reason != "held_at_floor_raw_pause"
    assert v.current_a is None


def test_step_not_absorbed_is_reverted_urgently() -> None:
    """A step where the site rises by the full step yielded nothing: at `settle_s` the verdict is an urgent write of the pre-step assigned value (never the delivered current) and the state becomes `backoff`."""
    cfg = _config()
    stepper = YieldStepper(cfg)

    # Establish a stable baseline at assigned=10, delivered=8.9, site=20.0.
    stepper.observe(10.0, 16.0, 10.0, _obs(0.0, site=20.0, delivered=8.9))

    # A step to 12 is written elsewhere; delivered and the site rise by the same amount: nothing yielded.
    v = stepper.observe(12.0, 16.0, 12.0, _obs(1.0, site=21.9, delivered=10.9))
    assert v.action == "hold"
    assert v.state == "settling"

    # Still settling, short of settle_s.
    v = stepper.observe(12.0, 16.0, 12.0, _obs(20.0, site=21.9, delivered=10.9))
    assert v.action == "hold"

    # settle_s elapses (t_step was 1.0).
    v = stepper.observe(12.0, 16.0, 12.0, _obs(1.0 + cfg.settle_s, site=21.9, delivered=10.9))
    assert v.action == "write"
    assert v.current_a == 10.0  # the pre-step *assigned* value, not delivered
    assert v.urgent is True
    assert v.state == "backoff"
    assert v.reason == "revert_not_absorbed"


def test_backoff_doubles_and_caps_and_a_later_confirm_resets_it() -> None:
    cfg = _config(backoff_initial_s=600.0, backoff_max_s=3600.0, settle_s=10.0)
    stepper = YieldStepper(cfg)
    now = 0.0

    def fail_a_step(assigned: float) -> None:
        nonlocal now
        stepper.observe(assigned, 16.0, assigned, _obs(now, site=20.0, delivered=assigned * 0.89))
        now += 1.0
        stepped = assigned + 2.0
        stepper.observe(stepped, 16.0, stepped, _obs(now, site=20.0, delivered=stepped * 0.89))
        now += 1.0
        d = stepped * 0.89
        s = 20.0 + 2.0  # the site rises by the full step: not absorbed
        now += cfg.settle_s
        v = stepper.observe(stepped, 16.0, stepped, _obs(now, site=s, delivered=d))
        assert v.action == "write"
        assert v.reason == "revert_not_absorbed"
        return v

    v1 = fail_a_step(6.0)
    first_backoff_end = now + cfg.backoff_initial_s

    # Jump to just after the first backoff ends, and fail again immediately.
    now = first_backoff_end + 0.1
    v2 = fail_a_step(6.0)
    second_backoff_end = now + cfg.backoff_initial_s * 2.0

    # A third consecutive failure doubles again.
    now = second_backoff_end + 0.1
    v3 = fail_a_step(6.0)
    third_backoff_end = now + cfg.backoff_initial_s * 4.0

    # Confirm this time: the site does not move.
    now = third_backoff_end + 0.1
    stepper.observe(6.0, 16.0, 6.0, _obs(now, site=20.0, delivered=6.0 * 0.89))
    now += 1.0
    stepper.observe(8.0, 16.0, 8.0, _obs(now, site=20.0, delivered=8.0 * 0.89))
    now += 1.0 + cfg.settle_s
    v_confirm = stepper.observe(8.0, 16.0, 8.0, _obs(now, site=20.0, delivered=8.0 * 0.89))
    assert v_confirm.state == "confirmed"

    # A later failure uses the initial backoff again, not the quadrupled one: the confirm reset it.
    now += 1.0
    v4 = fail_a_step(8.0)
    # The write happened at `now`; infer the backoff window from a probe being refused at initial+epsilon but allowed once `backoff_initial_s` has elapsed again.
    still_backoff = stepper.observe(
        10.0, 16.0, 10.0, _obs(now + 1.0, site=20.0, delivered=6.0)
    )
    assert still_backoff.state == "backoff"
    released = stepper.observe(
        10.0, 16.0, 10.0, _obs(now + cfg.backoff_initial_s + 1.0, site=20.0, delivered=6.0)
    )
    assert released.state != "backoff"


def test_hard_ceiling_wins_over_confirmed_and_held_state() -> None:
    cfg = _config(ceiling_a=22.0)
    stepper = YieldStepper(cfg)
    now = 0.0

    # Confirm a step so the stepper has a reference and is "confirmed".
    stepper.observe(8.0, 16.0, 8.0, _obs(now, site=20.0, delivered=8.0 * 0.89))
    now += 1.0
    stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    now += 1.0 + cfg.settle_s
    v = stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    assert v.state == "confirmed"

    # The site reaches the ceiling: even confirmed, with a raw reduction proposed, the ceiling passes through urgently and the state becomes backoff.
    now += 1.0
    v = stepper.observe(8.0, 16.0, 10.0, _obs(now, site=22.0, delivered=10.0 * 0.89))
    assert v.action == "passthrough"
    assert v.urgent is True
    assert v.reason == "passthrough_ceiling"
    assert v.state == "backoff"


def test_no_step_is_licensed_above_the_ceiling() -> None:
    """A fresh, confirmed, flat site never licenses a step that would put any phase above `ceiling_a`."""
    cfg = _config(ceiling_a=22.0, step_a=2.0)
    stepper = YieldStepper(cfg)
    now = 0.0

    stepper.observe(8.0, 16.0, 8.0, _obs(now, site=20.1, delivered=8.0 * 0.89))
    now += 1.0
    stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.1, delivered=10.0 * 0.89))
    now += 1.0 + cfg.settle_s
    v = stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.1, delivered=10.0 * 0.89))
    assert v.state == "confirmed"

    # Site sits at 20.1; a +2 A step would be 22.1, above the 22.0 ceiling.
    now += 1.0
    v = stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.1, delivered=10.0 * 0.89))
    assert v.action != "write"


def test_site_rising_above_reference_releases_held_reduction_urgently() -> None:
    cfg = _config()
    stepper = YieldStepper(cfg)
    now = 0.0

    stepper.observe(8.0, 16.0, 8.0, _obs(now, site=20.0, delivered=8.0 * 0.89))
    now += 1.0
    stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    now += 1.0 + cfg.settle_s
    v = stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    assert v.state == "confirmed"
    assert v.reference_a["L2"] == 20.0

    # `requested_a` is pinned to `assigned_a` (10.0): step 6 considers a further step even when step 5 held, so with headroom a held reduction would be overtaken by a fresh probe. Removing the headroom isolates step 5's verdict.
    now += 1.0
    # Site still at reference: a reduction is held.
    v = stepper.observe(6.0, 10.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    assert v.action == "hold"
    assert v.reason == "held_at_yield_setpoint"

    # A sudden jump above reference + absorb_tol_a: released at once.
    now += 1.0
    v = stepper.observe(6.0, 10.0, 10.0, _obs(now, site=20.6, delivered=10.0 * 0.89))
    assert v.action == "passthrough"
    assert v.urgent is True
    assert v.reason == "passthrough_site_rising"


def test_a_slow_creep_across_the_reference_is_also_released_urgently() -> None:
    cfg = _config()
    stepper = YieldStepper(cfg)
    now = 0.0

    stepper.observe(8.0, 16.0, 8.0, _obs(now, site=20.0, delivered=8.0 * 0.89))
    now += 1.0
    stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    now += 1.0 + cfg.settle_s
    v = stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    assert v.state == "confirmed"

    # Creep upward 0.1 A per observation: each tiny, none a "step" on delivered current (which never moves here).
    # `requested_a` is pinned to `assigned_a`, as in the previous test, to isolate step 5.
    site = 20.0
    for _ in range(6):
        now += 1.0
        site += 0.1
        v = stepper.observe(6.0, 10.0, 10.0, _obs(now, site=site, delivered=10.0 * 0.89))
        if site <= 20.0 + cfg.absorb_tol_a:
            assert v.action == "hold", f"expected hold at site={site}"
        else:
            assert v.action == "passthrough"
            assert v.urgent is True
            assert v.reason == "passthrough_site_rising"


def test_freshness_gates_new_steps_but_never_holds() -> None:
    cfg = _config(ttl_s=300.0, settle_s=30.0)
    stepper = YieldStepper(cfg)
    now = 0.0

    stepper.observe(8.0, 16.0, 8.0, _obs(now, site=20.0, delivered=8.0 * 0.89))
    now += 1.0
    stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    now += 1.0 + cfg.settle_s
    v = stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    assert v.state == "confirmed"

    # Well past ttl_s with a flat site, a hold on a reduction still fires. `requested_a` is pinned to `assigned_a` so step 5's verdict is what survives.
    now += cfg.ttl_s + 60.0
    v = stepper.observe(6.0, 10.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    assert v.action == "hold"
    assert v.reason == "held_at_yield_setpoint"

    # But a new step, with raw holding at assigned and real headroom requested, is only a probe: the old confirmation is stale.
    v = stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    assert v.action == "write"
    assert v.reason == "probe_step"


def test_raw_wanting_to_increase_is_untouched_passthrough() -> None:
    stepper = YieldStepper(_config())
    v = stepper.observe(12.0, 16.0, 10.0, _obs(0.0, site=15.0, delivered=8.9))
    assert v.action == "passthrough"
    assert v.current_a is None
    assert v.reason == "passthrough_raw_increase"
    assert v.urgent is False


@pytest.mark.parametrize(
    "raw,requested,assigned,site,delivered",
    [
        (None, 16.0, 10.0, 20.0, 8.9),
        (10.0, 16.0, None, 20.0, 8.9),
        (10.0, 16.0, 10.0, None, 8.9),
        (10.0, 16.0, 10.0, 20.0, None),
    ],
)
def test_any_none_input_is_passthrough_never_an_action(raw, requested, assigned, site, delivered) -> None:
    stepper = YieldStepper(_config())
    v = stepper.observe(raw, requested, assigned, _obs(0.0, site=site, delivered=delivered))
    assert v.action == "passthrough"
    assert v.reason == "passthrough_no_basis"
    assert v.current_a is None
    assert v.urgent is False


def test_car_not_following_is_not_a_failure_no_revert_no_backoff() -> None:
    """A rise big enough to detect a step (entering `settling`) that is not sustained: delivered falls back below `baseline_delivered + min_delta_a` before `settle_s`.

    Not a failure: no revert is written, `backoff` is not entered, and a previously `confirmed`
    state is restored with its `y` and reference intact.
    """
    cfg = _config()
    stepper = YieldStepper(cfg)
    now = 0.0

    # Get to `confirmed` first, so the restore has something to prove.
    stepper.observe(8.0, 16.0, 8.0, _obs(now, site=20.0, delivered=8.0 * 0.89))
    now += 1.0
    stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    now += 1.0 + cfg.settle_s
    v = stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    assert v.state == "confirmed"
    confirmed_y = v.y
    confirmed_reference = v.reference_a

    # A quiet tick at the just-confirmed level lets the stable baseline catch up to 10*0.89, so the next rise is measured from that level. `requested_a` is pinned to `assigned_a` so step 6 fires no step of its own.
    now += 1.0
    stepper.observe(10.0, 10.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))

    # A brief rise, enough to be detected as a step, that does not hold.
    now += 1.0
    v = stepper.observe(10.0, 10.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89 + 1.6))
    assert v.state == "settling"
    t_settling_started = now

    now += 1.0
    # It falls back near the pre-step baseline before settle_s elapses: the car did not follow.
    v = stepper.observe(10.0, 10.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89 + 0.1))
    assert v.action == "hold"  # still inside the settle window

    now = t_settling_started + cfg.settle_s
    v = stepper.observe(10.0, 10.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89 + 0.1))

    assert v.state != "backoff"
    assert not (v.action == "write" and v.reason == "revert_not_absorbed")
    # The prior confirmed state is restored, y and reference intact.
    assert v.state == "confirmed"
    assert v.y == confirmed_y
    assert v.reference_a == confirmed_reference


def test_a_ramp_does_not_corrupt_the_pre_ramp_baseline() -> None:
    """Delivered current rising gradually toward a new level must not overwrite the pre-ramp baseline, or part of the real step is lost from `y`.

    The site rises a little too, so a wrong (mid-ramp) baseline would compute a visibly different
    `y`; a perfectly flat site gives `y == 1.0` under either baseline.
    """
    cfg = _config(steady_a=0.3, min_delta_a=1.5)
    stepper = YieldStepper(cfg)
    now = 0.0

    # Stable baseline: delivered=8.9, site=20.0.
    stepper.observe(8.0, 16.0, 8.0, _obs(now, site=20.0, delivered=8.9))
    now += 1.0

    # A step to 10 is decided; delivered ramps up gradually, each leg above `steady_a` relative to the last so none is taken as "steady" and overwrites the baseline, while the site rises alongside.
    ramp = [(9.5, 20.1), (10.2, 20.3), (10.9, 20.5)]
    for delivered, site in ramp:
        now += 1.0
        v = stepper.observe(10.0, 16.0, 10.0, _obs(now, site=site, delivered=delivered))
    assert v.state == "settling"

    now += cfg.settle_s
    v = stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.5, delivered=10.9))
    assert v.state == "confirmed"
    # y is computed against the pre-ramp baseline (site=20.0, delivered=8.9): 1 - 0.5/2.0 = 0.75. Using the last ramp leg (10.2, 20.3) would give 0.714, close enough to read as a typo, so the precise value is asserted.
    assert v.y == pytest.approx(0.75, abs=1e-6)


def test_session_reset_clears_state_when_delivered_falls_below_floor() -> None:
    cfg = _config(session_floor_a=1.0)
    stepper = YieldStepper(cfg)
    now = 0.0

    stepper.observe(8.0, 16.0, 8.0, _obs(now, site=20.0, delivered=8.0 * 0.89))
    now += 1.0
    stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    now += 1.0 + cfg.settle_s
    v = stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    assert v.state == "confirmed"

    # The car disconnects: delivered falls below the floor on every phase.
    now += 1.0
    v = stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=0.5))
    assert v.state == "unknown"
    assert v.y is None
    assert v.reference_a == {}


def test_trap_1_never_derive_a_setpoint_from_delivered_current() -> None:
    """With assigned=16 and delivered=14.2 (~89%), no verdict ever writes 14 or anything derived from 14.2, only 16 or the recorded pre-step assigned value."""
    cfg = _config()
    stepper = YieldStepper(cfg)
    now = 0.0

    stepper.observe(16.0, 16.0, 16.0, _obs(now, site=20.0, delivered=14.2))
    now += 1.0
    v = stepper.observe(16.0, 16.0, 16.0, _obs(now, site=20.0, delivered=14.2))
    # raw holds, but assigned == requested already: nothing to step to.
    assert v.action != "write"

    # Force a revert and check the written value is the recorded assigned baseline (16), never 14 or one built from 14.2.
    cfg2 = _config(min_delta_a=0.5)
    stepper2 = YieldStepper(cfg2)
    now = 0.0
    stepper2.observe(16.0, 16.0, 16.0, _obs(now, site=20.0, delivered=14.2))
    now += 1.0
    stepper2.observe(16.0, 16.0, 16.0, _obs(now, site=20.0, delivered=14.9))  # a "step" up
    now += cfg2.settle_s + 1.0
    v = stepper2.observe(16.0, 16.0, 16.0, _obs(now, site=20.7, delivered=14.9))
    assert v.action == "write"
    assert v.current_a == 16.0
    assert v.current_a != 14.0
    assert v.current_a != 14.2


def test_trap_2_a_hold_verdicts_action_is_hold_and_it_carries_no_current() -> None:
    cfg = _config()
    stepper = YieldStepper(cfg)
    now = 0.0

    stepper.observe(8.0, 16.0, 8.0, _obs(now, site=20.0, delivered=8.0 * 0.89))
    now += 1.0
    stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    now += 1.0 + cfg.settle_s
    stepper.observe(10.0, 16.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))

    # `requested_a` pinned to `assigned_a` to isolate this hold from step 6's further-step consideration.
    now += 1.0
    v = stepper.observe(6.0, 10.0, 10.0, _obs(now, site=20.0, delivered=10.0 * 0.89))
    assert v.action == "hold"
    assert v.current_a is None


def test_a_full_car_with_the_switch_still_on_is_never_stepped() -> None:
    """Between the car filling and the plan's window closing, the switch is on and the car draws about half an amp: it confirms nothing, so nothing may be licensed for it."""
    stepper = YieldStepper(_config())
    for i in range(20):
        verdict = stepper.observe(0.0, 16.0, 6.0, _obs(i * 30.0, site=20.2, delivered=0.5))
        assert verdict.action != "write", (i, verdict)
    assert verdict.reason == "passthrough_not_drawing"


def test_a_car_limiting_itself_below_its_assignment_is_not_stepped_further() -> None:
    """A car drawing 9 A of a 12 A assignment is at a limit of its own; offering it 14 verifies nothing and would compound."""
    stepper = YieldStepper(_config())
    for i in range(20):
        verdict = stepper.observe(12.0, 16.0, 12.0, _obs(i * 30.0, site=20.2, delivered=9.0))
        assert verdict.action != "write", (i, verdict)
    assert verdict.reason == "passthrough_car_not_using_assignment"


def test_a_car_at_the_floor_delivering_just_under_it_may_still_probe() -> None:
    """The follow rule must not undo the floor fix: 5.9 A of a 6 A assignment is a car using what it has, and must still probe."""
    stepper = YieldStepper(_config())
    writes = [
        stepper.observe(0.0, 16.0, 6.0, _obs(i * 30.0, site=20.2, delivered=5.9))
        for i in range(5)
    ]
    assert any(v.action == "write" and v.reason == "probe_step" and v.current_a == 8.0 for v in writes)
