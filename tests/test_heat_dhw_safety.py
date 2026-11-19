"""The hot water's safety review fixes (`plans/review_dhw_2026-11-19.md`): each finding reproduced on the pilot's
site, then held."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.heat.adapters import CTC_BMS, MYUPLINK, NIBE_F
from custom_components.spotnav.heat.dhw_ledger import SOURCE_PLAN

from .test_heat_dhw_runtime import LUX, LUX_OPTIONS, Writes, _live_pick, _open_gate, _pump, _quick_read_back, _site, settle  # noqa: F401 - the autouse fixture

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


# ---- S4: a pulse is read back by the meter's run; a pulse relay left on is opened at the start

PULSE_SWITCH = "switch.larm_utgang_2"


async def test_s4_a_pulse_is_read_back_by_the_meters_run(hass: HomeAssistant, freezer, monkeypatch) -> None:
    from .test_heat_dhw_runtime import press

    site, controller = await _site(hass, freezer, monkeypatch, hot_water_entity=PULSE_SWITCH)
    hot_water = controller.heat_shadow.hot_water
    hass.states.async_set(PULSE_SWITCH, "off")
    Writes(hass)
    making = {"now": False}
    monkeypatch.setattr(hot_water, "making_now", lambda: making["now"])
    now = controller.demand.now()
    assert (await press(hass, "start", site_id=site.entry_id))["result"]["ok"] is True
    entry = hot_water.ledger.last()
    assert entry.read_back is None and hot_water.boost(now)["state"] == "starting"
    # The meter shows the run: read back.
    making["now"] = True
    hot_water.tick(now + timedelta(minutes=2))
    assert hot_water.ledger.last().read_back is True
    assert hot_water.boost(now + timedelta(minutes=2))["state"] == "running"
    # A second press later that brings no run: not read back after ten minutes, and failed.
    making["now"] = False
    hot_water.tick(now + timedelta(minutes=30))
    freezer.tick(timedelta(minutes=31))
    later = controller.demand.now()
    assert (await press(hass, "start", site_id=site.entry_id))["result"]["ok"] is True
    hot_water.tick(later + timedelta(minutes=11))
    assert hot_water.ledger.last().read_back is False
    assert hot_water.boost(later + timedelta(minutes=11))["state"] == "failed"


async def test_s4_a_planned_pulse_without_a_run_is_tried_once_more_then_ends_the_day(hass: HomeAssistant, freezer, monkeypatch) -> None:
    controller, hot_water, at = await _live(hass, freezer, monkeypatch, None, hot_water_entity=PULSE_SWITCH)
    hass.states.async_set(PULSE_SWITCH, "off")
    monkeypatch.setattr(hot_water, "making_now", lambda: False)
    monkeypatch.setattr(hot_water, "_data_fresh", lambda _now: True)
    writes = Writes(hass)
    hot_water.tick(at)
    await settle(hass)
    assert len(writes.calls) == 2  # on, off
    record = hot_water.days[hot_water.picks[0].peak.astimezone(hot_water.zone).date().isoformat()]
    assert record["live"] == []
    hot_water.tick(at + timedelta(minutes=11))
    await settle(hass)
    assert hot_water.ended_day is None and record["live"] == []
    # Tried once more (it is still within the pick's start window when the retry comes due).
    from dataclasses import replace

    first = hot_water.picks[0]
    hot_water.picks = [replace(first, start=at + timedelta(minutes=12), end=at + timedelta(minutes=42))]
    hot_water.tick(at + timedelta(minutes=16))
    await settle(hass)
    assert len(writes.calls) == 4
    hot_water.tick(at + timedelta(minutes=27))
    await settle(hass)
    assert hot_water.ended_day == at.astimezone(hot_water.zone).date().isoformat()


async def test_s4_a_pulse_relay_left_on_is_opened_at_the_start(hass: HomeAssistant, freezer, monkeypatch) -> None:
    from custom_components.spotnav.heat.dhw_ledger import SOURCE_BUTTON, Entry

    site, controller = await _site(hass, freezer, monkeypatch, hot_water_entity=PULSE_SWITCH)
    hot_water = controller.heat_shadow.hot_water
    writes = Writes(hass)
    hot_water.ledger.add(Entry(PULSE_SWITCH, "turn_on", controller.demand.now(), SOURCE_BUTTON))
    hass.states.async_set(PULSE_SWITCH, "on")
    await hot_water.async_start()
    assert writes.calls == [("switch", "turn_off", {"entity_id": PULSE_SWITCH})]
    # A switch SpotNav never pulsed is left alone.
    hot_water.ledger.entries = []
    writes.calls.clear()
    hass.states.async_set(PULSE_SWITCH, "on")
    await hot_water.async_start()
    assert writes.calls == []


# ---- S5: a boost not verified to end by itself: the button only, with a backstop stop

HEISHA = "switch.aquarea_force_dhw"


async def test_s5_an_unverified_boost_is_the_buttons_only_and_is_stopped_after_its_run(hass: HomeAssistant, freezer, monkeypatch) -> None:
    from custom_components.spotnav.heat.adapters import HEISHAMON

    from .test_heat_dhw_runtime import press

    controller, hot_water, at = await _live(hass, freezer, monkeypatch, _pump(HEISHAMON, HEISHA))
    hass.states.async_set(HEISHA, "off")
    writes = Writes(hass)
    assert hot_water.route().verified is False
    # No planned boost on it: not planned, not written.
    hot_water.tick(at)
    await settle(hass)
    assert writes.calls == []
    hot_water._plan(at)  # noqa: SLF001
    assert hot_water.picks == []
    assert hot_water.level(controller.heat_shadow.config(), hot_water.route()) is None
    # The button may start it; SpotNav stops it once its run should be over.
    site = hass.config_entries.async_entries("spotnav")
    entry_id = next(entry.entry_id for entry in site if entry.data.get("heat") is not None)
    assert (await press(hass, "start", site_id=entry_id))["result"]["ok"] is True
    assert writes.calls == [("switch", "turn_on", {"entity_id": HEISHA})]
    started = hot_water.ledger.last().at
    hot_water.run_len = timedelta(minutes=30)
    hot_water.tick(started + timedelta(minutes=40))
    await settle(hass)
    assert len(writes.calls) == 1
    hot_water.tick(started + timedelta(minutes=46))
    await settle(hass)
    assert writes.calls[-1] == ("switch", "turn_off", {"entity_id": HEISHA})
    # Once: it reads off and is the button's no more.
    hot_water.tick(started + timedelta(minutes=48))
    await settle(hass)
    assert len(writes.calls) == 2
