"""The review's findings of 2026-11-24 on the pool, spa and sauna control (`plans/review_pool_spa_night_2026-11-24.md`),
each reproduced as the reviewer's probe did and held: the spa's and the sauna's session latched through its heat-up and
hold with the real planner (B1), a "now" press never past the device's longest run (B2), an unreadable lock blocking a
start (B3), an unreadable heat pump keeping its circulation (B4), a panel setpoint left alone (S1), the circulation not
held for a pump that is not SpotNav's (S2), and no plan read as "keep what is there" while the need still runs (S5).
Every write goes to a fake service; nothing reaches a device."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant

from custom_components.spotnav.heat.config import read_config
from custom_components.spotnav.pool.control import PoolSpaControl
from custom_components.spotnav.pool.runtime import PoolSpaShadow
from custom_components.spotnav.spa.runtime import Reading, new_sauna, new_spa

from .pool_spa_fakes import MIN, NOW, S, TZ, Devices, looks, make_control

PUMP = "climate.poolpump"
CIRC = "switch.pool_cirkulation"
SAUNA = "climate.bastu"
SPA = "climate.spa"
SESSION = datetime(2026, 7, 1, 19, 0, tzinfo=ZoneInfo(TZ))


def _pool(**extra: Any) -> dict[str, Any]:
    return {
        "pool": {
            "heat_pump": {"entity_id": PUMP}, "circulation_id": CIRC, "heat_pump_kw": 2.0, "circulation_kw": 0.5,
            "control_heat_pump": True, "control_circulation": True, **extra,
        }
    }


def _pool_devices(hass: HomeAssistant, *, mode: str = "off", circ: str = "off", setpoint: float = 27.0, **attributes: Any) -> Devices:
    devices = Devices(hass)
    devices.add(PUMP, mode, temperature=setpoint, current_temperature=24.0, min_temp=12, max_temp=40, hvac_modes=["off", "heat", "cool"], **attributes)
    devices.add(CIRC, circ)
    return devices


async def _session(hass: HomeAssistant, heat: dict[str, Any], kind: str, entity: str, start_c: float, up: float, down: float, begin: datetime, end: datetime) -> None:
    """The real shadow and the real control through a session, the water or the room warming while it heats."""
    control, shadow, _order = make_control(hass, heat)
    real = new_sauna() if kind == "sauna" else new_spa()
    setattr(shadow, kind, real)
    config = getattr(read_config(heat), kind)
    temp = start_c
    moment = begin
    while moment <= end:
        state = hass.states.get(entity)
        assert state is not None
        heating = state.attributes.get("preset_mode") == "ready" if kind == "spa" else state.state == "heat"
        top = 80.0 if kind == "sauna" else 38.0
        temp = min(temp + up, top) if heating else max(20.0, temp - down)
        real.look(moment, TZ, config, Reading(temp, heating, None, None), [])
        await control.async_look(moment)
        moment += 30 * S


async def test_b1_the_sauna_heats_and_holds_its_session_without_toggling_or_stopping_at_its_time(hass: HomeAssistant) -> None:
    devices = Devices(hass)
    devices.add(SAUNA, "off", temperature=80, current_temperature=22, min_temp=40, max_temp=110, hvac_modes=["off", "heat"])
    heat = {"sauna": {"entity_id": SAUNA, "family": "saunum", "target_c": 80, "heater_kw": 9.0, "control": True,
                      "sessions": [{"weekdays": [SESSION.weekday()], "time": "19:00"}]}}
    # Heats 2 °C a minute, faster than the 45-minute seed: the need shrinks as it warms.
    await _session(hass, heat, "sauna", SAUNA, 22.0, 1.0, 0.25, SESSION - 60 * MIN, SESSION + 30 * MIN)
    # One start, no off at 19:00 nor before: the heater's own thermostat holds the target to the session's end.
    assert devices.writes() == [("climate.set_hvac_mode", SAUNA, "heat")]
    assert devices.state(SAUNA) == "heat"


async def test_b1_the_balboa_spa_stays_ready_through_its_heat_up_and_bath(hass: HomeAssistant) -> None:
    devices = Devices(hass)
    devices.add(SPA, "off", preset_mode="rest", temperature=38.0, current_temperature=36.0, min_temp=26.5, max_temp=40.0)
    heat = {"spa": {"entity_id": SPA, "family": "balboa", "bath_c": 38.0, "idle_c": 30.0, "floor_c": 10.0, "heater_kw": 3.0,
                    "control": True, "baths": [{"weekdays": [SESSION.weekday()], "time": "19:00"}]}}
    await _session(hass, heat, "spa", SPA, 36.0, 0.02, 0.003, SESSION - 180 * MIN, SESSION + 30 * MIN)
    presets = [call for call in devices.writes() if call[0] == "climate.set_preset_mode"]
    assert presets == [("climate.set_preset_mode", SPA, "ready")]
    assert devices.attr(SPA, "preset_mode") == "ready"


async def test_b1_an_exempt_off_never_spends_the_day_s_budget(hass: HomeAssistant) -> None:
    devices = Devices(hass)
    devices.add(SAUNA, "off", temperature=80, current_temperature=22, min_temp=40, max_temp=110, hvac_modes=["off", "heat"])
    control, shadow, _order = make_control(hass, {"sauna": {"entity_id": SAUNA, "family": "saunum", "target_c": 80, "control": True}})
    moment = NOW
    for _cycle in range(3):
        shadow.sauna.set("heat", current=22.0, session=moment + 60 * MIN)
        moment = await looks(control, moment, moment + MIN)
        shadow.sauna.set("off", current=60.0)
        moment = await looks(control, moment + 30 * S, moment + 2 * MIN)
    day = control.sauna.part.day(moment)
    assert control.sauna.part.budget.written(day, "heat", "switch") == 3


async def test_b2_a_now_press_never_runs_past_the_device_s_longest_run(hass: HomeAssistant) -> None:
    devices = Devices(hass)
    devices.add(SAUNA, "off", temperature=80, current_temperature=22, min_temp=40, max_temp=110, hvac_modes=["off", "heat"])
    heat = {"sauna": {"entity_id": SAUNA, "family": "harvia_sauna", "target_c": 80, "heater_kw": 9.0, "control": True}}
    control, shadow, _order = make_control(hass, heat)
    real = new_sauna()
    shadow.sauna = real
    config = read_config(heat).sauna
    assert config is not None and real.press_now(NOW, 80.0, 22.0) is None
    on_s, moment, temp = 0, NOW, 22.0
    restarted = False
    while moment <= NOW + timedelta(hours=3):
        state = devices.state(SAUNA)
        temp = min(80.0, temp + 0.7) if state == "heat" else max(22.0, temp - 0.2)
        on_s += 30 if state == "heat" else 0
        real.look(moment, TZ, config, Reading(temp, state == "heat", None, timedelta(minutes=60)), [])
        await control.async_look(moment)
        if not restarted and moment >= NOW + 30 * MIN:
            # A restart halfway: the run's on time is kept.
            await control.async_save()
            again = PoolSpaControl(hass, control.site)
            raw = await again._store.async_load()
            again.sauna.restore(raw["sauna"])  # type: ignore[index]
            again._started = True
            control, restarted = again, True
        moment += 30 * S
    # Its hour, give or take the look the off takes to show and the one at the restart.
    assert on_s <= 62 * 60
    # The restart writes the person's off first (as after a crash), then the run goes on; its hour is the whole run's.
    assert [call[2] for call in devices.writes()] == ["heat", "off", "heat", "off"]
    assert control.sauna.part.reason == "max_on"


async def test_b3_an_unreadable_lock_blocks_the_start_and_stops_a_running_sauna(hass: HomeAssistant) -> None:
    devices = Devices(hass)
    devices.add(SAUNA, "off", temperature=80, current_temperature=22, min_temp=40, max_temp=110, hvac_modes=["off", "heat"])
    lock = "binary_sensor.bastu_dorr"
    hass.states.async_set(lock, "unavailable")
    heat = {"sauna": {"entity_id": SAUNA, "family": "saunum", "target_c": 80, "control": True, "lock_id": lock}}
    config = read_config(heat).sauna
    assert config is not None
    reader = type("Reader", (), {"hass": hass, "_celsius": lambda self, v, u: v, "_harvia_on_time": lambda self, e: None})()
    reading = PoolSpaShadow._read_deadline(reader, config)  # type: ignore[arg-type]
    assert reading.locked is True and reading.lock_unreadable is True
    hass.states.async_remove(lock)
    assert PoolSpaShadow._read_deadline(reader, config).locked is True  # type: ignore[arg-type]
    control, shadow, _order = make_control(hass, heat)
    shadow.sauna.set("heat", current=22.0, session=NOW + 60 * MIN, locked=True)
    shadow.sauna.reading = Reading(22.0, False, True, None, True)
    await looks(control, NOW, NOW + MIN)
    assert devices.calls == [] and control.sauna.part.reason == "lock_unreadable"
    # Readable and closed: it starts; unreadable again: SpotNav's start is turned off.
    shadow.sauna.reading = Reading(22.0, False, False, None, False)
    await looks(control, NOW + 2 * MIN, NOW + 3 * MIN)
    shadow.sauna.reading = Reading(40.0, True, True, None, True)
    await control.async_look(NOW + 4 * MIN)
    assert [call[2] for call in devices.writes()] == ["heat", "off"]


async def test_b4_an_unreadable_heat_pump_last_seen_heating_keeps_its_circulation(hass: HomeAssistant) -> None:
    devices = _pool_devices(hass)
    control, shadow, _order = make_control(hass, _pool())
    shadow.pool_action("heat")
    moment = await looks(control, NOW, NOW + 45 * MIN)
    assert devices.state(PUMP) == "heat" and devices.state(CIRC) == "on"
    hass.states.async_set(PUMP, "unavailable")
    shadow.pool_action("off")
    await looks(control, moment + 30 * S, moment + 30 * MIN)
    assert devices.state(CIRC) == "on" and devices.writes(CIRC) == [("switch.turn_on", CIRC, "on")]
    # Read off again: the run-on, then off.
    devices.add(PUMP, "off", temperature=27.0, current_temperature=24.0, min_temp=12, max_temp=40, hvac_modes=["off", "heat", "cool"])
    await looks(control, moment + 31 * MIN, moment + 38 * MIN)
    assert devices.state(CIRC) == "off"


async def test_s1_a_setpoint_set_at_the_panel_stays_the_person_s(hass: HomeAssistant) -> None:
    devices = _pool_devices(hass, circ="on")
    control, shadow, _order = make_control(hass, _pool(target_c=28.0))
    shadow.pool_action("filter")
    moment = await looks(control, NOW, NOW + 3 * MIN)
    assert devices.attr(PUMP, "temperature") == 28.0
    devices.set(PUMP, temperature=30.0)
    await looks(control, moment + 30 * S, moment + 5 * MIN)
    count = len(devices.calls)
    later = NOW + timedelta(hours=20)
    await looks(control, later, later + 5 * MIN)
    assert devices.attr(PUMP, "temperature") == 30.0 and devices.writes()[count:] == []
    # A change of the dial is written again.
    shadow.raw = _pool(target_c=29.0)
    await looks(control, later + 6 * MIN, later + 7 * MIN)
    assert devices.attr(PUMP, "temperature") == 29.0


async def test_s2_the_circulation_alone_follows_the_plan_under_a_pump_that_is_not_spotnav_s(hass: HomeAssistant) -> None:
    devices = _pool_devices(hass, mode="heat", circ="on")
    control, shadow, _order = make_control(hass, _pool(control_heat_pump=False))
    shadow.pool_action("off")
    await looks(control, NOW, NOW + 40 * MIN, MIN)
    assert devices.state(CIRC) == "off" and control.pool.circ.reason == "pump_not_ours"
    # A pump that says it heats keeps it on; its stop seen gives the run-on.
    devices2 = _pool_devices(hass, mode="heat", circ="on", hvac_action="heating")
    control2, shadow2, _order2 = make_control(hass, _pool(control_heat_pump=False))
    shadow2.pool_action("off")
    await looks(control2, NOW, NOW + 40 * MIN, MIN)
    assert devices2.state(CIRC) == "on"
    devices2.set(PUMP, hvac_action="idle")
    await looks(control2, NOW + 41 * MIN, NOW + 45 * MIN, MIN)
    assert devices2.state(CIRC) == "on"
    await looks(control2, NOW + 47 * MIN, NOW + 48 * MIN, MIN)
    assert devices2.state(CIRC) == "off"


async def test_s5_a_stale_or_missing_plan_keeps_what_is_there(hass: HomeAssistant) -> None:
    devices = _pool_devices(hass)
    control, shadow, _order = make_control(hass, _pool(heat_pump=None, control_heat_pump=False))
    shadow.pool_action("filter")
    shadow.would_at = NOW  # type: ignore[attr-defined]
    await looks(control, NOW, NOW + MIN)
    assert devices.state(CIRC) == "on"
    # The look that decides stops (a failure); after a few minutes the control holds what is there, never off.
    shadow.pool_action("off")
    shadow.would_at = NOW  # type: ignore[attr-defined]
    await looks(control, NOW + 40 * MIN, NOW + 50 * MIN, MIN)
    assert devices.state(CIRC) == "on"
    shadow.would = None
    await looks(control, NOW + 51 * MIN, NOW + 60 * MIN, MIN)
    assert devices.state(CIRC) == "on"
