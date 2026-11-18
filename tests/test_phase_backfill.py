"""The house's per-phase peaks read back from the recorder's history (`site/phase_backfill.py`): the replay."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.site.phase_backfill import (
    UNKNOWN_AGE_S,
    Replay,
    SiteAt,
    StatesThen,
    entities_read,
    sample_times,
)
from custom_components.spotnav.site.phase_peaks import DAY_MINUTES, HourMark, PhasePeaks

from .world import measured_site_world

#: A clock hour's start.
START = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
T0 = START.timestamp()
BATTERY = "sensor.home_battery_power"
PLANT = tuple(f"sensor.home_plant_phase_{name}" for name in "abc")
VOLTS = 230.0


class Played:
    """A site whose sensors are moved by hand, each change kept as the recorder would keep it."""

    def __init__(self, hass: HomeAssistant, freezer: Any, controller: Any) -> None:
        self.hass = hass
        self.freezer = freezer
        self.controller = controller
        self.history: dict[str, list[Any]] = defaultdict(list)

    def set(self, entity_id: str, value: float | str, unit: str) -> None:
        self.hass.states.async_set(entity_id, str(value), {"unit_of_measurement": unit})
        state = self.hass.states.get(entity_id)
        if not self.history[entity_id] or self.history[entity_id][-1] is not state:
            self.history[entity_id].append(state)

    def site(self, watts: tuple[float, float, float], tick: int) -> None:
        """The meter's three phases, its voltage and reactive power moving a little so every reading is new."""
        for phase, value in zip(("l1", "l2", "l3"), watts, strict=True):
            self.set(f"sensor.home_site_power_{phase}", value, "W")
            self.set(f"sensor.home_site_reactive_{phase}", tick % 2, "var")
            self.set(f"sensor.home_site_voltage_{phase}", VOLTS + 0.1 * (tick % 2), "V")

    def charger(self, amps: float) -> None:
        for phase in ("l1", "l2", "l3"):
            self.set(f"sensor.home_charger_{phase}", amps, "A")

    async def sample_live(self, when: float) -> None:
        self.controller._recompute(schedule_apply=False)
        self.controller.phase_peaks.sample(self.controller, when)

    def replayed(self, start: float, end: float, *, fresh_start: bool = False) -> PhasePeaks:
        replay = Replay(self.controller)
        replay.states.load(self.history, start, fresh_start=fresh_start)
        replay.run(float(then) for then in sample_times(start, end))
        return replay.peaks


async def _world(
    hass: HomeAssistant, freezer: Any, *, plant: bool = False, own_backfill: bool = False, **site_kwargs: Any
) -> Played:
    """A site with a charger, a battery and (with `plant`) the plant's per-phase power. Its own reading back of the
    recorder is stopped unless `own_backfill`: the tests run their own, and the clock they move would start it."""
    freezer.move_to(START - timedelta(seconds=30))
    hass.states.async_set(BATTERY, "0", {"unit_of_measurement": "W"})
    _charger, _charger_controller, _site, controller, _derived = await measured_site_world(
        hass, "home", main_fuse_a=25.0, delivered_a=0, battery_aggregate_power_entity=BATTERY, **site_kwargs
    )
    await controller.battery_control.async_shutdown()
    if not own_backfill:
        await controller.phase_peaks.backfill.async_stop()
    if plant:
        controller.battery_shadow.facts = SimpleNamespace(plant_phase_entities=PLANT, plant_phase_sign=1.0)
    controller.phase_peaks.peaks = PhasePeaks()
    return Played(hass, freezer, controller)


def _marks(peaks: PhasePeaks) -> dict[int, dict[str, Any]]:
    return {hour: mark.as_dict() for hour, mark in sorted(peaks.hours.items())}


def _same(first: Any, second: Any) -> None:
    if isinstance(first, dict):
        assert isinstance(second, dict) and set(first) == set(second)
        for key in first:
            _same(first[key], second[key])
    elif isinstance(first, list):
        assert isinstance(second, list) and len(first) == len(second)
        for a, b in zip(first, second, strict=True):
            _same(a, b)
    elif isinstance(first, float):
        assert second == pytest.approx(first, abs=0.011)
    else:
        assert first == second


# --- the same reading ---------------------------------------------------------------------------------------------------


async def test_the_history_replayed_gives_the_marks_the_live_samples_of_the_same_readings_gave(
    hass: HomeAssistant, freezer: Any
) -> None:
    played = await _world(hass, freezer, plant=True)
    controller = played.controller
    minutes = 50
    for tick in range(minutes * 6):
        minute = tick // 6
        freezer.move_to(START + timedelta(seconds=tick * 10))
        house = [1000.0 + 10 * (tick % 7), 500.0 + 5 * (tick % 5), 2000.0 + 20 * (tick % 3)]
        if 5 <= minute < 7:
            house[2] = 3500.0 + tick % 3  # the house's peak on L3
        charging = 12 <= minute < 22
        car_w = 10.0 * VOLTS if charging else 0.0
        # The battery: in the band, far outside it for five minutes, silent for five, back.
        battery_w = -3000.0 - tick % 4 if 25 <= minute < 30 else -100.0 - tick % 4
        if not 35 <= minute < 40:
            played.set(BATTERY, battery_w, "W")
        # The plant's per-phase power, from minute 30 on (its sensors enabled then): the battery's discharge, uneven.
        plant = (0.0, 0.0, 0.0)
        if minute >= 30:
            plant = (60.0 + tick % 3, 20.0 + tick % 2, 20.0 + tick % 5)
            for entity_id, watts in zip(PLANT, plant, strict=True):
                played.set(entity_id, watts / 1000.0, "kW")
        played.site(tuple(house[index] + car_w - plant[index] for index in range(3)), tick)  # type: ignore[arg-type]
        if charging:
            played.charger(10.0 + 0.1 * (tick % 2))
        elif minute == 22 and tick % 6 == 0:
            played.charger(0.0)
        await played.sample_live(T0 + tick * 10)

    live = controller.phase_peaks.peaks
    replayed = played.replayed(T0, T0 + minutes * 60)
    assert _marks(live), "the live samples counted"
    _same(_marks(replayed), _marks(live))
    mark = live.hours[int(T0 // 3600)]
    # What the scenario is there to show, so the comparison is not of two empty stores: minutes by the band and
    # exact ones, the house's peak found with the car taken off, and minutes lost around the car's start and stop,
    # to the battery outside its band and to the battery silent.
    assert mark.minutes > 10 and mark.exact_minutes > 10
    assert mark.counted <= minutes - 8
    assert mark.house_a is not None and mark.house_a[2] == pytest.approx(3500.0 / VOLTS + 104.0 / VOLTS, abs=0.2)
    assert mark.house_a[0] < 6.0, "the car's 10 A never counted as house"


async def test_the_entities_read_are_the_ones_the_site_s_reading_touches(hass: HomeAssistant, freezer: Any) -> None:
    played = await _world(hass, freezer, plant=True)
    for entity_id in PLANT:
        played.set(entity_id, 0.1, "kW")
    read = entities_read(played.controller, T0)
    assert {BATTERY, *PLANT} <= set(read)
    assert {f"sensor.home_site_{kind}_{phase}" for kind in ("power", "reactive", "voltage") for phase in ("l1", "l2", "l3")} <= set(read)
    assert {f"sensor.home_charger_{phase}" for phase in ("l1", "l2", "l3")} <= set(read)
    # Reading them left the site as it was.
    assert played.controller.hass is hass


# --- what the history cannot say --------------------------------------------------------------------------------------


async def _steady(played: Played, minutes: int, *, house=(1000.0, 500.0, 2000.0), car_a: float = 0.0,
                  move=lambda tick: True, charger=lambda tick: None, battery=lambda tick: -100.0 - tick % 4) -> None:
    """`minutes` minutes of a steady house; `move(tick)`: whether the meter's readings change at this tick."""
    for tick in range(minutes * 6):
        played.freezer.move_to(START + timedelta(seconds=tick * 10))
        if move(tick):
            played.site(tuple(value + car_a * VOLTS + tick % 3 for value in house), tick)  # type: ignore[arg-type]
        amps = charger(tick)
        if amps is not None:
            played.charger(amps)
        watts = battery(tick)
        if watts is not None:
            played.set(BATTERY, watts, "W")


async def test_a_charger_s_current_standing_still_while_it_draws_counts_nothing_from_the_history(
    hass: HomeAssistant, freezer: Any
) -> None:
    played = await _world(hass, freezer)
    # A car draws an even 10 A for half an hour, its charger's reading the same throughout: one row in the history.
    await _steady(played, 30, car_a=10.0, charger=lambda tick: 10.0)
    replayed = played.replayed(T0, T0 + 30 * 60)
    mark = replayed.hours[int(T0 // 3600)]
    # The first two minutes the reading is new and the car is taken off; after that it cannot be told whether the
    # charger fell silent or the car draws evenly: neither the car as house (a false 14 A) nor a house without it.
    assert mark.counted <= 2
    assert mark.house_a is None or max(mark.house_a) < 2100.0 / VOLTS + 0.5


async def test_a_charger_standing_still_at_nothing_is_no_car(hass: HomeAssistant, freezer: Any) -> None:
    played = await _world(hass, freezer)
    await _steady(played, 30, charger=lambda tick: 0.0)
    mark = played.replayed(T0, T0 + 30 * 60).hours[int(T0 // 3600)]
    assert mark.minutes >= 25 and mark.house_a == pytest.approx((1102 / VOLTS, 602 / VOLTS, 2102 / VOLTS), abs=0.5)


async def test_a_meter_that_stood_still_counts_nothing_and_never_lowers_a_peak(hass: HomeAssistant, freezer: Any) -> None:
    played = await _world(hass, freezer)
    # Ten live minutes, then the meter's readings stand still for twenty (a lost link, or a house that drew the
    # same to the watt: the history cannot say), then ten more.
    await _steady(played, 40, move=lambda tick: not 60 <= tick < 180)
    replayed = played.replayed(T0, T0 + 40 * 60)
    mark = replayed.hours[int(T0 // 3600)]
    assert 10 <= mark.minutes <= 22, "the still minutes beyond the maximum age are not counted"
    # And a meter with no history at all counts nothing.
    for phase in ("l1", "l2", "l3"):
        del played.history[f"sensor.home_site_power_{phase}"]
    empty = played.replayed(T0, T0 + 40 * 60)
    assert all(mark.counted == 0 for mark in empty.hours.values())


async def test_a_battery_that_stood_still_is_not_taken_as_idle(hass: HomeAssistant, freezer: Any) -> None:
    played = await _world(hass, freezer)
    # The battery's power reads the same 0 W all along: full and idle, or its integration silent while it covers
    # the house. Only the first two minutes (the reading is new) count by the band.
    await _steady(played, 30, battery=lambda tick: 0.0)
    mark = played.replayed(T0, T0 + 30 * 60).hours[int(T0 // 3600)]
    assert mark.counted <= 2


async def test_a_state_held_from_before_the_first_hour_read_is_old(hass: HomeAssistant, freezer: Any) -> None:
    played = await _world(hass, freezer)
    await _steady(played, 20)
    # Read from the tenth minute with the recorder's state in force at the start: each sensor's first row is from
    # before it. They are taken as old until they change (five seconds later here), not as new at the start.
    middle = T0 + 605
    states = StatesThen()
    states.load(played.history, middle, fresh_start=True)
    states.at(middle, START + timedelta(hours=1))
    held = states.get("sensor.home_site_power_l1")
    assert held is not None and (START + timedelta(hours=1) - held.last_updated).total_seconds() >= UNKNOWN_AGE_S
    states.at(middle + 5, START + timedelta(hours=1))
    assert (START + timedelta(hours=1) - states.get("sensor.home_site_power_l1").last_updated).total_seconds() == 0.0
    # Carried over from the hours read before, a state keeps its own age.
    carried = StatesThen()
    carried.load({key: rows[:30] for key, rows in played.history.items()}, T0, fresh_start=True)
    carried.load({key: rows[30:] for key, rows in played.history.items()}, middle, fresh_start=False)
    whole = StatesThen()
    whole.load(played.history, T0, fresh_start=False)
    for then in (middle, middle + 10, middle + 300):
        carried.at(then, START)
        whole.at(then, START)
        for entity_id in played.history:
            first, second = carried.get(entity_id), whole.get(entity_id)
            assert (first.state, first.last_updated) == (second.state, second.last_updated)


def test_states_then_knows_nothing_before_an_entity_s_first_row() -> None:
    states = StatesThen()
    assert states.empty
    row = SimpleNamespace(state="5", attributes={}, last_updated=datetime.fromtimestamp(T0 + 100, UTC))
    assert states.load({"sensor.a": [row], "sensor.b": []}, T0, fresh_start=True) == 1
    assert not states.empty
    states.at(T0 + 50, START)
    assert states.get("sensor.a") is None and states.get("sensor.b") is None and states.get("sensor.c") is None
    states.at(T0 + 130, START)
    held = states.get("sensor.a")
    assert held.state == "5" and held.last_reported == held.last_updated == START - timedelta(seconds=30)


def test_the_samples_fall_on_the_clock_s_own_ten_seconds() -> None:
    assert list(sample_times(T0, T0 + 30)) == [T0, T0 + 10, T0 + 20]
    assert list(sample_times(T0 + 3, T0 + 31)) == [T0 + 10, T0 + 20, T0 + 30]


# --- the site as it read then -------------------------------------------------------------------------------------------


class _Site:
    """A site with one method that reads a state, one that sets something, and a property."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self.config = {"x": 1}
        self.noted: list[str] = []

    def read(self, entity_id: str) -> Any:
        return self.hass.states.get(entity_id)

    def note(self) -> None:
        self.seen = True

    @property
    def twice(self) -> Any:
        return (self.read("a"), self.read("a"))

    def _change_only_alive(self, entity_id: str) -> bool:
        return True


def test_the_site_s_own_methods_run_on_the_other_states_and_leave_the_site_alone() -> None:
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: "now"), data={"k": "v"})
    site = _Site(hass)
    then = SiteAt(site, SimpleNamespace(get=lambda entity_id: "then"))
    assert then.read("a") == "then" and then.twice == ("then", "then") and site.read("a") == "now"
    assert then.config is site.config and then.hass.data is hass.data
    then.note()
    assert not hasattr(site, "seen")
    assert then.from_history is True and then._change_only_alive("a") is False
    with pytest.raises(AttributeError):
        then.nothing_of_the_kind  # noqa: B018


# --- the marks merged ---------------------------------------------------------------------------------------------------


def _mark(peak: float, minutes: int, **kwargs: Any) -> HourMark:
    return HourMark(house_a=(peak, peak, peak), minutes=minutes, total_all=3 * peak, total_counted=3 * peak, **kwargs)


def test_live_hours_win_and_read_back_hours_are_marked() -> None:
    now = T0 + 5 * 3600
    hour = int(T0 // 3600)
    kept = PhasePeaks()
    kept.hours[hour] = _mark(4.0, 20)  # sampled live
    kept.hours[hour + 1] = HourMark(heat_kw=(1.0, 0.0, 0.0))  # live, nothing counted in it
    kept.hours[hour + 2] = _mark(5.0, 10, backfilled=True)
    kept.hours[hour + 3] = _mark(5.0, 30, backfilled=True)
    history = PhasePeaks()
    for offset in range(-1, 4):
        history.hours[hour + offset] = _mark(9.0, 25)
    history.hours[hour - 40 * 24] = _mark(9.0, 25)  # before the window
    history.hours[hour + 9] = _mark(9.0, 25)  # after now
    history.hours[hour + 4] = HourMark()  # read, nothing counted: kept so that it is not read again
    assert kept.merge_history(history, now) == 3
    assert kept.hours.pop(hour + 4).backfilled
    assert kept.hours[hour].house_a == (4.0, 4.0, 4.0) and not kept.hours[hour].backfilled
    assert kept.hours[hour - 1].backfilled and kept.hours[hour - 1].minutes == 25
    assert kept.hours[hour + 1].backfilled and kept.hours[hour + 1].heat_kw == (1.0, 0.0, 0.0)
    assert kept.hours[hour + 2].minutes == 25, "read back again with more minutes"
    assert kept.hours[hour + 3].minutes == 30, "and kept where the new reading has fewer"
    assert sorted(kept.hours) == [hour - 1, hour, hour + 1, hour + 2, hour + 3]
    assert kept.covered(hour - 1, 5) and not kept.covered(hour - 2, 2)


def test_read_back_hours_survive_the_store_and_say_how_many_days_rest_on_them() -> None:
    import json

    now = T0 + 6 * 86400
    hour = int(now // 3600)
    peaks = PhasePeaks()
    per_day = DAY_MINUTES // 30 + 1
    for day in range(1, 5):
        for offset in range(per_day):
            peaks.hours[hour - 24 * day - offset] = _mark(6.0, 30, backfilled=day >= 3)
    # A day that reaches its minutes only with its read back hours.
    for offset in range(per_day):
        peaks.hours[hour - 24 * 5 - offset] = _mark(6.0, 30, backfilled=offset < 2)
    learned = peaks.learned(now)
    assert learned.days == 5 and learned.history_days == 3 and learned.history_hours == 2 * per_day + 2
    again = PhasePeaks()
    again.load(json.loads(json.dumps(peaks.as_dict())), now)
    assert again.learned(now) == learned
    assert "h" not in peaks.hours[hour - 24].as_dict() and peaks.hours[hour - 72].as_dict()["h"] is True


# --- the run ------------------------------------------------------------------------------------------------------------


async def test_without_a_recorder_nothing_is_read_and_nothing_raises(hass: HomeAssistant, freezer: Any) -> None:
    from custom_components.spotnav.site.phase_backfill import Backfill

    played = await _world(hass, freezer)
    peaks = played.controller.phase_peaks.peaks
    summary = await Backfill(hass, peaks, lambda: None).async_once(played.controller, T0)
    assert summary["stopped"] == "no_recorder" and peaks.hours == {}
    assert played.controller.phase_peaks.backfill_status() is None  # the site's own has not run


async def test_the_site_starts_the_reading_back_in_the_background_and_cancels_it_at_its_unload(
    hass: HomeAssistant, freezer: Any
) -> None:
    played = await _world(hass, freezer, own_backfill=True)
    controller = played.controller
    backfill = controller.phase_peaks.backfill
    assert backfill is not None and backfill._task is not None and not backfill._task.done()
    task = backfill._task
    assert backfill.runs == 0, "it waits for the battery's facts before its first run"
    assert await hass.config_entries.async_unload(controller.entry_id)
    await hass.async_block_till_done()
    assert task.cancelled() and backfill._task is None


async def test_the_run_repeats_until_the_learning_is_done(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from custom_components.spotnav.site import phase_backfill
    from custom_components.spotnav.site.phase_backfill import Backfill
    from custom_components.spotnav.site.phase_peaks import READY_DAYS

    monkeypatch.setattr(phase_backfill, "START_DELAY_S", 0.0)
    monkeypatch.setattr(phase_backfill, "RETRY_S", 0.0)
    peaks = PhasePeaks()
    backfill = Backfill(hass, peaks, lambda: None)
    runs: list[float] = []

    async def once(site: Any, now: float) -> dict[str, Any]:
        runs.append(now)
        if len(runs) == 2:
            raise RuntimeError("the recorder is busy")  # tried again the next time
        if len(runs) == 3:  # two weeks kept by now
            hour = int(now // 3600)
            for day in range(READY_DAYS):
                for offset in range(DAY_MINUTES // 30 + 1):
                    peaks.hours[hour - 24 * day - offset - 1] = _mark(5.0, 30)
        return {}

    monkeypatch.setattr(backfill, "async_once", once)
    backfill.start(SimpleNamespace())
    await asyncio.wait_for(backfill._task, 5)
    assert len(runs) == 3 and backfill.last == {"at": backfill.last["at"], "stopped": "error"}
    await backfill.async_stop()
