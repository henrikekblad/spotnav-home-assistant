"""The year's review on a real site (`year_review/runtime.py`): the nightly run in December and on 1 January only,
the start-up run, the computation off the event loop, the fuse's episodes into the store, and each person's answer."""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.runtime import site_data
from custom_components.spotnav.year_review import runtime as year_runtime
from custom_components.spotnav.year_review.compute import STATE_COMPUTING, STATE_NONE, STATE_READY
from custom_components.spotnav.year_review.runtime import FORCED_FOR, STARTUP_DELAY_S, SiteYearReview, YearReviewRefusal

UTC = timezone.utc
#: 15 December 2026, 10:00 in Stockholm.
DECEMBER = datetime(2026, 12, 15, 9, 0, tzinfo=UTC)


async def _site(hass: HomeAssistant, freezer: Any, moment: datetime) -> SiteYearReview:
    from tests.world import setup_charger_and_site

    await hass.config.async_set_time_zone("Europe/Stockholm")
    freezer.move_to(moment)
    _charger, site = await setup_charger_and_site(hass, "entry_year")
    assert site is not None
    data = site_data(hass, site.entry_id)
    assert data is not None and data.year_review is not None
    return data.year_review


@pytest.fixture
def computed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """Each computation's year and the thread it ran in (the real one runs, its answer kept)."""
    seen: list[tuple[int, str]] = []
    real = year_runtime.compute_review

    def spy(inputs: Any) -> Any:
        seen.append((inputs.year, threading.current_thread().name))
        return real(inputs)

    monkeypatch.setattr(year_runtime, "compute_review", spy)
    return seen


async def test_a_start_up_in_december_works_the_year_out_off_the_loop_once_the_site_has_settled(
    hass: HomeAssistant, freezer: Any, computed: list[tuple[int, str]]
) -> None:
    review = await _site(hass, freezer, DECEMBER)
    assert review.store.review is None and computed == []
    freezer.tick(timedelta(seconds=STARTUP_DELAY_S + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [year for year, _thread in computed] == [2026]
    # In an executor job, never on the loop's thread.
    assert computed[0][1] != threading.main_thread().name
    assert review.store.year == 2026 and review.store.through == date(2026, 12, 14)
    pages = [page["id"] for page in review.store.review["pages"]]
    assert pages[0] == "cover" and pages[-1] == "thanks"
    # The site was set up today: a part of the year.
    assert review.store.review["since"] == "2026-12-15" and review.store.review["partial"] is True


async def test_outside_the_window_the_night_only_merges_and_nothing_is_worked_out(
    hass: HomeAssistant, freezer: Any, computed: list[tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _site(hass, freezer, datetime(2026, 7, 4, 9, tzinfo=UTC))
    merged: list[int] = []
    real = review._async_merge

    async def counting(days: list[date], today: date) -> None:
        merged.append(len(days))
        await real(days, today)

    monkeypatch.setattr(review, "_async_merge", counting)
    freezer.move_to(datetime(2026, 7, 5, 2, 40, tzinfo=UTC))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert merged == [year_runtime.MERGE_DAYS] and computed == []
    assert review.answer("anyone")["state"] == STATE_NONE


async def test_new_year_s_day_works_out_the_final_review_and_january_keeps_it(
    hass: HomeAssistant, freezer: Any, computed: list[tuple[int, str]]
) -> None:
    review = await _site(hass, freezer, datetime(2026, 12, 31, 9, tzinfo=UTC))
    freezer.move_to(datetime(2027, 1, 1, 3, 40, tzinfo=UTC))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [year for year, _thread in computed] == [2026]
    assert review.store.through == date(2026, 12, 31)
    freezer.move_to(datetime(2027, 1, 2, 3, 40, tzinfo=UTC))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(computed) == 1
    answer = review.answer("anna")
    assert answer["year"] == 2026 and answer["state"] == STATE_READY and answer["banner"] and answer["row"]


async def test_each_person_s_banner_goes_when_they_open_or_close_it_and_the_row_stays(
    hass: HomeAssistant, freezer: Any, computed: list[tuple[int, str]]
) -> None:
    review = await _site(hass, freezer, DECEMBER)
    await review.async_run(2026)
    review.store.mark("anna", 2026, "viewed")
    review.store.mark("bo", 2026, "dismissed")
    assert review.answer("anna")["banner"] is False and review.answer("anna")["row"] is True
    assert review.answer("bo")["banner"] is False
    assert review.answer("cecilia")["banner"] is True and review.answer("cecilia")["review"] is not None
    # In February nothing is offered and the year rule's year has no review.
    freezer.move_to(datetime(2027, 2, 3, 9, tzinfo=UTC))
    answer = review.answer("cecilia")
    assert answer["year"] == 2027 and answer["state"] == STATE_NONE and answer["review"] is None


async def test_the_service_s_showing_offers_the_review_for_a_week_whatever_the_date(
    hass: HomeAssistant, freezer: Any, computed: list[tuple[int, str]]
) -> None:
    review = await _site(hass, freezer, datetime(2026, 7, 4, 9, tzinfo=UTC))
    review.store.mark("anna", 2026, "dismissed")
    result = await review.async_show(None, reset=True)
    assert result["year"] == 2026 and result["review"]["through"] == "2026-07-03"
    assert review.answer("anna")["banner"] is True
    freezer.tick(FORCED_FOR + timedelta(seconds=1))
    assert review.answer("anna")["banner"] is False and review.answer("anna")["state"] == STATE_READY
    with pytest.raises(YearReviewRefusal):
        await review.async_run(2027)


async def test_a_run_says_it_is_computing_until_it_is_done(hass: HomeAssistant, freezer: Any) -> None:
    review = await _site(hass, freezer, DECEMBER)
    review.running = True
    assert review.answer("anna")["state"] == STATE_COMPUTING
    review.running = False


async def test_an_overload_episode_is_kept_on_its_local_day(hass: HomeAssistant, freezer: Any) -> None:
    review = await _site(hass, freezer, datetime(2026, 12, 15, 23, 30, tzinfo=UTC))
    review._controller.overload_episodes.observe(datetime(2026, 12, 15, 23, 30, tzinfo=UTC), True)
    # 00:30 in Stockholm: the 16th.
    assert review.store.day_parts[date(2026, 12, 16)].fuse == 1
    await hass.config_entries.async_unload(review.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    # Unloaded, nobody is told any more.
    assert review._controller.overload_episodes._listener is None


def test_the_review_reads_the_recorder_only_through_the_house_card_s_readers() -> None:
    """No statistics call, file read or sleep of its own: the recorder is read by `api/house_history` and `api/saving`
    in the recorder's executor, the store written off the loop (the loop I/O guard covers the rest at run time)."""
    import ast
    from pathlib import Path

    package = Path(year_runtime.__file__).parent
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names] + [getattr(node, "module", None) or ""]
                assert not any("recorder" in name or name == "time" for name in names), (path.name, names)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "sleep"}, path.name
