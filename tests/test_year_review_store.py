"""What the year's review keeps (`year_review/store.py`) and the fuse's overload episodes
(`site/overload_episodes.py`): a merge never loses a day or a part, an episode counts once, each person's seen state
is their own, and a store round-trips."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site.overload_episodes import EPISODE_GAP, OverloadEpisodes
from custom_components.spotnav.year_review.compute import DayParts
from custom_components.spotnav.year_review.store import KEPT_DAYS, YearReviewStore

UTC = timezone.utc
NOW = datetime(2026, 12, 15, 4, 40, tzinfo=UTC)
TODAY = date(2026, 12, 15)


def test_a_merge_lays_newer_parts_over_the_kept_and_never_loses_a_day() -> None:
    store = YearReviewStore(None, None)  # type: ignore[arg-type]
    store.merge_parts({date(2026, 11, 1): DayParts(battery=10.0, heat=4.0), date(2026, 11, 2): DayParts(car=3.0)}, TODAY)
    # The heat's review no longer holds the 1st, and the 2nd is not in tonight's at all.
    store.merge_parts({date(2026, 11, 1): DayParts(battery=12.0), date(2026, 11, 3): DayParts(car_est=5.0)}, TODAY)
    assert store.day_parts == {
        date(2026, 11, 1): DayParts(battery=12.0, heat=4.0),
        date(2026, 11, 2): DayParts(car=3.0),
        date(2026, 11, 3): DayParts(car_est=5.0),
    }


def test_a_merge_keeps_the_fuse_s_count_and_trims_only_beyond_the_kept_days() -> None:
    store = YearReviewStore(None, None)  # type: ignore[arg-type]
    store.add_fuse(date(2026, 11, 1))
    store.add_fuse(date(2026, 11, 1))
    old = TODAY - timedelta(days=KEPT_DAYS + 1)
    kept = TODAY - timedelta(days=KEPT_DAYS)
    store.merge_parts({date(2026, 11, 1): DayParts(battery=1.0), old: DayParts(car=1.0), kept: DayParts(car=2.0)}, TODAY)
    assert store.day_parts[date(2026, 11, 1)] == DayParts(battery=1.0, fuse=2)
    assert old not in store.day_parts and store.day_parts[kept] == DayParts(car=2.0)


def test_each_person_s_seen_state_is_their_own_and_a_new_year_starts_unseen() -> None:
    store = YearReviewStore(None, None)  # type: ignore[arg-type]
    store.mark("anna", 2026, "dismissed")
    store.mark("anna", 2026, "viewed")
    assert store.seen("anna", 2026) == (True, True)
    assert store.seen("bo", 2026) == (False, False)
    assert store.seen("anna", 2027) == (False, False)
    store.mark("anna", 2027, "viewed")
    assert store.seen("anna", 2027) == (True, False) and store.seen("anna", 2026) == (False, False)
    with pytest.raises(ValueError):
        store.mark("anna", 2027, "liked")
    store.reset_users()
    assert store.seen("anna", 2027) == (False, False)


def test_what_is_kept_round_trips_and_a_foreign_store_is_ignored() -> None:
    store = YearReviewStore(None, None)  # type: ignore[arg-type]
    store.merge_parts({date(2026, 3, 1): DayParts(battery=1.5, car_est=2.0)}, TODAY)
    store.add_fuse(date(2026, 3, 2))
    store.set_review(2026, date(2026, 12, 14), {"year": 2026, "pages": [{"id": "cover"}]}, NOW)
    store.mark("anna", 2026, "viewed")
    store.force(NOW + timedelta(days=7))
    again = YearReviewStore(None, None)  # type: ignore[arg-type]
    again.restore(store._snapshot())
    assert again._snapshot() == store._snapshot()
    assert again.forced(NOW) and not again.forced(NOW + timedelta(days=7))
    other = YearReviewStore(None, None)  # type: ignore[arg-type]
    other.restore({"schema": 99, "year": 2026, "review": {}})
    assert other.review is None and other.year is None


async def test_the_store_is_written_off_the_loop_and_read_back(hass: HomeAssistant) -> None:
    store = YearReviewStore(hass, "site_x")
    await store.async_load(TODAY)
    # The fuse's counter starts on the first load.
    assert store.fuse_since == TODAY
    store.add_fuse(TODAY)
    store.set_review(2026, date(2026, 12, 14), {"year": 2026, "pages": []}, NOW)
    await store.async_close()
    again = YearReviewStore(hass, "site_x")
    await again.async_load(TODAY + timedelta(days=3))
    assert again.fuse_since == TODAY and again.day_parts[TODAY].fuse == 1
    assert again.review == {"year": 2026, "pages": []} and again.through == date(2026, 12, 14)
    await again.async_close()
    await YearReviewStore.async_remove_stored(hass, "site_x")


# ------------------------------------------------------------------------------------------------ the episodes


def test_an_episode_counts_once_however_many_recomputes_it_lasts() -> None:
    episodes = OverloadEpisodes()
    seen: list[datetime] = []
    episodes.set_listener(seen.append)
    for step in range(60):
        episodes.observe(NOW + step * timedelta(seconds=5), True)
    last = NOW + 59 * timedelta(seconds=5)
    # Quiet for a while: not a new episode while the gap is short.
    episodes.observe(last + timedelta(seconds=5), False)
    moment = last + EPISODE_GAP - timedelta(seconds=1)
    episodes.observe(moment, True)
    assert seen == [NOW]
    moment += EPISODE_GAP
    assert episodes.observe(moment, True) is True
    assert seen == [NOW, moment]


def test_a_listener_that_fails_never_raises_into_the_recompute() -> None:
    episodes = OverloadEpisodes()

    def broken(_moment: datetime) -> None:
        raise RuntimeError("no")

    episodes.set_listener(broken)
    assert episodes.observe(NOW, True) is True


async def test_the_site_counts_an_overload_lowering_only_with_its_active_control_on(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.world import setup_charger_and_site

    _charger, site = await setup_charger_and_site(hass, "entry_fuse")
    assert site is not None
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    seen: list[Any] = []
    controller.overload_episodes.set_listener(seen.append)
    monkeypatch.setattr(controller, "_import_overload_now", lambda: True)
    monkeypatch.setattr(controller, "_active_control_allowed", lambda: False)
    controller._count_overload_episode()
    assert seen == []
    monkeypatch.setattr(controller, "_active_control_allowed", lambda: True)
    controller._count_overload_episode()
    controller._count_overload_episode()
    assert len(seen) == 1
