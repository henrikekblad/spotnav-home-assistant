"""The house's per-phase peaks read back from the recorder (`site/phase_backfill.py`): the read itself.

Recorder-backed tests need `recorder_mock` resolved before `hass` (see `tests/test_discovery_history.py`): the
module-level override below shadows the project-wide `auto_enable_custom_integrations` for this file only.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.components.recorder import history
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.spotnav.site import phase_backfill
from custom_components.spotnav.site.phase_backfill import Backfill
from custom_components.spotnav.site.phase_peaks import HourMark, PhasePeaks

from .test_phase_backfill import START, T0, VOLTS, _same, _steady, _world


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url: str, enable_custom_integrations) -> None:
    yield


def _without_flag(mark: HourMark) -> dict[str, Any]:
    return {key: value for key, value in mark.as_dict().items() if key not in ("h", "l")}


async def test_the_recorder_s_history_is_read_back_off_the_event_loop_into_the_hours_missing(
    recorder_mock, hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    played = await _world(hass, freezer)
    controller = played.controller
    # Twenty-five minutes of a house with a peak on L3, kept by the recorder as they happen.
    await _steady(played, 10)
    await _steady_from(played, 10, 15, house=(1000.0, 500.0, 3500.0))
    await _steady_from(played, 15, 25)
    await async_wait_recording_done(hass)
    now = T0 + 25 * 60
    freezer.move_to(START + timedelta(minutes=25))

    threads: list[int] = []
    read = history.get_significant_states

    def watched(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return read(*args, **kwargs)

    monkeypatch.setattr(history, "get_significant_states", watched)
    peaks = controller.phase_peaks.peaks
    assert peaks.hours == {}
    changed: list[bool] = []
    backfill = Backfill(hass, peaks, lambda: changed.append(True))
    with caplog.at_level("INFO", logger=phase_backfill.__name__):
        summary = await backfill.async_once(controller, now)

    hour = int(T0 // 3600)
    assert summary["stopped"] is None and summary["rows"] > 500 and summary["hours_taken"] == 1
    assert summary["entities"] >= 10 and summary["samples"] >= 150
    assert threads and threading.get_ident() not in threads, "the recorder is never read in the event loop"
    assert changed == [True]
    mark = peaks.hours[hour]
    assert mark.backfilled and mark.minutes >= 20 and mark.lost == 0
    assert mark.house_a is not None and mark.house_a[2] == pytest.approx(3500.0 / VOLTS + 0.45, abs=0.1)
    # The very marks the same history gives replayed straight, which are the live samples' (`test_phase_backfill`).
    _same(_without_flag(mark), _without_flag(played.replayed(T0 - 3600, now).hours[hour]))
    assert len({record.getMessage() for record in caplog.records if "read back from the recorder" in record.getMessage()}) == 1
    learned = peaks.learned(now)
    assert learned.history_hours == 1 and learned.as_dict()["hours_from_history"] == 1
    assert backfill.last is summary and backfill.runs == 1

    # A second run reads those hours no more.
    before = len(threads)
    again = await backfill.async_once(controller, now)
    assert again["hours_taken"] == 0 and again["samples"] == 0 and changed == [True]
    assert len(threads) - before < before, "the hours kept were not read again"


async def _steady_from(played: Any, first: int, last: int, **kwargs: Any) -> None:
    """`_steady` for the minutes `first` to `last` of the hour."""
    freezer = played.freezer

    class Later:
        def move_to(self, when: Any) -> None:
            freezer.move_to(when + timedelta(minutes=first))

    played.freezer = Later()
    try:
        await _steady(played, last - first, **kwargs)
    finally:
        played.freezer = freezer


async def test_an_hour_sampled_live_keeps_its_own_mark(recorder_mock, hass: HomeAssistant, freezer: Any) -> None:
    played = await _world(hass, freezer)
    await _steady(played, 8)
    await async_wait_recording_done(hass)
    now = T0 + 8 * 60
    freezer.move_to(START + timedelta(minutes=8))
    peaks: PhasePeaks = played.controller.phase_peaks.peaks
    hour = int(T0 // 3600)
    peaks.hours[hour] = HourMark(house_a=(1.0, 1.0, 1.0), minutes=3, total_all=3.0, total_counted=3.0)
    summary = await Backfill(hass, peaks, lambda: None).async_once(played.controller, now)
    assert summary["hours_taken"] == 0
    assert peaks.hours[hour].house_a == (1.0, 1.0, 1.0) and not peaks.hours[hour].backfilled


async def test_a_run_stops_at_its_row_limit_and_keeps_what_it_has(
    recorder_mock, hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    played = await _world(hass, freezer)
    await _steady(played, 8)
    await async_wait_recording_done(hass)
    freezer.move_to(START + timedelta(minutes=8))
    monkeypatch.setattr(phase_backfill, "MAX_ROWS", 10)
    peaks: PhasePeaks = played.controller.phase_peaks.peaks
    summary = await Backfill(hass, peaks, lambda: None).async_once(played.controller, T0 + 8 * 60)
    # The first hours read (the sensors' states at the site's start) passed it: the hour after them was never read.
    assert summary["stopped"] == "rows" and summary["hours_read"] == phase_backfill.CHUNK_HOURS
    assert summary["hours_taken"] == 0 and int(T0 // 3600) not in peaks.hours
    assert all(mark.backfilled and not mark.counted for mark in peaks.hours.values())


async def test_a_sensor_the_recorder_does_not_keep_counts_nothing_and_is_named(
    recorder_mock, hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    played = await _world(hass, freezer)
    # The meter's voltages are read live from here on but were never kept (excluded from the recorder): asked for
    # under names the recorder has no rows of.
    config = played.controller.config
    renamed = {
        phase: {**entities, "voltage": entities["voltage"] + "_unrecorded"}
        for phase, entities in config["derived_entities"].items()
    }
    await _steady(played, 8)
    await async_wait_recording_done(hass)
    played.controller.config = {**config, "derived_entities": renamed}
    freezer.move_to(START + timedelta(minutes=8))
    peaks: PhasePeaks = played.controller.phase_peaks.peaks
    peaks.hours.clear()
    with caplog.at_level("INFO", logger=phase_backfill.__name__):
        summary = await Backfill(hass, peaks, lambda: None).async_once(played.controller, T0 + 8 * 60)
    assert summary["hours_taken"] == 0 and all(not mark.counted for mark in peaks.hours.values())
    assert summary["without_history"] == sorted(entities["voltage"] for entities in renamed.values())
    assert "keeps no history of sensor.home_site_voltage_l1_unrecorded" in caplog.text


async def test_a_read_that_fails_keeps_the_hours_replayed_and_the_next_run_comes_in_an_hour(
    recorder_mock, hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    played = await _world(hass, freezer)
    await _steady(played, 8)
    await async_wait_recording_done(hass)
    freezer.move_to(START + timedelta(minutes=8))
    reads: list[int] = []
    read = history.get_significant_states

    def failing(*args: Any, **kwargs: Any) -> Any:
        reads.append(1)
        if len(reads) > 1:
            raise OSError("database is locked")
        return read(*args, **kwargs)

    monkeypatch.setattr(history, "get_significant_states", failing)
    peaks: PhasePeaks = played.controller.phase_peaks.peaks
    changed: list[bool] = []
    backfill = Backfill(hass, peaks, lambda: changed.append(True))
    scheduled: list[float] = []
    monkeypatch.setattr(backfill, "_schedule", lambda site, delay_s: scheduled.append(delay_s))
    with caplog.at_level("INFO", logger=phase_backfill.__name__):
        await backfill._async_due(played.controller)
    # The hours of the first read (before the failing one) are kept, and marked as read back.
    assert peaks.hours and all(mark.backfilled for mark in peaks.hours.values()) and changed == [True]
    assert int(T0 // 3600) not in peaks.hours
    assert backfill.last is not None and backfill.last["stopped"] == "error"
    assert scheduled == [phase_backfill.ERROR_RETRY_S]
    assert "database is locked" in caplog.text and "tried again in an hour" in caplog.text


async def test_nothing_is_read_while_the_recorder_migrates(
    recorder_mock, hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeassistant.components.recorder import get_instance

    played = await _world(hass, freezer)
    await _steady(played, 4)
    await async_wait_recording_done(hass)
    monkeypatch.setattr(get_instance(hass), "migration_in_progress", True)
    reads: list[int] = []
    monkeypatch.setattr(history, "get_significant_states", lambda *args, **kwargs: reads.append(1))
    backfill = Backfill(hass, played.controller.phase_peaks.peaks, lambda: None)
    scheduled: list[float] = []
    monkeypatch.setattr(backfill, "_schedule", lambda site, delay_s: scheduled.append(delay_s))
    await backfill._async_due(played.controller)
    assert backfill.last["stopped"] == "migration" and reads == []
    assert scheduled == [phase_backfill.ERROR_RETRY_S]


async def test_a_read_cut_short_leaves_none_of_its_hours_as_read(
    recorder_mock, hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    played = await _world(hass, freezer)
    await _steady(played, 12)
    await async_wait_recording_done(hass)
    now = T0 + 12 * 60
    freezer.move_to(START + timedelta(minutes=12))
    peaks: PhasePeaks = played.controller.phase_peaks.peaks
    backfill = Backfill(hass, peaks, lambda: None)
    replayed = backfill._async_replay
    calls: list[float] = []

    async def cut_short(replay: Any, start: float, end: float, until: float) -> bool:
        calls.append(start)
        if start < T0:
            return await replayed(replay, start, end, until)
        # The read of the hour itself: its first six minutes replayed, then the run's time is up.
        await replayed(replay, start, start + 360, until)
        return False

    monkeypatch.setattr(backfill, "_async_replay", cut_short)
    summary = await backfill.async_once(played.controller, now)
    hour = int(T0 // 3600)
    assert summary["stopped"] == "time" and calls[-1] == T0
    # Minutes of it were counted in the replay, and none of that is kept: the hour is read whole the next time.
    assert hour not in peaks.hours and not peaks.covered(hour, 1)
    assert peaks.hours and all(kept < hour for kept in peaks.hours)
    monkeypatch.setattr(backfill, "_async_replay", replayed)
    again = await backfill.async_once(played.controller, now)
    assert again["stopped"] is None and peaks.hours[hour].minutes >= 8
