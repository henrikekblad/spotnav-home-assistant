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
from custom_components.spotnav.year_review.compute import DayParts, PriceDay
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


def test_a_day_s_price_part_outlives_its_fuse_count_and_a_saving_merge_and_is_kept() -> None:
    store = YearReviewStore(None, None)  # type: ignore[arg-type]
    price = PriceDay(runs=((0, 900, 96),), cheap=0xFFFFFFFF, sells=(10.0,) * 24, top=(8, 250.5))
    day = date(2026, 11, 1)
    store.merge_parts({day: DayParts(price=price)}, TODAY)
    store.add_fuse(day)
    store.merge_parts({day: DayParts(battery=3.0)}, TODAY)
    assert store.day_parts[day] == DayParts(battery=3.0, fuse=1, price=price)
    again = YearReviewStore(None, None)  # type: ignore[arg-type]
    again.restore(store._snapshot())
    assert again.day_parts[day] == store.day_parts[day]


def test_the_merged_days_are_kept_as_runs_round_trip_and_are_trimmed_with_the_parts() -> None:
    store = YearReviewStore(None, None)  # type: ignore[arg-type]
    old = TODAY - timedelta(days=KEPT_DAYS + 1)
    days = [old, date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 10)]
    store.merge_parts({date(2026, 11, 2): DayParts(car=1.0)}, TODAY, merged=days)
    assert store.merged == set(days[1:])
    snapshot = store._snapshot()
    assert snapshot["merged"] == [["2026-11-01", "2026-11-03"], ["2026-11-10", "2026-11-10"]]
    again = YearReviewStore(None, None)  # type: ignore[arg-type]
    again.restore(snapshot)
    assert again.merged == store.merged
    # A store kept before the merged days were starts with none.
    del snapshot["merged"]
    fresh = YearReviewStore(None, None)  # type: ignore[arg-type]
    fresh.restore(snapshot)
    assert fresh.merged == set() and fresh.day_parts == store.day_parts


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


async def test_a_real_overload_lowering_is_one_episode_in_the_site_s_year_store(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's night of 2 October (`tests/test_balancing_retry.py`): a reading 3.6 A over the fuse lowers the car,
    read again on later passes and a new reading still over: one episode on the site's local day."""
    from custom_components.spotnav.runtime import site_data
    from tests.test_balancing_retry import _night, _site_a

    halo = await _night(hass, monkeypatch, "nightyear", battery_w=0.0)
    await halo.start()
    halo.switch("on")
    await hass.async_block_till_done()
    _site_a(hass, halo.site, 22.6, 23.6, 23.6)
    await halo.tick()
    for _ in range(3):
        await halo.tick()
    _site_a(hass, halo.site, 21.0, 22.0, 22.0)
    await hass.async_block_till_done()
    await halo.tick()
    assert halo.written()[-1] == "1.10,2.10"
    data = site_data(hass, halo.controller.entry_id)
    assert data is not None and data.year_review is not None
    assert sum(parts.fuse for parts in data.year_review.store.day_parts.values()) == 1
