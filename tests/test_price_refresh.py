"""The refresh manager's lifecycle: subscriptions, timers, and what it refuses to do.

Timers here are a recording double and time is injected, so every case runs to its
conclusion immediately: nothing sleeps waiting for a real clock, and a "timer
fired" is an explicit call. The transport and store doubles are the repository's
own, imported so there is one fake wire in the suite rather than two.
"""

from __future__ import annotations

import asyncio
import json

from datetime import datetime, timedelta, timezone
from functools import partial

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from tests.relay import (
    TODAY,
    TOMORROW,
    Clock,
    StoreDouble,
    StubTransport,
    day_body,
)

from custom_components.spotnav.pricing.price_refresh import (
    HEALTHY_POLL_SECONDS,
    PUBLICATION_POLL_SECONDS,
    SETTLED_POLL_SECONDS,
    AreaPriceSnapshot,
    PriceAreaUnavailable,
    PriceRefreshManager,
    backoff_delay,
    next_local_midnight,
)
from custom_components.spotnav.pricing.price_repository import PriceRepository
from .relay import stub_repository, FakeScheduler, FakeTimer
from custom_components.spotnav.runtime import domain_data

STOCKHOLM = "Europe/Stockholm"
BERLIN = "Europe/Berlin"


class Listener:
    """A listener that records the snapshots it was given."""

    def __init__(self, *, raises: bool = False) -> None:
        self.snapshots: list[AreaPriceSnapshot] = []
        self.raises = raises

    def __call__(self, snapshot: AreaPriceSnapshot) -> None:
        self.snapshots.append(snapshot)
        if self.raises:
            raise RuntimeError("this listener is broken")


@pytest.fixture
def repository(hass: HomeAssistant, transport: StubTransport, clock: Clock) -> PriceRepository:
    return stub_repository(hass, transport, now=clock, store=StoreDouble())


@pytest.fixture
def scheduler() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
def manager(
    hass: HomeAssistant, repository: PriceRepository, clock: Clock, scheduler: FakeScheduler
) -> PriceRefreshManager:
    # Jitter fixed at 0.5, which [jittered] leaves the delay exactly as it was: the
    # policy's own numbers are what the scheduling assertions below pin.
    return PriceRefreshManager(
        hass, repository, now=clock, jitter=lambda: 0.5, scheduler=scheduler
    )


#: The next Stockholm midnight after the fixture clock's start (08:00 local on
#: 2026-09-22). The rollover timer is the one scheduled for exactly this instant;
#: everything else live is a refresh timer.
ROLLOVER_WHEN = datetime(2026, 9, 22, 22, 0, tzinfo=timezone.utc)


def count_cycles(manager: PriceRefreshManager) -> list[int]:
    """Count the manager's global cycles from now on: a one-item list, read as `runs == [n]`."""
    runs = [0]
    original = manager._refresh_global  # noqa: SLF001 - the cycle itself, to count it

    async def counted() -> None:
        runs[0] += 1
        await original()

    manager._refresh_global = counted  # type: ignore[method-assign]  # noqa: SLF001
    return runs


def kind_of(timer: FakeTimer) -> str:
    """Which callback a timer would run, from the manager's own partial.

    The manager schedules each kind through a `functools.partial` of its handler,
    so the double can tell a refresh timer from a rollover timer without guessing
    from instants — and a test can ask for exactly the one it means.
    """
    action = timer.action
    assert isinstance(action, partial), f"expected a partial, got {action!r}"
    return action.func.__name__


def area_of(timer: FakeTimer) -> str:
    """Which area a timer belongs to, from the record the manager partial carries."""
    return timer.action.args[0].area_id


def refresh_timer(scheduler: FakeScheduler) -> FakeTimer:
    """The one live *global* refresh timer.

    There is one for the installation, whatever the area count: that is the whole
    point of the catalogue and index being installation-global resources.
    """
    live = [timer for timer in scheduler.pending if kind_of(timer) == "_on_global_refresh"]
    assert len(live) == 1, f"expected exactly one live global refresh timer, found {len(live)}"
    return live[0]


def rollover_timer(
    scheduler: FakeScheduler, *, when: datetime = ROLLOVER_WHEN, area: str | None = None
) -> FakeTimer:
    """The one live rollover timer, at the area-local midnight given."""
    live = [
        timer
        for timer in scheduler.pending
        if kind_of(timer) == "_on_rollover"
        and timer.when == when
        and (area is None or area_of(timer) == area)
    ]
    assert len(live) == 1, f"expected exactly one rollover timer at {when}, found {len(live)}"
    return live[0]


async def test_the_same_owner_subscribing_twice_is_counted_once(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport
) -> None:
    transport.serve_area()
    listener = Listener()

    first = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=listener)
    await hass.async_block_till_done()
    second = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=listener)
    await hass.async_block_till_done()

    snapshot = manager.area_snapshot("SE4")
    assert snapshot is not None and snapshot.subscriber_count == 1
    assert manager.owner_area("entry-a") == "SE4"
    # ...and the day was fetched once, not once per subscribe.
    assert transport.call_count(transport.day_path("SE4", TODAY)) == 1

    # Unsubscribing is idempotent, through either handle.
    first()
    first()
    second()
    assert manager.area_snapshot("SE4") is None
    assert manager.owner_area("entry-a") is None


async def test_two_owners_of_one_area_share_one_stream_and_one_leaves_the_other(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    # The shared cycle is held open inside its day request, so the second
    # subscription genuinely arrives while it is in flight — which is the case that
    # must join rather than start a poll of its own.
    today = transport.day_path("SE4", TODAY)
    transport.hold(today)
    first = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await transport.entered[today].wait()
    await manager.async_subscribe(owner_id="entry-b", area_id="SE4", listener=Listener())
    transport.release(today)
    await hass.async_block_till_done()

    snapshot = manager.area_snapshot("SE4")
    assert snapshot is not None and snapshot.subscriber_count == 2
    # One area, one day — however many chargers asked.
    assert transport.call_count(today) == 1
    assert len(scheduler.pending) == 2  # one refresh timer and one rollover timer
    assert refresh_timer(scheduler) is not None and rollover_timer(scheduler) is not None

    first()
    still = manager.area_snapshot("SE4")
    assert still is not None and still.subscriber_count == 1
    # The area's work is untouched by one owner leaving.
    assert len(scheduler.pending) == 2
    assert manager.owner_area("entry-b") == "SE4"


async def test_two_areas_progress_independently_and_share_one_cycle(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area("SE4")
    transport.serve("/v1/DE-LU/2026/09-22.json", 200, day_body("DE-LU", TODAY))
    runs = count_cycles(manager)

    today = transport.day_path("SE4", TODAY)
    transport.hold(today)
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await transport.entered[today].wait()
    await manager.async_subscribe(owner_id="entry-b", area_id="DE-LU", listener=Listener())
    transport.release(today)
    await hass.async_block_till_done()

    # One cycle, although the two subscriptions were made in consecutive event-loop turns: the cycle is global,
    # so a second area joins it instead of starting its own poll. Two areas' own days.
    assert runs == [1]
    assert transport.call_count(today) == 1
    assert transport.call_count(transport.day_path("DE-LU", TODAY)) == 1
    # No area list and no index is asked for: the list is bundled.
    assert transport.call_count("/v1/areas.json") == 0 and transport.call_count("/v1/index.json") == 0

    assert manager.area_snapshot("SE4") is not None
    assert manager.area_snapshot("DE-LU") is not None

    # A global cycle runs once per due cycle, not once per area; with today held, it asks for nothing new.
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert runs == [2]
    assert transport.call_count(today) == 1


async def test_an_area_without_a_known_timezone_is_refused_rather_than_guessed(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport
) -> None:
    with pytest.raises(PriceAreaUnavailable):
        await manager.async_subscribe(owner_id="entry-a", area_id="XX9", listener=Listener())
    assert manager.owner_area("entry-a") is None

    # A caller that already knows the zone — from its own settings, say — may
    # subscribe even to an area the list does not name.
    handle = await manager.async_subscribe(
        owner_id="entry-a", area_id="XX9", listener=Listener(), tz=STOCKHOLM
    )
    assert manager.owner_area("entry-a") == "XX9"
    snapshot = manager.area_snapshot("XX9")
    assert snapshot is not None and snapshot.today == TODAY
    handle()


async def test_a_new_subscriber_receives_the_known_state_promptly(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport
) -> None:
    transport.serve_area()
    existing = Listener()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=existing)
    await hass.async_block_till_done()

    late = Listener()
    await manager.async_subscribe(owner_id="entry-b", area_id="SE4", listener=late)

    # Before that call returned, and without any new fetch. The state is
    # `waiting_for_tomorrow` because only today is served -- tomorrow (2026-09-23)
    # was answered "not published yet", which is a state and not a failure.
    assert late.snapshots, "a new subscriber was not told the current state"
    assert late.snapshots[-1].state == "waiting_for_tomorrow"
    assert late.snapshots[-1].reason == "waiting_for_tomorrow"
    assert late.snapshots[-1].today_document is not None
    assert late.snapshots[-1].tomorrow_document is None


def brussels(hour: int, minute: int = 0) -> datetime:
    """An instant whose Brussels clock reads the given time (summer, +02:00)."""
    return datetime(2026, 9, 22, hour - 2, minute, tzinfo=timezone.utc)


async def test_the_next_refresh_is_scheduled_by_the_documented_policy(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler, clock: Clock
) -> None:
    transport.serve_area()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    # Today ready, tomorrow not out yet, and it is 08:00 Brussels: the ordinary cadence.
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=HEALTHY_POLL_SECONDS)

    # The timer fires: a new run, and a new timer at the same cadence, with the old
    # one gone rather than left behind.
    fired = refresh_timer(scheduler)
    fired.fire()
    await hass.async_block_till_done()
    assert fired.fired is True
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=HEALTHY_POLL_SECONDS)

    # Move into the publication window, with tomorrow still not out: 5 minutes.
    clock.now = brussels(13, 0)
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=PUBLICATION_POLL_SECONDS)

    # And a settled area — tomorrow in hand — is checked hourly.
    transport.serve(transport.day_path("SE4", TOMORROW), 200, day_body("SE4", TOMORROW))
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    snapshot = manager.area_snapshot("SE4")
    assert snapshot is not None and snapshot.state == "ready"
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=SETTLED_POLL_SECONDS)


async def test_jitter_moves_the_interval_but_never_the_midnight(
    hass: HomeAssistant, repository: PriceRepository, clock: Clock, transport: StubTransport
) -> None:
    transport.serve_area()
    scheduler = FakeScheduler()
    # rand = 0.0 is the low end: exactly ten percent early.
    manager = PriceRefreshManager(hass, repository, now=clock, jitter=lambda: 0.0, scheduler=scheduler)

    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=HEALTHY_POLL_SECONDS) * 0.9

    # The rollover timer sits at the area's exact local midnight, unjittered: 22:00
    # UTC on the evening before a Stockholm midnight, whatever the jitter is.
    assert rollover_timer(scheduler).when == ROLLOVER_WHEN


async def test_transport_failures_step_through_the_backoff_and_recover(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler, clock: Clock
) -> None:
    transport.serve(transport.day_path("SE4", TODAY), 503, "")
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM)
    await hass.async_block_till_done()

    assert manager.area_snapshot("SE4").reason == "index_unavailable"  # type: ignore[union-attr]
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=60)

    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=120)

    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=300)

    # Back, and the countdown starts over.
    transport.serve_area()
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=HEALTHY_POLL_SECONDS)
    assert manager.area_snapshot("SE4").state == "waiting_for_tomorrow"  # type: ignore[union-attr]


async def test_a_tomorrow_not_out_yet_is_waited_for_and_then_fetched_once(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()

    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()
    snapshot = manager.area_snapshot("SE4")
    # Asked once, answered "not published yet": waiting, and not a failure.
    assert snapshot is not None and snapshot.waiting_for_tomorrow is True
    assert snapshot.tomorrow_snapshot.attempt_error is None
    assert transport.call_count(transport.day_path("SE4", TOMORROW)) == 1

    # Once it is out, the next cycle fetches it: exactly one more request.
    transport.serve(transport.day_path("SE4", TOMORROW), 200, day_body("SE4", TOMORROW))
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()

    assert transport.call_count(transport.day_path("SE4", TOMORROW)) == 2
    settled = manager.area_snapshot("SE4")
    assert settled is not None and settled.state == "ready" and settled.tomorrow_document is not None

    # A complete immutable day is never downloaded twice.
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert transport.call_count(transport.day_path("SE4", TODAY)) == 1
    assert transport.call_count(transport.day_path("SE4", TOMORROW)) == 2


async def test_an_incomplete_day_stays_incomplete_and_is_not_re_requested(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    # A day *for today* that stops half-way: 48 of its 96 quarters published.
    partial = json.loads(day_body("SE4", TODAY))
    partial["prices"] = partial["prices"][:48]
    transport.serve(transport.day_path("SE4", TODAY), 200, json.dumps(partial))

    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    snapshot = manager.area_snapshot("SE4")
    assert snapshot is not None
    assert snapshot.state == "incomplete" and snapshot.reason == "incomplete_day"
    assert snapshot.today_document is not None and snapshot.today_document.interval_count == 48

    # Day documents are immutable: an incomplete one is reported, not
    # re-requested or cache-busted.
    for _ in range(3):
        refresh_timer(scheduler).fire()
        await hass.async_block_till_done()
    assert transport.call_count(transport.day_path("SE4", TODAY)) == 1
    assert manager.area_snapshot("SE4").state == "incomplete"  # type: ignore[union-attr]


async def test_a_contract_invalid_day_is_reported_and_never_hot_looped(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler, clock: Clock
) -> None:
    transport.serve_area()
    transport.serve(transport.day_path("SE4", TODAY), 200, "<html>not a document</html>")

    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    snapshot = manager.area_snapshot("SE4")
    assert snapshot is not None
    assert snapshot.state == "invalid" and snapshot.reason == "invalid_contract"
    assert snapshot.today_state == "invalid"
    # An invalid body is not a hot loop: at least the invalid cadence.
    assert refresh_timer(scheduler).when >= clock.now + timedelta(minutes=30)


async def test_the_local_midnight_rollover_republishes_the_new_date_keys(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler, clock: Clock
) -> None:
    transport.serve_area()
    listener = Listener()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=listener)
    await hass.async_block_till_done()
    assert manager.area_snapshot("SE4").today == TODAY  # type: ignore[union-attr]

    # The new today is out before the clock rolls over (the first cycle was answered "not published yet").
    transport.serve(transport.day_path("SE4", TOMORROW), 200, day_body("SE4", TOMORROW))

    # Move the clock to the area's local midnight, and fire the exact rollover timer
    # that was already scheduled for it.
    rollover = rollover_timer(scheduler)
    clock.now = rollover.when
    rollover.fire()
    await hass.async_block_till_done()

    after = manager.area_snapshot("SE4")
    assert after is not None
    assert after.today == TOMORROW
    assert after.tomorrow == TOMORROW + timedelta(days=1)
    assert listener.snapshots[-1].today == TOMORROW
    # The rollover's own run has already fetched the new today, and the next rollover is the following
    # local midnight.
    assert transport.call_count(transport.day_path("SE4", TOMORROW)) == 2
    assert after.today_state == "ready" and after.today_document is not None
    assert rollover_timer(scheduler, when=datetime(2026, 9, 23, 22, 0, tzinfo=timezone.utc)) is not None  # type: ignore[union-attr]


async def test_today_and_tomorrow_follow_each_areas_declared_timezone(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, clock: Clock, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    transport.serve("/v1/NZ/2026/09-23.json", 200, day_body("NZ", TOMORROW))

    # 12:00 UTC is 14:00 in Stockholm and exactly midnight in Auckland: the two
    # areas are on different calendar dates at the same instant.
    clock.now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM)
    await hass.async_block_till_done()
    await manager.async_subscribe(
        owner_id="entry-b", area_id="NZ", listener=Listener(), tz="Pacific/Auckland"
    )
    await hass.async_block_till_done()

    stockholm = manager.area_snapshot("SE4")
    auckland = manager.area_snapshot("NZ")
    assert stockholm is not None and auckland is not None
    assert (stockholm.today, stockholm.tomorrow) == (TODAY, TOMORROW)
    assert (auckland.today, auckland.tomorrow) == (TOMORROW, TOMORROW + timedelta(days=1))

    # Each area's rollover sits at its own local midnight: 22:00 UTC for Stockholm,
    # and 12:00 UTC the following day for Auckland — today, its local midnight is
    # the instant the clock already stands at, so the next one is a day later.
    assert rollover_timer(scheduler, area="SE4") is not None
    assert rollover_timer(scheduler, when=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc), area="NZ") is not None
    # 12:00 UTC is 14:00 Brussels, inside the midday publication window, so the shared cycle is
    # impatient. The window is one European clock on purpose, not each area's own: what decides is
    # the Brussels wall clock, so an area whose local clock reads midnight neither creates the
    # impatience nor removes it, and a far-away area never shifts the policy in either direction.
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=PUBLICATION_POLL_SECONDS)


async def test_a_timer_and_a_manual_refresh_overlap_into_one_run(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    path = transport.day_path("SE4", TOMORROW)
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    # The first cycle asked for tomorrow once and heard "not published yet"; now it is out.
    assert transport.call_count(path) == 1
    transport.serve(path, 200, day_body("SE4", TOMORROW))
    transport.hold(path)

    # The timer fires; while that run is still open, a manual refresh arrives.
    refresh_timer(scheduler).fire()
    manual = asyncio.ensure_future(manager.async_refresh_area("SE4"))
    await transport.entered[path].wait()
    transport.release(path)
    await manual
    await hass.async_block_till_done()

    # One more request for tomorrow, and one coherent result.
    assert transport.call_count(path) == 2
    snapshot = manager.area_snapshot("SE4")
    assert snapshot is not None and snapshot.state == "ready"
    assert snapshot.today_document is not None and snapshot.tomorrow_document is not None


async def test_two_triggers_while_one_failing_run_is_open_still_make_one_run(
    hass: HomeAssistant,
    manager: PriceRefreshManager,
    transport: StubTransport,
    scheduler: FakeScheduler,
    clock: Clock,
) -> None:
    """One run per cycle, not one per trigger — and the failure count is what proves it.

    The wire cannot see two runs here: the repository coalesces the day fetch, so a duplicated cycle would
    still make one request. What *is* observable is the global backoff count, which only a finished cycle may
    advance. With the source failing and one run open, a timer tick plus a manual refresh must therefore advance it **once** — two runs would count
    the same failure twice and book the second backoff step instead of the first.
    """
    transport.serve_area()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    # From here the source fails, and the request for tomorrow (asked every cycle until held) is held open.
    path = transport.day_path("SE4", TOMORROW)
    transport.serve(path, 500, "the source is unwell")
    transport.hold(path)
    before = transport.call_count(path)

    # A timer tick opens the one run, which is now inside the held request; a manual refresh arrives
    # while it is still open. No wall-clock wait: one event-loop turn is all either needs to reach
    # the fetch, and the gate decides when that fetch may answer.
    refresh_timer(scheduler).fire()
    await transport.entered[path].wait()
    manual = asyncio.ensure_future(manager.async_refresh_area("SE4"))
    await asyncio.sleep(0)
    transport.release(path)
    await manual
    await hass.async_block_till_done()

    # One run, one request, one counted failure: the first backoff step rather than the second.
    assert transport.call_count(path) - before == 1
    assert refresh_timer(scheduler).when == clock.now + backoff_delay(1)


async def test_moving_an_owner_to_another_area_suppresses_the_old_areas_late_work(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    old = Listener()
    held = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=old)
    await hass.async_block_till_done()
    transport.serve("/v1/DE-LU/2026/09-22.json", 200, day_body("DE-LU", TODAY))

    # Tomorrow is not held, so the next SE4 run *will* request it again: that
    # request is what is held open while the owner moves to DE-LU.
    path = transport.day_path("SE4", TOMORROW)
    transport.serve(path, 200, day_body("SE4", TOMORROW))
    transport.hold(path)
    refresh_timer(scheduler).fire()
    await transport.entered[path].wait()
    moved = await manager.async_subscribe(owner_id="entry-a", area_id="DE-LU", listener=old)
    before = len(old.snapshots)
    transport.release(path)
    await hass.async_block_till_done()

    # The late SE4 completion is not delivered as this owner's current area, and it
    # does not install an SE4 timer.
    assert manager.owner_area("entry-a") == "DE-LU"
    assert transport.call_count(path) == 2
    assert manager.area_snapshot("SE4") is None
    # Nothing at all was delivered after the move finished: the SE4 completion was
    # suppressed rather than relabelled, and the last thing this owner heard was
    # about the area it actually moved to.
    assert {snapshot.area_id for snapshot in old.snapshots[before:]} <= {"DE-LU"}
    assert old.snapshots[-1].area_id == "DE-LU"
    # The shared cycle is still scheduled, for the area that is still subscribed.
    assert refresh_timer(scheduler) is not None
    assert manager.area_snapshot("DE-LU") is not None
    # The work that did finish is still in the shared repository, where it belongs.
    assert manager.area_snapshot("DE-LU") is not None
    held()
    moved()


async def test_the_final_unsubscribe_and_shutdown_are_both_terminal(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    listener = Listener()
    unsubscribe = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=listener)
    await hass.async_block_till_done()
    assert len(scheduler.pending) == 2

    # The last unsubscribe cancels this area's timers only.
    unsubscribe()
    assert scheduler.pending == []
    assert manager.area_snapshot("SE4") is None
    # @action is idempotent, and re-subscribing starts the work again.
    unsubscribe()
    again = Listener()
    handle = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=again)
    await hass.async_block_till_done()
    assert len(scheduler.pending) == 2
    assert manager.area_snapshot("SE4") is not None

    # A late completion during shutdown cannot resurrect anything either. The run is
    # held inside the request for tomorrow, which every run makes while it is not held.
    path = transport.day_path("SE4", TOMORROW)
    transport.hold(path)
    refresh_timer(scheduler).fire()
    await transport.entered[path].wait()
    await manager.async_shutdown()
    transport.release(path)
    await hass.async_block_till_done()

    assert scheduler.pending == []
    assert manager.area_snapshot("SE4") is None
    assert manager.manager_snapshot().shutdown is True
    with pytest.raises(RuntimeError):
        await manager.async_subscribe(owner_id="entry-b", area_id="SE4", listener=Listener())
    handle()


async def test_one_listener_raising_does_not_stop_the_others(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    broken = Listener(raises=True)
    fine = Listener()

    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=broken)
    await hass.async_block_till_done()
    await manager.async_subscribe(owner_id="entry-b", area_id="SE4", listener=fine)
    await hass.async_block_till_done()

    # Both were called, and the area still has its timers and its state.
    assert broken.snapshots and fine.snapshots
    snapshot = manager.area_snapshot("SE4")
    assert snapshot is not None and snapshot.subscriber_count == 2
    assert len(scheduler.pending) == 2


async def test_a_listener_hears_every_meaningful_change_and_nothing_else(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    listener = Listener()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=listener)
    await hass.async_block_till_done()
    delivered = len(listener.snapshots)

    # A refresh that changes nothing at all is silent.
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert len(listener.snapshots) == delivered

    # A changed price document is not silent.
    changed = json.loads(day_body("SE4", TODAY))
    changed["prices"] = [round(value + 0.05, 5) for value in changed["prices"]]
    transport.serve(transport.day_path("SE4", TODAY), 200, json.dumps(changed))
    repository = manager._repository  # noqa: SLF001 - the shared repository, to force a new body
    await repository.async_get_day("SE4", TODAY, refresh=True)
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert len(listener.snapshots) > delivered

    # A state change is not silent either: asking for today again fails, and last good is kept.
    delivered = len(listener.snapshots)
    transport.serve(transport.day_path("SE4", TODAY), 503, "")
    await repository.async_get_day("SE4", TODAY, refresh=True)
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert len(listener.snapshots) > delivered
    assert listener.snapshots[-1].state == "stale"
    assert listener.snapshots[-1].reason == "last_good_retained"


async def test_the_manager_is_domain_scoped_and_setting_it_up_fetches_nothing(
    hass: HomeAssistant, repository: PriceRepository, transport: StubTransport, clock: Clock, scheduler: FakeScheduler
) -> None:
    from custom_components.spotnav.pricing.price_refresh import async_setup_price_refresh

    first = await async_setup_price_refresh(hass, repository, now=clock, scheduler=scheduler)
    second = await async_setup_price_refresh(hass, repository, now=clock, scheduler=scheduler)

    # One manager, and no request: setting the integration up must never reach the
    # network, and an unreachable source must not fail it.
    assert first is second
    assert domain_data(hass).price_refresh is first
    assert transport.calls == []


def redacted(blob: object) -> bool:
    """True when nothing a dump may not contain appears anywhere inside it."""
    text = json.dumps(blob)
    forbidden = ["prices", "intervals", "<html", "http", "entry-a", "entry-b", "webhook", "token", "Traceback"]
    return all(needle not in text for needle in forbidden)


async def test_diagnostics_describe_the_manager_in_every_state_without_leaking_data(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    from homeassistant.config_entries import ConfigEntry
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics

    entry: ConfigEntry = MockConfigEntry(domain="spotnav", data={"webhook_id": "secret"})
    entry.add_to_hass(hass)
    domain_data(hass).price_refresh = manager

    # Before anything has been fetched: an honest, non-raising section.
    empty = await async_get_config_entry_diagnostics(hass, entry)
    assert empty["price_data"]["available"] is True
    assert empty["price_data"]["subscribed_areas"] == 0
    assert empty["price_data"]["areas"] == []
    assert redacted(empty["price_data"])

    # Ready today, waiting for tomorrow: counts, never owner ids.
    transport.serve_area()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await manager.async_subscribe(owner_id="entry-b", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    ready = (await async_get_config_entry_diagnostics(hass, entry))["price_data"]
    assert ready["running"] is True and ready["shutdown"] is False
    assert ready["subscribed_areas"] == 1
    area = ready["areas"][0]
    assert area["area"] == "SE4" and area["subscribers"] == 2
    assert area["state"] == "waiting_for_tomorrow" and area["reason"] == "waiting_for_tomorrow"
    assert area["today"] == TODAY.isoformat() and area["tomorrow"] == TOMORROW.isoformat()
    assert area["today_data"]["interval_count"] == 96
    assert area["today_data"]["state"] == "ready"
    assert area["currency"] == "SEK" and area["timezone"] == "Europe/Stockholm"
    assert area["next_attempt"] is not None and area["fetched_at"] is not None
    assert ready["catalogue"]["area_count"] == 54 and "index" not in ready
    # The test's repository has no real provider objects: no source, and nothing that names one.
    assert area["price_source"]["key"] is None
    assert redacted(ready)

    # Stale last-good (the connection drops: a `network` failure).
    transport.serve_chunks(transport.day_path("SE4", TODAY), 200, [b"{"], fail_after=0)
    await manager._repository.async_get_day("SE4", TODAY, refresh=True)  # noqa: SLF001
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    stale = (await async_get_config_entry_diagnostics(hass, entry))["price_data"]["areas"][0]
    assert stale["state"] == "stale" and stale["reason"] == "last_good_retained"
    assert redacted(stale)

    # And after shutdown, still useful and still silent.
    await manager.async_shutdown()
    stopped = (await async_get_config_entry_diagnostics(hass, entry))["price_data"]
    assert stopped["running"] is False and stopped["shutdown"] is True
    assert stopped["subscribed_areas"] == 0 and stopped["areas"] == []
    assert redacted(stopped)


async def test_loading_prices_calls_no_service_and_no_charging_controller(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    """Acquiring prices is passive: no service call, no charger command, no plan."""
    from pytest_homeassistant_custom_component.common import async_mock_service


    services = [
        async_mock_service(hass, domain, service)
        for domain, service in (
            ("switch", "turn_on"),
            ("switch", "turn_off"),
            ("number", "set_value"),
            ("homeassistant", "turn_on"),
        )
    ]
    transport.serve_area()
    transport.serve(transport.day_path("SE4", TOMORROW), 200, day_body("SE4", TOMORROW))

    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()
    for _ in range(3):
        refresh_timer(scheduler).fire()
        await hass.async_block_till_done()
    await manager.async_refresh_area("SE4")

    assert services == [[] for _ in services]
    # No charger controller was constructed for the subscribing owner either.
    assert not hass.config_entries.async_entries("spotnav")


async def test_re_subscribing_replaces_the_listener_and_tells_it_the_current_state(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport
) -> None:
    transport.serve_area()
    first_listener = Listener()
    first_handle = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=first_listener)
    await hass.async_block_till_done()
    assert first_listener.snapshots

    # The snapshot has not changed since, which is exactly the case where
    # deduplication would otherwise leave a replacement listener in the dark.
    replacement = Listener()
    second_handle = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=replacement)
    assert replacement.snapshots, "the replacement listener was not told the current state"
    assert replacement.snapshots[-1].state == first_listener.snapshots[-1].state
    assert replacement.snapshots[-1].today_document is not None

    # Still one subscriber, not two.
    assert manager.area_snapshot("SE4").subscriber_count == 1  # type: ignore[union-attr]

    # The old handle is harmless now, and the new one still works.
    first_handle()
    assert manager.owner_area("entry-a") == "SE4"
    assert manager.area_snapshot("SE4") is not None
    second_handle()
    second_handle()
    assert manager.owner_area("entry-a") is None
    assert manager.area_snapshot("SE4") is None


async def test_moving_an_owner_invalidates_every_older_handle(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport
) -> None:
    transport.serve_area()
    transport.serve("/v1/DE-LU/2026/09-22.json", 200, day_body("DE-LU", TODAY))

    on_se4 = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    on_de_lu = await manager.async_subscribe(owner_id="entry-a", area_id="DE-LU", listener=Listener())
    await hass.async_block_till_done()
    assert manager.owner_area("entry-a") == "DE-LU"

    # Every handle from before the move is inert, and the newest is idempotent.
    on_se4()
    on_se4()
    assert manager.owner_area("entry-a") == "DE-LU"
    assert manager.area_snapshot("SE4") is None
    on_de_lu()
    on_de_lu()
    assert manager.owner_area("entry-a") is None
    assert manager.area_snapshot("DE-LU") is None


async def test_a_failed_tomorrow_degrades_the_horizon_without_taking_today_away(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    """Ready -> degraded -> ready."""
    transport.serve_area()
    transport.serve(transport.day_path("SE4", TOMORROW), 200, day_body("SE4", TOMORROW))
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    healthy = manager.area_snapshot("SE4")
    assert healthy is not None and healthy.state == "ready"

    # Tomorrow's document disappears, and its day cache is cleared by asking the
    # repository for a fresh one: today must stay usable and the horizon must be
    # reported as impaired rather than the whole area as unavailable.
    repository = manager._repository  # noqa: SLF001 - the shared repository this test owns
    transport.serve(transport.day_path("SE4", TOMORROW), 500, "")
    await repository.async_get_day("SE4", TOMORROW, refresh=True)

    degraded = manager.area_snapshot("SE4")
    assert degraded is not None
    assert degraded.state == "degraded" and degraded.reason == "tomorrow_stale"
    assert degraded.today_state == "ready" and degraded.today_document is not None
    assert degraded.tomorrow_state == "stale"

    # And it recovers on the next successful cycle.
    transport.serve(transport.day_path("SE4", TOMORROW), 200, day_body("SE4", TOMORROW))
    await repository.async_get_day("SE4", TOMORROW, refresh=True)
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    recovered = manager.area_snapshot("SE4")
    assert recovered is not None and recovered.state == "ready"


async def test_no_subscribers_means_no_timer_at_all(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    handle = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()
    assert len(scheduler.pending) == 2  # the global poll and the area's midnight

    # The last unsubscribe takes both away: nothing is subscribed, so nothing is
    # polled and nothing global is left running.
    handle()
    assert scheduler.pending == []
    assert manager.manager_snapshot().areas == ()


async def test_a_global_transport_failure_is_reported_to_every_area_once(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler, clock: Clock
) -> None:
    for area in ("SE4", "DE-LU"):
        transport.serve(transport.day_path(area, TODAY), 503, "")
    # Held open, so both subscriptions belong to the *same* failed cycle.
    transport.hold(transport.day_path("SE4", TODAY))
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM)
    await transport.entered[transport.day_path("SE4", TODAY)].wait()
    await manager.async_subscribe(owner_id="entry-b", area_id="DE-LU", listener=Listener(), tz=BERLIN)
    transport.release(transport.day_path("SE4", TODAY))
    await hass.async_block_till_done()

    for area in ("SE4", "DE-LU"):
        snapshot = manager.area_snapshot(area)
        assert snapshot is not None
        assert snapshot.today_state == "unavailable"
        assert snapshot.today_snapshot.attempt_error == "http_status"
    # Two areas, one failed cycle: the backoff is the installation's, not each area's, so it steps once and the
    # next poll is a minute away.
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=60)

    # The next cycle is the second step, not the second step *per area*.
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert transport.call_count(transport.day_path("SE4", TODAY)) == 2
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=120)


async def test_one_areas_day_failure_does_not_block_another_areas_fetch(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area("SE4")
    transport.serve("/v1/DE-LU/2026/09-22.json", 500, "")

    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await manager.async_subscribe(owner_id="entry-b", area_id="DE-LU", listener=Listener())
    await hass.async_block_till_done()

    # DE-LU's day failed; SE4's plain day fetch still happened and is shown.
    assert transport.call_count(transport.day_path("DE-LU", TODAY)) == 1
    se4 = manager.area_snapshot("SE4")
    assert se4 is not None and se4.today_document is not None and se4.today_state == "ready"
    broken = manager.area_snapshot("DE-LU")
    assert broken is not None and broken.today_state == "unavailable"
    # A failed attempt leaves nothing to rely on about today: `index_unavailable`, the reason of an unknown answer.
    assert broken.state == "unavailable" and broken.reason == "index_unavailable"


async def test_diagnostics_publish_the_tri_state_and_never_a_guess(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    from homeassistant.config_entries import ConfigEntry
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics

    entry: ConfigEntry = MockConfigEntry(domain="spotnav", data={})
    entry.add_to_hass(hass)
    domain_data(hass).price_refresh = manager
    transport.serve_area()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    async def price_data() -> dict:
        return (await async_get_config_entry_diagnostics(hass, entry))["price_data"]

    # Tomorrow was answered "not published yet": `not_listed`, and the convenience field says
    # `False` because the source is currently saying so.
    listed = (await price_data())["areas"][0]
    assert listed["tomorrow_authority"] == "not_listed"
    assert listed["tomorrow_published"] is False
    assert listed["today_authority"] == "listed"
    assert listed["state"] == "waiting_for_tomorrow"

    # The next attempt for tomorrow fails: authority is unknown, and the convenience field is `None` rather
    # than a lie in either direction.
    transport.serve(transport.day_path("SE4", TOMORROW), 503, "")
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    stale = (await price_data())["areas"][0]
    assert stale["tomorrow_authority"] == "unknown"
    assert stale["tomorrow_published"] is None
    assert stale["state"] == "degraded" and stale["reason"] == "index_unavailable"

    # Tomorrow is out: `listed`, and the day was fetched.
    transport.serve(transport.day_path("SE4", TOMORROW), 200, day_body("SE4", TOMORROW))
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    published = (await price_data())["areas"][0]
    assert published["tomorrow_authority"] == "listed"
    assert published["tomorrow_published"] is True
    assert published["state"] == "ready"
    # Day-level detail stays visible beside the combined result.
    assert published["tomorrow_data"]["state"] == "ready"
    assert published["tomorrow_data"]["interval_count"] == 96

    # And no subscription token or owner id is anywhere in the dump.
    assert redacted(await price_data())


async def test_a_second_owner_of_an_active_area_does_not_poll_again(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    runs = count_cycles(manager)
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    # The initial global cycle has fully finished.
    appointment = refresh_timer(scheduler).when
    assert runs == [1]

    # A second owner of the same area: current state, and nothing else.
    late = Listener()
    await manager.async_subscribe(owner_id="entry-b", area_id="SE4", listener=late)
    await hass.async_block_till_done()
    assert late.snapshots, "a new owner was not told the current state"
    assert runs == [1]
    assert refresh_timer(scheduler).when == appointment

    # The same owner replacing its listener: also nothing but current state.
    replacement = Listener()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=replacement)
    await hass.async_block_till_done()
    assert replacement.snapshots, "a replacement listener was not told the current state"
    assert runs == [1]
    assert refresh_timer(scheduler).when == appointment
    assert manager.area_snapshot("SE4").subscriber_count == 2  # type: ignore[union-attr]

    # A genuinely *new* area does start the shared cycle: its days have never been
    # fetched, so there is fan-out work to do.
    transport.serve("/v1/DE-LU/2026/09-22.json", 200, day_body("DE-LU", TODAY))
    await manager.async_subscribe(owner_id="entry-c", area_id="DE-LU", listener=Listener())
    await hass.async_block_till_done()
    assert runs == [2]
    assert manager.area_snapshot("DE-LU") is not None


async def test_topology_changes_do_not_advance_or_reset_the_backoff(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler, clock: Clock
) -> None:
    """Only a finished cycle may move the count; subscriptions merely reschedule."""
    probe = transport.day_path("SE4", TODAY)
    transport.serve(probe, 503, "")
    transport.serve(transport.day_path("DE-LU", TODAY), 503, "")

    # Two areas, one failed cycle: the count is at one step, and the appointment is a
    # minute out.
    transport.hold(probe)
    handle_a = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM)
    await transport.entered[probe].wait()
    handle_b = await manager.async_subscribe(owner_id="entry-b", area_id="DE-LU", listener=Listener(), tz=BERLIN)
    transport.release(probe)
    await hass.async_block_till_done()
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=60)

    # Removing one of the two areas while the source is in backoff: the appointment is
    # recomputed from the same count, not advanced by a "failure" that never happened.
    handle_b()
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=60)
    assert manager.area_snapshot("DE-LU") is None

    # Adding another owner to the remaining area changes nothing either.
    handle_c = await manager.async_subscribe(
        owner_id="entry-c", area_id="SE4", listener=Listener(), tz=STOCKHOLM
    )
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=60)

    # The next *actual* failed cycle advances exactly one step, to two minutes.
    attempts = transport.call_count(probe)
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert transport.call_count(probe) == attempts + 1
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=120)

    # The final unsubscribes cancel the timer without touching the recorded count.
    handle_a()
    handle_c()
    assert scheduler.pending == []

    # Coming back does not reset the count either. The area is freshly activated, so
    # it does start a cycle of its own — and that cycle advances the count *once*,
    # from the two steps the earlier failures earned to the third, rather than back
    # to a first step of one minute.
    back = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM)
    # The activation's own cycle fails again and is awaited here, so the appointment
    # below is the one *that* cycle earned — the third step, not a fresh first.
    await hass.async_block_till_done()
    assert transport.call_count(probe) == attempts + 2
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=300)
    back()


# Real scheduler boundary: the tests below use the production pair (`async_track_point_in_utc_time`
# and `dt_util.utcnow`) driven by `async_fire_time_changed`, so the event-loop boundary itself is tested.
# A callback not marked event-loop-safe runs on a `SyncWorker` thread, where `hass.async_create_task`
# is refused and the appointment chain would silently die.


@pytest.fixture
def live_repository(hass: HomeAssistant, transport: StubTransport) -> PriceRepository:
    """The repository on Home Assistant's *own* clock, so the manager's real timers agree with it."""
    return stub_repository(hass, transport, store=StoreDouble())


@pytest.fixture
def live_manager(hass: HomeAssistant, live_repository: PriceRepository) -> PriceRefreshManager:
    """The manager on the production timer path: no injected clock and no recording scheduler."""
    return PriceRefreshManager(hass, live_repository)


async def test_the_default_timer_runs_on_the_event_loop_and_keeps_the_chain_alive(
    hass: HomeAssistant,
    live_manager: PriceRefreshManager,
    transport: StubTransport,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One tick: one shared cycle, on the loop, and a later appointment afterwards."""
    transport.serve_area()
    runs = count_cycles(live_manager)
    await live_manager.async_subscribe(
        owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM
    )
    await hass.async_block_till_done()

    first = live_manager.area_snapshot("SE4")
    assert first is not None and first.next_attempt is not None
    fired_at = first.next_attempt
    index_calls = runs[0]

    # Home Assistant's own helper fires the appointment it was given. Nothing is called directly.
    async_fire_time_changed(hass, fired_at)
    await hass.async_block_till_done()

    # The event-loop boundary is asserted first: an unmarked callback is dispatched to a `SyncWorker` thread and
    # logs a warning (or raises `RuntimeError`). Nothing here greps the source.
    assert "from a thread other than the event loop" not in caplog.text, caplog.text
    assert "RuntimeError" not in caplog.text, caplog.text

    # One tick, one shared cycle, and an appointment after it: the chain is alive.
    assert runs[0] == index_calls + 1
    after = live_manager.area_snapshot("SE4")
    assert after is not None and after.next_attempt is not None
    assert after.next_attempt > dt_util.utcnow()

    # A second fired appointment performs a second cycle through the same path.
    async_fire_time_changed(hass, after.next_attempt)
    await hass.async_block_till_done()
    assert runs[0] == index_calls + 2
    third = live_manager.area_snapshot("SE4")
    assert third is not None and third.next_attempt is not None
    assert third.next_attempt > dt_util.utcnow()

    # And still no boundary crossing, through the second cycle either.
    assert "from a thread other than the event loop" not in caplog.text, caplog.text
    assert "RuntimeError" not in caplog.text, caplog.text

    # The test owns the manager, so it hands it back: Home Assistant fails a test that leaves one of
    # its timers installed, which is the same discipline the manager's own shutdown rule follows.
    await live_manager.async_shutdown()
    await hass.async_block_till_done()


async def test_the_rollover_callback_publishes_rearms_and_runs_on_the_event_loop(
    hass: HomeAssistant,
    live_manager: PriceRefreshManager,
    transport: StubTransport,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Local midnight: the new date keys are published, one shared run starts, and it re-arms."""
    transport.serve_area()
    runs = count_cycles(live_manager)
    listener = Listener()
    await live_manager.async_subscribe(
        owner_id="entry-a", area_id="SE4", listener=listener, tz=STOCKHOLM
    )
    await hass.async_block_till_done()

    midnight = next_local_midnight(dt_util.utcnow(), STOCKHOLM)
    index_calls = runs[0]
    notified = len(listener.snapshots)

    async_fire_time_changed(hass, midnight)
    await hass.async_block_till_done()

    # The boundary, first again: a rollover callback that is not event-loop-safe is dispatched to a
    # worker and takes the same path to the same dead end.
    assert "from a thread other than the event loop" not in caplog.text, caplog.text
    assert "RuntimeError" not in caplog.text, caplog.text

    # Published (the listener was told again, with the new date keys -- the rollover's own effect, and
    # the only thing that publishes them), and the shared run started. The cycle count *increases*
    # rather than gaining exactly one: local midnight is a distant instant, so Home Assistant also
    # fires the shared refresh appointment that had come due on the way there.
    assert len(listener.snapshots) > notified
    assert live_manager.area_snapshot("SE4") is not None
    assert runs[0] > index_calls
    scheduled = live_manager.area_snapshot("SE4")
    assert scheduled is not None and scheduled.next_attempt is not None
    assert scheduled.next_attempt > dt_util.utcnow()

    # It re-armed for the area's next local midnight, through the same partial-and-scheduler path:
    # firing just past that instant runs a second rollover, which an un-armed one-shot could not.
    index_calls = runs[0]
    notified = len(listener.snapshots)
    async_fire_time_changed(hass, midnight + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert len(listener.snapshots) > notified
    assert runs[0] > index_calls

    assert "from a thread other than the event loop" not in caplog.text, caplog.text
    assert "RuntimeError" not in caplog.text, caplog.text
    await live_manager.async_shutdown()
    await hass.async_block_till_done()


async def test_a_shutdown_manager_is_inert_through_the_real_timer_path(
    hass: HomeAssistant,
    live_manager: PriceRefreshManager,
    transport: StubTransport,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancelled and shutdown: a former appointment fires, and nothing at all happens."""
    transport.serve_area()
    runs = count_cycles(live_manager)
    await live_manager.async_subscribe(
        owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM
    )
    await hass.async_block_till_done()
    snapshot = live_manager.area_snapshot("SE4")
    assert snapshot is not None and snapshot.next_attempt is not None
    fired_at = snapshot.next_attempt

    await live_manager.async_shutdown()
    await hass.async_block_till_done()
    index_calls = runs[0]

    async_fire_time_changed(hass, fired_at)
    await hass.async_block_till_done()

    # No request, no new appointment and no resurrection: a shutdown manager is inert, and every
    # timer it had was cancelled rather than left to fire into a finished installation.
    assert runs[0] == index_calls
    assert live_manager.manager_snapshot().shutdown is True
    assert live_manager.area_snapshot("SE4") is None
    assert "from a thread other than the event loop" not in caplog.text
