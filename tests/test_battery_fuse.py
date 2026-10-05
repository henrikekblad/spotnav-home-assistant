"""A grid-charging home battery that holds the grid at the fuse: the band and the probe.

Replays the owner's 2026-10-03 situation (a 20 A fuse with margin 0, a battery charging from the grid
at about 15 kW and holding the grid at 20.0 A per phase, the car held at 0 A, `car_first`, yield
stepping on) through `SiteCapacityController._async_apply_active_control`, and the safety cases that
must not move: a real overload, a stale measurement, `battery_first`, active control off.

Same isolation as `test_yield_stepping_wiring.py`: explicit apply passes, the stepper's and the
damper's clocks moved by hand, handmade regulator decisions.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    SOLAR_PRIORITY_BATTERY_FIRST,
)
from custom_components.spotnav.site.battery_probe import (
    BatteryProbe,
    car_minimum_power_w,
    PROBE_BACKOFF_INITIAL_S,
    PROBE_BACKOFF_MAX_S,
    within_held_band,
)
from custom_components.spotnav.site.regulator_damping import RegulatorDamper

from .test_yield_stepping_wiring import (
    _DamperClock,
    _decision,
    _SecondsClock,
    _seed_assigned_a,
    _yield_setup,
)
from .world import controller_of, set_charger_delivered_a, set_site_current_a

_BATTERY = "sensor.battfuse_battery_power"
_PAUSE = "paused_safe_current_below_charger_minimum"
_NO_HEADROOM = "increase_within_safe_uncredited_margin"
_OVERLOAD = "reducing_current_due_to_active_import_overload"


def _battery_w(hass: HomeAssistant, watts: float) -> None:
    hass.states.async_set(_BATTERY, str(round(watts)), {"unit_of_measurement": "W"})


async def _owner_site(hass: HomeAssistant, entry_id: str, *, battery_w: float = 15000.0, **setup):
    """The owner's site: fuse 20 A, margin 0, the grid held at 20.0 A, the car not drawing."""
    _battery_w(hass, battery_w)
    site_entry, charger, calls = await _yield_setup(
        hass,
        entry_id=entry_id,
        main_fuse_a=20.0,
        safety_margin_a=0.0,
        battery_entity=_BATTERY,
        **setup,
    )
    controller = controller_of(hass, site_entry.entry_id)
    # Explicit passes only: the charger's own change notifications would otherwise schedule passes
    # of their own on decisions recomputed from partial states (as `_yield_setup` does for the
    # state listener).
    for cancel in controller._controller_listener_cancels:
        cancel()
    controller._controller_listener_cancels.clear()
    yield_clock = _SecondsClock()
    controller._yield_now = yield_clock
    damper_clock = _DamperClock()
    controller._dampers[charger.entry_id] = RegulatorDamper(now=damper_clock)
    controller._yield_steppers.clear()
    site_prefix, prefix = f"{entry_id}_site", f"{entry_id}_charger"
    set_site_current_a(hass, site_prefix, 20.0)
    set_charger_delivered_a(hass, prefix, 0.0)
    calls.clear()
    return controller, charger, calls, yield_clock, damper_clock, site_prefix, prefix


def _held_at_zero(controller, charger, *, reason: str = _NO_HEADROOM, requested_a: float = 0.0) -> None:
    """The decision of a stopped charger: nothing requested of it now, nothing proposed."""
    controller.regulator_decisions = {
        charger.entry_id: _decision(proposed_a=0.0, requested_a=requested_a, reason=reason)
    }


async def _paused_site(hass: HomeAssistant, monkeypatch, entry_id: str, **setup):
    """The owner's site with the car stopped by load balancing's pause (an OCPP charger like the owner's:
    below the floor it is stopped through its charge control, as any charger is), a vehicle plugged in, 16 A on
    record. Returns the apply-test tuple plus the charger's controller and its `turn_off` calls."""
    from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter

    controller, charger, calls, yield_clock, damper_clock, site, prefix = await _owner_site(
        hass, entry_id, **setup
    )
    charger_controller = controller_of(hass, charger.entry_id)
    monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda self: True)
    turn_off = async_mock_service(hass, "switch", "turn_off")
    hass.states.async_set(f"switch.{prefix}", "off")
    charger_controller._paused_by_balancing = True
    # The pause was written and recorded when it happened.
    controller._dampers[charger.entry_id].record_write(0.0)
    _held_at_zero(controller, charger)
    return controller, charger, calls, yield_clock, damper_clock, site, prefix, charger_controller, turn_off


def _values(calls) -> list[str]:
    return [call.data["value"] for call in calls]


# -- the pure parts


def test_the_cars_minimum_power_needs_every_voltage() -> None:
    assert car_minimum_power_w(6.0, [230.0, 230.0, 230.0]) == pytest.approx(4140.0)
    assert car_minimum_power_w(6.0, [230.0, None, 230.0]) is None
    assert car_minimum_power_w(6.0, []) is None


def test_the_held_band_is_half_an_amp_above_the_limit() -> None:
    phases = ("L1", "L2", "L3")
    assert within_held_band({"L1": -0.03, "L2": 0.0, "L3": 1.2}, phases)
    assert within_held_band({"L1": -0.5, "L2": 0.0, "L3": 0.0}, phases)
    assert not within_held_band({"L1": -0.51, "L2": 0.0, "L3": 0.0}, phases)
    # A phase the car uses with no margin refuses; a stranger's phase with none is ignored.
    assert not within_held_band({"L1": 0.0, "L2": None, "L3": 0.0}, phases)
    assert within_held_band({"L1": 0.0, "L2": None, "L3": None}, ("L1",))
    assert not within_held_band({"L1": 0.0}, ())


def _started(probe: BatteryProbe, now: float = 0.0) -> None:
    probe.start(now, 6.0, {"L1": 20.0, "L2": 20.0, "L3": 20.0}, ("L1", "L2", "L3"))


def _evaluate(probe: BatteryProbe, now: float, site, delivered=5.9, limit=20.0, fuse=20.0):
    return probe.evaluate(
        now,
        site_current_a=site if isinstance(site, dict) else {p: site for p in ("L1", "L2", "L3")},
        limit_a={p: limit for p in ("L1", "L2", "L3")},
        delivered_a={p: delivered for p in ("L1", "L2", "L3")},
        main_fuse_a=fuse,
    )


def test_a_probe_verifies_for_its_window_then_judges_the_grid() -> None:
    probe = BatteryProbe(window_s=60.0)
    assert probe.window_s == 30.0  # never longer than 30 s
    _started(probe)
    assert _evaluate(probe, 10.0, 20.0).state == "verifying"
    assert _evaluate(probe, 30.0, 20.03).state == "succeeded"  # the band, not a strict limit


def test_the_dwell_shortens_the_window_but_not_below_five_seconds() -> None:
    assert BatteryProbe(window_s=12.0).window_s == 12.0
    assert BatteryProbe(window_s=0.0).window_s == 5.0


@pytest.mark.parametrize(
    ("site", "delivered", "now", "reason"),
    [
        (26.0, 5.9, 30.0, "battery_did_not_yield"),
        (20.0, 0.2, 30.0, "car_not_drawing"),
        ({"L1": 20.0, "L2": None, "L3": 20.0}, 5.9, 5.0, "measurement_unusable"),
        (28.5, 5.9, 5.0, "over_fuse_cap"),
        (27.5, 5.9, 5.0, "excess_beyond_car"),
    ],
)
def test_a_probe_fails_on_any_doubt(site, delivered, now, reason) -> None:
    probe = BatteryProbe()
    _started(probe)
    verdict = _evaluate(probe, now, site, delivered=delivered)
    assert (verdict.state, verdict.reason) == ("failed", reason)


def test_back_off_doubles_to_an_hour_and_a_success_clears_it() -> None:
    probe = BatteryProbe()
    now = 0.0
    expected = PROBE_BACKOFF_INITIAL_S
    for _ in range(6):
        _started(probe, now)
        probe.failed(now, "battery_did_not_yield")
        assert probe.state == "stopping" and probe.probing
        probe.stopped()
        assert not probe.may_start(now + expected - 1.0)
        assert probe.may_start(now + expected)
        now += expected
        expected = min(expected * 2.0, PROBE_BACKOFF_MAX_S)
    assert expected == PROBE_BACKOFF_MAX_S
    _started(probe, now)
    probe.succeeded(now)
    assert probe.may_start(now) and probe.state == "idle"
    _started(probe, now)
    probe.failed(now, "x")
    probe.stopped()
    assert not probe.may_start(now + PROBE_BACKOFF_INITIAL_S - 1.0)
    assert probe.may_start(now + PROBE_BACKOFF_INITIAL_S)


# -- the owner's sequence


async def test_the_car_starts_at_the_minimum_the_battery_yields_and_the_car_climbs(
    hass: HomeAssistant, monkeypatch
) -> None:
    (controller, charger, calls, yield_clock, damper_clock, site, prefix, cc, turn_off) = (
        await _paused_site(hass, monkeypatch, "bfowner")
    )

    await controller._async_apply_active_control()
    # The probe: the car is started at its minimum, and the plan's 16 A stays on record.
    assert _values(calls) == ["1.6,2.10"]
    assert cc.requested_current_a == 16
    assert controller.battery_probe_snapshot[charger.entry_id]["state"] == "probing"
    calls.clear()

    # The car ramps up; the battery gives up what it takes and the grid stays at 20.0 A.
    hass.states.async_set(f"switch.{prefix}", "on")
    controller.regulator_decisions = {
        charger.entry_id: _decision(proposed_a=0.0, requested_a=16.0, reason=_PAUSE)
    }
    set_charger_delivered_a(hass, prefix, 5.34)
    yield_clock.advance(10.0)
    await controller._async_apply_active_control()
    assert calls == []
    assert controller.battery_probe_snapshot[charger.entry_id]["state"] == "probing"

    yield_clock.advance(21.0)
    await controller._async_apply_active_control()
    assert calls == [] and turn_off == []  # the car stays; nothing is stopped
    snapshot = controller.battery_probe_snapshot[charger.entry_id]
    assert (snapshot["state"], snapshot["last_outcome"], snapshot["last_reason"]) == (
        "idle",
        "succeeded",
        "battery_yielded",
    )

    # From here the ordinary yield stepping climbs the car (an unverified step first).
    await controller._async_apply_active_control()
    assert controller._yield_stepping_last[charger.entry_id]["reason"] == "probe_step"
    damper_clock.advance(61.0)
    await controller._async_apply_active_control()
    assert _values(calls) == ["1.8,2.10"]
    calls.clear()

    # The car takes the 2 A and the battery gives up 1.3 A per phase; the grid does not move.
    set_charger_delivered_a(hass, prefix, 7.12)
    _battery_w(hass, 15000.0 - 3 * 230.0 * 1.3)
    await controller._async_apply_active_control()
    assert controller._yield_stepping_last[charger.entry_id]["state"] == "settling"
    yield_clock.advance(31.0)
    await controller._async_apply_active_control()
    assert controller._yield_stepping_last[charger.entry_id]["state"] == "confirmed"
    # The steps that follow are credited from the battery and reach the plan's current: each time
    # the car takes what it was given, the battery gives up the same and the grid does not move.
    assigned = 8
    written: list[int] = []
    damper = controller._dampers[charger.entry_id]
    for _ in range(20):
        # The regulator, at the limit with the car running, proposes what it has.
        controller.regulator_decisions = {
            charger.entry_id: _decision(proposed_a=float(assigned), requested_a=16.0, reason=_NO_HEADROOM)
        }
        yield_clock.advance(31.0)
        damper_clock.advance(61.0)
        await controller._async_apply_active_control()
        assigned = int(damper.last_written_a)
        if not written or written[-1] != assigned:
            written.append(assigned)
        delivered = assigned * 0.89
        set_charger_delivered_a(hass, prefix, delivered)
        _battery_w(hass, 15000.0 - 3 * 230.0 * (delivered - 5.34))
        await controller._async_apply_active_control()
        assigned = int(damper.last_written_a)
        if assigned == 16:
            break
    assert assigned == 16, written
    # Each step is one the stepper licensed: credited steps of up to 3 A, never past the plan's.
    assert all(0 < later - earlier <= 3 for earlier, later in zip([8] + written, written))
    assert max(written) == 16
    log = controller.regulator_decision_log
    assert [entry["outcome"] for entry in log if entry["outcome"].startswith("probe_")] == [
        "probe_started",
        "probe_succeeded",
    ]


async def test_a_battery_that_does_not_yield_stops_the_car_within_the_window_and_backs_off(
    hass: HomeAssistant, monkeypatch
) -> None:
    (controller, charger, calls, yield_clock, damper_clock, site, prefix, cc, turn_off) = (
        await _paused_site(hass, monkeypatch, "bfstuck")
    )

    await controller._async_apply_active_control()
    assert _values(calls) == ["1.6,2.10"]
    calls.clear()
    hass.states.async_set(f"switch.{prefix}", "on")

    # The battery does not move: the grid takes the car's 6 A on top (within what it can explain).
    set_charger_delivered_a(hass, prefix, 5.9)
    set_site_current_a(hass, site, 26.0)
    yield_clock.advance(10.0)
    await controller._async_apply_active_control()
    assert calls == []  # verifying

    yield_clock.advance(21.0)
    await controller._async_apply_active_control()
    assert len(turn_off) == 1  # stopped, hard
    assert cc._paused_by_balancing is True  # the failed probe's stop is a balancing pause again
    snapshot = controller.battery_probe_snapshot[charger.entry_id]
    assert (snapshot["last_outcome"], snapshot["last_reason"]) == ("failed", "battery_did_not_yield")
    assert snapshot["state"] == "backoff" and snapshot["backoff_remaining_s"] > 590.0
    calls.clear()

    # Back at 0 A the car is not probed again for 10 minutes, whatever the battery does.
    hass.states.async_set(f"switch.{prefix}", "off")
    set_charger_delivered_a(hass, prefix, 0.0)
    set_site_current_a(hass, site, 20.0)
    yield_clock.advance(300.0)
    await controller._async_apply_active_control()
    yield_clock.advance(299.0)
    await controller._async_apply_active_control()
    assert "1.6,2.10" not in _values(calls)
    calls.clear()

    yield_clock.advance(2.0)
    await controller._async_apply_active_control()
    assert _values(calls) == ["1.6,2.10"]  # the second probe
    calls.clear()
    hass.states.async_set(f"switch.{prefix}", "on")
    set_charger_delivered_a(hass, prefix, 5.9)
    set_site_current_a(hass, site, 26.0)
    yield_clock.advance(31.0)
    await controller._async_apply_active_control()
    assert len(turn_off) == 2
    # The back-off doubled.
    assert controller.battery_probe_snapshot[charger.entry_id]["backoff_remaining_s"] > 1190.0
    log = [e for e in controller.regulator_decision_log if e["outcome"].startswith("probe_")]
    assert [e["outcome"] for e in log] == [
        "probe_started",
        "probe_failed",
        "probe_started",
        "probe_failed",
    ]
    assert log[1]["detail"] == "battery_did_not_yield"


async def test_a_real_overload_during_the_probe_stops_the_car_at_once(
    hass: HomeAssistant, monkeypatch
) -> None:
    (controller, charger, calls, yield_clock, damper_clock, site, prefix, cc, turn_off) = (
        await _paused_site(hass, monkeypatch, "bfover")
    )
    await controller._async_apply_active_control()
    assert _values(calls) == ["1.6,2.10"]
    calls.clear()
    hass.states.async_set(f"switch.{prefix}", "on")

    # Far more than the car's minimum can explain, seconds into the window.
    set_charger_delivered_a(hass, prefix, 5.9)
    set_site_current_a(hass, site, 29.0)
    yield_clock.advance(3.0)
    await controller._async_apply_active_control()
    assert len(turn_off) == 1
    snapshot = controller.battery_probe_snapshot[charger.entry_id]
    assert snapshot["last_reason"] == "over_fuse_cap"


async def test_a_stale_measurement_during_the_probe_stops_the_car(
    hass: HomeAssistant, monkeypatch
) -> None:
    (controller, charger, calls, yield_clock, damper_clock, site, prefix, cc, turn_off) = (
        await _paused_site(hass, monkeypatch, "bfstale")
    )
    await controller._async_apply_active_control()
    calls.clear()
    hass.states.async_set(f"switch.{prefix}", "on")

    for phase in ("l1", "l2", "l3"):
        hass.states.async_set(f"sensor.{site}_power_{phase}", "unavailable")
    yield_clock.advance(3.0)
    await controller._async_apply_active_control()
    assert len(turn_off) == 1
    assert controller.battery_probe_snapshot[charger.entry_id]["last_reason"] == "measurement_unusable"


async def test_a_probe_the_car_never_takes_ends_as_a_failure_not_a_charge(
    hass: HomeAssistant, monkeypatch
) -> None:
    (controller, charger, calls, yield_clock, damper_clock, site, prefix, cc, turn_off) = (
        await _paused_site(hass, monkeypatch, "bfnodraw")
    )
    await controller._async_apply_active_control()
    calls.clear()
    hass.states.async_set(f"switch.{prefix}", "on")
    # The car never takes the current: nothing proves the battery yielded.
    yield_clock.advance(31.0)
    await controller._async_apply_active_control()
    assert len(turn_off) == 1
    assert controller.battery_probe_snapshot[charger.entry_id]["last_reason"] == "car_not_drawing"


# -- no probe where it must not start


@pytest.mark.parametrize(
    "case",
    [
        "battery_first",
        "yield_off",
        "battery_too_small",
        "battery_not_charging",
        "grid_over_the_band",
        "car_drawing",
        "request_below_minimum",
        "headroom_for_the_minimum",
        "active_control_off",
        "not_paused_by_balancing",
        "no_vehicle",
        "held_by_the_charger",
        "overload_decision",
        "would_exceed_the_cap",
        "stale_grid",
    ],
)
async def test_no_probe_without_every_condition(hass: HomeAssistant, monkeypatch, case: str) -> None:
    from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter

    setup: dict = {}
    if case == "battery_first":
        setup["solar_priority"] = SOLAR_PRIORITY_BATTERY_FIRST
    if case == "yield_off":
        setup["yield_stepping_enabled"] = False
    battery_w = {"battery_too_small": 3000.0, "battery_not_charging": 0.0}.get(case, 15000.0)
    (controller, charger, calls, yield_clock, damper_clock, site, prefix, cc, turn_off) = (
        await _paused_site(hass, monkeypatch, f"bfno{case.replace('_', '')}", battery_w=battery_w, **setup)
    )
    if case == "overload_decision":
        _held_at_zero(controller, charger, reason=_OVERLOAD)
    if case == "grid_over_the_band":
        set_site_current_a(hass, site, 21.0)
    if case == "car_drawing":
        set_charger_delivered_a(hass, prefix, 3.0)
    if case == "headroom_for_the_minimum":
        set_site_current_a(hass, site, 12.0)
    if case == "active_control_off":
        controller.config[CONF_ACTIVE_CONTROL_ENABLED] = False
    if case == "request_below_minimum":
        cc._requested_current_a = 4
    if case == "not_paused_by_balancing":
        cc._paused_by_balancing = False  # a person's Stop, a window's end: never undone
    if case == "no_vehicle":
        monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda self: False)
    if case == "held_by_the_charger":
        monkeypatch.setattr(ChargerAdapter, "held_by_charger", lambda self: True)
    if case == "would_exceed_the_cap":
        set_site_current_a(hass, site, 20.0)
        controller.config["main_fuse_a"] = 14.0  # 20 A + 6 A is far past 1.4 x the fuse
    if case == "stale_grid":
        for phase in ("l1", "l2", "l3"):
            hass.states.async_set(f"sensor.{site}_power_{phase}", "unavailable")

    await controller._async_apply_active_control()

    assert calls == [], (case, _values(calls))
    assert turn_off == []
    assert [e for e in controller.regulator_decision_log if e["outcome"] == "probe_started"] == []
    assert cc.requested_current_a == (4 if case == "request_below_minimum" else 16)


async def test_a_start_the_charger_refuses_is_a_failed_probe_and_backs_off(
    hass: HomeAssistant, monkeypatch
) -> None:
    (controller, charger, calls, yield_clock, damper_clock, site, prefix, cc, turn_off) = (
        await _paused_site(hass, monkeypatch, "bfrefuse")
    )

    async def refuse(amps: int) -> bool:
        return False

    monkeypatch.setattr(cc, "async_battery_probe_start", refuse)
    await controller._async_apply_active_control()
    snapshot = controller.battery_probe_snapshot[charger.entry_id]
    assert (snapshot["last_outcome"], snapshot["last_reason"]) == ("failed", "start_refused")
    assert snapshot["backoff_remaining_s"] > 590.0 and snapshot["state"] == "backoff"
    assert calls == [] and turn_off == []


# -- the band: a battery-held limit is not an overload


async def _running_site(hass: HomeAssistant, entry_id: str, **setup):
    """The car running at 12 A on a grid the battery holds at 20.0 A."""
    controller, charger, calls, yield_clock, damper_clock, site, prefix = await _owner_site(
        hass, entry_id, **setup
    )
    set_charger_delivered_a(hass, prefix, 10.7)
    _seed_assigned_a(controller, charger.entry_id, 12.0)
    await controller._async_apply_active_control()
    assert _values(calls)[-1] == "1.12,2.10"
    calls.clear()
    controller.regulator_decisions = {
        charger.entry_id: _decision(proposed_a=11.0, requested_a=16.0, reason=_OVERLOAD)
    }
    return controller, charger, calls, yield_clock, damper_clock, site, prefix


async def test_the_grid_held_at_20_03_amps_does_not_ratchet_the_car_down(
    hass: HomeAssistant,
) -> None:
    controller, charger, calls, yield_clock, damper_clock, site, prefix = await _running_site(
        hass, "bfband"
    )
    set_site_current_a(hass, site, 20.03)
    for _ in range(4):
        yield_clock.advance(31.0)
        damper_clock.advance(61.0)
        await controller._async_apply_active_control()
    assert calls == []
    held = [e for e in controller.regulator_decision_log if e["outcome"] == "held"]
    assert held and held[-1]["detail"] == "held_battery_at_limit"


@pytest.mark.parametrize("amps", [20.6, 21.5, 24.0])
async def test_a_reading_above_the_band_still_steps_the_car_down(
    hass: HomeAssistant, amps: float
) -> None:
    controller, charger, calls, yield_clock, damper_clock, site, prefix = await _running_site(
        hass, f"bfabove{int(amps * 10)}"
    )
    set_site_current_a(hass, site, amps)
    await controller._async_apply_active_control()
    assert _values(calls) == ["1.11,2.10"]


@pytest.mark.parametrize("case", ["battery_first", "yield_off", "battery_not_charging", "small"])
async def test_the_band_is_not_granted_without_its_conditions(
    hass: HomeAssistant, case: str
) -> None:
    setup: dict = {}
    battery_w = 15000.0
    if case == "battery_first":
        setup["solar_priority"] = SOLAR_PRIORITY_BATTERY_FIRST
    if case == "yield_off":
        setup["yield_stepping_enabled"] = False
    if case == "battery_not_charging":
        battery_w = 0.0
    if case == "small":
        battery_w = 3000.0  # under the car's minimum power
    controller, charger, calls, yield_clock, damper_clock, site, prefix = await _running_site(
        hass, f"bfbandno{case}", battery_w=battery_w, **setup
    )
    set_site_current_a(hass, site, 20.03)
    await controller._async_apply_active_control()
    # Exactly what happens today: the reduction goes ahead.
    assert _values(calls) == ["1.11,2.10"], case


async def test_a_stale_measurement_still_holds_exactly_as_today(hass: HomeAssistant) -> None:
    controller, charger, calls, yield_clock, damper_clock, site, prefix = await _running_site(
        hass, "bfbandstale"
    )
    for phase in ("l1", "l2", "l3"):
        hass.states.async_set(f"sensor.{site}_power_{phase}", "unavailable")
    await controller._async_apply_active_control()
    assert calls == []
    held = [e for e in controller.regulator_decision_log if e["outcome"] == "held"]
    assert held[-1]["detail"] != "held_battery_at_limit"


async def test_a_lowered_request_is_still_followed_in_the_band(hass: HomeAssistant) -> None:
    controller, charger, calls, yield_clock, damper_clock, site, prefix = await _running_site(
        hass, "bfbandreq"
    )
    set_site_current_a(hass, site, 20.03)
    # The person asked for 8 A: that reduction is not the battery's doing.
    controller.regulator_decisions = {
        charger.entry_id: _decision(proposed_a=8.0, requested_a=8.0, reason="decrease_confirmed_safe")
    }
    damper_clock.advance(61.0)
    await controller._async_apply_active_control()
    assert _values(calls) == ["1.8,2.10"]


async def test_the_diagnostics_carry_the_probe_outcome(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics

    (controller, charger, calls, yield_clock, damper_clock, site, prefix, cc, turn_off) = (
        await _paused_site(hass, monkeypatch, "bfdiag")
    )
    await controller._async_apply_active_control()
    hass.states.async_set(f"switch.{prefix}", "on")
    set_charger_delivered_a(hass, prefix, 5.9)
    set_site_current_a(hass, site, 26.0)
    yield_clock.advance(31.0)
    await controller._async_apply_active_control()

    site_entry = hass.config_entries.async_get_entry(controller.entry_id)
    diagnostics = await async_get_config_entry_diagnostics(hass, site_entry)
    entry = diagnostics["battery_probe"][charger.entry_id]
    assert (entry["last_outcome"], entry["last_reason"], entry["probes"]) == (
        "failed",
        "battery_did_not_yield",
        1,
    )


# -- the start-up warning about the charger's own measured current


def _missing_verdict(reason: str = "charger_measurement_missing"):
    from custom_components.spotnav.site.solar_surplus import SolarVerdict

    return SolarVerdict(
        action="hold",
        requested_a=None,
        reason=reason,
        state="off",
        net_grid_w=None,
        export_w=None,
        car_w=None,
        battery_w=None,
        available_w=None,
        available_a=None,
        priority_effective=None,
    )


def test_the_missing_measurement_warning_waits_out_the_start_up_grace(caplog) -> None:
    import logging
    from unittest.mock import MagicMock

    from custom_components.spotnav.execution.solar_execution import (
        MEASUREMENT_WARNING_GRACE_S,
        SolarExecutionCoordinator,
    )

    now = [5000.0]
    coordinator = SolarExecutionCoordinator(
        MagicMock(), "c1", MagicMock(), MagicMock(), MagicMock(), now=lambda: now[0]
    )
    caplog.set_level(logging.WARNING)

    def warnings() -> list[str]:
        return [r.message for r in caplog.records if "own measured current is missing" in r.message]

    # The first minutes after start: the charger has not reported yet, nothing is said.
    coordinator._log_transition(_missing_verdict())
    now[0] += MEASUREMENT_WARNING_GRACE_S - 1.0
    coordinator._log_transition(_missing_verdict())
    assert warnings() == []

    # Still missing after the grace: said once, not on every tick.
    now[0] += 2.0
    coordinator._log_transition(_missing_verdict())
    coordinator._log_transition(_missing_verdict())
    assert len(warnings()) == 1


def test_a_measurement_that_arrives_within_the_grace_is_never_warned_about(caplog) -> None:
    import logging
    from unittest.mock import MagicMock

    from custom_components.spotnav.execution.solar_execution import SolarExecutionCoordinator

    now = [0.0]
    coordinator = SolarExecutionCoordinator(
        MagicMock(), "c1", MagicMock(), MagicMock(), MagicMock(), now=lambda: now[0]
    )
    caplog.set_level(logging.WARNING)
    coordinator._log_transition(_missing_verdict())
    now[0] += 30.0
    coordinator._log_transition(_missing_verdict("arming"))
    now[0] += 600.0
    coordinator._log_transition(_missing_verdict("arming"))
    assert [r for r in caplog.records if "own measured current" in r.message] == []


# -- below the floor an OCPP charger is paused, never "written"; starts are capped; resume


async def test_an_ocpp_charger_below_the_floor_is_paused_and_nothing_claims_it_was_written(
    hass: HomeAssistant,
) -> None:
    from custom_components.spotnav.execution.controller import REGULATED_STOPPED, REGULATED_WROTE

    controller, charger, calls, yield_clock, damper_clock, site, prefix = await _owner_site(hass, "bfocpp")
    cc = controller_of(hass, charger.entry_id)
    assert cc.adapter.is_ocpp
    turn_off = async_mock_service(hass, "switch", "turn_off")

    result = await cc.async_apply_regulated_current(5, must_lower=True)

    assert (result.outcome, result.code, result.written) == (REGULATED_STOPPED, "pause", False)
    assert result.outcome != REGULATED_WROTE
    assert len(turn_off) == 1 and calls == []  # stopped through the switch, no AssignedCurrent
    hass.states.async_set(f"switch.{prefix}", "off")
    assert cc.paused_by_balancing is True


async def test_an_ocpp_write_the_charger_could_not_take_is_not_reported_as_written(
    hass: HomeAssistant,
) -> None:
    from custom_components.spotnav.execution.controller import REGULATED_WROTE

    controller, charger, calls, yield_clock, damper_clock, site, prefix = await _owner_site(hass, "bfocpp2")
    cc = controller_of(hass, charger.entry_id)
    async_mock_service(hass, "switch", "turn_off")

    async def refuse(*args, **kwargs):
        return "read_failed"

    cc.adapter.async_set_current = refuse  # the charger's current could not be read or written
    result = await cc.async_apply_regulated_current(10, must_lower=False)

    assert result.outcome != REGULATED_WROTE and result.written is False


async def test_a_paused_ocpp_charge_resumes_when_headroom_returns(hass: HomeAssistant, monkeypatch) -> None:
    (controller, charger, calls, yield_clock, damper_clock, site, prefix, cc, turn_off) = (
        await _paused_site(hass, monkeypatch, "bfresume", battery_w=0.0)
    )
    # Headroom for the minimum and a little more, for the dwell: nothing at first, then a restart.
    set_site_current_a(hass, site, 11.0)  # 9 A of headroom on a 20 A fuse
    await controller._async_apply_active_control()
    assert calls == []
    yield_clock.advance(61.0)
    await controller._async_apply_active_control()
    assert _values(calls) == ["1.9,2.10"]  # min(16 requested, 9 allowed)
    assert cc.requested_current_a == 16
    assert [e["outcome"] for e in controller.regulator_decision_log][-1] == "resumed"


async def test_a_paused_charge_is_not_resumed_without_the_minimum_plus_a_margin(
    hass: HomeAssistant, monkeypatch
) -> None:
    (controller, charger, calls, yield_clock, damper_clock, site, prefix, cc, turn_off) = (
        await _paused_site(hass, monkeypatch, "bfnoresume", battery_w=0.0)
    )
    set_site_current_a(hass, site, 13.5)  # 6.5 A: under the floor plus the 1 A margin
    for _ in range(3):
        yield_clock.advance(61.0)
        await controller._async_apply_active_control()
    assert calls == []


async def test_a_session_start_never_gives_the_car_more_than_the_site_allows(hass: HomeAssistant) -> None:
    controller, charger, calls, yield_clock, damper_clock, site, prefix = await _owner_site(hass, "bfstart")
    cc = controller_of(hass, charger.entry_id)
    cc.set_start_cap(lambda: controller.start_allowance_a(charger.entry_id))
    turn_on = async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set(f"switch.{prefix}", "off")

    # 5.5 A of headroom: below the floor, the car is not started at all.
    set_site_current_a(hass, site, 14.5)
    assert await cc.async_start(16) is False
    assert turn_on == [] and calls == []
    assert cc.requested_current_a == 16 and cc._paused_by_balancing is True

    # 8 A of headroom: started at min(16, 8).
    set_site_current_a(hass, site, 12.0)
    assert await cc.async_start(16) is True
    assert _values(calls) == ["1.8,2.10"]
    assert len(turn_on) == 1 and cc.requested_current_a == 16

    # Active control off: no cap, exactly as before.
    controller.config[CONF_ACTIVE_CONTROL_ENABLED] = False
    calls.clear()
    hass.states.async_set(f"switch.{prefix}", "off")
    set_site_current_a(hass, site, 14.5)
    assert await cc.async_start(16) is True
    assert _values(calls) == ["1.16,2.10"]


# -- a paused charge that is no longer wanted is never resumed or probed

_ENDINGS = ["window_end", "final_window_end", "person_stop", "person_pause", "solar_off", "new_plan_later"]


async def _end_the_wish(hass: HomeAssistant, cc, charger, ending: str) -> None:
    from homeassistant.util import dt as dt_util

    from custom_components.spotnav.runtime import executor_for

    from .helpers import future_window, install_schedule

    if ending == "window_end":
        cc._async_end_callback(dt_util.utcnow())
    elif ending == "final_window_end":
        cc._async_final_end_callback(dt_util.utcnow())
    elif ending == "person_stop":
        await executor_for(hass, charger.entry_id).async_manual_stop()
    elif ending == "person_pause":
        await executor_for(hass, charger.entry_id).async_pause()
    elif ending == "solar_off":
        await executor_for(hass, charger.entry_id).async_solar_stop()
    elif ending == "new_plan_later":
        start, end = future_window()
        await install_schedule(cc, {"start": start, "end": end, "amps": 16})
    await hass.async_block_till_done()


@pytest.mark.parametrize("ending", _ENDINGS)
@pytest.mark.parametrize("what", ["resume", "probe"])
async def test_a_paused_charge_is_not_restarted_once_it_is_no_longer_wanted(
    hass: HomeAssistant, monkeypatch, ending: str, what: str
) -> None:
    # resume: headroom returns; probe: no headroom, the battery charges from the grid.
    (controller, charger, calls, yield_clock, damper_clock, site, prefix, cc, turn_off) = (
        await _paused_site(
            hass,
            monkeypatch,
            f"bfwish{what}{ending.replace('_', '')}",
            battery_w=0.0 if what == "resume" else 15000.0,
        )
    )
    turn_on = async_mock_service(hass, "switch", "turn_on")
    cc._paused_by_balancing = True
    if ending in ("window_end", "final_window_end"):
        # A window's end ends only the plan's own wish: the paused charge is the window's.
        cc._paused_charge = ("plan_window", True)
    await _end_the_wish(hass, cc, charger, ending)
    hass.states.async_set(f"switch.{prefix}", "off")
    set_site_current_a(hass, site, 11.0 if what == "resume" else 20.0)
    controller.regulator_decisions = {
        charger.entry_id: _decision(proposed_a=0.0, requested_a=0.0, reason=_NO_HEADROOM)
    }

    for _ in range(4):
        yield_clock.advance(61.0)
        await controller._async_apply_active_control()

    assert cc._paused_by_balancing is False
    assert calls == [] and turn_on == [], (what, ending)
    assert [e for e in controller.regulator_decision_log if e["outcome"] in ("resumed", "probe_started")] == []
