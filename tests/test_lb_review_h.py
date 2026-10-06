"""Adversarial probes of HA e891761 (start credit, balancing resume, single stop). Assert the SAFE behaviour:
a failing probe is a finding. Copy into tests/ to run."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_MEASURED_CURRENT_SOURCE,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    MEASUREMENT_MODE_DERIVED,
)
from custom_components.spotnav.execution.controller import STOP_SETTLE_S
from custom_components.spotnav.site.regulator_damping import RegulatorDamper

from .helpers import make_entry, make_site_entry
from .test_duplicate_stops import _charging, _report
from .test_lb_start_credit import _halo, _site_a
from .test_yield_stepping_wiring import _DamperClock, _SecondsClock
from .world import controller_of, separate_entities_source, set_charger_delivered_a, set_derived_site_entities


class _Two:
    pass


async def _two(hass: HomeAssistant, monkeypatch, *, fuse: float, base: float) -> _Two:
    """Two 3-phase OCPP chargers A and B on one derived site, both plugged in, neither charging."""
    from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter

    monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda self: True)
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.16,2.10"})
    configure = async_mock_service(hass, "ocpp", "configure")
    t = _Two()
    t.hass = hass
    t.configure = configure
    entries = {}
    for name in ("a", "b"):
        prefix = f"two_{name}_charger"
        hass.states.async_set(f"switch.{prefix}", "off")
        entry = make_entry(
            hass,
            entry_id=prefix,
            charge_control=f"switch.{prefix}",
            current_limit=f"number.{prefix}_connector_1_session_current_limit",
            webhook_id=f"webhook-two-{name}",
            title=f"C{name}",
            current_control=CURRENT_CONTROL_CHANGE_CONFIGURATION,
            ocpp_target=(prefix, 1),
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        set_charger_delivered_a(hass, prefix, 0.0)
        entries[name] = (entry, prefix)
    site = "two_site"
    derived = set_derived_site_entities(hass, site, 0.0)
    _site_a(hass, site, base, base, base)
    wiring = {
        entries[n][0].entry_id: {
            "phases": 3,
            "phase": None,
            "min_current_a": 6.0,
            CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                f"sensor.{entries[n][1]}_l1", f"sensor.{entries[n][1]}_l2", f"sensor.{entries[n][1]}_l3"
            ),
        }
        for n in ("a", "b")
    }
    site_entry = make_site_entry(
        hass,
        entry_id=site,
        main_fuse_a=fuse,
        safety_margin_a=0.0,
        charger_entry_ids=[entries["a"][0].entry_id, entries["b"][0].entry_id],
        phase_wiring=wiring,
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived,
        active_control_enabled=True,
        yield_stepping_enabled=True,
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    controller = controller_of(hass, site_entry.entry_id)
    t.turn_on = async_mock_service(hass, "switch", "turn_on")
    t.turn_off = async_mock_service(hass, "switch", "turn_off")
    if controller._state_listener_cancel is not None:
        controller._state_listener_cancel()
        controller._state_listener_cancel = None
    for cancel in controller._controller_listener_cancels:
        cancel()
    controller._controller_listener_cancels.clear()
    t.clock = _SecondsClock()
    controller._yield_now = t.clock
    t.damper_clock = _DamperClock()
    t.cc = {}
    t.prefix = {}
    for n in ("a", "b"):
        entry, prefix = entries[n]
        cc = controller_of(hass, entry.entry_id)
        cc.set_start_cap(
            lambda eid=entry.entry_id: controller.start_allowance_a(eid),
            reserve=lambda amps, eid=entry.entry_id: controller.reserve_start(eid, amps),
        )
        controller._dampers[entry.entry_id] = RegulatorDamper(now=t.damper_clock)
        t.cc[n] = cc
        t.prefix[n] = prefix
    controller._yield_steppers.clear()
    configure.clear()
    t.controller = controller
    t.site = site
    return t


async def _tick(t) -> None:
    t.controller._recompute(schedule_apply=False)
    await t.controller._async_apply_active_control()
    await t.hass.async_block_till_done()


def _writes(t, n: str) -> list[float]:
    out = []
    for call in t.configure:
        if call.data.get("devid", "") == t.prefix[n] or t.prefix[n] in str(call.data):
            out.append(float(call.data["value"].split(",")[0].split(".")[1]))
    return out


# -- (1) start credit


async def test_probe_two_chargers_one_rise_is_not_credited_twice(hass: HomeAssistant, monkeypatch) -> None:
    """A starts at 16, B gets the 6 A left. Only A's car begins drawing (site +16 A); neither charger's own reading
    has arrived. The 16 A rise is A's: B must not be credited with it as well."""
    t = await _two(hass, monkeypatch, fuse=23.0, base=1.0)
    assert await t.cc["a"].async_start(16, cause="plan_window")
    await hass.async_block_till_done()
    assert await t.cc["b"].async_start(16, cause="plan_window")
    await hass.async_block_till_done()
    for n in ("a", "b"):
        hass.states.async_set(f"switch.{t.prefix[n]}", "on")
    await hass.async_block_till_done()
    print("writes after starts", [c.data for c in t.configure])
    t.configure.clear()
    _site_a(hass, t.site, 17.0, 17.0, 17.0)  # A's car only
    await _tick(t)
    snap = t.controller.start_credit_snapshot()
    print("credit", snap, "writes", [c.data for c in t.configure])
    credited_total = sum(max(v["credited_a"].values() or [0.0]) for v in snap.values())
    assert credited_total <= 16.0 + 1e-6, f"a 16 A rise credited {credited_total} A in all: {snap}"


async def test_probe_two_chargers_double_credit_does_not_raise_b_past_the_fuse(hass: HomeAssistant, monkeypatch) -> None:
    """As above; whatever is written to B must keep A(16)+B within 23 - 1 = 22 A once B's car draws too."""
    t = await _two(hass, monkeypatch, fuse=23.0, base=1.0)
    assert await t.cc["a"].async_start(16, cause="plan_window")
    await hass.async_block_till_done()
    assert await t.cc["b"].async_start(16, cause="plan_window")
    await hass.async_block_till_done()
    for n in ("a", "b"):
        hass.states.async_set(f"switch.{t.prefix[n]}", "on")
    await hass.async_block_till_done()
    first = [c.data for c in t.configure]
    t.configure.clear()
    _site_a(hass, t.site, 17.0, 17.0, 17.0)
    for _ in range(4):
        await _tick(t)
        t.clock.advance(20.0)
        t.damper_clock.advance(20.0)
    later = [c.data for c in t.configure]
    print("first", first, "later", later)
    b_values = [int(d["value"].split(",")[0].split(".")[1]) for d in first + later if t.prefix["b"] in str(d)]
    assert all(v <= 6 for v in b_values), f"B raised past the 6 A left: {b_values}"


async def test_probe_a_house_load_does_not_free_a_reservation(hass: HomeAssistant, monkeypatch) -> None:
    """A's start of 13 is on the site; a 10 A house load starts with it, A's car not drawing yet. B's start may get
    only what is left with A's whole 13 still held (the reservation's own rule): 25 - 12 - 13 = 0."""
    t = await _two(hass, monkeypatch, fuse=25.0, base=2.0)
    assert await t.cc["a"].async_start(13, cause="plan_window")
    await hass.async_block_till_done()
    hass.states.async_set(f"switch.{t.prefix['a']}", "on")
    await hass.async_block_till_done()
    _site_a(hass, t.site, 12.0, 12.0, 12.0)  # a house load, not the car
    await _tick(t)
    allowance = t.controller.start_allowance_a(t.cc["b"].entry_id)
    print("credit", t.controller.start_credit_snapshot(), "B allowance", allowance)
    assert allowance is not None and allowance < 6.0, f"B may start at {allowance} A on a margin A's car will take"


async def test_probe_house_load_during_credit_lowers_before_the_car_draws(hass: HomeAssistant, monkeypatch) -> None:
    """HALO started at 13; a 10 A load on L1 arrives before the car draws and before its reading: L1 has 8.37 A
    left. Today's rule (no credit) lowers to 8 before the car's 13 A takes L1 to 24.6 A."""
    halo = await _halo(hass, monkeypatch, "probe_h3")
    await halo.start()
    halo.switch("on")
    await hass.async_block_till_done()
    halo.configure.clear()
    _site_a(hass, halo.site, 11.63, 0.92, 6.36)
    await halo.tick()
    print("credit", halo.controller.start_credit_snapshot(), "writes", halo.written(), halo.log()[-1])
    assert halo.written() and halo.written()[-1] in ("1.8,2.10", "1.8,2.1"), halo.written()


async def test_probe_credit_ends_with_a_stop(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _halo(hass, monkeypatch, "probe_stop")
    await halo.start()
    halo.switch("on")
    await hass.async_block_till_done()
    _site_a(hass, halo.site, 14.63, 13.92, 19.36)
    await halo.tick()
    assert halo.controller.start_credit_snapshot()
    await halo.cc.async_stop()
    halo.switch("off")
    await hass.async_block_till_done()
    await halo.tick()
    assert halo.controller.start_credit_snapshot() == {}


# -- (3) single stop


async def test_probe_a_safety_stop_is_not_swallowed_after_a_resend_of_current(hass: HomeAssistant, freezer) -> None:
    """A stop of ours went out; the charger reported off, then on again (the car resumed by itself) but the
    report of `off` was missed (the charger went off and on between two polls). A safety stop then must go out."""
    controller, _starts, stops = await _charging(hass)
    await controller._regulated_stop("pause")
    freezer.tick(timedelta(seconds=5))
    # the charger never reported off: still on 5 s later, and charging (it ignored or re-started)
    await controller._regulated_stop("safety_stop")
    assert len(stops) == 2, "a safety stop 5 s after an unanswered pause is swallowed by the settle window"
    await controller.async_shutdown()


async def test_probe_a_persons_stop_after_an_unanswered_pause_goes_out(hass: HomeAssistant, freezer) -> None:
    controller, _starts, stops = await _charging(hass)
    await controller._regulated_stop("pause")
    freezer.tick(timedelta(seconds=5))
    await controller.async_stop()
    assert len(stops) == 2, "a person's Stop 5 s after an unanswered pause sends nothing"
    await controller.async_shutdown()


async def test_probe_settle_window_ends(hass: HomeAssistant, freezer) -> None:
    controller, _starts, stops = await _charging(hass)
    await controller._regulated_stop("pause")
    freezer.tick(timedelta(seconds=STOP_SETTLE_S + 0.5))
    await controller.async_stop()
    assert len(stops) == 2
    await controller.async_shutdown()


async def test_probe_a_start_then_stop_is_sent(hass: HomeAssistant) -> None:
    controller, _starts, stops = await _charging(hass)
    await controller._regulated_stop("pause")
    await _report(hass, "off")
    assert await controller.async_start(10, manual=True)
    await _report(hass, "on")
    await controller.async_stop()
    assert len(stops) == 2
    await controller.async_shutdown()


# -- (2) resume


@pytest.mark.parametrize("ending", ["final_window_end", "window_end", "new_plan_later", "person_stop"])
async def test_probe_an_end_while_the_pause_settles_is_not_undone_by_a_repeat_pause(
    hass: HomeAssistant, monkeypatch, ending: str
) -> None:
    """Balancing pauses the car; the charger has not answered (still reads on) when the wish ends (the last window's
    end, say). The next pass pauses the still-'on' charger again: that repeat pause must not mark the charge as one
    to resume, or the regulator restarts it after the dwell past the end."""
    from .test_battery_fuse import _end_the_wish

    halo = await _halo(hass, monkeypatch, f"probe_end_{ending}")
    await halo.start()
    halo.switch("on")
    halo.reads(13.0)
    _site_a(hass, halo.site, 14.63, 13.92, 19.36)
    await hass.async_block_till_done()
    await halo.tick()
    _site_a(hass, halo.site, 30.0, 13.92, 19.36)
    await halo.tick()
    assert len(halo.turn_off) == 1
    await _end_the_wish(hass, halo.cc, halo.charger, ending)
    assert not halo.cc._paused_by_balancing
    halo.advance(2.0)
    await halo.tick()  # the charger still reads on and draws: the regulator pauses it again
    marked = halo.cc._paused_by_balancing
    halo.switch("off")
    halo.reads(0.0)
    _site_a(hass, halo.site, 1.6, 0.85, 4.44)
    await hass.async_block_till_done()
    starts = len(halo.turn_on)
    for _ in range(3):
        halo.advance(61.0)
        await halo.tick()
    print(ending, "marked", marked, "turn_off", len(halo.turn_off), "turn_on", len(halo.turn_on) - starts)
    assert len(halo.turn_on) == starts, f"restarted after {ending} (marked again: {marked})"


async def test_probe_resume_cycles_are_bounded(hass: HomeAssistant, monkeypatch) -> None:
    """A load on L1 that comes and goes every couple of minutes: how many pause/resume relay cycles in 10 min."""
    halo = await _halo(hass, monkeypatch, "probe_cycle")
    await halo.start()
    halo.switch("on")
    halo.reads(13.0)
    _site_a(hass, halo.site, 14.63, 13.92, 19.36)
    await hass.async_block_till_done()
    await halo.tick()
    for _ in range(5):
        _site_a(hass, halo.site, 30.0, 13.92, 19.36)
        await halo.tick()
        halo.switch("off")
        halo.reads(0.0)
        _site_a(hass, halo.site, 1.6, 0.85, 4.44)
        await hass.async_block_till_done()
        for _ in range(3):
            halo.advance(31.0)
            await halo.tick()
        halo.switch("on")
        halo.reads(6.0)
        _site_a(hass, halo.site, 7.6, 6.85, 10.44)
        await hass.async_block_till_done()
        await halo.tick()
    print("pauses", len(halo.turn_off), "resumes", len(halo.turn_on))
    assert len(halo.turn_off) <= 3, f"{len(halo.turn_off)} pauses and {len(halo.turn_on)} resumes in ~8 min"


# -- follow-ups


async def test_a_rise_another_chargers_own_reading_shows_is_not_credited_to_a_later_start(
    hass: HomeAssistant, monkeypatch
) -> None:
    """A's reading arrives (its credit ends); B, started with it, still reads nothing: the rise is A's, B gets none."""
    t = await _two(hass, monkeypatch, fuse=23.0, base=1.0)
    assert await t.cc["a"].async_start(16, cause="plan_window")
    await hass.async_block_till_done()
    assert await t.cc["b"].async_start(16, cause="plan_window")
    await hass.async_block_till_done()
    for n in ("a", "b"):
        hass.states.async_set(f"switch.{t.prefix[n]}", "on")
    set_charger_delivered_a(hass, t.prefix["a"], 16.0)
    _site_a(hass, t.site, 17.0, 17.0, 17.0)
    await hass.async_block_till_done()
    await _tick(t)
    snap = t.controller.start_credit_snapshot()
    assert t.cc["a"].entry_id not in snap
    assert not snap.get(t.cc["b"].entry_id, {}).get("credited_a"), snap



async def test_a_stop_under_a_persons_stop_that_sends_nothing_is_not_counted_toward_the_give_up(
    hass: HomeAssistant,
) -> None:
    """A stop of ours still settles: the stop under a person's Stop sends nothing, and is no attempt the charger
    could have ignored."""
    controller, _starts, stops = await _charging(hass)
    await controller._regulated_stop("pause")
    async with controller._lock:
        await controller._person_hold_stop_locked()
    assert len(stops) == 1
    assert controller._person_hold_stop_times == []
    await controller.async_shutdown()
