"""The spa's and the sauna's session latch with the real shadow and the real control together (the re-review of
2026-11-24, `plans/review_pool_spa_night_2026-11-24.md`): a session moved by "Later" stays the original's booking
(R1) and is not lost when the original time passes (R2); the latched heat-up keeps its planned quarters, their
prices and the room under the ceiling (R3); the sauna's on time counts its continuous run; a stale plan is not
followed; a spa look that fails leaves the sauna's plan fresh. Every write goes to a fake service."""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant

from custom_components.spotnav.heat.config import read_config
from custom_components.spotnav.spa.plan import HeatHour
from custom_components.spotnav.spa.runtime import Reading, new_sauna, new_spa

from .pool_spa_fakes import MIN, S, TZ, Devices, make_control

ZONE = ZoneInfo(TZ)
DAY = datetime(2026, 7, 1, tzinfo=ZONE)
WD = DAY.isoweekday()  # spa and sauna weekdays are numbered 1-7 like departures
SAUNA = "climate.bastu"
SPA = "climate.spa"


def at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


class Sim:
    """The real `DeadlineShadow` and the real control, a simple thermal model, a restart as a round trip of both
    records."""

    def __init__(self, hass: HomeAssistant, heat: dict[str, Any], kind: str, entity: str, temp: float, up: float, down: float, hours: list[HeatHour] | None = None) -> None:
        self.hass, self.heat, self.kind, self.entity = hass, heat, kind, entity
        self.temp, self.up, self.down = temp, up, down
        self.hours = hours or []
        self.control, self.shadow, self.order = make_control(hass, heat)
        self.real = new_sauna() if kind == "sauna" else new_spa()
        setattr(self.shadow, kind, self.real)
        self.log: list[tuple[datetime, str]] = []

    def heating(self) -> bool:
        state = self.hass.states.get(self.entity)
        assert state is not None
        # The heater draws while on and under the target (its own thermostat stops it there).
        top = 80.0 if self.kind == "sauna" else 38.0
        on = state.attributes.get("preset_mode") == "ready" if self.kind == "spa" else state.state == "heat"
        return on and self.temp < top

    def restart(self) -> None:
        kept_shadow = copy.deepcopy(self.real.as_dict())
        kept_control = copy.deepcopy(self.control._data())
        self.control, self.shadow, self.order = make_control(self.hass, self.heat)
        self.real = new_sauna() if self.kind == "sauna" else new_spa()
        self.real.restore(kept_shadow)
        setattr(self.shadow, self.kind, self.real)
        getattr(self.control, self.kind).restore(kept_control[self.kind])

    async def run(self, begin: datetime, end: datetime, events: dict[datetime, Callable[[Sim], None]] | None = None, *, look: bool = True) -> None:
        events = dict(events or {})
        moment = begin
        while moment <= end:
            for when in sorted(k for k in events if k <= moment):
                events.pop(when)(self)
            heating = self.heating()
            top = 80.0 if self.kind == "sauna" else 38.0
            self.temp = min(self.temp + self.up, top) if heating else max(20.0, self.temp - self.down)
            config = getattr(read_config(self.heat), self.kind)
            self.shadow.raw = self.heat
            if look:
                self.real.look(moment, TZ, config, Reading(self.temp, heating, None, None), self.hours)
            await self.control.async_look(moment)
            state = self.hass.states.get(self.entity)
            on = state is not None and (state.attributes.get("preset_mode") == "ready" if self.kind == "spa" else state.state == "heat")
            self.log.append((moment, "on" if on else "off"))
            moment += 30 * S

    def on_minutes(self, start: datetime, end: datetime) -> float:
        return sum(0.5 for moment, state in self.log if state == "on" and start <= moment < end)


def _sauna_devices(hass: HomeAssistant) -> Devices:
    devices = Devices(hass)
    devices.add(SAUNA, "off", temperature=80, current_temperature=22, min_temp=40, max_temp=110, hvac_modes=["off", "heat"])
    return devices


def _sauna(sessions: list[str], **extra: Any) -> dict[str, Any]:
    return {"sauna": {"entity_id": SAUNA, "family": "saunum", "target_c": 80, "heater_kw": 9.0, "control": True,
                      "sessions": [{"weekdays": [WD], "time": time} for time in sessions], **extra}}


def _spa_devices(hass: HomeAssistant) -> Devices:
    devices = Devices(hass)
    devices.add(SPA, "off", preset_mode="rest", temperature=38.0, current_temperature=32.0, min_temp=26.5, max_temp=40.0)
    return devices


def _spa(baths: list[str]) -> dict[str, Any]:
    return {"spa": {"entity_id": SPA, "family": "balboa", "bath_c": 38.0, "idle_c": 30.0, "floor_c": 10.0, "heater_kw": 3.0,
                    "control": True, "baths": [{"weekdays": [WD], "time": time} for time in baths]}}


def _hours(price: Callable[[int], float], room: Callable[[int], float | None] = lambda _h: None) -> list[HeatHour]:
    return [
        HeatHour(at(0) + timedelta(hours=i), price(i % 24), price(i % 24) / 2, None, room(i % 24), None if room(i % 24) is None else "fuse")
        for i in range(48)
    ]


def _seed_rate(sim: Sim, rate_c_h: float) -> None:
    """Ten learnt heat-up runs at `rate_c_h` (each counted from its first step), so the model's rate is known."""
    for index in range(10):
        start = at(0) - timedelta(days=index + 1)
        sim.real.runs.append((start, start + timedelta(hours=2), 30.0, 30.0 + 2 * rate_c_h))


async def test_r1_a_moved_session_whose_booking_is_removed_is_not_heated_again_after_a_restart(hass: HomeAssistant) -> None:
    devices = _sauna_devices(hass)
    sim = Sim(hass, _sauna(["19:30"]), "sauna", SAUNA, 22.0, 1.0, 0.25)
    sim.real.later[at(19, 30)] = at(20)

    def cancel(s: Sim) -> None:
        s.heat = _sauna([])

    await sim.run(at(18, 30), at(19, 40), {at(19, 25): cancel})
    await sim.control.async_shutdown()
    sim.restart()
    await sim.run(at(19, 45), at(22, 30))
    assert [call[2] for call in devices.writes()] == ["heat", "off"]
    assert sim.on_minutes(at(19, 30), at(22, 30)) == 0.0 and sim.real.later == {}


async def test_r2_a_session_moved_later_is_heated_for_its_new_time_through_the_original_time(hass: HomeAssistant) -> None:
    devices = _sauna_devices(hass)
    sim = Sim(hass, _sauna(["19:00"]), "sauna", SAUNA, 22.0, 1.0, 0.25)
    sim.real.later[at(19)] = at(21)
    await sim.run(at(17, 30), at(23, 30))
    assert [call[2] for call in devices.writes()] == ["heat", "off"]
    assert sim.on_minutes(at(17, 30), at(19, 45)) == 0.0
    assert sim.on_minutes(at(20, 0), at(23, 0)) > 100


async def test_r2_the_spa_s_later_bath_answered_on_the_notice_is_heated(hass: HomeAssistant) -> None:
    devices = _spa_devices(hass)
    hours = _hours(lambda _h: 1.0, lambda h: 0.0 if 12 <= h < 20 else 10.0)
    sim = Sim(hass, _spa(["19:00"]), "spa", SPA, 35.0, 0.0125, 0.0025, hours)
    moved: list[Any] = []

    def later(s: Sim) -> None:
        moved.append(s.real.move_later())

    await sim.run(at(12, 30), at(23, 59), {at(13, 0): later})
    assert moved[0][1] is None and moved[0][0] == at(23)
    assert devices.writes() and devices.writes()[0][2] == "ready"
    assert sim.real.plan is not None and sim.real.plan.next_session == (at(23), "weekly")


async def test_r3_the_latched_heat_up_keeps_its_cheap_quarters(hass: HomeAssistant) -> None:
    devices = _spa_devices(hass)

    def price(hour: int) -> float:
        return 0.2 if hour == 10 else 3.0 if 11 <= hour < 15 else 0.5 if 15 <= hour < 19 else 1.0

    sim = Sim(hass, _spa(["19:00"]), "spa", SPA, 32.0, 0.0125, 0.0025, _hours(price))
    _seed_rate(sim, 1.5)
    await sim.run(at(9, 0), at(21, 30))
    # Heated in the cheap hour and the cheaper evening, never in the expensive afternoon; ready for the bath.
    assert sim.on_minutes(at(11, 0), at(15, 0)) == 0.0
    assert sim.on_minutes(at(10, 0), at(11, 0)) > 0 and sim.on_minutes(at(15, 0), at(19, 0)) > 0
    assert sim.temp >= 37.5
    presets = [call[2] for call in devices.writes() if call[0] == "climate.set_preset_mode"]
    assert len(presets) <= 6


async def test_r3_the_latched_heat_up_stays_out_of_hours_without_room(hass: HomeAssistant) -> None:
    _spa_devices(hass)
    sim = Sim(hass, _spa(["19:00"]), "spa", SPA, 32.0, 0.0125, 0.0025, _hours(lambda h: 0.2 if h == 10 else 1.0, lambda h: 0.0 if 11 <= h < 15 else 10.0))
    _seed_rate(sim, 1.5)
    await sim.run(at(9, 0), at(21, 30))
    assert sim.on_minutes(at(11, 0), at(15, 0)) == 0.0 and sim.temp >= 37.5


async def test_the_sauna_s_continuous_run_counts_against_its_longest_run(hass: HomeAssistant) -> None:
    _sauna_devices(hass)
    sim = Sim(hass, _sauna(["19:00"]), "sauna", SAUNA, 22.0, 1.0, 0.25)

    def press(s: Sim) -> None:
        s.real.press_now(at(20, 55), 80.0, s.temp)

    await sim.run(at(17, 30), at(23, 59), {at(20, 55): press})
    # The booked session and the press on top of it are one run: at most SpotNav's three hours.
    assert sim.on_minutes(at(17, 30), at(23, 59)) <= 181


async def test_a_stale_plan_turns_the_sauna_off_and_leaves_the_spa(hass: HomeAssistant) -> None:
    devices = _sauna_devices(hass)
    sim = Sim(hass, _sauna(["19:00"]), "sauna", SAUNA, 22.0, 1.0, 0.25)
    await sim.run(at(18, 0), at(18, 20))
    assert devices.state(SAUNA) == "heat"
    # The shadow's look stops: the sauna is off within a few minutes.
    await sim.run(at(18, 20, ) + 30 * S, at(18, 30), look=False)
    assert devices.state(SAUNA) == "off" and sim.control.sauna.part.reason == "stale_plan"
    spa_devices = _spa_devices(hass)
    spa = Sim(hass, _spa(["19:00"]), "spa", SPA, 36.0, 0.0125, 0.0025, _hours(lambda _h: 1.0))
    await spa.run(at(17, 0), at(17, 30))
    count = len(spa_devices.calls)
    await spa.run(at(17, 30) + 30 * S, at(18, 0), look=False)
    assert len(spa_devices.calls) == count and spa.control.spa.part.reason == "stale_plan"
    _ = MIN


# ---- the final check (W1, the learner, the forecast's decimals)


async def test_w1_idle_cycles_never_heat_past_the_idle_temperature_when_the_day_s_writes_run_out(hass: HomeAssistant) -> None:
    devices = _spa_devices(hass)
    sim = Sim(hass, _spa([]), "spa", SPA, 30.5, 0.0125, 0.3 / 120, _hours(lambda _h: 1.0))
    temps: list[float] = []
    moment = at(0)
    while moment < at(23, 59) + timedelta(days=1):
        await sim.run(moment, moment + 29 * MIN)
        temps.append(sim.temp)
        moment += 30 * MIN
    # Each idle cycle is Ready then Rest; one whose Rest would not fit the day's six writes is not started.
    assert max(temps) <= 30.0 + 0.5
    presets = [call[2] for call in devices.writes() if call[0] == "climate.set_preset_mode"]
    assert presets and presets[-1] == "rest" and presets.count("ready") == presets.count("rest")


async def test_the_learner_counts_a_block_from_its_first_step(hass: HomeAssistant) -> None:
    _spa_devices(hass)

    def price(hour: int) -> float:
        return 0.2 if hour == 10 else 3.0 if 11 <= hour < 15 else 0.5 if 15 <= hour < 19 else 1.0

    # The first bath ever: no history. The one-hour block at 10 teaches the true 1.5 °C/h, so the evening's quarters
    # still suffice and nothing is heated in the expensive afternoon.
    sim = Sim(hass, _spa(["19:00"]), "spa", SPA, 32.0, 0.0125, 0.0025, _hours(price))
    await sim.run(at(9, 0), at(21, 30))
    assert sim.on_minutes(at(11, 0), at(15, 0)) == 0.0 and sim.temp >= 37.5
    from custom_components.spotnav.spa.plan import learnt_rate  # noqa: PLC0415

    rate, learnt = learnt_rate(sim.real.runs, 1.5)
    assert learnt and abs(rate - 1.5) < 0.1


async def test_a_forecast_s_last_decimal_never_places_the_quarters_again(hass: HomeAssistant) -> None:
    devices = _spa_devices(hass)
    flat = _hours(lambda _h: 1.0, lambda _h: 10.0)
    # Heats faster than learnt (2.0 against 1.5 °C/h): placed again on every jitter, this opened gaps.
    sim = Sim(hass, _spa(["19:00"]), "spa", SPA, 32.0, 2.0 / 120, 0.0025, flat)
    _seed_rate(sim, 1.5)
    moment, step = at(9, 0), 0
    while moment <= at(21, 0):
        sim.hours = _hours(lambda _h: 1.0, lambda _h, j=step: 10.0 + 0.001 * (j % 3))
        await sim.run(moment, moment + 9 * MIN + 30 * S)
        moment += 10 * MIN
        step += 1
    presets = [call[2] for call in devices.writes() if call[0] == "climate.set_preset_mode"]
    assert presets == ["ready", "rest"] and sim.temp >= 37.5


async def test_a_bath_day_keeps_the_writes_its_heat_up_needs(hass: HomeAssistant) -> None:
    _spa_devices(hass)

    def price(hour: int) -> float:
        return 0.2 if hour == 10 else 3.0 if 11 <= hour < 15 else 0.5 if 15 <= hour < 19 else 1.0

    # Idle cycles through the night and morning, then the bath's two blocks: the bath is ready.
    sim = Sim(hass, _spa(["19:00"]), "spa", SPA, 30.5, 0.0125, 0.3 / 120, _hours(price))
    _seed_rate(sim, 1.5)
    await sim.run(at(0, 0), at(19, 0))
    assert sim.temp >= 37.5


async def test_a_latched_session_never_takes_the_next_session_s_quarters(hass: HomeAssistant) -> None:
    devices = _sauna_devices(hass)
    sim = Sim(hass, _sauna(["16:00", "18:10", "20:20", "22:30"]), "sauna", SAUNA, 22.0, 1.0, 0.25)
    await sim.run(at(14, 0), at(23, 59))
    # Held through each session it serves, never switched on and off by the next session's plan.
    writes = [call[2] for call in devices.writes()]
    assert len(writes) <= 6 and writes == ["heat", "off"] * (len(writes) // 2)
