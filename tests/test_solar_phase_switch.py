"""The solar controller's phase decision (`site/solar_surplus.py`): a start's phases, the switch down on the stop
delay's own timer, the switch up after `UP_DELAY_S`, the budget per plug-in, and nothing at all where switching is
not allowed. Pure; each clock is the observation's own `now`."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from custom_components.spotnav.site.solar_surplus import (
    MAX_SWITCHES_PER_SESSION,
    MIN_BETWEEN_S,
    UP_DELAY_S,
    UP_MARGIN,
    SolarConfig,
    SolarController,
    SolarObservation,
    SolarVerdict,
)

THREE_PHASES = ("L1", "L2", "L3")
VOLTAGE_V = 230.0
MIN_A = 6.0
ONE_MIN_W = MIN_A * VOLTAGE_V  # 1380 W
THREE_MIN_W = 3 * MIN_A * VOLTAGE_V  # 4140 W
UP_W = UP_MARGIN * THREE_MIN_W  # 4554 W
ONE_MAX_A = 16.0
ONE_MAX_W = ONE_MAX_A * VOLTAGE_V  # 3680 W


def _obs(
    now: float,
    surplus_w: float,
    *,
    phases: int = 3,
    drawing_a: float = MIN_A,
    allowed: bool = True,
    one_max_a: float | None = ONE_MAX_A,
    room: bool = True,
    settling: bool = False,
) -> SolarObservation:
    """An observation whose surplus (the car's own draw counted in) is `surplus_w`, with the car drawing
    `drawing_a` on each of its `phases`."""
    car_phases = THREE_PHASES if phases == 3 else ("L1",)
    car_w = drawing_a * len(car_phases) * VOLTAGE_V
    net_grid_w = car_w - surplus_w
    return SolarObservation(
        now=now,
        signed_grid_w={p: net_grid_w / 3 for p in THREE_PHASES},
        voltage_v={p: VOLTAGE_V for p in THREE_PHASES},
        car_delivered_a={p: (drawing_a if p in car_phases else 0.0) for p in THREE_PHASES},
        battery_w=None,
        car_phases=car_phases,
        phase_switch_allowed=allowed,
        phases_now=3 if phases == 3 else 1,
        one_phase_max_a=one_max_a,
        three_phase_room=room,
        phase_switch_settling=settling,
    )


def _config() -> SolarConfig:
    return SolarConfig(verify_s=0.0)


def _running(now: float = 0.0) -> SolarController:
    """A controller with a charge of its own running since long ago (no `min_on_s` left)."""
    solar = SolarController(_config())
    solar.adopt(now, requested_a=MIN_A, on_since=now - 10_000.0)
    return solar


def _run(solar: SolarController, start: float, end: float, surplus_w: float, step: float = 10.0, **kw) -> list[SolarVerdict]:
    verdicts = []
    t = start
    while t <= end:
        verdicts.append(solar.observe(_obs(t, surplus_w, **kw)))
        t += step
    return verdicts


def _switches(verdicts: list[SolarVerdict]) -> list[int]:
    return [v.switch_phases for v in verdicts if v.switch_phases is not None]


# --- Start -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("surplus_w", "allowed", "expected"),
    [
        (2000.0, True, 1),  # carries one phase at the minimum, not three
        (5000.0, True, 3),  # carries three
        (5000.0, False, None),  # not allowed: the charger's own phases, as before
    ],
)
def test_a_solar_start_is_made_on_the_phases_the_surplus_carries(surplus_w, allowed, expected):
    solar = SolarController(_config())
    verdicts = _run(solar, 0.0, 200.0, surplus_w, drawing_a=0.0, allowed=allowed)
    starts = [v for v in verdicts if v.action == "start"]
    assert len(starts) == 1
    assert starts[0].start_phases == expected
    assert starts[0].switch_phases is None


def test_without_switching_a_surplus_for_one_phase_does_not_start_a_three_phase_charge():
    solar = SolarController(_config())
    verdicts = _run(solar, 0.0, 600.0, 2000.0, drawing_a=0.0, allowed=False)
    assert all(v.action == "hold" for v in verdicts)
    assert verdicts[-1].reason == "off_no_surplus"


@pytest.mark.parametrize(("surplus_w", "starts"), [(6.5 * VOLTAGE_V, False), (7.2 * VOLTAGE_V, True)])
def test_a_one_phase_start_needs_the_chargers_start_minimum_on_that_phase(surplus_w, starts):
    # Easee starts at 7 A or more (`min_start_current_a`), which the solar config's `start_a` carries.
    solar = SolarController(SolarConfig(verify_s=0.0, start_a=7.0))
    verdicts = _run(solar, 0.0, 600.0, surplus_w, drawing_a=0.0)
    started = [v for v in verdicts if v.action == "start"]
    if not starts:
        assert started == []
        return
    assert len(started) == 1
    assert started[0].start_phases == 1
    assert started[0].requested_a >= 7.0


def test_a_surplus_short_of_one_phase_does_not_start():
    solar = SolarController(_config())
    verdicts = _run(solar, 0.0, 600.0, ONE_MIN_W - 50.0, drawing_a=0.0)
    assert all(v.action == "hold" for v in verdicts)


# --- Down --------------------------------------------------------------------------------------------------------


def test_on_three_phases_short_of_the_minimum_the_stop_delay_switches_to_one_phase():
    solar = _running()
    cfg = solar.config
    verdicts = _run(solar, 0.0, cfg.stop_delay_s - 1.0, 3000.0, step=1.0)
    assert {v.reason for v in verdicts} == {"disarming_delay"}
    assert _switches(verdicts) == []
    verdict = solar.observe(_obs(cfg.stop_delay_s, 3000.0))
    assert verdict.switch_phases == 1
    assert verdict.reason == "phase_down"
    assert verdict.action == "hold"
    assert verdict.state == "on"
    assert solar.running


def test_between_the_stop_level_and_the_minimum_the_same_timer_switches_down():
    """Short of three phases at the minimum but above the stop level: without switching the charge runs on at the
    minimum; with it, the stop delay's timer runs for the switch."""
    surplus_w = 3800.0  # 5.5 A per phase: above `stop_a` (5 A), below the minimum (6 A)
    plain = _running()
    assert _switches(_run(plain, 0.0, 1000.0, surplus_w, allowed=False)) == []
    assert plain.observe(_obs(1010.0, surplus_w, allowed=False)).reason == "on_steady"

    solar = _running()
    verdicts = _run(solar, 0.0, solar.config.stop_delay_s, surplus_w, step=1.0)
    assert _switches(verdicts) == [1]
    assert verdicts[-1].reason == "phase_down"


def test_a_cloud_shorter_than_the_stop_delay_does_nothing():
    solar = _running()
    delay = solar.config.stop_delay_s
    verdicts = _run(solar, 0.0, delay - 60.0, 3000.0)
    verdicts += _run(solar, delay - 50.0, delay + 100.0, 5000.0)
    # The cloud comes back: the delay starts afresh, it does not continue.
    verdicts += _run(solar, delay + 110.0, delay + 110.0 + delay - 10.0, 3000.0)
    assert _switches(verdicts) == []
    assert all(v.action != "stop" for v in verdicts)


def test_a_surplus_that_does_not_carry_one_phase_stops_without_a_switch():
    """evcc #32991: a switch followed by a stop is two interruptions for nothing."""
    solar = _running()
    verdicts = _run(solar, 0.0, solar.config.stop_delay_s, ONE_MIN_W - 100.0, step=1.0)
    assert _switches(verdicts) == []
    assert verdicts[-1].action == "stop"
    assert verdicts[-1].reason == "stop_after_delay"


def test_not_allowed_the_stop_delay_stops_as_before():
    solar = _running()
    verdicts = _run(solar, 0.0, solar.config.stop_delay_s, 3000.0, step=1.0, allowed=False)
    assert _switches(verdicts) == []
    assert verdicts[-1].reason == "stop_after_delay"


# --- Up ----------------------------------------------------------------------------------------------------------


def test_on_one_phase_at_its_maximum_a_surplus_for_three_switches_up_after_the_delay():
    solar = _running()
    verdicts = _run(solar, 0.0, UP_DELAY_S - 1.0, 5000.0, phases=1, drawing_a=ONE_MAX_A, step=1.0)
    assert _switches(verdicts) == []
    verdict = solar.observe(_obs(UP_DELAY_S, 5000.0, phases=1, drawing_a=ONE_MAX_A))
    assert verdict.switch_phases == 3
    assert verdict.reason == "phase_up"
    assert verdict.state == "on"
    # The next modulation writes what the surplus carries on three phases.
    nxt = solar.observe(_obs(UP_DELAY_S + 10.0, 5000.0, phases=3, drawing_a=MIN_A))
    assert nxt.action == "set_current"
    assert nxt.requested_a == 7.0


@pytest.mark.parametrize(
    "kw",
    [
        {"surplus_w": UP_W - 20.0},  # short of the margin
        {"surplus_w": 5000.0, "room": False},  # no room on the site for three phases
        {"surplus_w": 5000.0, "one_max_a": 22.0},  # one phase not yet at its maximum (22 A = 5060 W)
        {"surplus_w": 5000.0, "one_max_a": None},  # maximum unknown
        {"surplus_w": 5000.0, "settling": True},
        {"surplus_w": 5000.0, "allowed": False},
    ],
)
def test_no_switch_up_without_every_condition(kw):
    solar = _running()
    surplus_w = kw.pop("surplus_w")
    verdicts = _run(solar, 0.0, 3 * UP_DELAY_S, surplus_w, phases=1, drawing_a=ONE_MAX_A, **kw)
    assert _switches(verdicts) == []


def test_the_up_delay_starts_afresh_when_the_surplus_falls_away():
    solar = _running()
    verdicts = _run(solar, 0.0, UP_DELAY_S - 100.0, 5000.0, phases=1, drawing_a=ONE_MAX_A)
    verdicts.append(solar.observe(_obs(UP_DELAY_S - 90.0, 4000.0, phases=1, drawing_a=ONE_MAX_A)))
    verdicts += _run(solar, UP_DELAY_S - 80.0, 2 * UP_DELAY_S - 100.0, 5000.0, phases=1, drawing_a=ONE_MAX_A)
    assert _switches(verdicts) == []
    verdicts = _run(solar, 2 * UP_DELAY_S - 90.0, 2 * UP_DELAY_S, 5000.0, phases=1, drawing_a=ONE_MAX_A)
    assert _switches(verdicts) == [3]


def test_settling_resets_the_up_delay():
    solar = _running()
    _run(solar, 0.0, UP_DELAY_S - 10.0, 5000.0, phases=1, drawing_a=ONE_MAX_A)
    solar.observe(_obs(UP_DELAY_S - 5.0, 5000.0, phases=1, drawing_a=ONE_MAX_A, settling=True))
    verdicts = _run(solar, UP_DELAY_S, 2 * UP_DELAY_S - 10.0, 5000.0, phases=1, drawing_a=ONE_MAX_A)
    assert _switches(verdicts) == []


# --- Dead band ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("surplus_w", [THREE_MIN_W + 10.0, 4300.0, UP_W - 10.0])
def test_a_surplus_in_the_dead_band_switches_neither_way(surplus_w):
    """Between three phases at the minimum and the up margin over it: three phases stay on three, one stays on one."""
    on_three = _running()
    assert _switches(_run(on_three, 0.0, 4 * UP_DELAY_S, surplus_w)) == []
    on_one = _running()
    assert _switches(_run(on_one, 0.0, 4 * UP_DELAY_S, surplus_w, phases=1, drawing_a=ONE_MAX_A)) == []


def test_no_flapping_between_one_phase_at_its_maximum_and_three_at_the_minimum():
    """A surplus that wobbles across both edges of the band, following the phases the controller chose, makes no
    switch inside `MIN_BETWEEN_S` of the last and stays within the budget."""
    solar = _running()
    phases = 3
    switch_times: list[float] = []
    t = 0.0
    while t < 6 * 3600.0:
        surplus_w = 4200.0 if int(t // 400) % 2 else 4100.0
        verdict = solar.observe(_obs(t, surplus_w, phases=phases, drawing_a=MIN_A if phases == 3 else ONE_MAX_A))
        if verdict.switch_phases is not None:
            phases = verdict.switch_phases
            switch_times.append(t)
        t += 10.0
    # Down once (4100 W for the stop delay); 4200 W never reaches the up margin.
    assert len(switch_times) == 1


# --- Budget ------------------------------------------------------------------------------------------------------


def test_switches_are_spaced_and_counted_per_plug_in():
    solar = _running()
    phases = 3
    switch_times: list[float] = []
    t = 0.0
    while t < 8 * 3600.0:
        # Strong sun and clouds in long spells: each spell would switch.
        surplus_w = 6000.0 if int(t // 1200) % 2 else 3000.0
        verdict = solar.observe(_obs(t, surplus_w, phases=phases, drawing_a=MIN_A if phases == 3 else ONE_MAX_A))
        if verdict.switch_phases is not None:
            phases = verdict.switch_phases
            switch_times.append(t)
        t += 10.0
    assert len(switch_times) == MAX_SWITCHES_PER_SESSION
    assert all(b - a >= MIN_BETWEEN_S for a, b in zip(switch_times, switch_times[1:]))
    # Beyond the budget, on three phases, the stop rule applies again.
    assert phases == 1  # down, up, down

    # A passing reading of no car does not refill the budget; a new plug-in does.
    solar.car_absent(_obs(t, 0.0, phases=phases))
    solar.adopt(t, requested_a=MIN_A, on_since=t - 10_000.0)
    verdicts = _run(solar, t, t + UP_DELAY_S + 10.0, 6000.0, phases=1, drawing_a=ONE_MAX_A)
    assert _switches(verdicts) == []
    t += UP_DELAY_S + 20.0
    solar.new_plug_in()
    solar.adopt(t, requested_a=MIN_A, on_since=t - 10_000.0)
    verdicts = _run(solar, t, t + UP_DELAY_S + 10.0, 6000.0, phases=1, drawing_a=ONE_MAX_A)
    assert _switches(verdicts) == [3]


def test_beyond_the_budget_three_phases_keep_the_rules_without_switching():
    """Up, down, up spends the budget on three phases: a surplus short of the minimum but above the stop level then
    keeps the charge at the minimum, and one below the stop level stops it, as without switching."""
    solar = _running()
    phases = 1
    switch_times: list[float] = []
    t = 0.0
    while t < 3600.0:
        surplus_w = 3800.0 if int(t // 1200) % 2 else 6000.0
        verdict = solar.observe(_obs(t, surplus_w, phases=phases, drawing_a=MIN_A if phases == 3 else ONE_MAX_A))
        if verdict.switch_phases is not None:
            phases = verdict.switch_phases
            switch_times.append(t)
        t += 10.0
    # Down only once `MIN_BETWEEN_S` is past: the stop delay's timer runs from then (above the stop level the charge
    # meanwhile runs on at the minimum, as without switching).
    assert switch_times == [UP_DELAY_S, UP_DELAY_S + MIN_BETWEEN_S + 300.0, 2400.0 + UP_DELAY_S]
    assert phases == 3
    verdicts = _run(solar, t, t + 1200.0, 3800.0)
    assert _switches(verdicts) == []
    assert {v.reason for v in verdicts} <= {"on_modulate", "on_steady"}
    assert {v.requested_a for v in verdicts if v.action == "set_current"} == {MIN_A}
    verdicts = _run(solar, t + 1210.0, t + 1210.0 + 300.0, 3000.0, step=1.0)
    assert _switches(verdicts) == []
    assert [v.reason for v in verdicts if v.action == "stop"] == ["stop_after_delay"]


def test_the_down_switch_waits_for_min_between_and_the_stop_rule_decides_meanwhile():
    solar = _running()
    verdicts = _run(solar, 0.0, UP_DELAY_S + 10.0, 6000.0, phases=1, drawing_a=ONE_MAX_A)
    assert _switches(verdicts) == [3]
    # A cloud right after: short of three phases but carrying one; within MIN_BETWEEN_S the old stop rule.
    verdicts = _run(solar, UP_DELAY_S + 20.0, UP_DELAY_S + 20.0 + 400.0, 3000.0, step=1.0)
    assert _switches(verdicts) == []
    assert [v.reason for v in verdicts if v.action == "stop"] == ["stop_after_delay"]


# --- Properties --------------------------------------------------------------------------------------------------

SETTLE_S = 90.0

def _expand(spells):
    """Spells of weather into ticks: each spell's surplus and inputs held for its duration, one tick per its step."""
    ticks = []
    for surplus_w, duration, step, allowed, room, one_max_a in spells:
        for _ in range(max(1, int(duration // step))):
            ticks.append((surplus_w, step, allowed, room, one_max_a))
    return ticks


# Spells rather than single ticks: the switches wait for a surplus held for minutes, which independent ticks never are.
_TICKS = st.lists(
    st.tuples(
        st.floats(min_value=0.0, max_value=9000.0, allow_nan=False),  # surplus, W
        st.floats(min_value=5.0, max_value=2400.0, allow_nan=False),  # how long the spell lasts, s
        st.floats(min_value=5.0, max_value=60.0, allow_nan=False),  # seconds between ticks
        st.sampled_from([True, True, True, False]),  # allowed
        st.booleans(),  # room for three phases
        st.sampled_from([10.0, 16.0, 20.0, None]),  # one phase's maximum
    ),
    min_size=1,
    max_size=20,
).map(_expand)


# Hypothesis notes a seeded random of the test harness it cannot manage; the test draws only its own data.
@pytest.mark.filterwarnings("ignore:It looks like `register_random`")
@given(ticks=_TICKS)
@settings(max_examples=200, deadline=None, derandomize=True, suppress_health_check=[HealthCheck.too_slow])
def test_switches_keep_their_rules_for_random_surplus_sequences(ticks):
    solar = SolarController(_config())
    phases = 3
    last_switch: float | None = None
    count = 0
    t = 0.0
    for surplus_w, step, allowed, room, one_max_a in ticks:
        settling = last_switch is not None and t - last_switch < SETTLE_S
        drawing = 0.0 if not solar.running else (MIN_A if phases == 3 else (one_max_a or MIN_A))
        obs = _obs(t, surplus_w, phases=phases, drawing_a=drawing, allowed=allowed, one_max_a=one_max_a,
                   room=room, settling=settling)
        verdict = solar.observe(obs)
        if verdict.start_phases is not None:
            assert allowed
            assert verdict.action == "start"
            phases = verdict.start_phases
        if verdict.switch_phases is not None:
            assert allowed and not settling
            count += 1
            assert count <= MAX_SWITCHES_PER_SESSION
            assert last_switch is None or t - last_switch >= MIN_BETWEEN_S
            if verdict.switch_phases == 1:
                assert phases == 3
                assert surplus_w >= ONE_MIN_W  # never down when one phase at the minimum is not carried
            else:
                assert phases == 1 and room
                assert one_max_a is not None and surplus_w >= one_max_a * VOLTAGE_V  # never up below the maximum
                assert surplus_w >= UP_W
            phases = verdict.switch_phases
            last_switch = t
        t += step


@pytest.mark.filterwarnings("ignore:It looks like `register_random`")
@given(ticks=_TICKS, phases_now=st.sampled_from([1, 3]))
@settings(max_examples=200, deadline=None, derandomize=True, suppress_health_check=[HealthCheck.too_slow])
def test_not_allowed_the_verdicts_are_the_ones_without_switching(ticks, phases_now):
    """With switching not allowed, whatever the other phase inputs say, every verdict equals the one an observation
    without them gets."""
    plain = SolarController(_config())
    gated = SolarController(_config())
    t = 0.0
    for surplus_w, step, _allowed, room, one_max_a in ticks:
        drawing = MIN_A if plain.running else 0.0
        base = _obs(t, surplus_w, drawing_a=drawing)
        base = replace(
            base, phase_switch_allowed=False, phases_now=3, one_phase_max_a=None, three_phase_room=False
        )
        noisy = replace(base, phases_now=phases_now, one_phase_max_a=one_max_a, three_phase_room=room,
                        phase_switch_settling=room)
        expected = plain.observe(base)
        assert gated.observe(noisy) == expected
        assert expected.switch_phases is None and expected.start_phases is None
        t += step
