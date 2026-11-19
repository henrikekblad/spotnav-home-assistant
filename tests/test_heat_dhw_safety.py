"""The hot water's safety review fixes (`plans/review_dhw_2026-11-19.md`): each finding reproduced on the pilot's
site, then held."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.heat.adapters import CTC_BMS, MYUPLINK, NIBE_F
from custom_components.spotnav.heat.dhw_ledger import SOURCE_PLAN

from .test_heat_dhw_runtime import LUX, LUX_OPTIONS, Writes, _live_pick, _open_gate, _pump, _site, settle

pytestmark = pytest.mark.usefixtures("offline_relay")

CTC = "number.ctc_extra_hot_water_1006"
CLOUD = "switch.f730_temporary_lux_50004"


def _fuse(monkeypatch: pytest.MonkeyPatch, hot_water: Any, state: str, measured: dict, estimated: dict | None = None) -> None:
    from types import SimpleNamespace

    phases = ("l1", "l2", "l3")
    result = SimpleNamespace(
        state=state,
        measured_margin_a={phase: measured.get(phase) for phase in phases},
        estimated_headroom_if_battery_yields_a={phase: (estimated or {}).get(phase) for phase in phases},
    )
    monkeypatch.setattr(hot_water, "_site_result", lambda: result)


async def _live(hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, pump: Any, **heat: Any) -> tuple[Any, Any, datetime]:
    """A site steering its hot water live, a pick due now, the meter fresh and room on the fuse."""
    site, controller = await _site(hass, freezer, monkeypatch, pump, control_enabled=True, **heat)
    hot_water = controller.heat_shadow.hot_water
    monkeypatch.setattr(hot_water, "_meter_fresh", lambda _now: True)
    monkeypatch.setattr(hot_water, "_meter_found", lambda: True)
    _fuse(monkeypatch, hot_water, "observing", {"l1": 20.0, "l2": 20.0, "l3": 20.0})
    now = controller.demand.now()
    _open_gate(hot_water, now.astimezone(hot_water.zone).date())
    pick = _live_pick(hot_water, now, timedelta(0))
    hot_water.picks = [pick]
    return controller, hot_water, max(now, pick.start)


# ---- B1: a planned boost never writes over a boost already running


@pytest.mark.parametrize(
    ("adapter", "entity_id", "value", "attributes"),
    [
        (NIBE_F, LUX, "3 hrs", {"options": LUX_OPTIONS}),
        (CTC_BMS, CTC, "3", {}),
        (MYUPLINK, CLOUD, "on", {}),
    ],
)
async def test_b1_a_running_boost_is_never_written_over(
    hass: HomeAssistant, freezer, monkeypatch, adapter: Any, entity_id: str, value: str, attributes: dict[str, Any]
) -> None:
    controller, hot_water, at = await _live(hass, freezer, monkeypatch, _pump(adapter, entity_id))
    hass.states.async_set(entity_id, value, attributes)
    writes = Writes(hass)
    hot_water.tick(at)
    await settle(hass)
    assert writes.calls == []
    # The peak counts as filled: not tried again, no budget spent, not SpotNav's to shed.
    record = hot_water.days[hot_water.picks[0].peak.astimezone(hot_water.zone).date().isoformat()]
    assert record["filled"] == [hot_water.picks[0].peak.isoformat()] and record["live"] == []
    assert hot_water.ledger.used_on(at.astimezone(hot_water.zone).date(), hot_water.zone, SOURCE_PLAN) == 0
    assert hot_water.boost()["source"] == "user"
    hot_water.tick(at + timedelta(minutes=2))
    await settle(hass)
    assert writes.calls == []
    # Nor is it counted as SpotNav's in what the plans see.
    hot_water._delta_key = None  # noqa: SLF001
    assert hot_water.forecast_delta(at) == {}


async def test_b1_no_planned_boost_while_the_pump_makes_hot_water(hass: HomeAssistant, freezer, monkeypatch) -> None:
    controller, hot_water, at = await _live(hass, freezer, monkeypatch, _pump(NIBE_F, LUX))
    hass.states.async_set(LUX, "Off", {"options": LUX_OPTIONS})
    monkeypatch.setattr(hot_water, "making_now", lambda: True)
    writes = Writes(hass)
    hot_water.tick(at)
    await settle(hass)
    assert writes.calls == []


# ---- B2: the safety reasons are computed at the start, on every path


OUTDOOR = "sensor.ute"
ROOM = "sensor.vardagsrum"


@pytest.mark.parametrize(
    ("outdoor", "room", "reason"),
    [("-18.0", "21.0", "too_cold"), ("4.0", "19.0", "room_cold"), ("4.0", "unavailable", "stale")],
)
async def test_b2_a_safety_reason_holds_a_boost_without_a_steered_contact(
    hass: HomeAssistant, freezer, monkeypatch, outdoor: str, room: str, reason: str
) -> None:
    """A cloud pump with no relay: the heat control never runs, so its cached safety stays empty; the boost's own
    check still finds the reason."""
    hass.states.async_set(OUTDOOR, outdoor, {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set(ROOM, room, {"unit_of_measurement": "°C", "device_class": "temperature"})
    controller, hot_water, at = await _live(
        hass, freezer, monkeypatch, _pump(MYUPLINK, CLOUD), outdoor_sensor=OUTDOOR, indoor_sensors=[ROOM]
    )
    hass.states.async_set(CLOUD, "off")
    writes = Writes(hass)
    assert controller.heat_shadow.control.safety is None
    assert controller.heat_shadow.control.safety_now(controller.heat_shadow.config(), at) == reason
    hot_water.tick(at)
    await settle(hass)
    assert writes.calls == []
    # Good readings: it starts.
    hass.states.async_set(OUTDOOR, "4.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set(ROOM, "21.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hot_water.tick(at + timedelta(minutes=1))
    await settle(hass)
    assert writes.calls == [("switch", "turn_on", {"entity_id": CLOUD})]


async def test_b2_an_old_cached_reason_does_not_hold_a_boost(hass: HomeAssistant, freezer, monkeypatch) -> None:
    hass.states.async_set(OUTDOOR, "4.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    controller, hot_water, at = await _live(hass, freezer, monkeypatch, _pump(MYUPLINK, CLOUD), outdoor_sensor=OUTDOOR)
    hass.states.async_set(CLOUD, "off")
    controller.heat_shadow.control.safety = "too_cold"
    writes = Writes(hass)
    hot_water.tick(at)
    await settle(hass)
    assert writes.calls == [("switch", "turn_on", {"entity_id": CLOUD})]


# ---- S7: a boost's owner is kept in the ledger, across a restart and a ticking timer


async def test_s7_ownership_survives_a_restart_and_a_counting_timer(hass: HomeAssistant, freezer, monkeypatch) -> None:
    import json

    from custom_components.spotnav.heat import dhw_runtime

    controller, hot_water, at = await _live(hass, freezer, monkeypatch, _pump(CTC_BMS, CTC))
    hass.states.async_set(CTC, "0")
    Writes(hass)
    hot_water.tick(at)
    await settle(hass)
    assert hot_water.boost()["source"] == "plan"
    # The timer counts down, long after the write: still SpotNav's.
    freezer.tick(timedelta(minutes=25))
    hass.states.async_set(CTC, "0.6")
    assert hot_water.boost()["source"] == "plan"
    # Home Assistant restarts: the kept ledger still says whose it is, and the picks are kept too.
    again = dhw_runtime.HotWater(hass, controller.heat_shadow)
    again.restore(json.loads(json.dumps(hot_water.stored())))
    monkeypatch.setattr(again, "_meter_fresh", lambda _now: True)
    monkeypatch.setattr(again, "_meter_found", lambda: True)
    assert again.picks == hot_water.picks
    controller.heat_shadow.hot_water = again
    freezer.tick(timedelta(minutes=5))
    hass.states.async_set(CTC, "0.5")
    assert again.boost()["source"] == "plan"
    assert controller.heat_shadow.control._boost_source(controller.heat_shadow.config()) == "plan"  # noqa: SLF001
    # It ends; the household's next boost is theirs.
    hass.states.async_set(CTC, "0")
    again.tick(at + timedelta(minutes=40))
    hass.states.async_set(CTC, "2")
    assert again.boost()["source"] == "user"


# ---- S1: the button's pulse ends a held block at once, and keeps it from starting, for the run's length


async def test_s1_a_pulse_ends_a_held_block_until_the_run_is_over(hass: HomeAssistant, freezer, monkeypatch) -> None:
    from custom_components.spotnav.heat import dhw_runtime

    from .test_heat_control import MIN, _running

    monkeypatch.setattr(dhw_runtime, "PULSE", timedelta(seconds=0))

    async def no_pause(_seconds: float) -> None:
        return None

    monkeypatch.setattr(dhw_runtime, "_pause", no_pause)
    _site_, control, relay, now = await _running(hass, freezer, 0, 1, hot_water_entity="switch.vv_puls")
    hot_water = control.shadow.hot_water
    monkeypatch.setattr(hot_water, "making_now", lambda: False)
    await control.async_look(now)
    assert relay.calls == ["on"] and control.held
    # The pulse is on for two seconds only: the control never sees the switch on.
    hass.states.async_set("switch.vv_puls", "off")
    error, _boost = await hot_water.async_press("start", now + MIN)
    await settle(hass)
    assert error is None
    # The ledger says a run was asked for: the block opens at the next look.
    await control.async_look(now + 2 * MIN)
    assert relay.calls == ["on", "off"] and control.boost_source == "user"
    # ...and no block starts while the run may still be coming or going.
    await control.async_look(now + 20 * MIN)
    assert relay.calls == ["on", "off"]
    # The meter shows the run, then its end: the block may come back.
    monkeypatch.setattr(hot_water, "making_now", lambda: True)
    hot_water.tick(now + 21 * MIN)
    monkeypatch.setattr(hot_water, "making_now", lambda: False)
    hot_water.tick(now + 40 * MIN)
    assert hot_water.pulse_owner(now + 40 * MIN) is None


# ---- S2: the live fuse check fails closed, counts what a battery would yield, and keeps the import target


async def _fuse_site(hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, datetime, Writes]:
    controller, hot_water, at = await _live(hass, freezer, monkeypatch, _pump(NIBE_F, LUX))
    hass.states.async_set(LUX, "Off", {"options": LUX_OPTIONS})
    return controller, hot_water, at, Writes(hass)


@pytest.mark.parametrize(
    ("state", "measured", "estimated", "starts"),
    [
        ("observing", {"l1": 20.0, "l2": 20.0, "l3": 20.0}, None, True),
        # Unknown phases: fail closed.
        ("observing", {"l1": 20.0, "l2": None, "l3": 20.0}, None, False),
        ("observing", {}, None, False),
        # A stale meter: the cars are in their failsafe; no boost either.
        ("stale_measurements", {"l1": 20.0, "l2": 20.0, "l3": 20.0}, None, False),
        # The battery holds the grid at the fuse: it would yield, so the boost fits.
        ("observing", {"l1": 0.5, "l2": 0.5, "l3": 0.5}, {"l1": 18.0, "l2": 18.0, "l3": 18.0}, True),
        ("observing", {"l1": 0.5, "l2": 0.5, "l3": 0.5}, {"l1": 18.0, "l2": 5.0, "l3": 18.0}, False),
    ],
)
async def test_s2_the_fuse_check_fails_closed_and_counts_the_battery(
    hass: HomeAssistant, freezer, monkeypatch, state: str, measured: dict, estimated: dict | None, starts: bool
) -> None:
    controller, hot_water, at, writes = await _fuse_site(hass, freezer, monkeypatch)
    _fuse(monkeypatch, hot_water, state, measured, estimated)
    hot_water.tick(at)
    await settle(hass)
    assert bool(writes.calls) is starts


async def test_s2_a_stale_meter_holds_a_boost(hass: HomeAssistant, freezer, monkeypatch) -> None:
    controller, hot_water, at, writes = await _fuse_site(hass, freezer, monkeypatch)
    _fuse(monkeypatch, hot_water, "observing", {"l1": 20.0, "l2": 20.0, "l3": 20.0})
    monkeypatch.setattr(hot_water, "_meter_fresh", lambda _now: False)
    monkeypatch.setattr(hot_water, "_meter_found", lambda: True)
    hot_water.tick(at)
    await settle(hass)
    assert writes.calls == []


async def test_s2_the_import_target_holds_a_boost(hass: HomeAssistant, freezer, monkeypatch) -> None:
    controller, hot_water, at, writes = await _fuse_site(hass, freezer, monkeypatch)
    _fuse(monkeypatch, hot_water, "observing", {"l1": 20.0, "l2": 20.0, "l3": 20.0})
    site = controller
    monkeypatch.setattr(site, "_import_target_kw", lambda: 6.0)
    monkeypatch.setattr(site, "effective_import_target_kw", lambda set_kw: set_kw)
    monkeypatch.setattr(hot_water, "_grid_kw", lambda: 3.5)
    hot_water.tick(at)
    await settle(hass)
    assert writes.calls == []
    monkeypatch.setattr(hot_water, "_grid_kw", lambda: 1.0)
    hot_water.tick(at + timedelta(minutes=1))
    await settle(hass)
    assert len(writes.calls) == 1


async def test_s2_shed_counts_what_the_battery_would_yield(hass: HomeAssistant, freezer, monkeypatch) -> None:
    controller, hot_water, at, writes = await _fuse_site(hass, freezer, monkeypatch)
    _fuse(monkeypatch, hot_water, "observing", {"l1": 20.0, "l2": 20.0, "l3": 20.0})
    hot_water.tick(at)
    await settle(hass)
    assert len(writes.calls) == 1
    # The battery charges up to the fuse: measured over, but it would yield.
    _fuse(monkeypatch, hot_water, "observing", {"l1": -1.0, "l2": 2.0, "l3": 2.0}, {"l1": 10.0, "l2": 12.0, "l3": 12.0})
    for minute in (1, 2, 3):
        hot_water.tick(at + timedelta(minutes=minute))
        await settle(hass)
    assert len(writes.calls) == 1


# ---- S3: the button refuses only while a phase is already over the fuse


async def test_s3_the_button_refuses_only_over_the_fuse(hass: HomeAssistant, freezer, monkeypatch) -> None:
    from .test_heat_dhw_runtime import press

    site, controller = await _site(hass, freezer, monkeypatch, _pump(NIBE_F, LUX))
    hot_water = controller.heat_shadow.hot_water
    hass.states.async_set(LUX, "Off", {"options": LUX_OPTIONS})
    writes = Writes(hass)
    _fuse(monkeypatch, hot_water, "observing", {"l1": -3.0, "l2": 10.0, "l3": 10.0})
    answer = (await press(hass, "start", site_id=site.entry_id))["result"]
    assert answer["error"] == "spotnav_no_headroom" and writes.calls == []
    # A refused press does not count toward the rate limit; a tight margin is still the household's to use.
    _fuse(monkeypatch, hot_water, "observing", {"l1": 1.0, "l2": 10.0, "l3": 10.0})
    assert (await press(hass, "start", site_id=site.entry_id))["result"]["ok"] is True
    assert len(writes.calls) == 1


async def test_s3_unknown_margins_do_not_refuse_the_button(hass: HomeAssistant, freezer, monkeypatch) -> None:
    from .test_heat_dhw_runtime import press

    site, controller = await _site(hass, freezer, monkeypatch, _pump(NIBE_F, LUX))
    hass.states.async_set(LUX, "Off", {"options": LUX_OPTIONS})
    writes = Writes(hass)
    _fuse(monkeypatch, controller.heat_shadow.hot_water, "stale_measurements", {})
    assert (await press(hass, "start", site_id=site.entry_id))["result"]["ok"] is True
    assert len(writes.calls) == 1
