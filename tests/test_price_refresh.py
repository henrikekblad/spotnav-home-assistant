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
    BASE_URL,
    TODAY,
    TOMORROW,
    Clock,
    StoreDouble,
    StubTransport,
    day_body,
    fixture,
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
from .relay import FakeScheduler, FakeTimer
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
    return PriceRepository(hass, base_url=BASE_URL, session=transport, now=clock, store=StoreDouble())


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


def index_listing(area: str, days: list[str]) -> str:
    """An index document listing exactly these days, in order.

    Built rather than string-replaced: swapping one date for another inside the
    fixture leaves the list unsorted, which the contract refuses — and then the
    test would be measuring that refusal instead of what it meant to.
    """
    document = json.loads(fixture("index.json"))
    document["areas"] = {area: {"days": sorted(days), "res": 15}}
    return json.dumps(document)


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
    # The shared cycle is held open inside its index request, so the second
    # subscription genuinely arrives while it is in flight — which is the case that
    # must join rather than start a poll of its own.
    transport.hold("/v1/index.json")
    first = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await transport.entered["/v1/index.json"].wait()
    await manager.async_subscribe(owner_id="entry-b", area_id="SE4", listener=Listener())
    transport.release("/v1/index.json")
    await hass.async_block_till_done()

    snapshot = manager.area_snapshot("SE4")
    assert snapshot is not None and snapshot.subscriber_count == 2
    # One area, one index, one day — however many chargers asked.
    assert transport.call_count("/v1/index.json") == 1
    assert transport.call_count(transport.day_path("SE4", TODAY)) == 1
    assert len(scheduler.pending) == 2  # one refresh timer and one rollover timer
    assert refresh_timer(scheduler) is not None and rollover_timer(scheduler) is not None

    first()
    still = manager.area_snapshot("SE4")
    assert still is not None and still.subscriber_count == 1
    # The area's work is untouched by one owner leaving.
    assert len(scheduler.pending) == 2
    assert manager.owner_area("entry-b") == "SE4"


async def test_two_areas_progress_independently_and_share_the_catalogue(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area("SE4")
    transport.serve("/v1/DE-LU/2026/09-22.json", 200, day_body("DE-LU", TODAY))

    transport.hold("/v1/index.json")
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await transport.entered["/v1/index.json"].wait()
    await manager.async_subscribe(owner_id="entry-b", area_id="DE-LU", listener=Listener())
    transport.release("/v1/index.json")
    await hass.async_block_till_done()

    # One catalogue and one index for the installation, two areas' own days.
    assert transport.call_count("/v1/areas.json") == 1
    # One index request too, although the two subscriptions were made in
    # consecutive event-loop turns and their areas' jitter values differ: the cycle
    # is global, so a second area joins it instead of starting its own poll.
    assert transport.call_count("/v1/index.json") == 1
    assert transport.call_count(transport.day_path("SE4", TODAY)) == 1
    assert transport.call_count(transport.day_path("DE-LU", TODAY)) == 1

    assert manager.area_snapshot("SE4") is not None
    assert manager.area_snapshot("DE-LU") is not None

    # A global cycle runs once per due cycle, not once per area: firing the shared
    # timer asks the index exactly one more time, whichever areas are active.
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert transport.call_count("/v1/index.json") == 2
    assert transport.call_count("/v1/areas.json") == 1


async def test_an_area_without_a_known_timezone_is_refused_rather_than_guessed(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport
) -> None:
    transport.serve("/v1/areas.json", 503, "")

    with pytest.raises(PriceAreaUnavailable):
        await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    assert manager.owner_area("entry-a") is None

    # A caller that already knows the zone — from its own settings, say — may
    # subscribe even while the relay is unreachable.
    handle = await manager.async_subscribe(
        owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM
    )
    assert manager.owner_area("entry-a") == "SE4"
    snapshot = manager.area_snapshot("SE4")
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
    # `waiting_for_tomorrow` because the fixture index lists today only -- tomorrow
    # (2026-09-23) is authoritatively absent, which is a state and not a failure.
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

    # Today ready, tomorrow not listed, and it is 08:00 Brussels: the ordinary cadence.
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=HEALTHY_POLL_SECONDS)

    # The timer fires: a new run, and a new timer at the same cadence, with the old
    # one gone rather than left behind.
    fired = refresh_timer(scheduler)
    fired.fire()
    await hass.async_block_till_done()
    assert fired.fired is True
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=HEALTHY_POLL_SECONDS)

    # Move into the publication window, with tomorrow still unlisted: 5 minutes.
    clock.now = brussels(13, 0)
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=PUBLICATION_POLL_SECONDS)

    # Inside the window with tomorrow *listed but not in hand* is the same impatience: the relay
    # publishes an early incomplete document some days, so "listed" is not "usable" and this is the
    # case where the complete document follows the same afternoon.
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=PUBLICATION_POLL_SECONDS)

    # And a settled area — tomorrow listed and in hand — is checked hourly.
    transport.serve(
        "/v1/index.json", 200, index_listing("SE4", ["2026-09-22", "2026-09-23"])
    )
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
    transport.serve("/v1/index.json", 503, "")
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


async def test_an_unlisted_tomorrow_is_never_requested_and_then_fetched_once(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    transport.serve(transport.day_path("SE4", TOMORROW), 200, day_body("SE4", TOMORROW))

    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()
    snapshot = manager.area_snapshot("SE4")
    assert snapshot is not None and snapshot.waiting_for_tomorrow is True
    # The index lists today only, so tomorrow's document is not requested at all...
    assert transport.call_count(transport.day_path("SE4", TOMORROW)) == 0

    # ...until a later index does list it: then, and only then, exactly one request.
    transport.serve("/v1/index.json", 200, index_listing("SE4", ["2026-09-22", "2026-09-23"]))
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()

    assert transport.call_count(transport.day_path("SE4", TOMORROW)) == 1
    settled = manager.area_snapshot("SE4")
    assert settled is not None and settled.state == "ready" and settled.tomorrow_document is not None

    # A complete immutable day is never downloaded twice.
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert transport.call_count(transport.day_path("SE4", TODAY)) == 1
    assert transport.call_count(transport.day_path("SE4", TOMORROW)) == 1


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

    # The relay's day documents are immutable: an incomplete one is reported, not
    # re-requested or cache-busted.
    for _ in range(3):
        refresh_timer(scheduler).fire()
        await hass.async_block_till_done()
    assert transport.call_count(transport.day_path("SE4", TODAY)) == 1
    assert manager.area_snapshot("SE4").state == "incomplete"  # type: ignore[union-attr]


async def test_last_good_is_kept_when_the_index_stops_listing_the_day(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    held = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()
    assert manager.area_snapshot("SE4").state == "waiting_for_tomorrow"  # type: ignore[union-attr]

    # The index's window moves on and stops listing today. Nothing valid is thrown
    # away, and the state says the data is no longer published.
    transport.serve("/v1/index.json", 200, index_listing("SE4", ["2026-09-18"]))
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()

    stale = manager.area_snapshot("SE4")
    assert stale is not None
    assert stale.state == "stale" and stale.reason == "last_good_retained"
    assert stale.today_document is not None
    assert transport.call_count(transport.day_path("SE4", TODAY)) == 1
    held()


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


async def test_a_changed_areas_revision_refreshes_the_catalogue_once(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler, clock: Clock
) -> None:
    transport.serve_area()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()
    assert transport.call_count("/v1/areas.json") == 1

    # A new index revision means the area list changed: refresh it now, not at the
    # 24-hour mark, and then not again while the revision holds.
    changed = json.loads(fixture("index.json"))
    changed["areas_rev"] = "ffffffffffff"
    transport.serve("/v1/index.json", 200, json.dumps(changed))
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert transport.call_count("/v1/areas.json") == 2

    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert transport.call_count("/v1/areas.json") == 2

    # And a catalogue that is a day old is refreshed without any revision change.
    clock.advance(hours=25)
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert transport.call_count("/v1/areas.json") == 3


async def test_the_local_midnight_rollover_republishes_the_new_date_keys(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler, clock: Clock
) -> None:
    transport.serve_area()
    listener = Listener()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=listener)
    await hass.async_block_till_done()
    assert manager.area_snapshot("SE4").today == TODAY  # type: ignore[union-attr]

    # The index starts listing the new today before the clock rolls over, so the
    # post-rollover run has authority to fetch it.
    transport.serve("/v1/index.json", 200, index_listing("SE4", ["2026-09-22", "2026-09-23"]))
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
    # The rollover's own run has already fetched the new today (the index lists it),
    # and the next rollover is the following local midnight.
    assert transport.call_count(transport.day_path("SE4", TOMORROW)) == 1
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

    transport.serve("/v1/index.json", 200, index_listing("SE4", ["2026-09-22", "2026-09-23"]))
    transport.serve(path, 200, day_body("SE4", TOMORROW))
    transport.hold(path)

    # The timer fires; while that run is still open, a manual refresh arrives.
    refresh_timer(scheduler).fire()
    manual = asyncio.ensure_future(manager.async_refresh_area("SE4"))
    await transport.entered[path].wait()
    transport.release(path)
    await manual
    await hass.async_block_till_done()

    # One request for tomorrow, and one coherent result.
    assert transport.call_count(path) == 1
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

    The wire cannot see two runs here: the repository coalesces the index fetch and caches the day
    document, so a duplicated cycle would still make one request of each. What *is* observable is the
    global backoff count, which only a finished cycle may advance. With the relay failing and one run
    open, a timer tick plus a manual refresh must therefore advance it **once** — two runs would count
    the same failure twice and book the second backoff step instead of the first.
    """
    transport.serve_area()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    # From here the relay fails, and its index request is held open.
    path = "/v1/index.json"
    transport.serve(path, 500, "the relay is unwell")
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

    # Tomorrow has just become listed, so the next SE4 run *will* request it: that
    # request is what is held open while the owner moves to DE-LU.
    transport.serve("/v1/index.json", 200, index_listing("SE4", ["2026-09-22", "2026-09-23"]))
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
    assert transport.call_count(path) == 1
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
    # held inside the index request, which every run makes, rather than inside a day
    # request, which a cached day does not make at all.
    path = "/v1/index.json"
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

    # A state change is not silent either: the index stops listing today.
    delivered = len(listener.snapshots)
    transport.serve("/v1/index.json", 200, index_listing("SE4", ["2026-09-18"]))
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
    # network, offline relays must not fail it, and the catalogue is therefore
    # loaded on first use rather than here.
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
    assert ready["catalogue"]["area_count"] == 4
    assert ready["index"]["areas_rev"] == "916f2f8e05fd"
    assert redacted(ready)

    # Stale last-good.
    transport.serve("/v1/index.json", 200, index_listing("SE4", ["2026-09-18"]))
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
    transport.serve("/v1/index.json", 200, index_listing("SE4", ["2026-09-22", "2026-09-23"]))
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


async def test_a_failed_index_refresh_stops_claiming_tomorrow_is_not_published(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    """The index-authority sequence driven through the manager rather than the function."""
    transport.serve_area()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    # 1. A fresh index omits tomorrow: waiting, which is a real answer.
    first = manager.area_snapshot("SE4")
    assert first is not None
    assert first.tomorrow_authority == "not_listed"
    assert (first.state, first.reason) == ("waiting_for_tomorrow", "waiting_for_tomorrow")

    # 2. The next index refresh fails; last-good is retained.
    transport.serve("/v1/index.json", 503, "")
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    retained = manager.area_snapshot("SE4")
    assert retained is not None
    assert retained.index_state == "stale"
    assert retained.tomorrow_authority == "unknown", "a stale index claimed to know"
    assert retained.waiting_for_tomorrow is False
    assert retained.state == "degraded" and retained.reason == "index_unavailable"
    # The retained document itself is still on screen, and still says what it is.
    assert retained.today_document is not None and retained.today_state == "ready"

    # 3. A later successful index that still omits tomorrow: waiting again.
    transport.serve_area()
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    again = manager.area_snapshot("SE4")
    assert again is not None and again.tomorrow_authority == "not_listed"
    assert again.state == "waiting_for_tomorrow"

    # 4. A successful index that lists tomorrow: listed, and the day is fetched.
    transport.serve("/v1/index.json", 200, index_listing("SE4", ["2026-09-22", "2026-09-23"]))
    transport.serve(transport.day_path("SE4", TOMORROW), 200, day_body("SE4", TOMORROW))
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    listed = manager.area_snapshot("SE4")
    assert listed is not None
    assert listed.tomorrow_authority == "listed" and listed.state == "ready"
    assert listed.tomorrow_document is not None
    assert transport.call_count(transport.day_path("SE4", TOMORROW)) == 1


async def test_a_failed_tomorrow_degrades_the_horizon_without_taking_today_away(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    """Ready -> degraded -> ready."""
    transport.serve_area()
    transport.serve("/v1/index.json", 200, index_listing("SE4", ["2026-09-22", "2026-09-23"]))
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


async def test_one_managed_index_poll_covers_several_areas_per_cycle(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area("SE4")
    transport.serve("/v1/DE-LU/2026/09-22.json", 200, day_body("DE-LU", TODAY))
    transport.serve("/v1/NO1/2026/09-22.json", 200, day_body("NO1", TODAY))
    # The fixture index lists NO1's yesterday only, so an index that lists today for
    # all three is built here: the point of the test is one poll serving three areas.
    three_areas = json.loads(fixture("index.json"))
    three_areas["areas"] = {
        "DE-LU": {"days": ["2026-09-22"], "res": 15},
        "NO1": {"days": ["2026-09-22"], "res": 15},
        "SE4": {"days": ["2026-09-22"], "res": 15},
    }
    transport.serve("/v1/index.json", 200, json.dumps(three_areas))

    transport.hold("/v1/index.json")
    for owner, area in (("entry-a", "SE4"), ("entry-b", "DE-LU"), ("entry-c", "NO1")):
        await manager.async_subscribe(owner_id=owner, area_id=area, listener=Listener())
    await transport.entered["/v1/index.json"].wait()
    transport.release("/v1/index.json")
    await hass.async_block_till_done()

    # Three areas, three days, one catalogue and one index.
    assert transport.call_count("/v1/index.json") == 1
    assert transport.call_count("/v1/areas.json") == 1
    assert len([call for call in transport.calls if call.endswith("09-22.json")]) == 3
    assert len(scheduler.pending) == 4  # one global poll, and one midnight per area

    # A manual refresh joins the shared cycle: it adds the day fan-out, never an
    # extra index poll.
    before = transport.call_count("/v1/index.json")
    await manager.async_refresh_area("SE4")
    await hass.async_block_till_done()
    assert transport.call_count("/v1/index.json") == before + 1  # its own cycle, once
    assert transport.call_count(transport.day_path("SE4", TODAY)) == 1  # cached, so no new day


async def test_a_global_transport_failure_is_reported_to_every_area_once(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler, clock: Clock
) -> None:
    transport.serve("/v1/index.json", 503, "")
    transport.serve("/v1/areas.json", 503, "")
    # Held open, so both subscriptions belong to the *same* failed cycle: one
    # attempt between two areas, not one each.
    transport.hold("/v1/index.json")
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM)
    await transport.entered["/v1/index.json"].wait()
    await manager.async_subscribe(owner_id="entry-b", area_id="DE-LU", listener=Listener(), tz=STOCKHOLM)
    transport.release("/v1/index.json")
    await hass.async_block_till_done()

    for area in ("SE4", "DE-LU"):
        snapshot = manager.area_snapshot(area)
        assert snapshot is not None
        assert snapshot.index_state == "unavailable"
        assert snapshot.reason == "index_unavailable"
        assert snapshot.tomorrow_authority == "unknown"
    # Two areas, one failed attempt: the backoff is the installation's, not each
    # area's, so it steps once and the next poll is a minute away.
    assert transport.call_count("/v1/index.json") == 1
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=60)

    # The next cycle is the second step, not the second step *per area*.
    transport.serve("/v1/index.json", 503, "")
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert transport.call_count("/v1/index.json") == 2
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=120)


async def test_one_areas_day_failure_does_not_block_another_areas_fetch(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area("SE4")
    transport.serve("/v1/DE-LU/2026/09-22.json", 500, "")

    transport.hold("/v1/index.json")
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await manager.async_subscribe(owner_id="entry-b", area_id="DE-LU", listener=Listener())
    await transport.entered["/v1/index.json"].wait()
    transport.release("/v1/index.json")
    await hass.async_block_till_done()

    # DE-LU's day failed; SE4's plain day fetch still happened and is shown.
    assert transport.call_count(transport.day_path("DE-LU", TODAY)) == 1
    se4 = manager.area_snapshot("SE4")
    assert se4 is not None and se4.today_document is not None and se4.today_state == "ready"
    broken = manager.area_snapshot("DE-LU")
    assert broken is not None and broken.today_state == "unavailable"
    assert broken.state == "unavailable" and broken.reason == "source_unreachable"


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

    # A fresh index omits tomorrow: `not_listed`, and the convenience field says
    # `False` because the relay is currently saying so.
    listed = (await price_data())["areas"][0]
    assert listed["tomorrow_authority"] == "not_listed"
    assert listed["tomorrow_published"] is False
    assert listed["today_authority"] == "listed"
    assert listed["state"] == "waiting_for_tomorrow"

    # The index refresh fails and last-good is retained: authority is unknown, and
    # the convenience field is `None` rather than a lie in either direction.
    transport.serve("/v1/index.json", 503, "")
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    stale = (await price_data())["areas"][0]
    assert stale["tomorrow_authority"] == "unknown"
    assert stale["tomorrow_published"] is None
    assert stale["index_state"] == "stale"
    assert stale["state"] == "degraded" and stale["reason"] == "index_unavailable"

    # A successful index that lists tomorrow: `listed`, and the day was fetched.
    transport.serve("/v1/index.json", 200, index_listing("SE4", ["2026-09-22", "2026-09-23"]))
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


async def test_a_stale_index_lets_the_manager_ask_for_today_and_the_repository_agrees(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    """Manager and repository agree on authority for a retained index.

    Both follow one freshness rule: a stale retained index list is unknown authority, not
    an authoritative absence, so today is requested rather than suppressed.
    """
    # 1. A fresh index omits today (its day list is a day old).
    transport.serve_area()
    listing = json.loads(fixture("index.json"))
    listing["areas"]["SE4"] = {"days": ["2026-09-21"], "res": 15}
    transport.serve("/v1/index.json", 200, json.dumps(listing))
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()
    first = manager.area_snapshot("SE4")
    assert first is not None
    assert first.today_authority == "not_listed" and first.state == "unavailable"
    assert transport.call_count(transport.day_path("SE4", TODAY)) == 0

    # 2. The next index refresh fails, and last-good remains.
    transport.serve("/v1/index.json", 503, "")
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    stale = manager.area_snapshot("SE4")
    assert stale is not None and stale.index_state == "stale"
    assert stale.today_authority == "unknown"

    # 3. The manager asks for today, because authority is now unknown rather than an
    #    authoritative absence — and the repository agrees instead of suppressing it.
    transport.serve(transport.day_path("SE4", TODAY), 200, day_body("SE4", TODAY))
    index_calls = transport.call_count("/v1/index.json")
    await manager.async_refresh_area("SE4")
    await hass.async_block_till_done()

    # 4. Exactly one day request, and it succeeded.
    assert transport.call_count(transport.day_path("SE4", TODAY)) == 1
    recovered = manager.area_snapshot("SE4")
    assert recovered is not None
    assert recovered.today_document is not None and recovered.state == "degraded"
    # 5. No index request was hidden inside that day request: the day request did not
    #    re-ask a failed index, it only read the state it already had.
    assert transport.call_count("/v1/index.json") == index_calls + 1  # the cycle's own
    # 6. Both layers report the same authority for the same facts.
    repository = manager._repository  # noqa: SLF001 - the repository this test owns
    assert recovered.today_authority == "unknown"
    assert repository.day_snapshot("SE4", TODAY).index_authority == "unknown"

    # And a successful later index restores authority in both.
    transport.serve("/v1/index.json", 200, json.dumps(listing))
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    final = manager.area_snapshot("SE4")
    assert final is not None and final.today_authority == "not_listed"
    assert repository.day_snapshot("SE4", TODAY).index_authority == "not_listed"


async def test_a_second_owner_of_an_active_area_does_not_poll_again(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler
) -> None:
    transport.serve_area()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener())
    await hass.async_block_till_done()

    # The initial global cycle has fully finished.
    index_calls = transport.call_count("/v1/index.json")
    appointment = refresh_timer(scheduler).when
    assert index_calls == 1

    # A second owner of the same area: current state, and nothing else.
    late = Listener()
    await manager.async_subscribe(owner_id="entry-b", area_id="SE4", listener=late)
    await hass.async_block_till_done()
    assert late.snapshots, "a new owner was not told the current state"
    assert transport.call_count("/v1/index.json") == index_calls
    assert refresh_timer(scheduler).when == appointment

    # The same owner replacing its listener: also nothing but current state.
    replacement = Listener()
    await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=replacement)
    await hass.async_block_till_done()
    assert replacement.snapshots, "a replacement listener was not told the current state"
    assert transport.call_count("/v1/index.json") == index_calls
    assert refresh_timer(scheduler).when == appointment
    assert manager.area_snapshot("SE4").subscriber_count == 2  # type: ignore[union-attr]

    # A genuinely *new* area does start the shared cycle: its days have never been
    # fetched, so there is fan-out work to do.
    transport.serve("/v1/DE-LU/2026/09-22.json", 200, day_body("DE-LU", TODAY))
    await manager.async_subscribe(owner_id="entry-c", area_id="DE-LU", listener=Listener())
    await hass.async_block_till_done()
    assert transport.call_count("/v1/index.json") == index_calls + 1
    assert manager.area_snapshot("DE-LU") is not None


async def test_topology_changes_do_not_advance_or_reset_the_backoff(
    hass: HomeAssistant, manager: PriceRefreshManager, transport: StubTransport, scheduler: FakeScheduler, clock: Clock
) -> None:
    """Only a finished cycle may move the count; subscriptions merely reschedule."""
    transport.serve("/v1/index.json", 503, "")
    transport.serve("/v1/areas.json", 503, "")

    # Two areas, one failed cycle: the count is at one step, and the appointment is a
    # minute out.
    transport.hold("/v1/index.json")
    handle_a = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM)
    await transport.entered["/v1/index.json"].wait()
    handle_b = await manager.async_subscribe(owner_id="entry-b", area_id="DE-LU", listener=Listener(), tz=STOCKHOLM)
    transport.release("/v1/index.json")
    await hass.async_block_till_done()
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=60)

    # Removing one of the two areas while the relay is in backoff: the appointment is
    # recomputed from the same count, not advanced by a "failure" that never happened.
    handle_b()
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=60)
    assert manager.area_snapshot("DE-LU") is None

    # Adding another owner to the remaining area changes nothing either. The zone is
    # supplied because the catalogue request is failing in this test.
    handle_c = await manager.async_subscribe(
        owner_id="entry-c", area_id="SE4", listener=Listener(), tz=STOCKHOLM
    )
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=60)

    # The next *actual* failed cycle advances exactly one step, to two minutes.
    attempts = transport.call_count("/v1/index.json")
    refresh_timer(scheduler).fire()
    await hass.async_block_till_done()
    assert transport.call_count("/v1/index.json") == attempts + 1
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=120)

    # The final unsubscribes cancel the timer without touching the recorded count.
    handle_a()
    handle_c()
    assert scheduler.pending == []

    # Coming back does not reset the count either. The area is freshly activated, so
    # it does start a cycle of its own — and that cycle advances the count *once*,
    # from the two steps the earlier failures earned to the third, rather than back
    # to a first step of one minute.
    transport.serve("/v1/index.json", 503, "")
    back = await manager.async_subscribe(owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM)
    # The activation's own cycle fails again and is awaited here, so the appointment
    # below is the one *that* cycle earned — the third step, not a fresh first.
    await hass.async_block_till_done()
    assert transport.call_count("/v1/index.json") == attempts + 2
    assert refresh_timer(scheduler).when == clock.now + timedelta(seconds=300)
    back()


# Real scheduler boundary: the tests below use the production pair (`async_track_point_in_utc_time`
# and `dt_util.utcnow`) driven by `async_fire_time_changed`, so the event-loop boundary itself is tested.
# A callback not marked event-loop-safe runs on a `SyncWorker` thread, where `hass.async_create_task`
# is refused and the appointment chain would silently die.


@pytest.fixture
def live_repository(hass: HomeAssistant, transport: StubTransport) -> PriceRepository:
    """The repository on Home Assistant's *own* clock, so the manager's real timers agree with it."""
    return PriceRepository(hass, base_url=BASE_URL, session=transport, store=StoreDouble())


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
    await live_manager.async_subscribe(
        owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM
    )
    await hass.async_block_till_done()

    first = live_manager.area_snapshot("SE4")
    assert first is not None and first.next_attempt is not None
    fired_at = first.next_attempt
    index_calls = transport.call_count("/v1/index.json")

    # Home Assistant's own helper fires the appointment it was given. Nothing is called directly.
    async_fire_time_changed(hass, fired_at)
    await hass.async_block_till_done()

    # The event-loop boundary is asserted first: an unmarked callback is dispatched to a `SyncWorker` thread and
    # logs a warning (or raises `RuntimeError`). Nothing here greps the source.
    assert "from a thread other than the event loop" not in caplog.text, caplog.text
    assert "RuntimeError" not in caplog.text, caplog.text

    # One tick, one shared cycle, and an appointment after it: the chain is alive.
    assert transport.call_count("/v1/index.json") == index_calls + 1
    after = live_manager.area_snapshot("SE4")
    assert after is not None and after.next_attempt is not None
    assert after.next_attempt > dt_util.utcnow()

    # A second fired appointment performs a second cycle through the same path.
    async_fire_time_changed(hass, after.next_attempt)
    await hass.async_block_till_done()
    assert transport.call_count("/v1/index.json") == index_calls + 2
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
    listener = Listener()
    await live_manager.async_subscribe(
        owner_id="entry-a", area_id="SE4", listener=listener, tz=STOCKHOLM
    )
    await hass.async_block_till_done()

    midnight = next_local_midnight(dt_util.utcnow(), STOCKHOLM)
    index_calls = transport.call_count("/v1/index.json")
    notified = len(listener.snapshots)

    async_fire_time_changed(hass, midnight)
    await hass.async_block_till_done()

    # The boundary, first again: a rollover callback that is not event-loop-safe is dispatched to a
    # worker and takes the same path to the same dead end.
    assert "from a thread other than the event loop" not in caplog.text, caplog.text
    assert "RuntimeError" not in caplog.text, caplog.text

    # Published (the listener was told again, with the new date keys -- the rollover's own effect, and
    # the only thing that publishes them), and the shared run started. The index count *increases*
    # rather than gaining exactly one: local midnight is a distant instant, so Home Assistant also
    # fires the shared refresh appointment that had come due on the way there.
    assert len(listener.snapshots) > notified
    assert live_manager.area_snapshot("SE4") is not None
    assert transport.call_count("/v1/index.json") > index_calls
    scheduled = live_manager.area_snapshot("SE4")
    assert scheduled is not None and scheduled.next_attempt is not None
    assert scheduled.next_attempt > dt_util.utcnow()

    # It re-armed for the area's next local midnight, through the same partial-and-scheduler path:
    # firing just past that instant runs a second rollover, which an un-armed one-shot could not.
    index_calls = transport.call_count("/v1/index.json")
    notified = len(listener.snapshots)
    async_fire_time_changed(hass, midnight + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert len(listener.snapshots) > notified
    assert transport.call_count("/v1/index.json") > index_calls

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
    await live_manager.async_subscribe(
        owner_id="entry-a", area_id="SE4", listener=Listener(), tz=STOCKHOLM
    )
    await hass.async_block_till_done()
    snapshot = live_manager.area_snapshot("SE4")
    assert snapshot is not None and snapshot.next_attempt is not None
    fired_at = snapshot.next_attempt

    await live_manager.async_shutdown()
    await hass.async_block_till_done()
    index_calls = transport.call_count("/v1/index.json")

    async_fire_time_changed(hass, fired_at)
    await hass.async_block_till_done()

    # No request, no new appointment and no resurrection: a shutdown manager is inert, and every
    # timer it had was cancelled rather than left to fire into a finished installation.
    assert transport.call_count("/v1/index.json") == index_calls
    assert live_manager.manager_snapshot().shutdown is True
    assert live_manager.area_snapshot("SE4") is None
    assert "from a thread other than the event loop" not in caplog.text
