"""The pure solar-surplus state machine (`site/solar_surplus.py`); no test sleeps, each clock is the observation's own `now`."""

from __future__ import annotations

import math
import random

import pytest

from custom_components.spotnav.site.solar_surplus import (
    SolarConfig,
    SolarController,
    SolarObservation,
)

THREE_PHASES = ("L1", "L2", "L3")
VOLTAGE_V = 230.0


def _obs(
    now: float,
    *,
    grid_w: float | dict[str, float | None] = 0.0,
    voltage_v: float | dict[str, float | None] = VOLTAGE_V,
    car_delivered_a: float | dict[str, float | None] = 0.0,
    battery_w: float | None = None,
    battery_configured: bool = False,
    car_phases: tuple[str, ...] = THREE_PHASES,
) -> SolarObservation:
    """Build an observation.

    A bare `grid_w` is the net total, split evenly across phases; a dict gives per-phase control
    (omit a phase or use `None` for an unusable reading). Bare `voltage_v` and `car_delivered_a`
    apply to every phase.
    """
    grid_map = (
        {p: grid_w / len(THREE_PHASES) for p in THREE_PHASES}
        if not isinstance(grid_w, dict)
        else dict(grid_w)
    )
    voltage_map = (
        {p: voltage_v for p in THREE_PHASES} if not isinstance(voltage_v, dict) else dict(voltage_v)
    )
    delivered_map = (
        {p: car_delivered_a for p in THREE_PHASES}
        if not isinstance(car_delivered_a, dict)
        else dict(car_delivered_a)
    )
    return SolarObservation(
        now=now,
        signed_grid_w=grid_map,
        voltage_v=voltage_map,
        car_delivered_a=delivered_map,
        battery_w=battery_w,
        car_phases=car_phases,
        battery_configured=battery_configured,
    )


def _config(**overrides) -> SolarConfig:
    return SolarConfig(**overrides)


def test_start_a_defaults_to_min_current_a():
    cfg = SolarConfig()
    assert cfg.start_a == cfg.min_current_a == 6.0
    assert cfg.stop_a == 5.0


def test_start_a_and_stop_a_overridable_independently():
    cfg = SolarConfig(min_current_a=8.0, stop_a=4.0)
    assert cfg.start_a == 8.0  # still follows min_current_a
    assert cfg.stop_a == 4.0  # explicit override kept


def test_pin_four_point_one_kw_at_230v_three_phase_is_six_amp_start():
    # 6 A x 3 phases x 230 V = 4140 W, the "~4.1 kW" pin.
    ctrl = SolarController(_config())
    obs = _obs(0.0, grid_w=-4140.0)  # all export, nothing else
    verdict = ctrl.observe(obs)
    assert verdict.available_a == 6.0
    assert verdict.state == "arming"


def test_just_below_pin_does_not_arm():
    ctrl = SolarController(_config())
    obs = _obs(0.0, grid_w=-4100.0)  # 4100 / 690 = 5.94 A < 6.0
    verdict = ctrl.observe(obs)
    assert verdict.available_a < 6.0
    assert verdict.state == "off"
    assert verdict.action == "hold"


def test_car_starting_does_not_stop_itself():
    """The car's own draw must be added back, or starting it would look like the surplus vanished and stop the charge."""
    cfg = _config(start_delay_s=0.0, min_off_s=0.0)
    ctrl = SolarController(cfg)

    # Before the car draws: 4600 W all export.
    v1 = ctrl.observe(_obs(0.0, grid_w=-4600.0, car_delivered_a=0.0))
    assert v1.action == "start"
    assert v1.state == "on"
    assert v1.available_a == pytest.approx(4600.0 / (3 * VOLTAGE_V))

    # The car now draws the 4600 W that was being exported (net grid moves from -4600 to 0): "available = export" would read this as the surplus vanishing.
    car_a = 4600.0 / (3 * VOLTAGE_V)
    v2 = ctrl.observe(_obs(1.0, grid_w=0.0, car_delivered_a=car_a))
    assert v2.state == "on"
    assert v2.action in ("hold", "set_current")
    # available_w is unchanged by the car's own draw: export_w now ~0 but car_w ~5520, net the same.
    assert v2.available_w == pytest.approx(v1.available_w, abs=1.0)


# `available_w = car_w + battery_w - net_grid_w` is the energy-balance identity (PV - house_load), true whether the grid imports or exports.
# battery_first differs only by reserving the battery's charging power (never its discharge).


def test_car_first_is_the_plain_energy_balance_identity():
    # House load draws 200 W from the grid; the battery charges 1000 W; the car is not yet drawing.
    cfg = _config(priority="car_first")
    ctrl = SolarController(cfg)
    verdict = ctrl.observe(_obs(0.0, grid_w=200.0, battery_w=1000.0, car_delivered_a=0.0))
    assert verdict.priority_effective == "car_first"
    # car_w(0) + battery_w(1000) - net_grid_w(200) = 800.
    assert verdict.available_w == pytest.approx(800.0)


def test_battery_first_reserves_only_the_batterys_charging():
    # Same scene under battery_first: the battery's charging is reserved, so the car sees the import subtracted with nothing added back.
    cfg = _config(priority="battery_first")
    ctrl = SolarController(cfg)
    verdict = ctrl.observe(_obs(0.0, grid_w=200.0, battery_w=1000.0, car_delivered_a=0.0))
    assert verdict.priority_effective == "battery_first"
    assert verdict.available_w == pytest.approx(-200.0)


def test_battery_first_still_credits_a_discharging_battery():
    # All 6 kW of PV goes into the battery (net_grid_w = 0): car_first sees 6 kW, battery_first sees none.
    cfg_car_first = _config(priority="car_first")
    cfg_battery_first = _config(priority="battery_first")
    obs = _obs(0.0, grid_w=0.0, battery_w=6000.0, car_delivered_a=0.0)

    v_car_first = SolarController(cfg_car_first).observe(obs)
    v_battery_first = SolarController(cfg_battery_first).observe(obs)

    assert v_car_first.available_w == pytest.approx(6000.0)
    assert v_battery_first.available_w == pytest.approx(0.0)

    # The battery discharges: battery_first never reserves a discharge, only a charge; min(0, battery_w) passes a negative value through in both formulas.
    obs_discharging = _obs(0.0, grid_w=-500.0, battery_w=-500.0, car_delivered_a=0.0)
    v2_car_first = SolarController(cfg_car_first).observe(obs_discharging)
    v2_battery_first = SolarController(cfg_battery_first).observe(obs_discharging)
    assert v2_car_first.available_w == pytest.approx(0.0)  # 0 + (-500) - (-500)
    assert v2_battery_first.available_w == pytest.approx(0.0)  # same: discharge not reserved


def test_grid_charged_battery_with_no_sun_contributes_nothing_under_car_first():
    """A battery charging 5 kW entirely from grid import with no PV: the identity reports zero surplus."""
    cfg = _config(priority="car_first")
    ctrl = SolarController(cfg)
    # All 5000 W of grid import goes into the battery; no house load, car draw or PV.
    verdict = ctrl.observe(_obs(0.0, grid_w=5000.0, battery_w=5000.0, car_delivered_a=0.0))
    assert verdict.available_w == pytest.approx(0.0)
    assert verdict.state == "off"


def test_2026_09_26_reading_gives_true_surplus_under_car_first_and_none_under_battery_first():
    """Battery charging 14.07 kW, grid importing ~13.9 kW (cheap-hour charge), car drawing 4.1 kW.

    A guard keyed on "is the grid importing" would call this zero surplus; the identity finds
    the ~4.27 kW that is genuinely PV, because the car's own draw is part of the import and
    must be added back rather than read off net_grid_w.
    """
    cfg = _config(priority="car_first")
    ctrl = SolarController(cfg)
    obs = _obs(0.0, grid_w=13900.0, battery_w=14070.0, car_delivered_a=4100.0 / (3 * VOLTAGE_V))
    v_car_first = ctrl.observe(obs)
    assert v_car_first.available_w == pytest.approx(4270.0, abs=1.0)

    v_battery_first = SolarController(_config(priority="battery_first")).observe(obs)
    assert v_battery_first.available_w <= 0.0


def test_no_battery_configured_car_first_behaves_as_battery_first():
    cfg = _config(priority="car_first")
    ctrl = SolarController(cfg)
    verdict = ctrl.observe(_obs(0.0, grid_w=-1000.0, battery_w=None, car_delivered_a=0.0))
    assert verdict.priority_effective == "battery_first"
    # battery_w treated as 0.0 in the formula: 0 + 0 - (-1000) = 1000.
    assert verdict.available_w == pytest.approx(1000.0)


# "Not configured" versus "configured but unreadable": with the grid held at zero by a discharging battery
# and `battery_w` unreadable, pricing it as `battery_w = 0.0` would let the car sustain itself on the house battery.


def test_battery_configured_but_unreadable_is_no_basis_not_zero():
    """A configured battery this tick cannot read is no basis at all, like a missing grid or voltage reading, so the state machine may not arm."""
    cfg = _config(start_delay_s=0.0, min_off_s=0.0)
    ctrl = SolarController(cfg)
    # Grid held at zero (as a discharging battery would), car drawing 6 A on all phases: an unfixed trap reads 4140 W of "surplus" (car_w + 0 - 0).
    obs = _obs(
        0.0,
        grid_w=0.0,
        car_delivered_a=6.0,
        battery_w=None,
        battery_configured=True,
    )
    verdict = ctrl.observe(obs)
    assert verdict.state == "off"
    assert verdict.action == "hold"
    assert verdict.reason == "no_basis_off"


def test_battery_configured_but_unreadable_stops_a_running_charge_after_stale_grace():
    """While `on`, a configured-but-unreadable battery is "no basis": the charge is kept only for `stale_grace_s`, then stops."""
    cfg = _config(start_delay_s=0.0, min_off_s=0.0, stale_grace_s=120.0, stop_delay_s=300.0)
    ctrl = SolarController(cfg)

    v1 = ctrl.observe(
        _obs(0.0, grid_w=-6000.0, battery_w=0.0, battery_configured=True)
    )
    assert v1.action == "start"
    assert v1.state == "on"

    # The battery becomes unreadable while the grid holds at zero. Still inside `stale_grace_s`: held, not stopped, and not zero surplus.
    v2 = ctrl.observe(
        _obs(60.0, grid_w=0.0, battery_w=None, battery_configured=True)
    )
    assert v2.state == "on"
    assert v2.reason == "no_basis_grace"
    assert v2.action == "hold"

    # Past stale_grace_s: stops, like any other prolonged "no basis".
    v3 = ctrl.observe(
        _obs(181.0, grid_w=0.0, battery_w=None, battery_configured=True)
    )
    assert v3.state == "off"
    assert v3.action == "stop"
    assert v3.reason == "no_basis_stopped"


def test_battery_not_configured_keeps_todays_exact_behaviour():
    """`battery_configured=False` (the default, no battery entity) still treats `battery_w=None` as `0.0`."""
    cfg = _config(priority="car_first", start_delay_s=0.0, min_off_s=0.0)
    ctrl = SolarController(cfg)
    obs = _obs(
        0.0,
        grid_w=0.0,
        car_delivered_a=6.0,
        battery_w=None,
        battery_configured=False,
    )
    verdict = ctrl.observe(obs)
    assert verdict.priority_effective == "battery_first"
    assert verdict.available_w == pytest.approx(4140.0)
    assert verdict.state == "on"
    assert verdict.action == "start"


def test_net_grid_is_summed_across_phases_not_per_phase():
    # L1 imports 300 W, L2 and L3 each export 1000 W: net -1700 W (net export), though L1 alone imports.
    cfg = _config()
    ctrl = SolarController(cfg)
    verdict = ctrl.observe(
        _obs(0.0, grid_w={"L1": 300.0, "L2": -1000.0, "L3": -1000.0}, car_delivered_a=0.0)
    )
    assert verdict.net_grid_w == pytest.approx(-1700.0)
    assert verdict.export_w == pytest.approx(1700.0)


def test_missing_reading_never_starts_from_off():
    cfg = _config(start_delay_s=0.0, min_off_s=0.0)
    ctrl = SolarController(cfg)
    obs = _obs(0.0, grid_w={"L1": None, "L2": -3000.0, "L3": -3000.0})
    verdict = ctrl.observe(obs)
    assert verdict.state == "off"
    assert verdict.reason == "no_basis_off"
    assert verdict.action == "hold"


def test_missing_voltage_on_car_phase_is_no_basis():
    cfg = _config(start_delay_s=0.0, min_off_s=0.0)
    ctrl = SolarController(cfg)
    obs = _obs(
        0.0,
        grid_w=-5000.0,
        voltage_v={"L1": None, "L2": 230.0, "L3": 230.0},
        car_phases=("L1", "L2", "L3"),
    )
    verdict = ctrl.observe(obs)
    assert verdict.reason == "no_basis_off"


def test_stale_grace_holds_then_stops_running_charge():
    cfg = _config(start_delay_s=0.0, min_off_s=0.0, stale_grace_s=120.0, stop_delay_s=300.0)
    ctrl = SolarController(cfg)

    v1 = ctrl.observe(_obs(0.0, grid_w=-6000.0))
    assert v1.action == "start"
    assert v1.state == "on"

    # Reading goes stale for less than stale_grace_s: still held on.
    v2 = ctrl.observe(_obs(60.0, grid_w={"L1": None, "L2": -3000.0, "L3": -3000.0}))
    assert v2.state == "on"
    assert v2.reason == "no_basis_grace"
    assert v2.action == "hold"

    # Past stale_grace_s (stale from t=60, grace 120 s: stops at t=180 or later).
    v3 = ctrl.observe(_obs(181.0, grid_w={"L1": None, "L2": -3000.0, "L3": -3000.0}))
    assert v3.state == "off"
    assert v3.action == "stop"
    assert v3.reason == "no_basis_stopped"


def test_reading_returning_inside_grace_resumes_normally():
    cfg = _config(start_delay_s=0.0, min_off_s=0.0, stale_grace_s=120.0, stop_delay_s=300.0)
    ctrl = SolarController(cfg)

    v1 = ctrl.observe(_obs(0.0, grid_w=-6000.0))
    assert v1.action == "start"

    v2 = ctrl.observe(_obs(60.0, grid_w={"L1": None, "L2": -3000.0, "L3": -3000.0}))
    assert v2.state == "on"

    # Fresh data returns at t=90, within the grace window; still ample surplus.
    v3 = ctrl.observe(_obs(90.0, grid_w=-6000.0))
    assert v3.state == "on"
    assert v3.reason in ("on_steady", "on_modulate")


def test_start_requires_both_start_delay_and_min_off():
    cfg = _config(start_delay_s=120.0, min_off_s=300.0)
    ctrl = SolarController(cfg)

    # Ample surplus throughout.
    v1 = ctrl.observe(_obs(0.0, grid_w=-6000.0))
    assert v1.state == "arming"
    assert v1.reason == "arming_delay"

    v2 = ctrl.observe(_obs(119.0, grid_w=-6000.0))
    assert v2.state == "arming"  # not yet 120s

    v3 = ctrl.observe(_obs(120.0, grid_w=-6000.0))
    assert v3.state == "on"
    assert v3.action == "start"
    assert v3.reason == "start_after_delay"


def test_start_withheld_by_min_off_after_delay_elapsed():
    cfg = _config(start_delay_s=10.0, min_off_s=300.0, stop_delay_s=0.0, min_on_s=0.0)
    ctrl = SolarController(cfg)

    # Start once, then stop it so there is a "last stop" to guard against.
    ctrl.observe(_obs(0.0, grid_w=-6000.0))
    v_on = ctrl.observe(_obs(10.0, grid_w=-6000.0))
    assert v_on.action == "start"

    v_stop = ctrl.observe(_obs(11.0, grid_w=0.0))  # surplus gone, stop_delay_s=0
    assert v_stop.action == "stop"

    # Surplus returns and the delay is satisfied quickly, but min_off_s (300 s) has not passed since the stop at t=11.
    v_arm = ctrl.observe(_obs(12.0, grid_w=-6000.0))
    assert v_arm.state == "arming"
    v_wait = ctrl.observe(_obs(30.0, grid_w=-6000.0))
    assert v_wait.state == "arming"
    assert v_wait.reason == "arming_min_off_wait"

    v_finally = ctrl.observe(_obs(11.0 + 300.0 + 1, grid_w=-6000.0))
    assert v_finally.action == "start"


def test_dip_during_arming_restarts_delay():
    cfg = _config(start_delay_s=120.0, min_off_s=0.0)
    ctrl = SolarController(cfg)

    ctrl.observe(_obs(0.0, grid_w=-6000.0))
    v_dip = ctrl.observe(_obs(100.0, grid_w=0.0))  # dip below start_a
    assert v_dip.state == "off"
    assert v_dip.reason == "arming_dip"

    v_rearm = ctrl.observe(_obs(101.0, grid_w=-6000.0))
    assert v_rearm.state == "arming"

    # Only 119 s since the restart at t=101: not yet on.
    v_still_arming = ctrl.observe(_obs(101.0 + 119.0, grid_w=-6000.0))
    assert v_still_arming.state == "arming"

    v_on = ctrl.observe(_obs(101.0 + 120.0, grid_w=-6000.0))
    assert v_on.action == "start"


def test_stop_requires_both_stop_delay_and_min_on():
    cfg = _config(start_delay_s=0.0, min_off_s=0.0, stop_delay_s=300.0, min_on_s=600.0)
    ctrl = SolarController(cfg)

    ctrl.observe(_obs(0.0, grid_w=-6000.0))  # on at t=0

    # Surplus gone at t=100; stop_delay_s is satisfied at t=400, but min_on_s (600 s since the start at t=0) is not.
    v_disarm = ctrl.observe(_obs(100.0, grid_w=0.0))
    assert v_disarm.state == "disarming"
    v_delay_ok_but_min_on_not = ctrl.observe(_obs(401.0, grid_w=0.0))
    assert v_delay_ok_but_min_on_not.state == "disarming"
    assert v_delay_ok_but_min_on_not.reason == "disarming_min_on_wait"

    v_stop = ctrl.observe(_obs(601.0, grid_w=0.0))
    assert v_stop.action == "stop"
    assert v_stop.reason == "stop_after_delay"


def test_recovery_during_disarming_returns_to_on():
    cfg = _config(start_delay_s=0.0, min_off_s=0.0, stop_delay_s=300.0, min_on_s=0.0)
    ctrl = SolarController(cfg)

    ctrl.observe(_obs(0.0, grid_w=-6000.0))  # on
    v_disarm = ctrl.observe(_obs(50.0, grid_w=0.0))
    assert v_disarm.state == "disarming"

    v_recover = ctrl.observe(_obs(100.0, grid_w=-6000.0))
    assert v_recover.state == "on"
    assert v_recover.reason == "disarming_recover"

    # A real stop must not happen just because 300 s have passed since the original disarming attempt at t=50: recovery reset the clock.
    v_should_still_be_on = ctrl.observe(_obs(400.0, grid_w=-6000.0))
    assert v_should_still_be_on.state == "on"


def test_cloud_shorter_than_stop_delay_never_stops():
    cfg = _config(start_delay_s=0.0, min_off_s=0.0, stop_delay_s=300.0, min_on_s=0.0)
    ctrl = SolarController(cfg)

    ctrl.observe(_obs(0.0, grid_w=-6000.0))  # on

    actions = []
    # A cloud: surplus drops for 60 s (well under the 300 s stop_delay_s), then returns.
    for t, grid in [(30.0, 0.0), (60.0, 0.0), (90.0, -6000.0), (120.0, -6000.0)]:
        actions.append(ctrl.observe(_obs(t, grid_w=grid)).action)

    assert "stop" not in actions


def test_requested_current_is_floored_and_clamped():
    cfg = _config(start_delay_s=0.0, min_off_s=0.0, min_current_a=6.0, max_current_a=16.0)
    ctrl = SolarController(cfg)

    # 20 A worth of available power clamps down to max_current_a=16.
    verdict = ctrl.observe(_obs(0.0, grid_w=-20.5 * 3 * VOLTAGE_V))
    assert verdict.action == "start"
    assert verdict.requested_a == 16.0


def test_requested_current_floors_before_clamping_up_to_minimum():
    cfg = _config(start_delay_s=0.0, min_off_s=0.0, min_current_a=6.0, stop_a=4.0, start_a=4.0)
    ctrl = SolarController(cfg)
    # 5.9 A worth of power floors to 5, then clamps up to min_current_a=6.
    verdict = ctrl.observe(_obs(0.0, grid_w=-5.9 * 3 * VOLTAGE_V))
    assert verdict.action == "start"
    assert verdict.requested_a == 6.0


def test_set_current_only_on_whole_amp_change():
    cfg = _config(start_delay_s=0.0, min_off_s=0.0)
    ctrl = SolarController(cfg)

    v1 = ctrl.observe(_obs(0.0, grid_w=-8.4 * 3 * VOLTAGE_V))  # floors to 8
    assert v1.action == "start"
    assert v1.requested_a == 8.0

    # A tiny change that still floors to 8 must not re-request.
    v2 = ctrl.observe(_obs(1.0, grid_w=-8.9 * 3 * VOLTAGE_V))
    assert v2.action == "hold"
    assert v2.reason == "on_steady"

    # A real change to 9 A worth of power.
    v3 = ctrl.observe(_obs(2.0, grid_w=-9.2 * 3 * VOLTAGE_V))
    assert v3.action == "set_current"
    assert v3.requested_a == 9.0


def test_requested_a_only_present_on_start_and_set_current():
    cfg = _config(start_delay_s=0.0, min_off_s=0.0, stop_delay_s=0.0, min_on_s=0.0)
    ctrl = SolarController(cfg)

    v_hold_off = ctrl.observe(_obs(0.0, grid_w=0.0))
    assert v_hold_off.action == "hold"
    assert v_hold_off.requested_a is None

    v_start = ctrl.observe(_obs(1.0, grid_w=-6000.0))
    assert v_start.action == "start"
    assert v_start.requested_a is not None

    v_stop = ctrl.observe(_obs(2.0, grid_w=0.0))
    assert v_stop.action == "stop"
    assert v_stop.requested_a is None


def test_scene_2026_09_28_battery_charging_no_start():
    """Grid ~0 W, battery charging 1.2 kW, car not drawing: 1.2 kW is below the ~4.1 kW three-phase minimum, so no start even though car_first credits the battery's charging in full."""
    cfg = _config()
    ctrl = SolarController(cfg)
    verdict = ctrl.observe(_obs(0.0, grid_w=0.0, battery_w=1200.0, car_delivered_a=0.0))
    assert verdict.priority_effective == "car_first"
    assert verdict.available_w == pytest.approx(1200.0)
    assert verdict.state == "off"
    assert verdict.action == "hold"


def test_scene_sunny_afternoon_battery_6kw_starts_after_delay_at_about_8a():
    cfg = _config(start_delay_s=120.0, min_off_s=0.0)
    ctrl = SolarController(cfg)

    ctrl.observe(_obs(0.0, grid_w=0.0, battery_w=6000.0, car_delivered_a=0.0))
    verdict = ctrl.observe(_obs(120.0, grid_w=0.0, battery_w=6000.0, car_delivered_a=0.0))

    assert verdict.action == "start"
    assert verdict.state == "on"
    # 6000 / (3 * 230) = 8.69 A, floors to 8.
    assert verdict.requested_a == 8.0


# Closed-loop regression: a plant is simulated by the energy-balance identity in reverse (from a true PV surplus and the car's
# actual delivered draw, never the controller's last request), so a controller whose own request feeds back into the next
# reading cannot pass. A single static tick cannot catch that.


def _simulate_energy_balance(
    ctrl: SolarController,
    true_surplus_w,
    *,
    total_s: float,
    step_s: float,
    battery_mode: str | None,
    follow_ratio: float = 0.887,
) -> list[dict]:
    """Drive `ctrl` through a plant derived from the energy-balance identity, given each tick's true PV surplus and the car's delivered draw.

    `battery_mode`:
    * `None`: no battery, `net_grid_w = car_w - true_surplus_w(t)`.
    * `"grid_zero"`: a battery regulating the grid to zero (the site `execution/yield_stepping.py`
      is for), `battery_w = true_surplus_w(t) - car_w` and `net_grid_w = 0.0`.

    The car delivers `follow_ratio` (0.887) of the last requested current while charging, and nothing once stopped.
    """
    n = int(total_s / step_s)
    requested_a = 0.0
    charging = False
    records: list[dict] = []
    for i in range(n):
        t = i * step_s
        surplus = true_surplus_w(t)
        car_a = follow_ratio * requested_a if charging else 0.0
        car_w = car_a * 3 * VOLTAGE_V
        if battery_mode == "grid_zero":
            battery_w = surplus - car_w
            net_grid_w = 0.0
        else:
            battery_w = None
            net_grid_w = car_w - surplus
        verdict = ctrl.observe(
            _obs(t, grid_w=net_grid_w, car_delivered_a=car_a, battery_w=battery_w)
        )
        if verdict.action == "start":
            charging = True
            requested_a = verdict.requested_a
        elif verdict.action == "set_current":
            requested_a = verdict.requested_a
        elif verdict.action == "stop":
            charging = False
            requested_a = 0.0
        records.append({"t": t, "action": verdict.action, "state": verdict.state})
    return records


def _lasting_cloud_surplus(t: float) -> float:
    # 5 kW of true PV surplus (above the ~4.1 kW pin) for ten minutes, then a lasting cloud drops it to 3 kW for good.
    return 5000.0 if t < 600.0 else 3000.0


def test_no_battery_lasting_cloud_eventually_stops():
    """No battery, a lasting cloud (5 kW then 3 kW, below the 4.1 kW start): it must stop once `stop_delay_s` and `min_on_s` have elapsed.

    A formula using `export_w + car_w` would never stop, leaving the car importing bought power as "solar".
    """
    cfg = _config(start_delay_s=120.0, stop_delay_s=300.0, min_on_s=600.0, min_off_s=300.0)
    ctrl = SolarController(cfg)
    records = _simulate_energy_balance(
        ctrl, _lasting_cloud_surplus, total_s=2000.0, step_s=30.0, battery_mode=None
    )
    actions = [r["action"] for r in records]
    assert "start" in actions
    assert "stop" in actions
    stop_index = actions.index("stop")
    cloud_start_index = next(i for i, r in enumerate(records) if r["t"] >= 600.0)
    # The stop must land within stop_delay_s (plus a couple of ticks) of the cloud's arrival, not merely eventually.
    assert (records[stop_index]["t"] - records[cloud_start_index]["t"]) <= 300.0 + 60.0
    # Once stopped it must not immediately re-start (true surplus is only 3 kW, below the 4.1 kW pin).
    assert all(a != "start" for a in actions[stop_index + 1 :])


def test_grid_zero_battery_lasting_cloud_eventually_stops():
    """A battery holding the grid at zero under `car_first`, same lasting cloud: it must stop the same way.

    The battery's discharge is subtracted back out, so the true 3 kW surplus governs
    rather than the battery quietly feeding the car as apparent solar.
    """
    cfg = _config(
        priority="car_first",
        start_delay_s=120.0,
        stop_delay_s=300.0,
        min_on_s=600.0,
        min_off_s=300.0,
    )
    ctrl = SolarController(cfg)
    records = _simulate_energy_balance(
        ctrl, _lasting_cloud_surplus, total_s=2000.0, step_s=30.0, battery_mode="grid_zero"
    )
    actions = [r["action"] for r in records]
    assert "start" in actions
    assert "stop" in actions
    stop_index = actions.index("stop")
    cloud_start_index = next(i for i, r in enumerate(records) if r["t"] >= 600.0)
    assert (records[stop_index]["t"] - records[cloud_start_index]["t"]) <= 300.0 + 60.0
    assert all(a != "start" for a in actions[stop_index + 1 :])


def test_noisy_trace_produces_few_transitions():
    """A noisy solar/household trace with two clouds: a debounced controller makes at most 6 start/stop transitions.

    One sunrise and one sunset crossing give one start/stop pair; 6 leaves slack for noise near
    the edges without being brittle to seed or delay changes, yet still fails if debouncing
    regresses to chatter. Only "start"/"stop" actions count, not entries into arming/disarming.
    """
    cfg = _config(start_delay_s=120.0, stop_delay_s=300.0, min_on_s=300.0, min_off_s=120.0)
    ctrl = SolarController(cfg)
    rng = random.Random(20260928)

    step_s = 30.0
    total_hours = 12.0
    n_steps = int(total_hours * 3600 / step_s)

    actions = []
    for i in range(n_steps):
        t = i * step_s
        hour = t / 3600.0
        # A solar bell curve peaking at midday (hour 6 of 12) at 7000 W, above the ~4.1 kW pin, zero at the edges.
        solar_w = max(0.0, 7000.0 * math.sin(math.pi * hour / total_hours))
        # A couple of clouds: short dips, each well under stop_delay_s (300 s).
        if 2.0 * 3600 <= t < 2.0 * 3600 + 120:
            solar_w *= 0.1
        if 7.0 * 3600 <= t < 7.0 * 3600 + 150:
            solar_w *= 0.1
        noise = rng.gauss(0.0, 150.0)
        export_w = max(0.0, solar_w + noise)
        grid_w = -export_w
        verdict = ctrl.observe(_obs(t, grid_w=grid_w))
        actions.append(verdict.action)

    transitions = [a for a in actions if a in ("start", "stop")]
    assert len(transitions) <= 6, transitions
    # At least one real start: the trace clears the threshold at midday.
    assert "start" in transitions


def test_a_noisy_day_with_the_car_in_the_loop_never_charges_it_from_the_grid() -> None:
    """A noisy trace with the car's own draw feeding back into the grid reading it is judged by.

    A ten-hour bell curve with noise and two clouds, a battery holding the grid at zero until
    full or empty, and a car delivering 0.887 of what it is asked. A formula crediting the
    car's draw to export would sustain it on bought or stored power after every cloud.
    """
    import math
    import random

    rng = random.Random(7)
    phases = ("L1", "L2", "L3")
    volts = 230.0
    controller = SolarController(SolarConfig(priority="car_first"))
    assigned = delivered = 0.0
    soc = 0.4
    starts = stops = 0
    grid_import_kwh = discharge_kwh = car_kwh = 0.0
    step = 30.0
    for i in range(int(10 * 3600 / step)):
        t = i * step
        clouded = 2.5 * 3600 < t < 2.9 * 3600 or 6 * 3600 < t < 6.2 * 3600
        pv = max(
            0.0,
            7500 * math.sin(math.pi * t / (10 * 3600)) * (0.35 if clouded else 1.0)
            - 600
            + rng.gauss(0, 250),
        )
        car_w = delivered * 3 * volts
        battery_w = pv - car_w
        if (battery_w > 0 and soc >= 1.0) or (battery_w < 0 and soc <= 0.1):
            battery_w = 0.0
        soc += battery_w * step / 3600 / 30000
        net_w = car_w + battery_w - pv

        verdict = controller.observe(
            SolarObservation(
                now=t,
                signed_grid_w={p: net_w / 3 for p in phases},
                voltage_v={p: volts for p in phases},
                car_delivered_a={p: delivered for p in phases},
                battery_w=battery_w,
                car_phases=phases,
            )
        )
        if verdict.action in ("start", "set_current"):
            assigned = verdict.requested_a
        if verdict.action == "start":
            starts += 1
        if verdict.action == "stop":
            assigned = 0.0
            stops += 1
        delivered += max(-1.5, min(1.5, assigned * 0.887 - delivered))

        grid_import_kwh += max(0.0, net_w) * step / 3.6e6
        discharge_kwh += max(0.0, -battery_w) * step / 3.6e6
        car_kwh += car_w * step / 3.6e6

    assert car_kwh > 20.0, "a sunny day should put real energy into the car"
    assert grid_import_kwh < 0.01, "solar mode bought power from the grid"
    # Hysteresis has a small cost: during a stop delay, and when a request between the stop and start currents is held at the 6 A floor, the battery covers the difference. Bounded, not sustained.
    assert discharge_kwh < 0.05 * car_kwh
    # Two clouds and a sunset: at most one start and one stop around each.
    assert starts <= 4 and stops <= 4
